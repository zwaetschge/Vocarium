# Vocarium Worker Lifecycle First-Wave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Qwen TTS, Qwen ASR, ACE-Step, and MMAudio responsive and memory-safe during cold starts, cancellations, long requests, idle unloads, and manual unloads.

**Architecture:** Each worker owns its lifecycle state. Slow model/process work runs in executor threads, while an active-request invariant prevents unload or model switching until the blocking GPU work itself—not merely the client coroutine—has finished. Worker clients reuse connection pools and expose enough state for direct lifecycle smokes.

**Tech Stack:** Python 3.11, FastAPI, asyncio, threading, aiohttp/httpx, PyTorch/CUDA, Docker Compose

## Global Constraints

- Follow `AGENTS.md`: direct container request after changing an inference endpoint, then verify the loaded mounted source.
- Run the Vocarium GPU status and preflight before every real generation.
- Use GPU 0 / RTX 3060 only. Never bypass the guard or use protected GPU 1.
- Use the shortest useful input: one short TTS sentence, a tiny ASR sample, 10-second music, and 1-second SFX.
- A client cancellation must never make an executor thread invisible to unload/model-switch logic.
- Do not shorten cold-start timeouts.
- Existing uncommitted `AGENTS.md` and `CLAUDE.md` changes remain untouched.
- Each worker is changed and verified in a separate commit.

---

### Task 1: Add lifecycle contract tests

**Files:**
- Create: `vocarium-api/tests/test_worker_lifecycle_contracts.py`

**Interfaces:**
- Consumes: worker source files without importing CUDA dependencies
- Produces: AST/source contracts that fail before each worker fix and remain in the normal host-side suite

- [ ] **Step 1: Create source-level lifecycle tests**

```python
import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def source(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


class TTSLifecycleContractTest(unittest.TestCase):
    def test_active_count_is_owned_by_blocking_model_wrapper(self):
        text = source("qwen3-tts/server.py")
        tree = ast.parse(text)
        wrapper = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_run_with_model"
        )
        body = ast.get_source_segment(text, wrapper) or ""
        self.assertIn("active_requests += 1", body)
        self.assertIn("active_requests -= 1", body)
        self.assertIn("used_model_id", body)

    def test_async_load_and_unload_use_threads(self):
        text = source("qwen3-tts/server.py")
        self.assertIn(
            "await asyncio.to_thread(ensure_model, request.model_id)", text
        )
        self.assertIn("await asyncio.to_thread(_unload_if_idle)", text)


class ASRLifecycleContractTest(unittest.TestCase):
    def test_proxy_tracks_active_requests_around_http_call(self):
        text = source("asr_proxy.py")
        self.assertIn("await asyncio.to_thread(acquire_backend)", text)
        self.assertIn("await asyncio.to_thread(release_backend)", text)
        self.assertIn("active_requests == 0", text)
        self.assertIn("def _client() -> httpx.AsyncClient:", text)


class AceLifecycleContractTest(unittest.TestCase):
    def test_proxy_prevents_unload_while_requests_are_active(self):
        text = source("acestep/proxy.py")
        self.assertIn("active_requests", text)
        self.assertIn('"status": "busy"', text)
        self.assertIn("def _session() -> aiohttp.ClientSession:", text)


class MMAudioLifecycleContractTest(unittest.TestCase):
    def test_requested_steps_are_applied_before_generation(self):
        text = source("mmaudio/server.py")
        self.assertIn("fm.num_steps = req.num_steps", text)

    def test_cuda_output_is_copied_while_generation_lock_is_held(self):
        text = source("mmaudio/server.py")
        tree = ast.parse(text)
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_generate_sfx_blocking"
        )
        with_nodes = [node for node in ast.walk(fn) if isinstance(node, ast.With)]
        lock_block = next(
            node
            for node in with_nodes
            if any(
                isinstance(item.context_expr, ast.Name)
                and item.context_expr.id == "lock"
                for item in node.items
            )
        )
        lock_text = ast.get_source_segment(text, lock_block) or ""
        self.assertIn(".cpu()", lock_text)
```

