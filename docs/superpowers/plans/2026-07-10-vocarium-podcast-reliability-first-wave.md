# Vocarium Podcast Reliability First-Wave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent private podcast content from silently falling back to an unintended LLM, reject incomplete speech renders, stream HTTP ranges with bounded memory, and stop reusing corrupt segment artifacts.

**Architecture:** Fail closed at data and audio integrity boundaries. Keep route schemas unchanged, add a dependency-free range parser/iterator, and make cache validity depend on both semantic fingerprints and file size.

**Tech Stack:** Python 3.11, FastAPI/Starlette, SQLite, asyncio, ffmpeg, `unittest`

## Global Constraints

- Every podcast, source, voice, and artifact lookup remains scoped through the owning podcast and `user_id`.
- Speech/reaction failures must not produce a `ready` podcast with missing dialogue.
- Optional music/SFX overlay failure may remain non-fatal and must be logged explicitly.
- File streaming uses bounded chunks and performs no full-range allocation.
- No database schema change is included in this wave.
- Run a non-GPU podcast route smoke before any optional audio smoke.
- Existing uncommitted `AGENTS.md` and `CLAUDE.md` edits remain untouched.

---

### Task 1: Fail closed when per-user LLM provider lookup errors

**Files:**
- Create: `vocarium-api/tests/test_podcast_reliability.py`
- Modify: `vocarium-api/podcast/llm_client.py:324-345`

**Interfaces:**
- Preserves: `get_llm_client(user_id: int | None = None) -> LLMClient`
- Changes: database exceptions propagate instead of selecting environment fallback

- [ ] **Step 1: Write the failing AST regression test**

```python
import ast
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parent
sys.path.insert(0, str(API_ROOT))


class LLMProviderIsolationTest(unittest.TestCase):
    def test_user_provider_lookup_does_not_swallow_database_errors(self):
        path = API_ROOT / "podcast" / "llm_client.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "get_llm_client"
        )
        handlers = [node for node in ast.walk(fn) if isinstance(node, ast.ExceptHandler)]
        self.assertEqual(handlers, [])
```

- [ ] **Step 2: Run the test and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_podcast_reliability.LLMProviderIsolationTest -v
```

Expected: FAIL because the current function has a broad `except Exception`.

- [ ] **Step 3: Remove the unsafe fallback-on-error branch**

Replace the user-specific tail of `get_llm_client` with:

```python
    from database import get_active_llm_provider

    provider = get_active_llm_provider(user_id)
    return LLMClient(config=_config_from_provider(provider))
```

An actual `None` provider still uses the documented environment fallback via
`_config_from_provider(None)`. A database failure now aborts the request before
any podcast content is sent to another destination.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_podcast_reliability.LLMProviderIsolationTest -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest vocarium-api.tests.test_production_hardening
git add vocarium-api/podcast/llm_client.py \
  vocarium-api/tests/test_podcast_reliability.py
git commit -m "fix: fail closed on LLM provider lookup errors"
```

### Task 2: Reject podcasts with missing required dialogue

**Files:**
- Modify: `vocarium-api/tests/test_podcast_reliability.py`
- Modify: `vocarium-api/podcast/audio_assembler.py:418-693`

**Interfaces:**
- Preserves: `AudioAssembler._synthesize_segments(segments, project_id, options, on_progress, user_id=None, force=False) -> list[SynthesisResult]`
- Changes: raises `RuntimeError` when a speech/reaction segment lacks generated audio

- [ ] **Step 1: Add a failing behavior test with a real assembler and fake TTS**

Append:

```python
class RequiredDialogueTest(unittest.TestCase):
    def test_required_speech_failure_aborts_synthesis(self):
        from podcast.audio_assembler import AssemblyOptions, AudioAssembler
        from podcast.disfluency import ScriptSegment

        class FailingTTS:
            async def default_voice_for_speaker(self, speaker, *, user_id=None):
                return "voice-1"

            async def synthesize_to_file(
                self,
                text,
                voice,
                output_path,
                output_format,
                *,
                user_id=None,
                notes=None,
            ):
                raise RuntimeError("synthetic TTS failure")

        segment = ScriptSegment(
            id="seg-1",
            script_id=None,
            speaker="Host",
            text="Required dialogue",
            type="speech",
            voice="voice-1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            assembler = AudioAssembler(FailingTTS(), output_dir=tmp)
            with self.assertRaisesRegex(
                RuntimeError, "required dialogue segment.*seg-1"
            ):
                asyncio.run(
                    assembler._synthesize_segments(
                        [segment],
                        "pod-test",
                        AssemblyOptions(output_format="wav"),
                        None,
                        user_id=1,
                    )
                )
```

