"""FastAPI routes for podcast generation.

Exposes CRUD for hosts, podcasts (projects), sources, plus SSE-driven script
and audio generation backed by the GPU queue.

The TTS bridge (``VocariumTTSGenerator``) enforces the project constraint that
podcasts may only use custom voices (``voices.source = 'custom'``). Custom
voices are built on the Qwen3-TTS-CustomVoice model — prebuilt speakers plus
optional ``instruct`` steering — and always target the custom TTS endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import aiohttp
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from metrics import inc, observe

from .audio_assembler import (
    AssemblyOptions,
    AssemblyProgress,
    AudioAssembler,
    AudioFormat,
    MusicGenerator,
    SFXGenerator,
    TTSGenerator,
)
from .disfluency import ScriptSegment
from .docling_client import DoclingClient, get_docling_client
from .embedding_client import EmbeddingClient, get_embedding_client
from .helpers import chunk_text, count_words, estimate_speaking_duration, generate_id
from .script_generator import (
    HostCharacter,
    ScriptGenerationContext,
    ScriptGenerationOptions,
    ScriptGenerator,
    SourceChunk,
    SourceInfo,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ALLOWED_SOURCE_EXTENSIONS = {
    ".md", ".pdf", ".docx", ".doc", ".pptx", ".ppt",
    ".xlsx", ".xls", ".odt", ".rtf", ".txt",
    ".html", ".htm", ".epub", ".xml",
}

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_TEXT_CHARS = 100_000
MIN_TEXT_CHARS = 10
MAX_SOURCE_URL_CHARS = 2048
MAX_TITLE_CHARS = 240
MAX_HOST_NAME_CHARS = 120
MAX_HOST_PROFILE_CHARS = 2_000
MAX_TOPIC_CHARS = 500
MAX_LANGUAGE_CHARS = 32
MAX_SEGMENT_TEXT_CHARS = 20_000
MAX_SEGMENT_PROMPT_CHARS = 2_000
MAX_MEDIA_DURATION_MS = 10 * 60 * 1000
ALLOWED_PODCAST_FORMATS = {"dialog", "monolog", "custom"}
ALLOWED_PODCAST_DURATIONS = {"short", "medium", "long"}
ALLOWED_AUDIO_FORMATS = {"mp3", "wav"}
ALLOWED_SEGMENT_TYPES = {"speech", "reaction", "pause", "sfx", "music"}


# ---------------------------------------------------------------------------
# TTS bridge
# ---------------------------------------------------------------------------


@dataclass
class _VoiceRow:
    id: str
    name: str
    source: str


class VocariumTTSGenerator:
    """Concrete TTSGenerator that calls qwen3-tts through a TTS URL pool.

    Podcast TTS must only use custom voices — this is enforced in
    :meth:`_resolve_voice` and :meth:`default_voice_for_speaker`.
    """

    def __init__(
        self,
        tts_url: str,
        db_getter: Callable[[], sqlite3.Connection],
        gpu_submit: Callable[..., Any],
        *,
        extra_tts_urls: list[str] | None = None,
        max_parallel_workers: int = 2,
    ):
        # Primary TTS endpoint
        self.tts_url = tts_url
        # Extra TTS endpoints (failover order). Empty entries are filtered so
        # callers can pass an unconditional list with optional URLs.
        self._extra_tts_urls = [u for u in (extra_tts_urls or []) if u]
        self._db_getter = db_getter
        self._gpu_submit = gpu_submit
        self._tts_urls = [self.tts_url, *self._extra_tts_urls]
        self._url_semaphores = {url: asyncio.Semaphore(1) for url in self._tts_urls}
        self._next_url = 0
        self._url_lock = asyncio.Lock()
        self._aiosession: aiohttp.ClientSession | None = None

    @property
    def _session(self) -> aiohttp.ClientSession:
        if self._aiosession is None or self._aiosession.closed:
            from aiohttp import TCPConnector
            self._aiosession = aiohttp.ClientSession(
                connector=TCPConnector(limit=20, ttl_dns_cache=300),
            )
        return self._aiosession

    async def aclose(self) -> None:
        if self._aiosession is not None and not self._aiosession.closed:
            await self._aiosession.close()
        self._aiosession = None

    async def _ordered_tts_urls(self) -> list[str]:
        async with self._url_lock:
            if not self._tts_urls:
                return []
            start = self._next_url % len(self._tts_urls)
            self._next_url += 1
            return self._tts_urls[start:] + self._tts_urls[:start]

    async def run_batch(self, description: str, work: Callable[[], Any]) -> Any:
        async def queued_work():
            result = work()
            if asyncio.iscoroutine(result):
                return await result
            return result

        _, future = await self._gpu_submit("tts", description, queued_work)
        return await future

    def _custom_voices(self, user_id: int | None = None) -> list[_VoiceRow]:
        rows = self._db_getter().execute(
            "SELECT id, name, source FROM voices WHERE user_id=? AND source='custom' ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [_VoiceRow(id=r[0], name=r[1], source=r[2]) for r in rows]

    def _resolve_voice(self, voice: str, user_id: int | None = None) -> str:
        """Accept either a custom voice_id or a speaker name. Raise if the
        voice is unknown or not a custom voice."""
        db = self._db_getter()
        row = db.execute(
            "SELECT id, name, source FROM voices WHERE user_id=? AND (id=? OR name=? COLLATE NOCASE)",
            (user_id, voice, voice),
        ).fetchone()
        if not row:
            # Try to look the name up in hosts → voice_id (scope to user)
            host = db.execute(
                "SELECT voice_id FROM hosts WHERE user_id=? AND name=? COLLATE NOCASE AND voice_id IS NOT NULL",
                (user_id, voice),
            ).fetchone()
            if host and host[0]:
                row = db.execute(
                    "SELECT id, name, source FROM voices WHERE user_id=? AND id=?",
                    (user_id, host[0]),
                ).fetchone()
        if not row:
            raise RuntimeError(
                f"Podcast TTS: voice {voice!r} is not a known custom voice"
            )
        if row[2] != "custom":
            raise RuntimeError(
                f"Podcast TTS: voice {row[0]} (source={row[2]}) is not a custom voice. "
                "Podcasts may only use custom voices."
            )
        return row[0]

    def _custom_voice_preset(self, voice_id: str, user_id: int | None = None) -> dict:
        """Look up the stored (speaker, instruct, language) preset for a
        custom voice. Raises if the voice is not a custom preset."""
        db = self._db_getter()
        row = db.execute(
            "SELECT speaker, instruct, language, source FROM voices WHERE user_id=? AND id=?",
            (user_id, voice_id),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Podcast TTS: voice {voice_id!r} not found")
        if row[3] != "custom":
            raise RuntimeError(
                f"Podcast TTS: voice {voice_id!r} is not a custom voice"
            )
        speaker = (row[0] or "").strip()
        if not speaker:
            raise RuntimeError(
                f"Podcast TTS: custom voice {voice_id!r} has no speaker set"
            )
        return {
            "speaker": speaker,
            "instruct": (row[1] or "").strip() or None,
            "language": (row[2] or "English").strip() or "English",
        }

    async def synthesize_to_file(
        self,
        text: str,
        voice: str,
        output_path: Path,
        output_format: AudioFormat,
        *,
        user_id: int | None = None,
        notes: str | None = None,
    ) -> float:
        voice_id = self._resolve_voice(voice, user_id=user_id)
        preset = self._custom_voice_preset(voice_id, user_id=user_id)
        response_format = "mp3" if output_format == "mp3" else "wav"

        # Only per-segment notes steer the TTS. The voice preset's `instruct`
        # field is deliberately NOT used as a fallback here: presets are
        # English-worded (e.g. "warm, energetic, clear articulation") and
        # bias the model toward English pronunciation when synthesizing
        # non-English text. The script generator already emits language-
        # appropriate notes per segment; if notes are missing we prefer an
        # un-steered generation over a mis-steered one.
        segment_instruct = (notes or "").strip() or None

        payload: dict[str, Any] = {
            "text": text,
            "speaker": preset["speaker"],
            "language": preset["language"],
            "response_format": response_format,
        }
        if segment_instruct:
            payload["instruct"] = segment_instruct

        urls = await self._ordered_tts_urls()
        last_err: Exception | None = None

        for attempt, url in enumerate(urls):
            async def work(url: str = url, attempt: int = attempt):
                logger.info(
                    "TTS request attempt %d/%d -> %s (speaker=%s, len=%d)",
                    attempt + 1, len(urls), url, preset["speaker"], len(text),
                )
                timeout = aiohttp.ClientTimeout(
                    total=3600, sock_connect=15, sock_read=3600
                )
                start = time.perf_counter()
                status: int | str = "error"
                try:
                    async with self._session.post(
                        f"{url}/v1/audio/speech/custom",
                        json=payload,
                        timeout=timeout,
                    ) as resp:
                        status = resp.status
                        body = await resp.read()
                        if resp.status >= 400:
                            raise RuntimeError(
                                f"TTS failed ({resp.status}): "
                                f"{body.decode('utf-8', 'replace')[:300]}"
                            )
                        headers = {k.lower(): v for k, v in resp.headers.items()}
                        duration = float(headers.get("x-audio-duration", "0") or 0)
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        await asyncio.to_thread(output_path.write_bytes, body)
                        if duration <= 0:
                            duration = max(1.0, len(text.split()) / 2.3)
                        return duration
                finally:
                    labels = {"path": "/v1/audio/speech/custom", "status": status}
                    inc("vocarium_tts_requests_total", labels=labels)
                    observe(
                        "vocarium_tts_inference_seconds",
                        time.perf_counter() - start,
                        labels,
                    )

            try:
                async with self._url_semaphores[url]:
                    return await work()
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "TTS attempt %d/%d failed on %s: %s", attempt + 1, len(urls), url, exc
                )
                # try next URL
        assert last_err is not None
        raise last_err

    async def default_voice_for_speaker(self, speaker: str, *, user_id: int | None = None) -> str:
        """Return a custom voice_id. Prefer a host.voice_id match for the
        given speaker name, otherwise fall back to the first custom voice."""
        db = self._db_getter()
        host = db.execute(
            "SELECT voice_id FROM hosts WHERE user_id=? AND name=? COLLATE NOCASE AND voice_id IS NOT NULL",
            (user_id, speaker),
        ).fetchone()
        if host and host[0]:
            return host[0]

        custom = self._custom_voices(user_id=user_id)
        if custom:
            return custom[0].id
        raise RuntimeError(
            "No custom voices available — create at least one custom voice "
            "before generating podcast audio."
        )


class VocariumMusicGenerator:
    """ACE-Step bridge for podcast background music. Goes through the GPU
    queue so TTS gets evicted before music starts on shared hardware."""

    def __init__(self, music_url: str, gpu_submit: Callable[..., Any]):
        self._music_url = music_url
        self._gpu_submit = gpu_submit
        self._aiosession: aiohttp.ClientSession | None = None

    @property
    def _session(self) -> aiohttp.ClientSession:
        if self._aiosession is None or self._aiosession.closed:
            self._aiosession = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
            )
        return self._aiosession

    async def aclose(self) -> None:
        if self._aiosession is not None and not self._aiosession.closed:
            await self._aiosession.close()
        self._aiosession = None

    async def generate_to_file(
        self,
        prompt: str,
        duration_s: float,
        output_path: Path,
        output_format: AudioFormat,
        *,
        user_id: int | None = None,
    ) -> float:
        payload = {
            "prompt": prompt,
            "audio_duration": float(duration_s),
            "thinking": False,
            "model": "acestep-v15-turbo",
            "inference_steps": 8,
            "batch_size": 1,
            "audio_format": output_format,
            "use_random_seed": True,
        }
        timeout = aiohttp.ClientTimeout(total=600, sock_connect=30, sock_read=600)

        async def work():
            async with self._session.post(
                f"{self._music_url}/release_task", json=payload, timeout=timeout
            ) as resp:
                submit_body = await resp.read()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"music submit failed ({resp.status}): "
                        f"{submit_body.decode('utf-8', 'replace')[:300]}"
                    )
                submit = json.loads(submit_body)
            task_id = (submit.get("data") or {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"music submit returned no task_id: {submit}")

            # Poll until status == 1 (success) or 2 (failure).
            for _ in range(300):  # ~10 minutes
                await asyncio.sleep(2)
                async with self._session.post(
                    f"{self._music_url}/query_result",
                    json={"task_id_list": [task_id]},
                    timeout=timeout,
                ) as poll_resp:
                    if poll_resp.status >= 400:
                        continue
                    poll = json.loads(await poll_resp.read())
                tasks = poll.get("data") or []
                if not tasks:
                    continue
                task = tasks[0]
                if task.get("status") == 1:
                    result = task.get("result")
                    if isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except json.JSONDecodeError:
                            result = []
                    if not result or not isinstance(result, list):
                        raise RuntimeError(f"music returned empty result: {task}")
                    first = result[0] or {}
                    file_ref = first.get("file") or ""
                    path_part = file_ref.replace("/v1/audio?path=", "")
                    if not path_part:
                        raise RuntimeError(
                            f"music result missing 'file': {first}"
                        )
                    async with self._session.get(
                        f"{self._music_url}/v1/audio",
                        params={"path": path_part},
                        timeout=timeout,
                    ) as audio_resp:
                        audio_body = await audio_resp.read()
                        if audio_resp.status >= 400:
                            raise RuntimeError(
                                f"music download failed ({audio_resp.status})"
                            )
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(output_path.write_bytes, audio_body)
                    return float(duration_s)
                if task.get("status") == 2:
                    raise RuntimeError(f"music generation failed: {task}")
            raise RuntimeError("music generation timed out after ~10 minutes")

        _, future = await self._gpu_submit("music", "Podcast Music", work)
        return await future


class VocariumSFXGenerator:
    """MMAudio bridge for podcast prompted sound effects. Synchronous WAV
    response, written straight to disk."""

    def __init__(self, sfx_url: str, gpu_submit: Callable[..., Any]):
        self._sfx_url = sfx_url
        self._gpu_submit = gpu_submit
        self._aiosession: aiohttp.ClientSession | None = None

    @property
    def _session(self) -> aiohttp.ClientSession:
        if self._aiosession is None or self._aiosession.closed:
            self._aiosession = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
            )
        return self._aiosession

    async def aclose(self) -> None:
        if self._aiosession is not None and not self._aiosession.closed:
            await self._aiosession.close()
        self._aiosession = None

    async def generate_to_file(
        self,
        prompt: str,
        duration_s: float,
        output_path: Path,
        output_format: AudioFormat,
        *,
        user_id: int | None = None,
    ) -> float:
        payload = {
            "prompt": prompt,
            "negative_prompt": "",
            "duration": float(duration_s),
            "cfg_strength": 4.5,
            "num_steps": 25,
        }
        timeout = aiohttp.ClientTimeout(total=300, sock_connect=30, sock_read=300)

        async def work():
            async with self._session.post(
                f"{self._sfx_url}/generate", json=payload, timeout=timeout
            ) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"sfx failed ({resp.status}): "
                        f"{body.decode('utf-8', 'replace')[:300]}"
                    )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(output_path.write_bytes, body)
            return float(duration_s)

        _, future = await self._gpu_submit("sfx", "Podcast SFX", work)
        return await future


# ---------------------------------------------------------------------------
# Helpers — DB → dict serialisation
# ---------------------------------------------------------------------------


def _host_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "personality": row[2] or "",
        "speaking_style": row[3] or "",
        "voice_id": row[4],
        "role": row[5] or "host",
        "created_at": row[6],
        "updated_at": row[7],
    }


_HOST_COLUMNS = (
    "id, name, personality, speaking_style, voice_id, role, created_at, updated_at"
)


def _embedding_to_blob(embedding: list[float] | None) -> bytes | None:
    if not embedding:
        return None
    try:
        return struct.pack(f"<{len(embedding)}f", *[float(v) for v in embedding])
    except (TypeError, ValueError, struct.error):
        return None


def _embedding_from_blob(blob: bytes | memoryview | None) -> list[float] | None:
    if not blob:
        return None
    raw = bytes(blob)
    if len(raw) % 4:
        return None
    try:
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))
    except struct.error:
        return None


def _embedding_from_json(text: str | None) -> list[float] | None:
    if not text:
        return None
    try:
        values = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(values, list):
        return None
    try:
        return [float(v) for v in values]
    except (TypeError, ValueError):
        return None


def _podcast_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "topic": row[1] or "",
        "format": row[2],
        "disfluency_level": row[3],
        "duration": row[4],
        "language": row[5],
        "status": row[6],
        "error_message": row[7],
        "hosts": json.loads(row[8]) if row[8] else [],
        "sources": json.loads(row[9]) if row[9] else [],
        "script": json.loads(row[10]) if row[10] else None,
        "audio_path": row[11],
        "audio_duration": row[12] or 0,
        "audio_format": row[13] or "mp3",
        "audio_size": row[14] or 0,
        "audio_sha256": row[15] or "",
        "total_words": row[16] or 0,
        "created_at": row[17],
        "updated_at": row[18],
    }


_PODCAST_COLUMNS = (
    "id, topic, format, disfluency_level, duration, language, status, error_message, "
    "hosts_json, sources_json, script_json, audio_path, audio_duration, audio_format, "
    "audio_size, audio_sha256, total_words, created_at, updated_at"
)


def _source_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "podcast_id": row[1],
        "type": row[2],
        "title": row[3],
        "content": row[4],
        "url": row[5],
        "status": row[6],
        "error_message": row[7],
        "chunk_count": row[8] or 0,
        "created_at": row[9],
        "processed_at": row[10],
    }


_SOURCE_COLUMNS = (
    "id, podcast_id, type, title, content, url, status, error_message, "
    "chunk_count, created_at, processed_at"
)


def _segment_to_dict(seg: ScriptSegment) -> dict:
    return {
        "id": seg.id,
        "script_id": seg.script_id,
        "speaker": seg.speaker,
        "text": seg.text,
        "type": seg.type,
        "voice": seg.voice,
        "notes": seg.notes,
        "position": seg.position,
        "word_count": seg.word_count,
        "estimated_duration": seg.estimated_duration,
        "regenerated_from": seg.regenerated_from,
        "created_at": seg.created_at,
        "updated_at": seg.updated_at,
        "overlap_ms": seg.overlap_ms,
        "prompt": seg.prompt,
        "duration_ms": seg.duration_ms,
        "volume_db": seg.volume_db,
    }


def _dict_to_segment(d: dict) -> ScriptSegment:
    return ScriptSegment(
        id=d.get("id") or generate_id("seg"),
        script_id=d.get("script_id"),
        speaker=d.get("speaker") or "",
        text=d.get("text") or "",
        type=d.get("type") or "speech",
        voice=d.get("voice"),
        notes=d.get("notes"),
        position=d.get("position") or 0,
        word_count=d.get("word_count") or 0,
        estimated_duration=d.get("estimated_duration") or 0,
        regenerated_from=d.get("regenerated_from"),
        created_at=d.get("created_at") or "",
        updated_at=d.get("updated_at") or "",
        overlap_ms=int(d.get("overlap_ms") or 0),
        prompt=d.get("prompt"),
        duration_ms=int(d.get("duration_ms") or 0),
        volume_db=float(d.get("volume_db") or 0.0),
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sse_event(kind: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {kind}\ndata: {payload}\n\n"


def _clean_label(value: Any, fallback: str = "") -> str:
    text = str(value or fallback).strip()
    return text[:MAX_TITLE_CHARS]


def _clean_limited_text(value: Any, field: str, max_chars: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise HTTPException(400, f"{field} is required")
    if len(text) > max_chars:
        raise HTTPException(413, f"{field} too long (max {max_chars} chars)")
    return text


def _clean_language(value: Any) -> str:
    language = str(value or "de").strip() or "de"
    if len(language) > MAX_LANGUAGE_CHARS:
        raise HTTPException(413, f"language too long (max {MAX_LANGUAGE_CHARS} chars)")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", language):
        raise HTTPException(400, "language must be a short language code or name")
    return language


def _require_choice(value: Any, allowed: set[str], field: str) -> str:
    cleaned = str(value or "").strip()
    if cleaned not in allowed:
        raise HTTPException(
            400,
            f"{field} must be one of: {', '.join(sorted(allowed))}",
        )
    return cleaned


def _validate_source_url(raw_url: str) -> str:
    value = raw_url.strip()
    if not value:
        raise HTTPException(400, "url is required")
    if len(value) > MAX_SOURCE_URL_CHARS:
        raise HTTPException(400, f"url too long (max {MAX_SOURCE_URL_CHARS} chars)")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "url must be an http(s) URL")
    host = (parsed.hostname or "").strip().lower()
    if host in {"localhost"} or host.endswith(".localhost"):
        raise HTTPException(400, "localhost URLs are not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and not ip.is_global:
        raise HTTPException(400, "private network URLs are not allowed")
    if ip is None and os.environ.get("PODCAST_VALIDATE_SOURCE_DNS", "true").lower() in ("1", "true", "yes", "on"):
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise HTTPException(400, f"url host could not be resolved: {exc}") from exc
        resolved = {
            item[4][0]
            for item in infos
            if item and len(item) >= 5 and item[4]
        }
        if not resolved:
            raise HTTPException(400, "url host could not be resolved")
        for address in resolved:
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError:
                raise HTTPException(400, "url resolved to an invalid address")
            if not resolved_ip.is_global:
                raise HTTPException(400, "url resolves to a private network address")
    return value


def _upload_root() -> Path:
    root = Path(os.environ.get("PODCAST_UPLOAD_PATH", "/app/data/podcast_uploads"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _audio_root() -> Path:
    root = Path(os.environ.get("PODCAST_AUDIO_PATH", "/app/data/podcast_audio"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_audio_path(path_value: str) -> Path:
    try:
        root = _audio_root().resolve()
        candidate = Path(path_value).resolve()
    except OSError:
        raise HTTPException(404, "Audio file missing on disk")
    if candidate != root and root not in candidate.parents:
        raise HTTPException(404, "Audio file missing on disk")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unlink_uploaded_file(path_value: str) -> None:
    path = Path(path_value)
    try:
        upload_root = _upload_root().resolve()
        candidate = path.resolve()
    except OSError:
        return
    if upload_root == candidate.parent and candidate.exists():
        candidate.unlink(missing_ok=True)


def _get_podcast_or_404(db: sqlite3.Connection, podcast_id: str, user_id: int) -> dict:
    row = db.execute(
        f"SELECT {_PODCAST_COLUMNS} FROM podcasts WHERE id=? AND user_id=?",
        (podcast_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Podcast not found")
    return _podcast_row_to_dict(row)


def _get_host_or_404(db: sqlite3.Connection, host_id: str, user_id: int) -> dict:
    row = db.execute(
        f"SELECT {_HOST_COLUMNS} FROM hosts WHERE id=? AND user_id=?",
        (host_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Host not found")
    return _host_row_to_dict(row)


def _get_custom_voice_or_400(
    db: sqlite3.Connection,
    voice_id: str,
    user_id: int,
) -> tuple[str, str]:
    row = db.execute(
        "SELECT id, source FROM voices WHERE id=? AND user_id=?",
        (voice_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(400, f"voice_id {voice_id} not found")
    if row[1] != "custom":
        raise HTTPException(
            400,
            "Only custom voices can be assigned to podcast hosts",
        )
    return row[0], row[1]


def _coerce_int(value: Any, field: str, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be an integer")


def _coerce_float(value: Any, field: str, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be numeric")


def _validate_segment_text(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) > MAX_SEGMENT_TEXT_CHARS:
        raise HTTPException(
            413,
            f"text too long (max {MAX_SEGMENT_TEXT_CHARS} chars)",
        )
    return cleaned


def _validate_segment_prompt(prompt: str) -> str:
    cleaned = prompt.strip()
    if len(cleaned) > MAX_SEGMENT_PROMPT_CHARS:
        raise HTTPException(
            413,
            f"prompt too long (max {MAX_SEGMENT_PROMPT_CHARS} chars)",
        )
    return cleaned


def _validate_duration_ms(duration_ms: int) -> int:
    if duration_ms < 0 or duration_ms > MAX_MEDIA_DURATION_MS:
        raise HTTPException(400, "duration_ms must be between 0 and 600000")
    return duration_ms


def _validate_volume_db(volume_db: float) -> float:
    if volume_db < -60 or volume_db > 12:
        raise HTTPException(400, "volume_db must be between -60 and 12")
    return volume_db


def _validate_overlap_ms(overlap_ms: int) -> int:
    if overlap_ms < -30000 or overlap_ms > 30000:
        raise HTTPException(400, "overlap_ms must be between -30000 and 30000")
    return overlap_ms


def _validate_segment_semantics(segment: dict) -> None:
    seg_type = str(segment.get("type") or "speech")
    text = str(segment.get("text") or "").strip()
    prompt = str(segment.get("prompt") or "").strip()
    if seg_type in ("speech", "reaction") and not text:
        raise HTTPException(400, f"{seg_type} segments require text")
    if seg_type in ("music", "sfx") and not (prompt or text):
        raise HTTPException(400, "music/sfx segments require a prompt")


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_podcast_router(
    *,
    get_current_user: Callable[[Request], dict],
    tts_url: str,
    db_getter: Callable[[], sqlite3.Connection],
    gpu_submit: Callable[..., Any],
    extra_tts_urls: list[str] | None = None,
    assembler_output_dir: str | os.PathLike[str] | None = None,
    assembler_sfx_dir: str | os.PathLike[str] | None = None,
    music_url: str | None = None,
    sfx_url: str | None = None,
) -> tuple[APIRouter, AudioAssembler]:
    """Return (router, assembler). Caller wires the router into the app and
    uses the assembler for startup housekeeping."""

    router = APIRouter(prefix="/api", tags=["podcast"])
    tts_bridge = VocariumTTSGenerator(
        tts_url, db_getter, gpu_submit, extra_tts_urls=extra_tts_urls
    )
    music_bridge: MusicGenerator | None = (
        VocariumMusicGenerator(music_url, gpu_submit) if music_url else None
    )
    sfx_bridge: SFXGenerator | None = (
        VocariumSFXGenerator(sfx_url, gpu_submit) if sfx_url else None
    )
    assembler = AudioAssembler(
        tts=tts_bridge,
        music=music_bridge,
        sfx=sfx_bridge,
        output_dir=assembler_output_dir,
        sfx_dir=assembler_sfx_dir,
    )

    script_generator = ScriptGenerator()
    embedding_client: EmbeddingClient = get_embedding_client()
    docling_client: DoclingClient = get_docling_client()

    # ---- Hosts CRUD --------------------------------------------------------

    @router.get("/hosts")
    async def list_hosts(request: Request):
        user = get_current_user(request)
        db = db_getter()
        rows = db.execute(
            f"SELECT {_HOST_COLUMNS} FROM hosts WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
        return {"hosts": [_host_row_to_dict(r) for r in rows]}

    @router.get("/hosts/{host_id}")
    async def get_host(host_id: str, request: Request):
        user = get_current_user(request)
        return _get_host_or_404(db_getter(), host_id, user["id"])

    @router.post("/hosts")
    async def create_host(body: dict, request: Request):
        user = get_current_user(request)
        name = _clean_limited_text(
            body.get("name"), "name", MAX_HOST_NAME_CHARS, required=True
        )
        role = _require_choice(body.get("role") or "host", {"host", "expert"}, "role")
        voice_id = (body.get("voice_id") or "").strip() or None
        db = db_getter()
        # If voice_id given, verify it belongs to user and is a custom voice
        if voice_id:
            _get_custom_voice_or_400(db, voice_id, user["id"])
        host_id = generate_id("host")
        now = _now_iso()
        db.execute(
            "INSERT INTO hosts (id, user_id, name, personality, speaking_style, voice_id, role, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                host_id,
                user["id"],
                name,
                _clean_limited_text(body.get("personality"), "personality", MAX_HOST_PROFILE_CHARS),
                _clean_limited_text(body.get("speaking_style"), "speaking_style", MAX_HOST_PROFILE_CHARS),
                voice_id,
                role,
                now,
                now,
            ),
        )
        db.commit()
        return _get_host_or_404(db, host_id, user["id"])

    @router.patch("/hosts/{host_id}")
    async def update_host(host_id: str, body: dict, request: Request):
        user = get_current_user(request)
        db = db_getter()
        _get_host_or_404(db, host_id, user["id"])
        fields: list[str] = []
        values: list[Any] = []
        for key in ("name", "personality", "speaking_style", "voice_id", "role"):
            if key in body:
                value = body[key]
                if key == "name":
                    value = _clean_limited_text(
                        value, "name", MAX_HOST_NAME_CHARS, required=True
                    )
                elif key in ("personality", "speaking_style"):
                    value = _clean_limited_text(
                        value, key, MAX_HOST_PROFILE_CHARS
                    )
                elif key == "role":
                    value = _require_choice(value, {"host", "expert"}, "role")
                elif key == "voice_id":
                    value = (value or "").strip() or None
                    if value:
                        _get_custom_voice_or_400(db, value, user["id"])
                fields.append(f"{key}=?")
                values.append(value)
        if not fields:
            return _get_host_or_404(db, host_id, user["id"])
        fields.append("updated_at=?")
        values.append(_now_iso())
        values.extend([host_id, user["id"]])
        db.execute(
            f"UPDATE hosts SET {', '.join(fields)} WHERE id=? AND user_id=?", values
        )
        db.commit()
        return _get_host_or_404(db, host_id, user["id"])

    @router.delete("/hosts/{host_id}")
    async def delete_host(host_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        _get_host_or_404(db, host_id, user["id"])
        db.execute("DELETE FROM hosts WHERE id=? AND user_id=?", (host_id, user["id"]))
        db.commit()
        return {"status": "deleted", "id": host_id}

    # ---- Podcast CRUD ------------------------------------------------------

    @router.get("/podcasts")
    async def list_podcasts(request: Request):
        user = get_current_user(request)
        db = db_getter()
        rows = db.execute(
            f"SELECT {_PODCAST_COLUMNS} FROM podcasts WHERE user_id=? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        return {"podcasts": [_podcast_row_to_dict(r) for r in rows]}

    @router.get("/podcasts/{podcast_id}")
    async def get_podcast(podcast_id: str, request: Request):
        user = get_current_user(request)
        return _get_podcast_or_404(db_getter(), podcast_id, user["id"])

    @router.post("/podcasts")
    async def create_podcast(body: dict, request: Request):
        user = get_current_user(request)
        fmt = _require_choice(body.get("format") or "dialog", ALLOWED_PODCAST_FORMATS, "format")
        duration = _require_choice(
            body.get("duration") or "medium", ALLOWED_PODCAST_DURATIONS, "duration"
        )
        audio_format = _require_choice(
            body.get("audio_format") or "mp3", ALLOWED_AUDIO_FORMATS, "audio_format"
        )
        try:
            disfluency_level = int(body.get("disfluency_level", 2))
        except (TypeError, ValueError):
            raise HTTPException(400, "disfluency_level must be 0..3")
        if disfluency_level not in (0, 1, 2, 3):
            raise HTTPException(400, "disfluency_level must be 0..3")

        host_ids = body.get("host_ids") or []
        if not isinstance(host_ids, list):
            raise HTTPException(400, "host_ids must be a list")

        db = db_getter()
        hosts: list[dict] = []
        for hid in host_ids:
            hosts.append(_get_host_or_404(db, hid, user["id"]))

        # Enforce: every host with a voice_id must use a custom voice
        for h in hosts:
            if h["voice_id"]:
                _get_custom_voice_or_400(db, h["voice_id"], user["id"])

        podcast_id = generate_id("pod")
        now = _now_iso()
        db.execute(
            "INSERT INTO podcasts "
            "(id, user_id, topic, format, disfluency_level, duration, language, status, "
            " hosts_json, sources_json, audio_format, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                podcast_id,
                user["id"],
                _clean_limited_text(body.get("topic"), "topic", MAX_TOPIC_CHARS),
                fmt,
                disfluency_level,
                duration,
                _clean_language(body.get("language")),
                "draft",
                json.dumps(hosts),
                json.dumps([]),
                audio_format,
                now,
                now,
            ),
        )
        db.commit()
        return _get_podcast_or_404(db, podcast_id, user["id"])

    @router.patch("/podcasts/{podcast_id}")
    async def update_podcast(podcast_id: str, body: dict, request: Request):
        user = get_current_user(request)
        db = db_getter()
        _get_podcast_or_404(db, podcast_id, user["id"])
        fields: list[str] = []
        values: list[Any] = []
        for key in ("topic", "format", "duration", "language", "audio_format"):
            if key in body:
                if key == "format":
                    body[key] = _require_choice(body[key], ALLOWED_PODCAST_FORMATS, "format")
                elif key == "duration":
                    body[key] = _require_choice(body[key], ALLOWED_PODCAST_DURATIONS, "duration")
                elif key == "audio_format":
                    body[key] = _require_choice(body[key], ALLOWED_AUDIO_FORMATS, "audio_format")
                elif key == "topic":
                    body[key] = _clean_limited_text(body[key], "topic", MAX_TOPIC_CHARS)
                elif key == "language":
                    body[key] = _clean_language(body[key])
                fields.append(f"{key}=?")
                values.append(body[key])
        if "disfluency_level" in body:
            try:
                lvl = int(body["disfluency_level"])
            except (TypeError, ValueError):
                raise HTTPException(400, "disfluency_level must be 0..3")
            if lvl not in (0, 1, 2, 3):
                raise HTTPException(400, "disfluency_level must be 0..3")
            fields.append("disfluency_level=?")
            values.append(lvl)
        if "host_ids" in body:
            if not isinstance(body["host_ids"], list):
                raise HTTPException(400, "host_ids must be a list")
            hosts: list[dict] = []
            for hid in body["host_ids"] or []:
                hosts.append(_get_host_or_404(db, hid, user["id"]))
            for h in hosts:
                if h["voice_id"]:
                    _get_custom_voice_or_400(db, h["voice_id"], user["id"])
            fields.append("hosts_json=?")
            values.append(json.dumps(hosts))
        if not fields:
            return _get_podcast_or_404(db, podcast_id, user["id"])
        fields.append("updated_at=?")
        values.append(_now_iso())
        values.extend([podcast_id, user["id"]])
        db.execute(
            f"UPDATE podcasts SET {', '.join(fields)} WHERE id=? AND user_id=?",
            values,
        )
        db.commit()
        return _get_podcast_or_404(db, podcast_id, user["id"])

    @router.delete("/podcasts/{podcast_id}")
    async def delete_podcast(podcast_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        p = _get_podcast_or_404(db, podcast_id, user["id"])
        db.execute(
            "DELETE FROM podcasts WHERE id=? AND user_id=?", (podcast_id, user["id"])
        )
        db.commit()

        # Clean up audio files + per-project temp dir
        project_dir = assembler.output_dir / podcast_id
        if project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
        if p.get("audio_path"):
            audio_path = Path(p["audio_path"])
            if audio_path.exists():
                audio_path.unlink(missing_ok=True)
        return {"status": "deleted", "id": podcast_id}

    # ---- Sources -----------------------------------------------------------

    async def _process_source_async(
        source_id: str, podcast_id: str, kind: str, content: str | None, url: str | None
    ):
        """Parse + chunk + embed a source. Runs in background. Updates status."""
        db = db_getter()
        try:
            if kind == "file":
                path = Path(content or "")
                parsed = await docling_client.parse_file(path)
            elif kind == "url":
                parsed = await docling_client.parse_url(url or "")
            else:
                parsed = {"status": "success", "text": content or ""}

            if parsed.get("status") != "success":
                raise RuntimeError(parsed.get("error") or "parse failed")

            text = parsed.get("text") or ""
            if not text.strip():
                raise RuntimeError("empty text extracted from source")

            chunks = chunk_text(text)
            if not chunks:
                raise RuntimeError("text produced no usable chunks")

            try:
                embeddings = await embedding_client.embed_batch(chunks)
            except Exception as exc:
                logger.warning(
                    "Embedding failed for source=%s, proceeding without embeddings: %s",
                    source_id, exc,
                )
                embeddings = [None] * len(chunks)  # type: ignore[list-item]

            db.execute(
                "DELETE FROM podcast_chunks WHERE source_id=? AND podcast_id=?",
                (source_id, podcast_id),
            )
            for pos, chunk in enumerate(chunks):
                chunk_id = generate_id("chunk")
                emb_blob = _embedding_to_blob(embeddings[pos])
                db.execute(
                    "INSERT INTO podcast_chunks (id, source_id, podcast_id, content, embedding, embedding_blob, position) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (chunk_id, source_id, podcast_id, chunk, None, emb_blob, pos),
                )
            db.execute(
                "UPDATE podcast_sources SET status='processed', chunk_count=?, processed_at=CURRENT_TIMESTAMP, error_message=NULL "
                "WHERE id=? AND podcast_id=?",
                (len(chunks), source_id, podcast_id),
            )
            db.commit()
            logger.info("Source %s processed (chunks=%d)", source_id, len(chunks))
        except Exception as exc:
            logger.exception("Source processing failed: %s", exc)
            db.execute(
                "UPDATE podcast_sources SET status='failed', error_message=? WHERE id=? AND podcast_id=?",
                (str(exc), source_id, podcast_id),
            )
            db.commit()

    def _assert_podcast(db: sqlite3.Connection, podcast_id: str, user_id: int):
        row = db.execute(
            "SELECT id FROM podcasts WHERE id=? AND user_id=?",
            (podcast_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Podcast not found")

    @router.get("/podcasts/{podcast_id}/sources")
    async def list_sources(podcast_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        _assert_podcast(db, podcast_id, user["id"])
        rows = db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM podcast_sources WHERE podcast_id=? ORDER BY created_at",
            (podcast_id,),
        ).fetchall()
        return {"sources": [_source_row_to_dict(r) for r in rows]}

    @router.get("/podcasts/{podcast_id}/sources/{source_id}")
    async def get_source(podcast_id: str, source_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        _assert_podcast(db, podcast_id, user["id"])
        row = db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM podcast_sources WHERE id=? AND podcast_id=?",
            (source_id, podcast_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Source not found")
        return _source_row_to_dict(row)

    @router.get("/podcasts/{podcast_id}/sources/{source_id}/chunks")
    async def get_source_chunks(
        podcast_id: str, source_id: str, request: Request
    ):
        user = get_current_user(request)
        db = db_getter()
        _assert_podcast(db, podcast_id, user["id"])
        rows = db.execute(
            "SELECT id, source_id, podcast_id, content, position FROM podcast_chunks "
            "WHERE source_id=? AND podcast_id=? ORDER BY position",
            (source_id, podcast_id),
        ).fetchall()
        return {
            "chunks": [
                {
                    "id": r[0],
                    "source_id": r[1],
                    "podcast_id": r[2],
                    "content": r[3],
                    "position": r[4],
                }
                for r in rows
            ]
        }

    @router.post("/podcasts/{podcast_id}/sources/upload")
    async def upload_source(
        podcast_id: str,
        request: Request,
        file: UploadFile = File(...),
    ):
        user = get_current_user(request)
        db = db_getter()
        _assert_podcast(db, podcast_id, user["id"])

        filename = file.filename or "upload"
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_SOURCE_EXTENSIONS:
            raise HTTPException(
                400,
                f"Unsupported file type {suffix!r}. Allowed: {sorted(ALLOWED_SOURCE_EXTENSIONS)}",
            )

        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"File too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)"
            )
        if not data:
            raise HTTPException(400, "File is empty")

        source_id = generate_id("src")
        uploads_dir = _upload_root()
        safe_suffix = suffix if suffix else ""
        disk_path = uploads_dir / f"{source_id}{safe_suffix}"
        await asyncio.to_thread(disk_path.write_bytes, data)

        db.execute(
            "INSERT INTO podcast_sources (id, podcast_id, type, title, content, status) "
            "VALUES (?,?,?,?,?,?)",
            (source_id, podcast_id, "file", _clean_label(filename, "upload"), str(disk_path), "pending"),
        )
        db.commit()

        asyncio.create_task(
            _process_source_async(source_id, podcast_id, "file", str(disk_path), None)
        )

        row = db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM podcast_sources WHERE id=? AND podcast_id=?",
            (source_id, podcast_id),
        ).fetchone()
        return _source_row_to_dict(row)

    @router.post("/podcasts/{podcast_id}/sources/url")
    async def add_url_source(podcast_id: str, body: dict, request: Request):
        user = get_current_user(request)
        db = db_getter()
        _assert_podcast(db, podcast_id, user["id"])
        url = _validate_source_url(body.get("url") or "")
        title = _clean_label(body.get("title"), url)
        source_id = generate_id("src")
        db.execute(
            "INSERT INTO podcast_sources (id, podcast_id, type, title, url, status) "
            "VALUES (?,?,?,?,?,?)",
            (source_id, podcast_id, "url", title, url, "pending"),
        )
        db.commit()

        asyncio.create_task(
            _process_source_async(source_id, podcast_id, "url", None, url)
        )

        row = db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM podcast_sources WHERE id=? AND podcast_id=?",
            (source_id, podcast_id),
        ).fetchone()
        return _source_row_to_dict(row)

    @router.post("/podcasts/{podcast_id}/sources/text")
    async def add_text_source(podcast_id: str, body: dict, request: Request):
        user = get_current_user(request)
        db = db_getter()
        _assert_podcast(db, podcast_id, user["id"])
        text = body.get("content") or body.get("text") or ""
        if not isinstance(text, str):
            raise HTTPException(400, "content must be a string")
        text = text.strip()
        if len(text) < MIN_TEXT_CHARS:
            raise HTTPException(400, f"content too short (min {MIN_TEXT_CHARS} chars)")
        if len(text) > MAX_TEXT_CHARS:
            raise HTTPException(400, f"content too long (max {MAX_TEXT_CHARS} chars)")
        title = _clean_label(body.get("title"), "Text Input")
        source_id = generate_id("src")
        db.execute(
            "INSERT INTO podcast_sources (id, podcast_id, type, title, content, status) "
            "VALUES (?,?,?,?,?,?)",
            (source_id, podcast_id, "text", title, text, "pending"),
        )
        db.commit()

        asyncio.create_task(
            _process_source_async(source_id, podcast_id, "text", text, None)
        )

        row = db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM podcast_sources WHERE id=? AND podcast_id=?",
            (source_id, podcast_id),
        ).fetchone()
        return _source_row_to_dict(row)

    @router.delete("/podcasts/{podcast_id}/sources/{source_id}")
    async def delete_source(podcast_id: str, source_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        _assert_podcast(db, podcast_id, user["id"])
        row = db.execute(
            "SELECT content, type FROM podcast_sources WHERE id=? AND podcast_id=?",
            (source_id, podcast_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Source not found")
        # If it was a file upload, delete from disk
        if row[1] == "file" and row[0]:
            _unlink_uploaded_file(row[0])
        db.execute(
            "DELETE FROM podcast_sources WHERE id=? AND podcast_id=?",
            (source_id, podcast_id),
        )
        db.commit()
        return {"status": "deleted", "id": source_id}

    # ---- Script generation (SSE) ------------------------------------------

    def _load_podcast_chunks(db, podcast_id: str) -> list[SourceChunk]:
        rows = db.execute(
            "SELECT id, source_id, content, position, embedding, embedding_blob FROM podcast_chunks "
            "WHERE podcast_id=? ORDER BY position",
            (podcast_id,),
        ).fetchall()
        chunks: list[SourceChunk] = []
        for r in rows:
            emb = _embedding_from_blob(r[5]) or _embedding_from_json(r[4])
            chunks.append(
                SourceChunk(
                    id=r[0], source_id=r[1], content=r[2], position=r[3], embedding=emb
                )
            )
        return chunks

    def _load_podcast_sources(db, podcast_id: str) -> list[SourceInfo]:
        rows = db.execute(
            "SELECT id, title, type, status FROM podcast_sources WHERE podcast_id=?",
            (podcast_id,),
        ).fetchall()
        return [
            SourceInfo(id=r[0], title=r[1] or "", type=r[2])
            for r in rows
            if r[3] == "processed"
        ]

    def _hosts_from_dicts(hosts: list[dict]) -> list[HostCharacter]:
        return [
            HostCharacter(
                name=h["name"],
                personality=h.get("personality") or "",
                speaking_style=h.get("speaking_style") or "",
                role=h.get("role") or "host",
                voice_id=h.get("voice_id"),
            )
            for h in hosts
        ]

    @router.post("/podcasts/{podcast_id}/script/generate")
    async def generate_script(podcast_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        podcast = _get_podcast_or_404(db, podcast_id, user["id"])

        hosts = _hosts_from_dicts(podcast["hosts"])
        if not hosts:
            raise HTTPException(400, "Podcast has no hosts assigned")

        # Podcasts require custom voices — verify every host with a voice
        for h in hosts:
            if h.voice_id:
                _get_custom_voice_or_400(db, h.voice_id, user["id"])

        chunks = _load_podcast_chunks(db, podcast_id)
        sources = _load_podcast_sources(db, podcast_id)
        if not chunks:
            raise HTTPException(
                400,
                "No processed source chunks found. Upload sources and wait for processing.",
            )

        options = ScriptGenerationOptions(
            format=podcast["format"],
            duration=podcast["duration"],
            disfluency_level=podcast["disfluency_level"],
            language=podcast.get("language") or "de",
            hosts=hosts,
            topic=podcast.get("topic") or None,
        )
        ctx = ScriptGenerationContext(
            project_id=podcast_id,
            chunks=chunks,
            sources=sources,
            options=options,
        )

        db.execute(
            "UPDATE podcasts SET status='generating_script', error_message=NULL, updated_at=? "
            "WHERE id=? AND user_id=?",
            (_now_iso(), podcast_id, user["id"]),
        )
        db.commit()

        # --- Real-time SSE: initial event + background worker with queue ---
        event_queue: asyncio.Queue[str | None] = asyncio.Queue()

        def push_progress(stage: str, progress: int, message: str):
            event_queue.put_nowait(_sse_event("progress", {
                "stage": stage, "progress": progress, "message": message,
            }))

        def push_complete(payload):
            event_queue.put_nowait(_sse_event("progress", {
                "stage": "complete", "progress": 100, "message": "Script ready",
            }))
            event_queue.put_nowait(_sse_event("complete", payload))

        def push_error(msg: str):
            event_queue.put_nowait(_sse_event("error", {"error": msg}))

        async def worker():
            push_progress("preparing", 5, "Preparing script generation")
            try:
                result = await script_generator.generate(ctx, user_id=user["id"])
            except asyncio.CancelledError:
                logger.info("Script generation cancelled (podcast=%s user=%s)", podcast_id, user["id"])
                db.execute(
                    "UPDATE podcasts SET status='cancelled', error_message=?, updated_at=? "
                    "WHERE id=? AND user_id=?",
                    ("cancelled", _now_iso(), podcast_id, user["id"]),
                )
                db.commit()
                push_error("cancelled")
                await event_queue.put(None)
                raise
            except Exception as exc:
                logger.exception("Script generation failed")
                db.execute(
                    "UPDATE podcasts SET status='error', error_message=?, updated_at=? "
                    "WHERE id=? AND user_id=?",
                    (str(exc), _now_iso(), podcast_id, user["id"]),
                )
                db.commit()
                push_error(str(exc))
                await event_queue.put(None)
                return

            segments_json = [_segment_to_dict(s) for s in result.segments]
            script_payload = {
                "segments": segments_json,
                "total_words": result.total_words,
                "estimated_duration": result.estimated_duration,
            }

            db.execute(
                "UPDATE podcasts SET status='script_ready', script_json=?, total_words=?, updated_at=? "
                "WHERE id=? AND user_id=?",
                (json.dumps(script_payload), result.total_words, _now_iso(), podcast_id, user["id"]),
            )
            db.commit()
            push_complete(script_payload)
            await event_queue.put(None)

        async def event_stream():
            yield _sse_event("progress", {
                "stage": "queued", "progress": 0, "message": "Script generation queued...",
            })
            task = asyncio.create_task(worker())
            try:
                while True:
                    event = await event_queue.get()
                    if event is None:
                        break
                    yield event
                # Re-raise if background task failed
                if task.done() and not task.cancelled() and task.exception():
                    raise task.exception()
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @router.patch("/podcasts/{podcast_id}/script/segments/{segment_id}")
    async def update_segment(
        podcast_id: str, segment_id: str, body: dict, request: Request
    ):
        user = get_current_user(request)
        db = db_getter()
        podcast = _get_podcast_or_404(db, podcast_id, user["id"])
        script = podcast.get("script") or {}
        segments = script.get("segments") or []
        target = None
        for seg in segments:
            if seg.get("id") == segment_id:
                target = seg
                break
        if not target:
            raise HTTPException(404, "Segment not found")
        for key in ("speaker", "text", "type", "voice", "notes", "prompt"):
            if key in body:
                if key == "type":
                    target[key] = _require_choice(body[key], ALLOWED_SEGMENT_TYPES, "type")
                elif key == "voice":
                    voice = (body[key] or "").strip() or None
                    if voice:
                        _get_custom_voice_or_400(db, voice, user["id"])
                    target[key] = voice
                elif key == "text":
                    target[key] = _validate_segment_text(str(body[key] or ""))
                elif key == "prompt":
                    prompt = _validate_segment_prompt(str(body[key] or ""))
                    target[key] = prompt or None
                else:
                    target[key] = str(body[key] or "").strip()
        if "overlap_ms" in body:
            target["overlap_ms"] = _validate_overlap_ms(
                _coerce_int(body["overlap_ms"], "overlap_ms")
            )
        if "duration_ms" in body:
            target["duration_ms"] = _validate_duration_ms(
                _coerce_int(body["duration_ms"], "duration_ms")
            )
        if "volume_db" in body:
            target["volume_db"] = _validate_volume_db(
                _coerce_float(body["volume_db"], "volume_db")
            )
        if target.get("type") in ("music", "sfx"):
            prompt = (target.get("prompt") or target.get("text") or "").strip()
            if not prompt:
                raise HTTPException(400, "music/sfx segments require a prompt")
            target["prompt"] = prompt
        if target.get("type") == "pause" and not target.get("duration_ms"):
            target["duration_ms"] = 500
        _validate_segment_semantics(target)
        # Cached audio for music/sfx segments must be invalidated when the
        # prompt or duration changes — let the assembler regenerate next run.
        if "prompt" in body or "duration_ms" in body:
            project_dir = Path(
                os.environ.get("PODCAST_AUDIO_PATH", "/app/data/podcast_audio")
            ) / podcast_id
            for ext in ("mp3", "wav"):
                cached = project_dir / f"{segment_id}.{ext}"
                if cached.exists():
                    try:
                        cached.unlink()
                    except OSError:
                        pass
        if "text" in body:
            text = target.get("text") or ""
            target["word_count"] = count_words(text)
            target["estimated_duration"] = estimate_speaking_duration(target["word_count"])
        # Music/SFX duration drives estimated_duration directly.
        if target.get("type") in ("music", "sfx") and target.get("duration_ms"):
            target["estimated_duration"] = target["duration_ms"] / 1000.0
        target["updated_at"] = _now_iso()
        script["segments"] = segments
        script["total_words"] = sum(s.get("word_count", 0) for s in segments)
        script["estimated_duration"] = sum(s.get("estimated_duration", 0) for s in segments)

        db.execute(
            "UPDATE podcasts SET script_json=?, total_words=?, updated_at=? WHERE id=? AND user_id=?",
            (json.dumps(script), script["total_words"], _now_iso(), podcast_id, user["id"]),
        )
        db.commit()
        return _get_podcast_or_404(db, podcast_id, user["id"])

    @router.post("/podcasts/{podcast_id}/script/segments")
    async def add_segment(podcast_id: str, body: dict, request: Request):
        """Insert a new segment (typically music or sfx) at a given position."""
        user = get_current_user(request)
        db = db_getter()
        podcast = _get_podcast_or_404(db, podcast_id, user["id"])
        script = podcast.get("script") or {}
        segments = script.get("segments") or []

        seg_type = _require_choice(
            body.get("type") or "speech", ALLOWED_SEGMENT_TYPES, "type"
        )

        text = _validate_segment_text(str(body.get("text") or ""))
        prompt = body.get("prompt")
        if seg_type in ("music", "sfx"):
            prompt = _validate_segment_prompt(str(prompt or text or ""))
            if not prompt:
                raise HTTPException(400, "music/sfx segments require a prompt")
        elif prompt:
            prompt = _validate_segment_prompt(str(prompt))

        voice = (body.get("voice") or "").strip() or None
        if voice:
            _get_custom_voice_or_400(db, voice, user["id"])

        duration_ms = _validate_duration_ms(
            _coerce_int(body.get("duration_ms"), "duration_ms")
        )
        overlap_ms = _validate_overlap_ms(
            _coerce_int(body.get("overlap_ms"), "overlap_ms")
        )
        volume_db = _validate_volume_db(
            _coerce_float(body.get("volume_db"), "volume_db")
        )

        # Sensible defaults per type.
        if seg_type == "pause" and duration_ms <= 0:
            duration_ms = 500
        if seg_type == "music" and duration_ms <= 0:
            duration_ms = 30000
        if seg_type == "music" and volume_db == 0.0:
            volume_db = -14.0  # ducked under speech by default
        if seg_type == "sfx" and duration_ms <= 0 and prompt:
            duration_ms = 4000

        now = _now_iso()
        new_seg = {
            "id": generate_id("seg"),
            "script_id": None,
            "speaker": body.get("speaker") or "",
            "text": text or (prompt or ""),
            "type": seg_type,
            "voice": voice,
            "notes": str(body.get("notes") or "").strip() or None,
            "position": 0,  # set below
            "word_count": count_words(text),
            "estimated_duration": (duration_ms / 1000.0) if duration_ms else 0.0,
            "regenerated_from": None,
            "created_at": now,
            "updated_at": now,
            "overlap_ms": overlap_ms,
            "prompt": prompt,
            "duration_ms": duration_ms,
            "volume_db": volume_db,
        }
        _validate_segment_semantics(new_seg)

        # Insertion index: explicit `position` (int) takes precedence, else after.
        try:
            insert_at = int(body["position"]) if "position" in body else len(segments)
        except (TypeError, ValueError):
            insert_at = len(segments)
        insert_at = max(0, min(insert_at, len(segments)))
        segments.insert(insert_at, new_seg)
        for idx, seg in enumerate(segments):
            seg["position"] = idx

        script["segments"] = segments
        script["total_words"] = sum(s.get("word_count", 0) for s in segments)
        script["estimated_duration"] = sum(s.get("estimated_duration", 0) for s in segments)

        db.execute(
            "UPDATE podcasts SET script_json=?, total_words=?, updated_at=? WHERE id=? AND user_id=?",
            (json.dumps(script), script["total_words"], _now_iso(), podcast_id, user["id"]),
        )
        db.commit()
        return _get_podcast_or_404(db, podcast_id, user["id"])

    @router.delete("/podcasts/{podcast_id}/script/segments/{segment_id}")
    async def delete_segment(podcast_id: str, segment_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        podcast = _get_podcast_or_404(db, podcast_id, user["id"])
        script = podcast.get("script") or {}
        original = script.get("segments") or []
        segments = [s for s in original if s.get("id") != segment_id]
        if len(segments) == len(original):
            raise HTTPException(404, "Segment not found")
        for idx, seg in enumerate(segments):
            seg["position"] = idx
        script["segments"] = segments
        script["total_words"] = sum(s.get("word_count", 0) for s in segments)
        script["estimated_duration"] = sum(s.get("estimated_duration", 0) for s in segments)
        db.execute(
            "UPDATE podcasts SET script_json=?, total_words=?, updated_at=? WHERE id=? AND user_id=?",
            (json.dumps(script), script["total_words"], _now_iso(), podcast_id, user["id"]),
        )
        db.commit()
        return {"status": "deleted", "id": segment_id}

    # ---- Audio generation (SSE) -------------------------------------------

    @router.post("/podcasts/{podcast_id}/audio/generate")
    async def generate_audio(podcast_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        podcast = _get_podcast_or_404(db, podcast_id, user["id"])
        script = podcast.get("script")
        if not script or not script.get("segments"):
            raise HTTPException(
                400, "Podcast has no script. Generate a script first."
            )

        # RE-LOAD script fresh from DB in case it was just regenerated
        row = db.execute(
            "SELECT script_json FROM podcasts WHERE id=? AND user_id=?",
            (podcast_id, user["id"]),
        ).fetchone()
        if row and row[0]:
            fresh_script = json.loads(row[0])
            if fresh_script and fresh_script.get("segments"):
                script = fresh_script
                logger.info("Re-loaded fresh script for podcast %s (%d segments)", podcast_id, len(fresh_script["segments"]))

        force = False
        try:
            q = request.query_params
            force = str(q.get("force", "")).lower() in ("1", "true", "yes")
        except Exception:
            pass

        segments = [_dict_to_segment(s) for s in script["segments"]]
        audio_format: AudioFormat = "mp3" if (podcast.get("audio_format") or "mp3") == "mp3" else "wav"

        db.execute(
            "UPDATE podcasts SET status='generating_audio', error_message=NULL, updated_at=? "
            "WHERE id=? AND user_id=?",
            (_now_iso(), podcast_id, user["id"]),
        )
        db.commit()

        result_holder: dict = {"done": False, "error": None, "result": None}
        event_queue: asyncio.Queue[str | None] = asyncio.Queue()

        def make_progress_event(p: AssemblyProgress) -> str:
            return _sse_event("progress", {
                "stage": p.stage,
                "progress": p.progress,
                "message": p.message,
                "segment_id": p.segment_id,
                "segment_position": p.segment_position,
            })

        def make_error_event(msg: str) -> str:
            return _sse_event("error", {"error": msg})

        def make_complete_event(r) -> str:
            return _sse_event("complete", {
                "audio_path": str(r.file_path),
                "duration": r.duration,
                "file_size": r.file_size,
                "audio_sha256": getattr(r, "audio_sha256", ""),
                "audio_format": audio_format,
            })

        async def worker():
            """Background task that runs the synthesis and pushes events into the queue."""
            try:
                result = await assembler.assemble_from_segments(
                    segments=segments,
                    project_id=podcast_id,
                    options=AssemblyOptions(output_format=audio_format),
                    on_progress=lambda p: event_queue.put_nowait(make_progress_event(p)),
                    force=force,
                    user_id=user["id"],
                )
                audio_sha256 = await asyncio.to_thread(_sha256_file, result.file_path)
                result.audio_sha256 = audio_sha256
                db.execute(
                    "UPDATE podcasts SET status='ready', audio_path=?, audio_duration=?, "
                    "audio_format=?, audio_size=?, audio_sha256=?, "
                    "error_message=NULL, updated_at=? WHERE id=? AND user_id=?",
                    (
                        str(result.file_path),
                        result.duration,
                        audio_format,
                        result.file_size,
                        audio_sha256,
                        _now_iso(),
                        podcast_id,
                        user["id"],
                    ),
                )
                db.commit()
                await event_queue.put(make_complete_event(result))
            except asyncio.CancelledError:
                logger.info("Audio assembly cancelled (podcast=%s user=%s)", podcast_id, user["id"])
                db.execute(
                    "UPDATE podcasts SET status='cancelled', error_message=?, updated_at=? "
                    "WHERE id=? AND user_id=?",
                    ("cancelled", _now_iso(), podcast_id, user["id"]),
                )
                db.commit()
                await event_queue.put(make_error_event("cancelled"))
                raise
            except Exception as exc:
                logger.exception("Audio assembly failed")
                db.execute(
                    "UPDATE podcasts SET status='error', error_message=?, updated_at=? "
                    "WHERE id=? AND user_id=?",
                    (str(exc), _now_iso(), podcast_id, user["id"]),
                )
                db.commit()
                await event_queue.put(make_error_event(str(exc)))
            finally:
                await event_queue.put(None)

        async def event_stream():
            yield make_progress_event(AssemblyProgress(
                stage="queued", progress=0, message="Audio generation queued...",
            ))
            task = asyncio.create_task(worker())
            try:
                while True:
                    event = await event_queue.get()
                    if event is None:
                        break
                    yield event
                # Re-raise if background task failed
                if task.done() and not task.cancelled() and task.exception():
                    raise task.exception()
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # ---- Audio download + streaming ---------------------------------------

    def _locate_audio(podcast: dict) -> Path:
        audio_path = podcast.get("audio_path")
        if not audio_path:
            raise HTTPException(404, "Audio not generated for this podcast")
        path = _safe_audio_path(audio_path)
        if not path.exists():
            raise HTTPException(404, "Audio file missing on disk")
        expected_size = int(podcast.get("audio_size") or 0)
        actual_size = path.stat().st_size
        if expected_size and actual_size != expected_size:
            raise HTTPException(409, "Audio artifact integrity check failed")
        expected_sha = str(podcast.get("audio_sha256") or "")
        verify_hash = os.environ.get("VERIFY_ARTIFACT_HASH_ON_READ", "false").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if verify_hash and expected_sha:
            actual_sha = _sha256_file(path)
            if actual_sha != expected_sha:
                raise HTTPException(409, "Audio artifact integrity check failed")
        return path

    @router.get("/podcasts/{podcast_id}/audio/download")
    async def download_audio(podcast_id: str, request: Request):
        user = get_current_user(request)
        podcast = _get_podcast_or_404(db_getter(), podcast_id, user["id"])
        path = _locate_audio(podcast)
        fmt = podcast.get("audio_format") or "mp3"
        media_type = "audio/mpeg" if fmt == "mp3" else "audio/wav"
        safe_topic = re.sub(r"[^a-zA-Z0-9_-]+", "_", (podcast.get("topic") or "podcast"))[:60]
        filename = f"{safe_topic or 'podcast'}.{fmt}"
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            content_disposition_type="attachment",
        )

    @router.get("/podcasts/{podcast_id}/audio/stream")
    async def stream_audio(podcast_id: str, request: Request):
        user = get_current_user(request)
        podcast = _get_podcast_or_404(db_getter(), podcast_id, user["id"])
        path = _locate_audio(podcast)
        fmt = podcast.get("audio_format") or "mp3"
        media_type = "audio/mpeg" if fmt == "mp3" else "audio/wav"

        file_size = path.stat().st_size
        range_header = request.headers.get("range") or request.headers.get("Range")

        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not match:
                raise HTTPException(416, "Invalid Range header")
            start = int(match.group(1))
            end_raw = match.group(2)
            end = int(end_raw) if end_raw else file_size - 1
            if start >= file_size or end >= file_size or start > end:
                raise HTTPException(416, "Range not satisfiable")
            length = end - start + 1

            async def reader():
                loop = asyncio.get_event_loop()
                def _read():
                    with path.open("rb") as fh:
                        fh.seek(start)
                        return fh.read(length)
                data = await loop.run_in_executor(None, _read)
                yield data

            return StreamingResponse(
                reader(),
                status_code=206,
                media_type=media_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(length),
                },
            )

        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            },
        )

    return router, assembler