- [ ] **Step 2: Run the tests and verify RED**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m unittest vocarium-api.tests.test_worker_lifecycle_contracts -v
```

Expected: failures for the missing TTS wrapper, synchronous ASR/TTS lifecycle,
missing ACE active tracking/session reuse, ignored MMAudio steps, and the CPU
copy outside the lock.

- [ ] **Step 3: Commit only the failing contract tests**

```bash
git add vocarium-api/tests/test_worker_lifecycle_contracts.py
git commit -m "test: define worker lifecycle invariants"
```

### Task 2: Make Qwen TTS model ownership cancellation-safe

**Files:**
- Modify: `qwen3-tts/server.py:184-233,452-490,642-711,991-1405`
- Modify: `vocarium-api/tests/test_worker_lifecycle_contracts.py`

**Interfaces:**
- Produces: `_run_with_model(model_id: str, work: Callable[[str], T]) -> T`
- Produces: `_unload_if_idle() -> dict`
- Preserves all existing HTTP request/response shapes

- [ ] **Step 1: Add the typed blocking model wrapper**

Add imports:

```python
from collections.abc import Callable
from typing import Optional, TypeVar

T = TypeVar("T")
```

Replace the body shared by `ensure_model` with a lock-held helper and add the
blocking ownership wrapper:

```python
def _ensure_model_locked(target: str) -> str:
    global last_used
    if target not in AVAILABLE_MODELS:
        raise ValueError(f"Unknown model: {target}")
    if model is not None and current_model_id != target and active_requests > 0:
        raise RuntimeError(
            f"Model {current_model_id} is busy; cannot switch to {target}"
        )
    if model is None or current_model_id != target:
        if model is not None:
            _do_unload_model()
        _do_load_model(target)
    last_used = time.time()
    assert current_model_id is not None
    return current_model_id


def ensure_model(model_id: str | None = None) -> str:
    target = model_id or current_model_id or DEFAULT_MODEL
    with model_lock:
        used_model_id = _ensure_model_locked(target)
    _schedule_unload()
    return used_model_id


def _run_with_model(model_id: str, work: Callable[[str], T]) -> T:
    global active_requests, last_used
    with model_lock:
        used_model_id = _ensure_model_locked(model_id)
        active_requests += 1
    try:
        return work(used_model_id)
    finally:
        with model_lock:
            active_requests -= 1
            last_used = time.time()
        _schedule_unload()
```

The invariant is that the executor thread increments and decrements the count.
Cancelling the awaiting coroutine does not cancel that thread, so unload still
sees the request as active until CUDA work actually returns.

- [ ] **Step 2: Move model loading and active ownership into executor work**

For `create_speech`, `design_voice`, and `custom_voice`:

1. Remove the direct `ensure_model(target_model)` call.
2. Remove the endpoint-level `active_requests += 1` / `-= 1` blocks.
3. Change the blocking closure to accept `used_model_id: str`.
4. Execute it with:

```python
        result = await asyncio.to_thread(
            _run_with_model,
            target_model,
            _run_generate,
        )