- [ ] **Step 2: Run the test and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_podcast_reliability.RequiredDialogueTest -v
```

Expected: FAIL because `_synthesize_segments` returns a missing-file result
instead of raising.

- [ ] **Step 3: Propagate programming errors and detect caught synthesis failures**

Replace the foreground gather block with:

```python
        async def _run_foreground_tts() -> None:
            workers = [
                asyncio.create_task(_synthesize_one_limited(i, segments[i]))
                for i in foreground_indices
            ]
            if workers:
                await asyncio.gather(*workers)

            failed_required = [
                segments[i].id
                for i in foreground_indices
                if segments[i].type in ("speech", "reaction")
                and (
                    results[i] is None
                    or results[i].file_path is None
                    or not results[i].file_path.exists()
                )
            ]
            if failed_required:
                joined = ", ".join(failed_required[:10])
                raise RuntimeError(
                    "required dialogue segment synthesis failed: " + joined
                )
```

Do not change the existing optional music/SFX error handling.

- [ ] **Step 4: Run focused and full tests**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_podcast_reliability.RequiredDialogueTest -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest discover -s vocarium-api/tests -v
```

Expected: all pass.

- [ ] **Step 5: Commit the fail-closed audio behavior**

```bash
git add vocarium-api/podcast/audio_assembler.py \
  vocarium-api/tests/test_podcast_reliability.py
git commit -m "fix: reject incomplete podcast dialogue"
```

### Task 3: Stream podcast byte ranges with bounded memory

**Files:**
- Create: `vocarium-api/podcast/file_stream.py`
- Modify: `vocarium-api/tests/test_podcast_reliability.py`
- Modify: `vocarium-api/podcast/routes.py:1969-2015`

**Interfaces:**
- Produces: `parse_single_range(header: str, file_size: int) -> tuple[int, int]`
- Produces: `iter_file_range(path: Path, start: int, end: int, chunk_size: int = 65536) -> Iterator[bytes]`

- [ ] **Step 1: Add failing range parser and bounded-iterator tests**

Append:

```python
class FileRangeTest(unittest.TestCase):
    def test_range_iterator_never_allocates_the_whole_range(self):
        from podcast.file_stream import iter_file_range, parse_single_range

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.bin"
            path.write_bytes(bytes(range(256)) * 1024)
            start, end = parse_single_range("bytes=100-200000", path.stat().st_size)
            chunks = list(iter_file_range(path, start, end, chunk_size=4096))

        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk) <= 4096 for chunk in chunks))
        self.assertEqual(sum(map(len, chunks)), end - start + 1)

    def test_range_parser_supports_open_and_suffix_ranges(self):
        from podcast.file_stream import parse_single_range

        self.assertEqual(parse_single_range("bytes=10-", 100), (10, 99))
        self.assertEqual(parse_single_range("bytes=-10", 100), (90, 99))
        with self.assertRaises(ValueError):
            parse_single_range("bytes=0-1,4-5", 100)
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest vocarium-api.tests.test_podcast_reliability.FileRangeTest -v
```

Expected: FAIL because `podcast.file_stream` does not exist.

- [ ] **Step 3: Implement the pure range parser and iterator**

```python
# vocarium-api/podcast/file_stream.py
"""Bounded synchronous file iterators for Starlette StreamingResponse."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def parse_single_range(header: str, file_size: int) -> tuple[int, int]:
    if file_size <= 0:
        raise ValueError("empty file")
    match = _RANGE_RE.fullmatch((header or "").strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise ValueError("invalid byte range")
    start_text, end_text = match.groups()
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        start = max(0, file_size - suffix)
        return start, file_size - 1
    start = int(start_text)
    end = int(end_text) if end_text else file_size - 1
    end = min(end, file_size - 1)
    if start >= file_size or start > end:
        raise ValueError("range not satisfiable")
    return start, end


def iter_file_range(
    path: Path,
    start: int,
    end: int,
    chunk_size: int = 64 * 1024,
) -> Iterator[bytes]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    remaining = end - start + 1
    with Path(path).open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
```

- [ ] **Step 4: Replace the one-shot range reader in the route**

Import:

```python
from .file_stream import iter_file_range, parse_single_range
```

Replace the current range parser and nested async reader with:

```python
        if range_header:
            try:
                start, end = parse_single_range(range_header, file_size)
            except ValueError as exc:
                raise HTTPException(
                    416,
                    "Range not satisfiable",
                    headers={"Content-Range": f"bytes */{file_size}"},
                ) from exc
            length = end - start + 1
            return StreamingResponse(
                iter_file_range(path, start, end),
                status_code=206,
                media_type=media_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(length),
                },
            )
```

Starlette runs synchronous iterators in its thread pool, so filesystem reads do
not block the event loop and no requested range is materialized as one bytes
object.

- [ ] **Step 5: Verify and commit**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest vocarium-api.tests.test_podcast_reliability.FileRangeTest -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest discover -s vocarium-api/tests -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m py_compile vocarium-api/podcast/file_stream.py \
  vocarium-api/podcast/routes.py
