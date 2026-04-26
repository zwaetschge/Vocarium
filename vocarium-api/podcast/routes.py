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
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import aiohttp
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from .audio_assembler import (
    AssemblyOptions,
    AssemblyProgress,
    AudioAssembler,
    AudioFormat,
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


# ---------------------------------------------------------------------------
# TTS bridge
# ---------------------------------------------------------------------------


@dataclass
class _VoiceRow:
    id: str
    name: str
    source: str


class VocariumTTSGenerator:
    """Concrete TTSGenerator that calls qwen3-tts through the GPU queue.

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
        # Primary TTS endpoint (GPU 0 via gpu_queue)
        self.tts_url = tts_url
        # Extra TTS endpoints (GPU 1 direct) – round-robin used for parallel workers
        self._extra_tts_urls = list(extra_tts_urls or [])
        self._db_getter = db_getter
        self._gpu_submit = gpu_submit
        self._max_parallel_workers = max_parallel_workers
        self._aiosession: aiohttp.ClientSession | None = None

    @property
    def _session(self) -> aiohttp.ClientSession:
        if self._aiosession is None or self._aiosession.closed:
            from aiohttp import TCPConnector
            self._aiosession = aiohttp.ClientSession(
                connector=TCPConnector(limit=20, ttl_dns_cache=300),
            )
        return self._aiosession

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

        urls = [self.tts_url, *self._extra_tts_urls]
        url_idx = 0
        last_err: Exception | None = None

        for attempt, url in enumerate(urls):
            try:
                logger.info(
                    "TTS request attempt %d/%d -> %s (speaker=%s, len=%d)",
                    attempt + 1, len(urls), url, preset["speaker"], len(text),
                )
                timeout = aiohttp.ClientTimeout(
                    total=3600, sock_connect=15, sock_read=3600
                )
                async with self._session.post(
                    f"{url}/v1/audio/speech/custom",
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    body = await resp.read()
                    if resp.status >= 400:
                        raise RuntimeError(
                            f"TTS failed ({resp.status}): "
                            f"{body.decode('utf-8', 'replace')[:300]}"
                        )
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    duration = float(headers.get("x-audio-duration", "0") or 0)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(body)
                    if duration <= 0:
                        duration = max(1.0, len(text.split()) / 2.3)
                    return duration
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
        "total_words": row[14] or 0,
        "created_at": row[15],
        "updated_at": row[16],
    }


_PODCAST_COLUMNS = (
    "id, topic, format, disfluency_level, duration, language, status, error_message, "
    "hosts_json, sources_json, script_json, audio_path, audio_duration, audio_format, "
    "total_words, created_at, updated_at"
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
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sse_event(kind: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {kind}\ndata: {payload}\n\n"


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
) -> tuple[APIRouter, AudioAssembler]:
    """Return (router, assembler). Caller wires the router into the app and
    uses the assembler for startup housekeeping."""

    router = APIRouter(prefix="/api", tags=["podcast"])
    tts_bridge = VocariumTTSGenerator(
        tts_url, db_getter, gpu_submit, extra_tts_urls=extra_tts_urls
    )
    assembler = AudioAssembler(
        tts=tts_bridge,
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
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        role = body.get("role") or "host"
        if role not in ("host", "expert"):
            raise HTTPException(400, "role must be 'host' or 'expert'")
        voice_id = body.get("voice_id") or None
        db = db_getter()
        # If voice_id given, verify it belongs to user and is a custom voice
        if voice_id:
            vrow = db.execute(
                "SELECT id, source FROM voices WHERE id=?", (voice_id,)
            ).fetchone()
            if not vrow:
                raise HTTPException(400, f"voice_id {voice_id} not found")
            if vrow[1] != "custom":
                raise HTTPException(
                    400, "Only custom voices can be assigned to podcast hosts"
                )
        host_id = generate_id("host")
        now = _now_iso()
        db.execute(
            "INSERT INTO hosts (id, user_id, name, personality, speaking_style, voice_id, role, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                host_id,
                user["id"],
                name,
                body.get("personality") or "",
                body.get("speaking_style") or "",
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
                if key == "role" and body[key] not in ("host", "expert"):
                    raise HTTPException(400, "role must be 'host' or 'expert'")
                if key == "voice_id" and body[key]:
                    vrow = db.execute(
                        "SELECT id, source FROM voices WHERE id=?", (body[key],)
                    ).fetchone()
                    if not vrow:
                        raise HTTPException(400, f"voice_id {body[key]} not found")
                    if vrow[1] != "custom":
                        raise HTTPException(
                            400, "Only custom voices can be assigned to hosts"
                        )
                fields.append(f"{key}=?")
                values.append(body[key])
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
        fmt = body.get("format") or "dialog"
        if fmt not in ("dialog", "monolog", "custom"):
            raise HTTPException(400, "format must be dialog | monolog | custom")
        duration = body.get("duration") or "medium"
        if duration not in ("short", "medium", "long"):
            raise HTTPException(400, "duration must be short | medium | long")
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
                row = db.execute(
                    "SELECT source FROM voices WHERE id=?", (h["voice_id"],)
                ).fetchone()
                if row and row[0] != "custom":
                    raise HTTPException(
                        400,
                        f"Host {h['name']} is assigned non-custom voice {h['voice_id']!r}. "
                        "Podcasts require custom voices only.",
                    )

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
                body.get("topic") or "",
                fmt,
                disfluency_level,
                duration,
                body.get("language") or "de",
                "draft",
                json.dumps(hosts),
                json.dumps([]),
                body.get("audio_format") or "mp3",
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
            hosts: list[dict] = []
            for hid in body["host_ids"] or []:
                hosts.append(_get_host_or_404(db, hid, user["id"]))
            for h in hosts:
                if h["voice_id"]:
                    row = db.execute(
                        "SELECT source FROM voices WHERE id=?", (h["voice_id"],)
                    ).fetchone()
                    if row and row[0] != "custom":
                        raise HTTPException(
                            400, "Podcasts require custom voices only"
                        )
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
                "DELETE FROM podcast_chunks WHERE source_id=?", (source_id,)
            )
            for pos, chunk in enumerate(chunks):
                chunk_id = generate_id("chunk")
                emb_json = json.dumps(embeddings[pos]) if embeddings[pos] else None
                db.execute(
                    "INSERT INTO podcast_chunks (id, source_id, podcast_id, content, embedding, position) "
                    "VALUES (?,?,?,?,?,?)",
                    (chunk_id, source_id, podcast_id, chunk, emb_json, pos),
                )
            db.execute(
                "UPDATE podcast_sources SET status='processed', chunk_count=?, processed_at=CURRENT_TIMESTAMP, error_message=NULL "
                "WHERE id=?",
                (len(chunks), source_id),
            )
            db.commit()
            logger.info("Source %s processed (chunks=%d)", source_id, len(chunks))
        except Exception as exc:
            logger.exception("Source processing failed: %s", exc)
            db.execute(
                "UPDATE podcast_sources SET status='failed', error_message=? WHERE id=?",
                (str(exc), source_id),
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

        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                400, f"File too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)"
            )

        source_id = generate_id("src")
        uploads_dir = Path(os.environ.get("PODCAST_UPLOAD_PATH", "/app/data/podcast_uploads"))
        uploads_dir.mkdir(parents=True, exist_ok=True)
        safe_suffix = suffix if suffix else ""
        disk_path = uploads_dir / f"{source_id}{safe_suffix}"
        disk_path.write_bytes(data)

        db.execute(
            "INSERT INTO podcast_sources (id, podcast_id, type, title, content, status) "
            "VALUES (?,?,?,?,?,?)",
            (source_id, podcast_id, "file", filename, str(disk_path), "pending"),
        )
        db.commit()

        asyncio.create_task(
            _process_source_async(source_id, podcast_id, "file", str(disk_path), None)
        )

        row = db.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM podcast_sources WHERE id=?",
            (source_id,),
        ).fetchone()
        return _source_row_to_dict(row)

    @router.post("/podcasts/{podcast_id}/sources/url")
    async def add_url_source(podcast_id: str, body: dict, request: Request):
        user = get_current_user(request)
        db = db_getter()
        _assert_podcast(db, podcast_id, user["id"])
        url = (body.get("url") or "").strip()
        if not url:
            raise HTTPException(400, "url is required")
        title = (body.get("title") or url).strip()
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
            f"SELECT {_SOURCE_COLUMNS} FROM podcast_sources WHERE id=?",
            (source_id,),
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
        if len(text) < MIN_TEXT_CHARS:
            raise HTTPException(400, f"content too short (min {MIN_TEXT_CHARS} chars)")
        if len(text) > MAX_TEXT_CHARS:
            raise HTTPException(400, f"content too long (max {MAX_TEXT_CHARS} chars)")
        title = (body.get("title") or "Text Input").strip()
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
            f"SELECT {_SOURCE_COLUMNS} FROM podcast_sources WHERE id=?",
            (source_id,),
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
            Path(row[0]).unlink(missing_ok=True)
        db.execute(
            "DELETE FROM podcast_sources WHERE id=? AND podcast_id=?",
            (source_id, podcast_id),
        )
        db.commit()
        return {"status": "deleted", "id": source_id}

    # ---- Script generation (SSE) ------------------------------------------

    def _load_podcast_chunks(db, podcast_id: str) -> list[SourceChunk]:
        rows = db.execute(
            "SELECT id, source_id, content, position, embedding FROM podcast_chunks "
            "WHERE podcast_id=? ORDER BY position",
            (podcast_id,),
        ).fetchall()
        chunks: list[SourceChunk] = []
        for r in rows:
            emb = None
            if r[4]:
                try:
                    emb = json.loads(r[4])
                except json.JSONDecodeError:
                    emb = None
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
                row = db.execute(
                    "SELECT source FROM voices WHERE id=?", (h.voice_id,)
                ).fetchone()
                if row and row[0] != "custom":
                    raise HTTPException(
                        400,
                        f"Host {h.name!r} has a non-custom voice. "
                        "Podcasts may only use custom voices.",
                    )

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
            "UPDATE podcasts SET status='generating_script', error_message=NULL, updated_at=? WHERE id=?",
            (_now_iso(), podcast_id),
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
            except Exception as exc:
                logger.exception("Script generation failed")
                db.execute(
                    "UPDATE podcasts SET status='error', error_message=?, updated_at=? WHERE id=?",
                    (str(exc), _now_iso(), podcast_id),
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
                "WHERE id=?",
                (json.dumps(script_payload), result.total_words, _now_iso(), podcast_id),
            )
            db.commit()
            push_complete(script_payload)
            await event_queue.put(None)

        async def event_stream():
            yield _sse_event("progress", {
                "stage": "queued", "progress": 0, "message": "Script generation queued...",
            })
            task = asyncio.create_task(worker())
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                yield event
            # Re-raise if background task failed
            if task.done() and task.exception():
                raise task.exception()

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
        for key in ("speaker", "text", "type", "voice", "notes"):
            if key in body:
                target[key] = body[key]
        if "text" in body:
            text = body["text"] or ""
            target["word_count"] = count_words(text)
            target["estimated_duration"] = estimate_speaking_duration(target["word_count"])
        target["updated_at"] = _now_iso()
        script["segments"] = segments
        script["total_words"] = sum(s.get("word_count", 0) for s in segments)
        script["estimated_duration"] = sum(s.get("estimated_duration", 0) for s in segments)

        db.execute(
            "UPDATE podcasts SET script_json=?, total_words=?, updated_at=? WHERE id=?",
            (json.dumps(script), script["total_words"], _now_iso(), podcast_id),
        )
        db.commit()
        return _get_podcast_or_404(db, podcast_id, user["id"])

    @router.delete("/podcasts/{podcast_id}/script/segments/{segment_id}")
    async def delete_segment(podcast_id: str, segment_id: str, request: Request):
        user = get_current_user(request)
        db = db_getter()
        podcast = _get_podcast_or_404(db, podcast_id, user["id"])
        script = podcast.get("script") or {}
        segments = [s for s in (script.get("segments") or []) if s.get("id") != segment_id]
        script["segments"] = segments
        script["total_words"] = sum(s.get("word_count", 0) for s in segments)
        script["estimated_duration"] = sum(s.get("estimated_duration", 0) for s in segments)
        db.execute(
            "UPDATE podcasts SET script_json=?, total_words=?, updated_at=? WHERE id=?",
            (json.dumps(script), script["total_words"], _now_iso(), podcast_id),
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
            "SELECT script_json FROM podcasts WHERE id=?", (podcast_id,)
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
            "UPDATE podcasts SET status='generating_audio', error_message=NULL, updated_at=? WHERE id=?",
            (_now_iso(), podcast_id),
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
                db.execute(
                    "UPDATE podcasts SET status='ready', audio_path=?, audio_duration=?, "
                    "audio_format=?, error_message=NULL, updated_at=? WHERE id=?",
                    (
                        str(result.file_path),
                        result.duration,
                        audio_format,
                        _now_iso(),
                        podcast_id,
                    ),
                )
                db.commit()
                await event_queue.put(make_complete_event(result))
            except Exception as exc:
                logger.exception("Audio assembly failed")
                db.execute(
                    "UPDATE podcasts SET status='error', error_message=?, updated_at=? WHERE id=?",
                    (str(exc), _now_iso(), podcast_id),
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
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                yield event
            # Re-raise if background task failed
            if task.done() and task.exception():
                raise task.exception()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # ---- Audio download + streaming ---------------------------------------

    def _locate_audio(podcast: dict) -> Path:
        audio_path = podcast.get("audio_path")
        if not audio_path:
            raise HTTPException(404, "Audio not generated for this podcast")
        path = Path(audio_path)
        if not path.exists():
            raise HTTPException(404, "Audio file missing on disk")
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
        return Response(
            content=path.read_bytes(),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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

        return Response(
            content=path.read_bytes(),
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            },
        )

    return router, assembler