```

For design/custom, use fixed targets `"1.7b-design"` and `"1.7b-custom"`.
Return `used_model_id` from each blocking closure alongside audio metrics and
use that immutable value for `X-Model` instead of the mutable global.

For `create_speech_stream`, remove the pre-response `ensure_model` and active
increment. Wrap all work performed by `_run_stream_request` like this:

```python
        def _stream_work(used_model_id: str) -> None:
            total_audio_dur = 0.0
            sr = 24000
            with inference_lock:
                for i, chunk in enumerate(chunks):
                    chunk_len = len(chunk)
                    token_limit = _clone_token_limit(
                        chunk,
                        request.max_new_tokens,
                    )
                    print(
                        f"[stream chunk {i + 1}/{n_chunks}] "
                        f"{chunk_len} chars → {token_limit} tokens",
                        flush=True,
                    )

                    wavs, sr = _gen_chunk(i, chunk, token_limit)
                    audio = wavs[0]
                    audio_dur = len(audio) / sr
                    total_audio_dur += audio_dur

                    buf = io.BytesIO()
                    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
                    b64 = base64.b64encode(buf.getvalue()).decode()
                    event_data = {
                        "index": i,
                        "total": n_chunks,
                        "audio": b64,
                        "duration": round(audio_dur, 2),
                        "text": chunk[:80],
                    }
                    loop.call_soon_threadsafe(
                        event_queue.put_nowait,
                        f"event: chunk\ndata: {json.dumps(event_data)}\n\n",
                    )

            gen_time = time.time() - t0
            rtf = gen_time / total_audio_dur if total_audio_dur > 0 else 0
            cg = "CG" if use_cuda_graphs else "fallback"
            print(
                f"[stream/{voice_id}] {total_audio_dur:.1f}s in "
                f"{gen_time:.1f}s (RTF {rtf:.2f}x) chunks={n_chunks} [{cg}]",
                flush=True,
            )
            done_data = {
                "total_duration": round(total_audio_dur, 2),
                "generation_time": round(gen_time, 2),
                "rtf": round(rtf, 4),
                "model": used_model_id,
                "voice": voice_id,
                "chunks": n_chunks,
            }
            loop.call_soon_threadsafe(
                event_queue.put_nowait,
                f"event: done\ndata: {json.dumps(done_data)}\n\n",
            )

        def _run_stream_request() -> None:
            try:
                _run_with_model(target_model, _stream_work)
            except Exception as exc:
                loop.call_soon_threadsafe(
                    event_queue.put_nowait,
                    f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n",
                )
            finally:
                loop.call_soon_threadsafe(event_queue.put_nowait, None)
```

Move the existing chunk-loop code in place of the comment without changing its
chunking, generation parameters, or SSE schema. Remove the async generator's
old active-counter `finally`; the blocking wrapper now owns it.

- [ ] **Step 3: Offload explicit load and unload endpoints**

Add:

```python
def _unload_if_idle() -> dict:
    with model_lock:
        if active_requests > 0:
            return {
                "status": "busy",
                "was_loaded": model is not None,
                "model_id": current_model_id,
                "active_requests": active_requests,
            }
        was_loaded = model is not None
        model_id = current_model_id
        _do_unload_model()
    return {"status": "unloaded", "was_loaded": was_loaded, "model_id": model_id}
```

Use it from `/unload`:

```python
@app.post("/unload")
async def unload():
    return await asyncio.to_thread(_unload_if_idle)