git add vocarium-api/podcast/file_stream.py vocarium-api/podcast/routes.py \
  vocarium-api/tests/test_podcast_reliability.py
git commit -m "fix: stream podcast ranges in bounded chunks"
```

### Task 4: Reject truncated or mismatched cached segment files

**Files:**
- Modify: `vocarium-api/tests/test_podcast_reliability.py`
- Modify: `vocarium-api/podcast/audio_assembler.py:727-753`

**Interfaces:**
- Preserves: `_segment_output_is_current(output_path: Path, fingerprint: str) -> bool`
- Changes: manifest filename and size must match the current file

- [ ] **Step 1: Add the failing corrupt-cache test**

Append:

```python
class SegmentCacheIntegrityTest(unittest.TestCase):
    def test_manifest_size_must_match_segment_file(self):
        from podcast.audio_assembler import AudioAssembler

        class NoopTTS:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            assembler = AudioAssembler(NoopTTS(), output_dir=tmp)
            output = Path(tmp) / "segment.wav"
            output.write_bytes(b"bad")
            output.with_suffix(".wav.json").write_text(
                '{"fingerprint":"same","file":"segment.wav","size":99}',
                encoding="utf-8",
            )
            self.assertFalse(
                assembler._segment_output_is_current(output, "same")
            )
```

- [ ] **Step 2: Run the test and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_podcast_reliability.SegmentCacheIntegrityTest -v
```

Expected: FAIL because only the fingerprint is currently checked.

- [ ] **Step 3: Enforce manifest/file agreement**

Replace the final return in `_segment_output_is_current` with:

```python
        try:
            actual_size = output_path.stat().st_size
        except OSError:
            return False
        return (
            data.get("fingerprint") == fingerprint
            and data.get("file") == output_path.name
            and isinstance(data.get("size"), int)
            and data["size"] > 0
            and data["size"] == actual_size
        )
```

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_podcast_reliability.SegmentCacheIntegrityTest -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest discover -s vocarium-api/tests -v
git add vocarium-api/podcast/audio_assembler.py \
  vocarium-api/tests/test_podcast_reliability.py
git commit -m "fix: validate podcast segment cache files"
```

### Task 5: Recreate and verify Podcast Studio

**Files:**
- Verify only; no planned source edits

**Interfaces:**
- Verifies route smoke, mounted package source, range behavior, and no-GPU regressions

- [ ] **Step 1: Run the full static gate**

```bash
git diff --check
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m compileall -q vocarium-api
PYTHONPYCACHEPREFIX=/tmp/vocarium-podcast-pyc \
  python3 -m unittest discover -s vocarium-api/tests -v
docker compose config --quiet
```

- [ ] **Step 2: Restart only the mounted API service**

```bash
docker compose restart vocarium-api
docker exec vocarium-api python -c \
  "import inspect, podcast.file_stream, podcast.audio_assembler; print(inspect.getsourcefile(podcast.file_stream)); print(inspect.getsourcefile(podcast.audio_assembler))"
```

Expected: both modules load from `/app/podcast`.

- [ ] **Step 3: Run the non-GPU Podcast API smoke**

```bash
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py podcast-smoke
```

Expected: host/podcast creation, invalid-format rejection, and cleanup all pass.

- [ ] **Step 4: Verify one existing owned podcast range without printing content**

Select an existing ready podcast owned by the authenticated user, then request
a small range from inside `vocarium-api` and assert only status/length:

```bash
docker exec -i vocarium-api python - <<'PY'
import json
import urllib.request

base_url = "http://127.0.0.1:8280/api"
identity = {"Remote-User": "api"}
list_request = urllib.request.Request(
    f"{base_url}/podcasts",
    headers=identity,
)
podcasts = json.load(urllib.request.urlopen(list_request)).get("podcasts", [])
ready = next(
    (
        podcast
        for podcast in podcasts
        if podcast.get("status") == "ready" and podcast.get("audio_path")
    ),
    None,
)
if ready is None:
    print("SKIP: no ready owned podcast is available for the optional range probe")
else:
    range_request = urllib.request.Request(
        f"{base_url}/podcasts/{ready['id']}/audio/stream",
        headers={**identity, "Range": "bytes=0-1023"},
    )
    with urllib.request.urlopen(range_request) as response:
        data = response.read()
        content_range = response.headers.get("Content-Range")
        print(response.status, len(data), content_range)
        assert response.status == 206
        assert len(data) == 1024
        assert content_range and content_range.startswith("bytes 0-1023/")
PY
```

If no ready podcast exists, the command skips this optional runtime probe and
the pure iterator test remains the verification; do not generate expensive
audio solely for a range check.

- [ ] **Step 5: Inspect logs and status**

```bash
docker compose logs --since=15m --no-color vocarium-api \
  | rg -n -i '(traceback|exception|error)' || true
git status --short
```

Expected: no new traceback and no unrelated staged files.