```

Use the thread-offloaded `ensure_model` in `/v1/models/load`:

```python
    try:
        await asyncio.to_thread(ensure_model, request.model_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
```

- [ ] **Step 4: Run RED-to-GREEN checks and syntax validation**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_worker_lifecycle_contracts.TTSLifecycleContractTest -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m py_compile qwen3-tts/server.py
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m unittest vocarium-api.tests.test_production_hardening
```

Expected: all pass.

- [ ] **Step 5: Restart and run direct TTS lifecycle smokes**

```bash
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py gpu-status
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py preflight --kind tts
docker compose restart qwen3-tts
docker exec qwen3-tts python -c \
  "import inspect, server; print(inspect.getsourcefile(server)); assert '_run_with_model' in inspect.getsource(server._run_with_model)"
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py \
  tts-worker-smoke --invalid-format-check
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py \
  tts-worker-smoke --text "Kurzer Lifecycle-Test." --out /tmp/tts-lifecycle.wav
```

Expected: preflight selects GPU 0; validation returns the expected 400; real
smoke returns a non-empty valid WAV.

- [ ] **Step 6: Commit the TTS lifecycle fix**

```bash
git add qwen3-tts/server.py vocarium-api/tests/test_worker_lifecycle_contracts.py
git commit -m "fix: make TTS model lifecycle cancellation safe"
```

### Task 3: Make the ASR proxy non-blocking and active-request aware

**Files:**
- Modify: `asr_proxy.py:31-215`
- Modify: `vocarium-api/tests/test_worker_lifecycle_contracts.py`

**Interfaces:**
- Produces: `acquire_backend() -> None`, `release_backend() -> None`
- Produces: pooled `_client() -> httpx.AsyncClient`
- `/unload` returns `status=busy` while a proxied request owns the backend

- [ ] **Step 1: Add lifecycle state and pooled client**

Add globals and helpers:

```python
active_requests = 0
backend_ready = False
last_start_error: str | None = None
_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=30.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client
```

On startup-timeout or child death, call `stop_backend()` before raising and set
`backend_ready=False`. Catch `httpx.HTTPError` during readiness probes, not only
`httpx.ConnectError`. Set `backend_ready=True` only after `/v1/models` returns
200.

- [ ] **Step 2: Replace request-start idle scheduling with acquire/release ownership**

Implement:

```python
def acquire_backend() -> None:
    global active_requests, last_used, last_start_error
    with lock:
        try:
            start_backend()
        except Exception as exc:
            last_start_error = str(exc)
            raise
        active_requests += 1
        last_used = time.time()


def release_backend() -> None:
    global active_requests, last_used
    with lock:
        active_requests = max(0, active_requests - 1)
        last_used = time.time()
    schedule_unload()
```

Change the timer check to unload only when:

```python
active_requests == 0 and backend_ready and elapsed >= IDLE_TIMEOUT
```

Wrap the proxy request:

```python
    await asyncio.to_thread(acquire_backend)
    try:
        resp = await _client().request(
            method=request.method,
            url=url,
            content=body,
            headers=headers,
            params=list(request.query_params.multi_items()),
        )
    finally:
        await asyncio.to_thread(release_backend)
```

Add `import asyncio`. Remove the per-request `AsyncClient` context manager.

- [ ] **Step 3: Make unload non-blocking and fail busy**

```python
def _unload_if_idle() -> dict:
    with lock:
        if active_requests > 0:
            return {
                "status": "busy",
                "was_running": process is not None and process.poll() is None,
                "active_requests": active_requests,
            }
        was_running = process is not None and process.poll() is None
        stop_backend()
        return {"status": "unloaded", "was_running": was_running}


@app.post("/unload")
async def unload():
    return await asyncio.to_thread(_unload_if_idle)
```

Include `active_requests`, `backend_ready`, and `last_start_error` in `/health`.
Close the shared client in a shutdown handler:

```python
@app.on_event("shutdown")
async def shutdown():
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None
```

- [ ] **Step 4: Verify tests and syntax**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_worker_lifecycle_contracts.ASRLifecycleContractTest -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc python3 -m py_compile asr_proxy.py
```

Expected: pass.

- [ ] **Step 5: Restart and run a guarded ASR cold-start smoke**

```bash
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py preflight --kind asr
docker compose restart qwen3-asr
docker exec qwen3-asr python -c \
  "import inspect, asr_proxy; print(inspect.getsourcefile(asr_proxy)); assert hasattr(asr_proxy, 'acquire_backend')"
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py \
  transcribe --file /tmp/tts-lifecycle.wav
```

Expected: GPU 0 allowed; transcription completes; `/health` remains responsive
during cold start when probed from a second shell/request.

- [ ] **Step 6: Commit ASR changes**

```bash
git add asr_proxy.py vocarium-api/tests/test_worker_lifecycle_contracts.py
git commit -m "fix: protect active ASR backend requests"
```

### Task 4: Protect ACE-Step requests and reuse its HTTP session

**Files:**
- Modify: `acestep/proxy.py:21-277`
- Modify: `vocarium-api/tests/test_worker_lifecycle_contracts.py`

**Interfaces:**
- Produces: `_session() -> aiohttp.ClientSession`
- Produces: `_acquire_backend(start_if_needed: bool) -> None` and `_release_backend() -> None`
- `/unload` returns busy without terminating an active request

- [ ] **Step 1: Add shared session and active-request state**

```python
active_requests = 0
_http_session: aiohttp.ClientSession | None = None


def _session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        )
    return _http_session
```

Capture the Popen object in the log thread instead of rereading the mutable
global:

```python
    started_process = process

    def log_output():
        if started_process.stdout:
            for line in started_process.stdout:
                print(f"[acestep] {line.decode(errors='replace').rstrip()}", flush=True)
```

- [ ] **Step 2: Add request ownership around every proxied operation**

Implement:

```python
async def _acquire_backend(start_if_needed: bool) -> None:
    global active_requests, last_activity, last_start_error
    with lock:
        active_requests += 1
        last_activity = time.time()
        running = process is not None and process.poll() is None
        if not running and start_if_needed:
            start_backend()
        elif not running:
            active_requests -= 1
            raise HTTPException(503, "ACE-Step backend not running")
    if start_if_needed:
        try:
            await wait_for_backend(STARTUP_TIMEOUT_SECONDS)
        except Exception as exc:
            with lock:
                active_requests = max(0, active_requests - 1)
                last_start_error = str(exc)
            raise


def _release_backend() -> None:
    global active_requests, last_activity
    with lock:
        active_requests = max(0, active_requests - 1)
        last_activity = time.time()
```

For `release_task` and `format_input`, call `_acquire_backend(True)`; for
`query_result`, `get_audio`, and running-only model calls, call
`_acquire_backend(False)`. Wrap the shared-session request in `try/finally` and
call `_release_backend()` in the finally block.

Replace every endpoint-local construction of `aiohttp.ClientSession` with
`_session()` while preserving the endpoint-specific timeout object on each
request.

- [ ] **Step 3: Make idle/manual unload respect active requests**

Change the idle condition to require `active_requests == 0`. Move blocking
`stop_backend()` work into `asyncio.to_thread` from the watcher.

Add:

```python
def _unload_if_idle() -> dict:
    with lock:
        running = process is not None and process.poll() is None
        if active_requests > 0:
            return {
                "status": "busy",
                "was_running": running,
                "active_requests": active_requests,
            }
        stop_backend()
        return {"status": "unloaded", "was_running": running}
```

Use `return await asyncio.to_thread(_unload_if_idle)` in `/unload`. Include
`active_requests` in `/health`. Close `_http_session` in a shutdown handler.

- [ ] **Step 4: Verify tests and syntax**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_worker_lifecycle_contracts.AceLifecycleContractTest -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc python3 -m py_compile acestep/proxy.py
```

Expected: pass.

- [ ] **Step 5: Recreate ACE-Step and run a 10-second guarded smoke**

```bash
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py preflight --kind music
docker compose up -d --no-deps --force-recreate acestep
docker exec acestep python3 -c \
  "import inspect, sys; sys.path.insert(0, '/app'); import proxy; print(inspect.getsourcefile(proxy)); assert hasattr(proxy, '_acquire_backend')"
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py \
  music --prompt "short ambient piano lifecycle test" --duration 10 \
  --out /tmp/music-lifecycle.wav
```

Expected: GPU 0 allowed; a valid non-empty WAV is returned; `/unload` reports
busy rather than stopping the backend if invoked while a query is active.

- [ ] **Step 6: Commit ACE-Step changes**

```bash
git add acestep/proxy.py vocarium-api/tests/test_worker_lifecycle_contracts.py
git commit -m "fix: protect active ACE-Step requests"
```

### Task 5: Apply MMAudio step count and keep CUDA output under lock

**Files:**
- Modify: `mmaudio/server.py:179-268`
- Modify: `vocarium-api/tests/test_worker_lifecycle_contracts.py`

**Interfaces:**
- Preserves: `POST /generate` schema
- Changes: `num_steps` now controls `FlowMatching.num_steps`

- [ ] **Step 1: Add direct-worker input validation**

In `generate_sfx`, validate before entering the executor:

```python
    if not req.prompt.strip():
        raise HTTPException(400, "Prompt is required")
    if len(req.prompt) > 1000 or len(req.negative_prompt) > 1000:
        raise HTTPException(413, "Prompt fields must be at most 1000 characters")
    if req.duration < 1 or req.duration > 30:
        raise HTTPException(400, "Duration must be between 1 and 30 seconds")
    if req.cfg_strength < 1 or req.cfg_strength > 10:
        raise HTTPException(400, "cfg_strength must be between 1 and 10")
    if req.num_steps < 1 or req.num_steps > 100:
        raise HTTPException(400, "num_steps must be between 1 and 100")
```

- [ ] **Step 2: Apply steps and copy GPU output before releasing the lock**

Inside `with lock:` immediately before the existing `generate` call:

```python
            fm.num_steps = req.num_steps
```

Move the tensor conversion into the same lock block, after generation timing:

```python
            audio = audios.float().detach().cpu()[0]
```

Delete the old conversion after the `with lock:` block. Keep shape normalization
and WAV encoding outside the lock because they operate only on the CPU tensor.

- [ ] **Step 3: Verify tests and syntax**

```bash
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m unittest \
  vocarium-api.tests.test_worker_lifecycle_contracts.MMAudioLifecycleContractTest -v
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc python3 -m py_compile mmaudio/server.py
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m unittest vocarium-api.tests.test_production_hardening
```

- [ ] **Step 4: Restart and run a guarded 1-second SFX smoke**

```bash
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py preflight --kind sfx
docker compose restart mmaudio
docker exec mmaudio python -c \
  "import inspect, sys; sys.path.insert(0, '/app'); import server; print(inspect.getsourcefile(server)); assert 'fm.num_steps' in inspect.getsource(server._generate_sfx_blocking)"
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py \
  sfx --prompt "short dry click" --duration 1 --out /tmp/sfx-lifecycle.wav
```

Expected: GPU 0 allowed and output is a valid non-empty WAV.

- [ ] **Step 5: Commit MMAudio changes**

```bash
git add mmaudio/server.py vocarium-api/tests/test_worker_lifecycle_contracts.py
git commit -m "fix: honor MMAudio generation lifecycle settings"
```

### Task 6: Run the combined worker verification gate

**Files:**
- Verify only; no planned source edits

**Interfaces:**
- Verifies all four workers together and returns the machine to a safe VRAM state

- [ ] **Step 1: Run the full host-side gate**

```bash
git diff --check
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m compileall -q qwen3-tts acestep mmaudio asr_proxy.py vocarium-api
PYTHONPYCACHEPREFIX=/tmp/vocarium-worker-pyc \
  python3 -m unittest discover -s vocarium-api/tests -v
docker compose config --quiet
```

- [ ] **Step 2: Inspect recent errors and GPU placement**

```bash
docker compose logs --since=30m --no-color qwen3-tts qwen3-asr acestep mmaudio \
  | rg -n -i '(traceback|exception|cuda error|out of memory|oom|failed)' || true
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py gpu-status
```

Expected: no new worker traceback/OOM; GPU 1 remains protected and unused by
the tested Vocarium workers.

- [ ] **Step 3: Unload idle models and confirm release**

```bash
docker exec qwen3-tts python -c \
  "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8880/unload', method='POST')).read().decode())"
docker exec qwen3-asr python -c \
  "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/unload', method='POST')).read().decode())"
python3 ~/.claude/skills/vocarium-audio-api/scripts/vocarium_audio.py gpu-status
```

Expected: unloaded or explicitly busy; after work completes, GPU 0 no longer
holds idle model VRAM beyond expected CUDA/process overhead.
