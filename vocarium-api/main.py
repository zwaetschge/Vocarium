"""Vocarium API Gateway.

Central voice management API. Stores voice metadata in SQLite,
proxies generation requests to Qwen3-TTS, and orchestrates
cloning / design / benchmark workflows.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Response, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from artifact_cleanup import cleanup_artifacts
from database import (
    gpu_queue_quota_decision,
    get_db,
    get_gpu_queue_job,
    get_or_create_user,
    init_db,
    is_gpu_queue_cancel_requested,
    list_gpu_queue_jobs,
    mark_interrupted_gpu_jobs,
    request_cancel_gpu_queue_job,
    upsert_gpu_queue_job,
    backfill_hosts_for_all_users,
)
from gpu_queue import (
    GpuResourceError,
    QueueQuotaError,
    get_resource_status,
    gpu_queue,
    register_cancel_checker,
    register_job_recorder,
    register_quota_checker,
    register_unloaders,
)
from metrics import inc, observe, render_prometheus
from podcast.routes import create_podcast_router
from request_context import request_id_var, user_id_var
from url_security import URLValidationError, normalize_http_base_url

logger = logging.getLogger(__name__)

API_INSTANCE_ID = os.environ.get("VOCARIUM_INSTANCE_ID") or socket.gethostname()
TTS_URL = os.environ.get("TTS_URL", "http://qwen3-tts:8880")
# Optional second TTS replica (set when running with COMPOSE_PROFILES=dual-gpu).
# Empty/unset means single-GPU mode — all TTS goes through TTS_URL.
TTS_URL_2 = os.environ.get("TTS_URL_2", "").strip()
EXTRA_TTS_URLS = [TTS_URL_2] if TTS_URL_2 else []
ASR_URL = os.environ.get("ASR_URL", "http://qwen3-asr:8000")
MUSIC_URL = os.environ.get("MUSIC_URL", "http://acestep:8003")
SFX_URL = os.environ.get("SFX_URL", "http://mmaudio:8004")
SFX_GENERATE_TIMEOUT_SECONDS = int(os.environ.get("SFX_GENERATE_TIMEOUT_SECONDS", "900"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
MAX_VOICE_UPLOAD_BYTES = int(os.environ.get("MAX_VOICE_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_TRANSCRIBE_UPLOAD_BYTES = int(
    os.environ.get("MAX_TRANSCRIBE_UPLOAD_BYTES", str(500 * 1024 * 1024))
)
MAX_TTS_TEXT_CHARS = int(os.environ.get("MAX_TTS_TEXT_CHARS", "20000"))
MAX_LLM_TEST_BODY_BYTES = int(os.environ.get("MAX_LLM_TEST_BODY_BYTES", str(32 * 1024)))
MAX_MUSIC_ENHANCE_BODY_BYTES = int(
    os.environ.get("MAX_MUSIC_ENHANCE_BODY_BYTES", str(64 * 1024))
)
TTS_RESPONSE_FORMATS = {"wav", "mp3", "flac", "opus", "aac", "pcm"}
MUSIC_RESPONSE_FORMATS = {"wav", "mp3", "flac"}
SUPPORTED_TTS_ENGINES = {"qwen"}
SUPPORTED_MUSIC_ENGINES = {"acestep"}
SUPPORTED_SFX_ENGINES = {"mmaudio"}
VOICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
TRACE_HEADER = "X-Request-ID"
OPENAI_TTS_DEFAULT_VOICE_PERSONA_ALIASES = {
    "michael scott",
    "michaelscott",
    "michael-scott",
    "michael_scott",
}

# `true` allows local single-user setups to fall back to a shared "api" user
# when no Remote-User header is present. Disable for multi-user deployments.
ALLOW_ANONYMOUS = os.environ.get("ALLOW_ANONYMOUS", "true").lower() in ("1", "true", "yes")

# Comma-separated list, or "*" for all (only safe in dev). Set per-deployment.
_cors = os.environ.get("CORS_ORIGINS", "*").strip()
CORS_ORIGINS = ["*"] if _cors == "*" else [o.strip() for o in _cors.split(",") if o.strip()]

DATA_DIR.mkdir(parents=True, exist_ok=True)
VOICES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Vocarium API")
_aiohttp_session: aiohttp.ClientSession | None = None


def _http_session() -> aiohttp.ClientSession:
    global _aiohttp_session
    if _aiohttp_session is None or _aiohttp_session.closed:
        _aiohttp_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
        )
    return _aiohttp_session


async def _close_http_session() -> None:
    global _aiohttp_session
    if _aiohttp_session is not None and not _aiohttp_session.closed:
        await _aiohttp_session.close()
    _aiohttp_session = None


@app.exception_handler(GpuResourceError)
async def gpu_resource_exception_handler(request: Request, exc: GpuResourceError):
    inc("vocarium_gpu_guard_blocks_total", labels={"service_type": exc.service_type})
    return JSONResponse(
        status_code=503,
        headers={TRACE_HEADER: getattr(request.state, "request_id", "")},
        content={
            "error": "gpu_resource_unavailable",
            "message": str(exc),
            "service_type": exc.service_type,
            "decision": exc.decision,
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(QueueQuotaError)
async def queue_quota_exception_handler(request: Request, exc: QueueQuotaError):
    inc("vocarium_gpu_queue_quota_blocks_total", labels={"service_type": exc.service_type})
    return JSONResponse(
        status_code=429,
        headers={TRACE_HEADER: getattr(request.state, "request_id", "")},
        content={
            "error": "gpu_queue_quota_exceeded",
            "message": str(exc),
            "service_type": exc.service_type,
            "decision": exc.decision,
            "request_id": getattr(request.state, "request_id", None),
        },
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Audio-Duration",
        "X-Generation-Time",
        "X-RTF",
        "X-Model",
        "X-Voice",
        "X-Chunks",
        TRACE_HEADER,
    ],
)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    request_id = _request_id_from_headers(request)
    request.state.request_id = request_id
    request_token = request_id_var.set(request_id)
    user_token = user_id_var.set(None)
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers[TRACE_HEADER] = request_id
        return response
    finally:
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        labels = {
            "method": request.method,
            "path": route_path,
            "status": status_code,
        }
        inc("vocarium_http_requests_total", labels=labels)
        observe("vocarium_http_request_seconds", time.perf_counter() - start, labels)
        request_id_var.reset(request_token)
        user_id_var.reset(user_token)


def _request_id_from_headers(request: Request) -> str:
    for header in (TRACE_HEADER, "X-Correlation-ID", "X-Request-Id"):
        raw = (request.headers.get(header) or "").strip()
        if raw and REQUEST_ID_RE.fullmatch(raw):
            return raw
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Auth: forward-auth via Remote-User / X-Forwarded-User header
# ---------------------------------------------------------------------------
def get_current_user(request: Request, allow_anonymous: bool = True) -> dict:
    """Extract authenticated user from the upstream identity proxy.

    Reads ``Remote-User`` (Authelia) or ``X-Forwarded-User`` (oauth2-proxy etc.).
    Auto-creates the user on first login. When ``ALLOW_ANONYMOUS`` is set,
    requests without a header are routed to a shared ``api`` user. This keeps
    local single-user UI/API access usable without an auth proxy. Disable in
    any multi-user deployment by setting ``ALLOW_ANONYMOUS=false``.
    """
    username = (
        request.headers.get("Remote-User")
        or request.headers.get("X-Forwarded-User")
        or ""
    ).strip()
    if not username and allow_anonymous and ALLOW_ANONYMOUS:
        user = get_or_create_user("api")
        user_id_var.set(user["id"])
        request.state.user = user
        return user
    if not username:
        raise HTTPException(401, "Not authenticated — Remote-User header missing")
    if len(username) > 255 or any(ord(ch) < 32 for ch in username):
        raise HTTPException(400, "Invalid Remote-User header")
    user = get_or_create_user(username)
    user_id_var.set(user["id"])
    request.state.user = user
    return user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mb(n_bytes: int) -> int:
    return max(1, n_bytes // (1024 * 1024))


def _size_label(n_bytes: int) -> str:
    if n_bytes >= 1024 * 1024:
        return f"{_mb(n_bytes)} MB"
    return f"{max(1, n_bytes // 1024)} KB"


def _voice_dir_for(voice_id: str) -> Path:
    cleaned = (voice_id or "").strip()
    if not VOICE_ID_RE.fullmatch(cleaned):
        raise HTTPException(400, "Invalid voice_id")
    root = VOICES_DIR.resolve()
    target = (root / cleaned).resolve()
    if target == root or root not in target.parents:
        raise HTTPException(400, "Invalid voice_id")
    return target


def _voice_has_audio(voice_id: str) -> bool:
    try:
        return (_voice_dir_for(voice_id) / "ref_audio.wav").exists()
    except HTTPException:
        return False


async def _read_upload_limited(
    upload: UploadFile,
    max_bytes: int,
    label: str,
) -> bytes:
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, f"{label} too large (max {_mb(max_bytes)} MB)")
    return data


def _require_text_limit(text: str, field: str = "text") -> None:
    if not (text or "").strip():
        raise HTTPException(400, f"{field} is required")
    if len(text) > MAX_TTS_TEXT_CHARS:
        raise HTTPException(
            413,
            f"{field} too long (max {MAX_TTS_TEXT_CHARS} characters)",
        )


def _validate_audio_format(
    response_format: str,
    *,
    allowed: set[str],
    field: str = "response_format",
) -> str:
    fmt = (response_format or "wav").strip().lower()
    if fmt not in allowed:
        raise HTTPException(
            400,
            f"{field} must be one of: {', '.join(sorted(allowed))}",
        )
    return fmt


def _validate_engine(engine: str, supported: set[str], field: str = "engine") -> str:
    value = (engine or "").strip().lower()
    if value not in supported:
        raise HTTPException(
            400,
            f"{field} must be one of: {', '.join(sorted(supported))}",
        )
    return value


def _select_tts_backend(req_model: str | None, req_engine: str | None) -> tuple[str, str]:
    engine = (req_engine or "").strip().lower()
    if engine:
        engine = _validate_engine(engine, SUPPORTED_TTS_ENGINES)
    else:
        engine = "qwen"

    return TTS_URL, "qwen"


def _validate_lufs(normalize_lufs: float | None) -> float | None:
    if normalize_lufs is None:
        return None
    if normalize_lufs < -40 or normalize_lufs > 0:
        raise HTTPException(400, "normalize_lufs must be between -40 and 0")
    return normalize_lufs


def _validate_fade_ms(fade_ms: int) -> int:
    if fade_ms < 0 or fade_ms > 10000:
        raise HTTPException(400, "fade_ms must be between 0 and 10000")
    return fade_ms


def _coerce_bool(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    raise HTTPException(400, f"{field} must be a boolean")


def _merge_negative_prompt(base: str, *parts: str) -> str:
    values = [base.strip()] if base and base.strip() else []
    values.extend(part for part in parts if part)
    return ", ".join(values)


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _normalize_http_base_url(raw_url: str, field: str = "base_url") -> str:
    try:
        return normalize_http_base_url(
            raw_url,
            field=field,
            allow_private_env="LLM_ALLOW_PRIVATE_BASE_URLS",
            allowed_hosts_env="LLM_ALLOWED_PRIVATE_HOSTS",
            validate_dns_env="LLM_VALIDATE_BASE_URL_DNS",
        )
    except URLValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


def _validate_llm_common(
    *,
    name: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    provider_type: str | None = None,
    require_name: bool = False,
    require_base_url: bool = False,
    require_model: bool = False,
) -> dict:
    updates: dict[str, str | float | int] = {}
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise HTTPException(400, "name is required")
        updates["name"] = cleaned
    elif require_name:
        raise HTTPException(400, "name is required")

    if base_url is not None:
        updates["base_url"] = _normalize_http_base_url(base_url)
    elif require_base_url:
        raise HTTPException(400, "base_url is required")

    if model is not None:
        cleaned = model.strip()
        if not cleaned:
            raise HTTPException(400, "model is required")
        updates["model"] = cleaned
    elif require_model:
        raise HTTPException(400, "model is required")

    if temperature is not None:
        if temperature < 0 or temperature > 2:
            raise HTTPException(400, "temperature must be between 0 and 2")
        updates["temperature"] = temperature

    if max_tokens is not None:
        if max_tokens < 1 or max_tokens > 262144:
            raise HTTPException(400, "max_tokens must be between 1 and 262144")
        updates["max_tokens"] = max_tokens

    if provider_type is not None:
        cleaned = provider_type.strip().lower()
        if cleaned not in ("openai", "openai-compatible"):
            raise HTTPException(400, "provider_type must be openai or openai-compatible")
        updates["provider_type"] = cleaned

    return updates


def _is_supported_media_url(raw_url: str) -> bool:
    parsed = urlparse(raw_url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "vimeo.com"
        or host.endswith(".vimeo.com")
    )


async def _post_unload(
    url: str,
    label: str,
    loaded_key: str,
    *,
    wait_if_busy: bool = False,
) -> None:
    attempts = 60 if wait_if_busy else 1
    for attempt in range(attempts):
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with _http_session().post(f"{url}/unload", timeout=timeout) as resp:
                if resp.status < 400:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    if data.get("status") == "busy":
                        if attempt + 1 < attempts:
                            await asyncio.sleep(2)
                            continue
                        logger.warning(
                            "%s stayed busy; continuing without unloading", label
                        )
                        return
                    if data.get(loaded_key):
                        logger.info("Unloaded %s to free shared GPU VRAM", label)
                    return
                body = await resp.text()
                logger.warning("%s unload failed (%s): %s", label, resp.status, body[:200])
                return
        except Exception as exc:
            logger.debug("%s unload skipped: %s", label, exc)
            return


async def _unload_tts():
    """Unload the primary TTS model if it is idle."""
    await _post_unload(TTS_URL, "Qwen3-TTS", "was_loaded", wait_if_busy=True)


async def _unload_extra_tts():
    """Unload the optional secondary TTS model if configured and idle."""
    if TTS_URL_2:
        await _post_unload(TTS_URL_2, "Qwen3-TTS-2", "was_loaded", wait_if_busy=True)


async def _unload_asr():
    """Tell the ASR proxy to unload its backend, freeing shared GPU VRAM."""
    await _post_unload(ASR_URL, "Qwen3-ASR", "was_running")


async def _unload_music():
    """Tell ACE-Step proxy to unload backend, freeing shared GPU VRAM."""
    await _post_unload(MUSIC_URL, "ACE-Step", "was_running")


async def _unload_sfx():
    """Tell MMAudio to unload model, freeing shared GPU VRAM."""
    await _post_unload(SFX_URL, "MMAudio", "was_loaded", wait_if_busy=True)


async def tts_request(method: str, path: str, *, url: str | None = None, **kwargs) -> tuple[int, dict, bytes]:
    target = url or TTS_URL
    timeout = aiohttp.ClientTimeout(total=600, sock_connect=30, sock_read=600)
    start = time.perf_counter()
    status = 0
    try:
        async with _http_session().request(
            method, f"{target}{path}", timeout=timeout, **kwargs
        ) as resp:
            status = resp.status
            body = await resp.read()
            # Normalize header keys to exact case for X- headers
            headers = {}
            for k, v in resp.headers.items():
                headers[k] = v
            return resp.status, headers, body
    finally:
        labels = {"path": path, "status": status or "error"}
        inc("vocarium_tts_requests_total", labels=labels)
        observe("vocarium_tts_inference_seconds", time.perf_counter() - start, labels)


async def tts_json(method: str, path: str, **kwargs) -> dict:
    status, headers, body = await tts_request(method, path, **kwargs)
    if status >= 400:
        detail = body.decode(errors="replace")
        raise HTTPException(status, detail=detail)
    return json.loads(body)


async def _run_tts_job(description: str, work_maker, *, tts_url: str = TTS_URL):
    """Run a TTS job on the GPU TTS service via the shared GPU queue.

    work_maker(tts_url) → coroutine that performs the actual TTS work.
    """
    async def work():
        return await work_maker(tts_url)

    _, future = await gpu_queue.submit("tts", description, work)
    return await future


# ---------------------------------------------------------------------------
# Podcast router — mounted here so it can capture get_current_user/gpu_queue
# ---------------------------------------------------------------------------
podcast_router, audio_assembler = create_podcast_router(
    get_current_user=get_current_user,
    tts_url=TTS_URL,
    extra_tts_urls=EXTRA_TTS_URLS,
    db_getter=get_db,
    gpu_submit=gpu_queue.submit,
    music_url=MUSIC_URL,
    sfx_url=SFX_URL,
)
app.include_router(podcast_router)


# ---------------------------------------------------------------------------
# Auth endpoint
# ---------------------------------------------------------------------------
@app.get("/api/auth/me")
async def auth_me(request: Request):
    """Return the currently authenticated user."""
    user = get_current_user(request)
    return {"user": user}


# ---------------------------------------------------------------------------
# Queue status
# ---------------------------------------------------------------------------
@app.get("/api/queue/status")
async def queue_status(request: Request):
    """Return current GPU queue state."""
    user = get_current_user(request)
    return gpu_queue.get_status(user_id=user["id"])


@app.get("/api/queue/jobs")
async def queue_jobs(request: Request, limit: int = 50):
    """Return recent persisted GPU queue jobs, including interrupted jobs."""
    user = get_current_user(request)
    return {"jobs": list_gpu_queue_jobs(limit, user_id=user["id"])}


@app.get("/api/queue/status/{job_id}")
async def queue_job_status(job_id: str, request: Request):
    """Return status of a specific queued job."""
    user = get_current_user(request)
    info = gpu_queue.get_job_status(job_id, user_id=user["id"])
    if not info:
        info = get_gpu_queue_job(job_id, user_id=user["id"])
    if not info:
        raise HTTPException(404, "Job not found")
    return info


@app.post("/api/queue/jobs/{job_id}/cancel")
async def cancel_queue_job(job_id: str, request: Request):
    """Request cancellation for one of the caller's queued/running GPU jobs."""
    user = get_current_user(request)
    info = gpu_queue.cancel_job(job_id, user_id=user["id"])
    if not info:
        info = request_cancel_gpu_queue_job(job_id, user_id=user["id"])
    if not info:
        raise HTTPException(404, "Job not found")
    return info


@app.get("/api/resources/status")
async def resources_status(request: Request):
    """Return gputasks-backed GPU guard decisions for each service."""
    get_current_user(request, allow_anonymous=True)
    return await get_resource_status()


@app.get("/api/metrics")
async def metrics(request: Request):
    """Return process metrics in Prometheus text format."""
    get_current_user(request, allow_anonymous=True)
    return Response(content=render_prometheus(), media_type="text/plain; version=0.0.4")


@app.post("/api/admin/artifacts/cleanup")
async def admin_artifact_cleanup(
    request: Request,
    dry_run: bool = Query(True),
    max_age_hours: float = Query(24.0, ge=0.0, le=24 * 365),
):
    """Sweep orphaned local generated artifacts.

    ``dry_run=true`` reports candidates without deleting them.
    """
    get_current_user(request)
    return await asyncio.to_thread(
        cleanup_artifacts,
        get_db(),
        DATA_DIR,
        VOICES_DIR,
        max_age_hours=max_age_hours,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    init_db(DATA_DIR / "vocarium.db")
    interrupted = mark_interrupted_gpu_jobs(worker_id=API_INSTANCE_ID)
    if interrupted:
        logger.warning("Marked %d persisted GPU queue job(s) as interrupted", interrupted)
    def record_gpu_job(job: dict) -> None:
        job["worker_id"] = API_INSTANCE_ID
        upsert_gpu_queue_job(job)

    register_job_recorder(record_gpu_job)
    register_quota_checker(gpu_queue_quota_decision)
    register_cancel_checker(is_gpu_queue_cancel_requested)
    # Backfill preset hosts for existing users (idempotent)
    backfill = backfill_hosts_for_all_users()
    if backfill:
        logger.info("Backfilled %d preset host(s)", backfill)
    # Sync voices from TTS filesystem into SQLite if needed
    await _sync_voices_from_tts()
    # Start GPU queue with unload callbacks. The queue decides conflicts from
    # GPU_TTS_*/GPU_ASR/GPU_MUSIC/GPU_SFX, so single-GPU and dual-GPU layouts
    # both unload only what can actually collide.
    unloaders = {
        "tts": _unload_tts,
        "asr": _unload_asr,
        "music": _unload_music,
        "sfx": _unload_sfx,
    }
    if TTS_URL_2:
        unloaders["tts_extra"] = _unload_extra_tts
    register_unloaders(unloaders)
    gpu_queue.start()
    try:
        cleaned = await audio_assembler.clean_stale_jobs()
        if cleaned:
            logger.info("Cleaned %d stale podcast assembly job(s)", cleaned)
    except Exception as exc:
        logger.warning("Podcast cleanup skipped: %s", exc)
    logger.info("Vocarium API ready (GPU queue active)")


@app.on_event("shutdown")
async def shutdown():
    await _close_http_session()
    try:
        await audio_assembler.aclose()
    except Exception as exc:
        logger.debug("Podcast client shutdown skipped: %s", exc)
    try:
        from podcast.docling_client import close_docling_client
        from podcast.embedding_client import close_embedding_client
        from podcast.llm_client import close_llm_clients

        await close_docling_client()
        await close_embedding_client()
        await close_llm_clients()
    except Exception as exc:
        logger.debug("HTTPX client shutdown skipped: %s", exc)


async def _sync_voices_from_tts():
    """Ensure all voices in the shared volume are tracked in SQLite."""
    db = get_db()
    existing_ids = {row[0] for row in db.execute("SELECT id FROM voices").fetchall()}
    for voice_dir in sorted(VOICES_DIR.iterdir()):
        if not voice_dir.is_dir():
            continue
        vid = voice_dir.name
        if not VOICE_ID_RE.fullmatch(vid):
            logger.warning("Skipping unsafe voice directory during sync: %s", vid)
            continue
        if vid in existing_ids:
            continue
        meta_file = voice_dir / "metadata.json"
        if not meta_file.exists():
            continue
        meta = json.loads(meta_file.read_text())
        # Legacy voices (pre-multi-user) get user_id=NULL
        db.execute(
            "INSERT OR IGNORE INTO voices (id, user_id, name, language, source, design_prompt, ref_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, meta.get("user_id"), meta.get("name", vid), meta.get("language", "English"),
             meta.get("source", "clone"), meta.get("design_prompt", ""),
             meta.get("ref_text", ""), meta.get("created_at", "")),
        )
    db.commit()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@app.get("/api/models")
async def list_models(request: Request):
    get_current_user(request)
    return await tts_json("GET", "/v1/models")


@app.get("/api/models/current")
async def current_model(request: Request):
    get_current_user(request)
    return await tts_json("GET", "/v1/models/current")


class SwitchModelRequest(BaseModel):
    model_id: str


@app.post("/api/models/switch")
async def switch_model(req: SwitchModelRequest, request: Request):
    get_current_user(request)

    async def work():
        return await tts_json("POST", "/v1/models/load", json={"model_id": req.model_id})

    _, future = await gpu_queue.submit("tts", f"Switch TTS model to {req.model_id}", work)
    return await future


# ---------------------------------------------------------------------------
# Voices CRUD
# ---------------------------------------------------------------------------
@app.get("/api/voices")
async def list_voices(request: Request):
    user = get_current_user(request)
    db = get_db()
    rows = db.execute(
        "SELECT id, name, language, source, design_prompt, ref_text, speaker, instruct, created_at "
        "FROM voices WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    voices = []
    for r in rows:
        voices.append({
            "id": r[0], "name": r[1], "language": r[2], "source": r[3],
            "design_prompt": r[4], "ref_text": r[5],
            "speaker": r[6], "instruct": r[7], "created_at": r[8],
            "has_audio": _voice_has_audio(r[0]),
        })
    return {"voices": voices}


@app.get("/api/voices/{voice_id}")
async def get_voice(voice_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    r = db.execute(
        "SELECT id, name, language, source, design_prompt, ref_text, speaker, instruct, created_at "
        "FROM voices WHERE id=? AND user_id=?",
        (voice_id, user["id"]),
    ).fetchone()
    if not r:
        raise HTTPException(404, "Voice not found")
    return {
        "id": r[0], "name": r[1], "language": r[2], "source": r[3],
        "design_prompt": r[4], "ref_text": r[5],
        "speaker": r[6], "instruct": r[7], "created_at": r[8],
        "has_audio": _voice_has_audio(r[0]),
    }


@app.delete("/api/voices/{voice_id}")
async def delete_voice(voice_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    r = db.execute("SELECT id FROM voices WHERE id=? AND user_id=?", (voice_id, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Voice not found")
    db.execute(
        "UPDATE hosts SET voice_id=NULL, updated_at=? WHERE user_id=? AND voice_id=?",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ"), user["id"], voice_id),
    )
    db.execute("DELETE FROM voices WHERE id=? AND user_id=?", (voice_id, user["id"]))
    db.commit()
    # Tell TTS to remove cached prompt
    try:
        await tts_json("DELETE", f"/v1/voices/{voice_id}")
    except Exception:
        pass
    return {"status": "deleted", "voice_id": voice_id}


@app.get("/api/voices/{voice_id}/audio")
async def get_voice_audio(voice_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    r = db.execute("SELECT id FROM voices WHERE id=? AND user_id=?", (voice_id, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Voice not found")
    audio_path = _voice_dir_for(voice_id) / "ref_audio.wav"
    if not audio_path.exists():
        raise HTTPException(404, "Audio not found")
    return FileResponse(audio_path, media_type="audio/wav")


# ---------------------------------------------------------------------------
# Voice Registration helper
# ---------------------------------------------------------------------------
async def _register_voice_on_tts(
    voice_id: str, name: str, language: str, ref_text: str,
    audio_bytes: bytes, *, endpoint: str = "/v1/voices/register",
    design_prompt: str | None = None,
):
    """Register a voice on every configured TTS service."""
    urls = _tts_registration_urls()

    for url in urls:
        form = aiohttp.FormData()
        form.add_field("voice_id", voice_id)
        form.add_field("ref_text", ref_text)
        form.add_field("language", language)
        form.add_field("name", name)
        if design_prompt:
            form.add_field("design_prompt", design_prompt)
        form.add_field("ref_audio", audio_bytes,
                       filename="ref_audio.wav", content_type="audio/wav")
        try:
            status, _, body = await tts_request("POST", endpoint, url=url, data=form)
            if status >= 400:
                logger.warning(
                    "TTS registration warning on %s: %s",
                    url,
                    body.decode(errors="replace"),
                )
        except Exception as e:
            logger.warning("TTS registration failed on %s: %s", url, e)


def _tts_registration_urls() -> list[str]:
    return [TTS_URL, *EXTRA_TTS_URLS]


def _convert_reference_audio_to_wav(audio_bytes: bytes, audio_path: Path) -> None:
    import subprocess

    result = subprocess.run(
        ["ffmpeg", "-y", "-i", "pipe:0", "-ar", "24000", "-ac", "1", str(audio_path)],
        input=audio_bytes,
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace")[:500]
        raise RuntimeError(detail or "ffmpeg conversion failed")


# ---------------------------------------------------------------------------
# Voice Cloning
# ---------------------------------------------------------------------------
@app.post("/api/voices/clone")
async def clone_voice(
    request: Request,
    name: str = Form(...),
    language: str = Form("German"),
    ref_text: str = Form(""),
    auto_transcribe: bool = Form(True),
    ref_audio: UploadFile = File(...),
):
    """Clone a voice from reference audio."""
    user = get_current_user(request)
    name = name.strip()
    language = language.strip() or "German"
    if not name:
        raise HTTPException(400, "name is required")
    voice_id = str(uuid.uuid4())[:8]
    voice_dir = _voice_dir_for(voice_id)
    voice_dir.mkdir(parents=True, exist_ok=True)
    try:
        audio_bytes = await _read_upload_limited(
            ref_audio, MAX_VOICE_UPLOAD_BYTES, "Reference audio"
        )
        audio_path = voice_dir / "ref_audio.wav"

        # Convert to WAV without blocking the FastAPI event loop.
        if ref_audio.filename and not ref_audio.filename.lower().endswith(".wav"):
            try:
                await asyncio.to_thread(
                    _convert_reference_audio_to_wav, audio_bytes, audio_path
                )
            except Exception as exc:
                raise HTTPException(400, f"Audio conversion failed: {exc}")
        else:
            await asyncio.to_thread(audio_path.write_bytes, audio_bytes)

        # Auto-transcribe if no ref_text provided (always attempt as fallback)
        if not ref_text.strip():
            try:
                ref_text = await _transcribe_audio(audio_path)
            except Exception as e:
                logger.warning("Auto-transcription failed: %s", e)
                ref_text = ""

        ref_text = ref_text.strip()
        if not ref_text:
            raise HTTPException(400, "Reference text is required and auto-transcription failed")

        # Save metadata
        meta = {
            "name": name,
            "ref_text": ref_text,
            "language": language,
            "source": "clone",
            "user_id": user["id"],
            "username": user["username"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        await asyncio.to_thread(
            (voice_dir / "metadata.json").write_text,
            json.dumps(meta, indent=2),
        )

        # Register on all TTS instances (GPU 0 + GPU 1 fallback)
        registered_audio = await asyncio.to_thread(audio_path.read_bytes)
        await _register_voice_on_tts(
            voice_id, name, language, ref_text, registered_audio,
        )

        # Save to DB
        db = get_db()
        db.execute(
            "INSERT INTO voices (id, user_id, name, language, source, ref_text, created_at) VALUES (?,?,?,?,?,?,?)",
            (voice_id, user["id"], name, language, "clone", ref_text, meta["created_at"]),
        )
        db.commit()

        return {"status": "created", "voice_id": voice_id, "name": name}
    except HTTPException:
        shutil.rmtree(voice_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(voice_dir, ignore_errors=True)
        raise


async def _transcribe_audio(audio_path: Path) -> str:
    """Send audio to ASR for transcription via GPU queue."""
    audio_bytes = await asyncio.to_thread(audio_path.read_bytes)

    async def work():
        form = aiohttp.FormData()
        form.add_field("file", audio_bytes, filename="audio.wav", content_type="audio/wav")
        form.add_field("model", "Qwen/Qwen3-ASR-0.6B")
        async with _http_session().post(f"{ASR_URL}/v1/audio/transcriptions", data=form) as resp:
            if resp.status >= 400:
                raise Exception(f"ASR error {resp.status}")
            data = await resp.json()
            return data.get("text", "").strip()

    _, future = await gpu_queue.submit("asr", "Transcribe (clone)", work)
    return await future


# ---------------------------------------------------------------------------
# Voice Design
# ---------------------------------------------------------------------------
class DesignPreviewRequest(BaseModel):
    text: str
    description: str
    language: str = "English"


@app.post("/api/voices/design/preview")
async def design_preview(req: DesignPreviewRequest, request: Request):
    """Preview a designed voice (does not save it)."""
    get_current_user(request)  # auth check
    _require_text_limit(req.text)
    if not req.description.strip():
        raise HTTPException(400, "description is required")

    async def work_maker(tts_url):
        status, headers, body = await tts_request(
            "POST", "/v1/audio/design", url=tts_url,
            json={"text": req.text, "description": req.description,
                  "language": req.language, "response_format": "wav"},
        )
        if status >= 400:
            raise HTTPException(status, body.decode(errors="replace"))
        return {"body": body, "headers": {k: v for k, v in headers.items() if k.startswith("X-")}}

    result = await _run_tts_job("Voice Design Preview", work_maker)
    return Response(content=result["body"], media_type="audio/wav", headers=result["headers"])


class DesignSaveRequest(BaseModel):
    name: str
    description: str
    text: str
    language: str = "English"


@app.post("/api/voices/design/save")
async def design_and_save(req: DesignSaveRequest, request: Request):
    """Design a voice and save it for future use."""
    user = get_current_user(request)
    _require_text_limit(req.text)
    name = req.name.strip()
    description = req.description.strip()
    if not name:
        raise HTTPException(400, "name is required")
    if not description:
        raise HTTPException(400, "description is required")

    async def work_maker(tts_url):
        status, headers, audio_bytes = await tts_request(
            "POST", "/v1/audio/design", url=tts_url,
            json={"text": req.text, "description": req.description,
                  "language": req.language, "response_format": "wav"},
        )
        if status >= 400:
            raise HTTPException(status, audio_bytes.decode(errors="replace"))

        voice_id = str(uuid.uuid4())[:8]
        voice_dir = _voice_dir_for(voice_id)
        voice_dir.mkdir(parents=True, exist_ok=True)

        audio_path = voice_dir / "ref_audio.wav"
        await asyncio.to_thread(audio_path.write_bytes, audio_bytes)

        meta = {
            "name": name,
            "ref_text": req.text,
            "language": req.language,
            "source": "design",
            "design_prompt": description,
            "user_id": user["id"],
            "username": user["username"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        await asyncio.to_thread(
            (voice_dir / "metadata.json").write_text,
            json.dumps(meta, indent=2),
        )

        # Register on both TTS instances so either GPU can use this voice
        await _register_voice_on_tts(
            voice_id, name, req.language, req.text, audio_bytes,
            endpoint="/v1/voices/register-designed", design_prompt=description,
        )

        db = get_db()
        db.execute(
            "INSERT INTO voices (id, user_id, name, language, source, design_prompt, ref_text, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (voice_id, user["id"], name, req.language, "design", description, req.text, meta["created_at"]),
        )
        db.commit()
        return {"status": "created", "voice_id": voice_id, "name": name}

    result = await _run_tts_job("Voice Design Save", work_maker)
    return result


# ---------------------------------------------------------------------------
# Custom Voice (prebuilt speakers + optional steering)
# ---------------------------------------------------------------------------


@app.get("/api/speakers")
async def list_speakers(request: Request):
    """Return prebuilt speakers exposed by the CustomVoice model."""
    get_current_user(request)  # auth check
    return await tts_json("GET", "/v1/speakers")


class CustomVoicePreviewRequest(BaseModel):
    text: str
    speaker: str
    language: str = "English"
    instruct: str | None = None


@app.post("/api/voices/custom/preview")
async def custom_voice_preview(req: CustomVoicePreviewRequest, request: Request):
    """Preview a custom-voice generation (does not save)."""
    get_current_user(request)  # auth check
    _require_text_limit(req.text)

    async def work_maker(tts_url):
        status, headers, body = await tts_request(
            "POST", "/v1/audio/speech/custom", url=tts_url,
            json={
                "text": req.text, "speaker": req.speaker,
                "language": req.language,
                "instruct": req.instruct or None,
                "response_format": "wav",
            },
        )
        if status >= 400:
            raise HTTPException(status, body.decode(errors="replace"))
        return {"body": body, "headers": {k: v for k, v in headers.items() if k.startswith("X-")}}

    result = await _run_tts_job("Custom Voice Preview", work_maker)
    return Response(content=result["body"], media_type="audio/wav", headers=result["headers"])


class CustomVoiceSaveRequest(BaseModel):
    name: str
    speaker: str
    instruct: str | None = None
    language: str = "English"


@app.post("/api/voices/custom/save")
async def custom_voice_save(req: CustomVoiceSaveRequest, request: Request):
    """Persist a (speaker, instruct) preset as a custom voice.

    Unlike clone/design voices, custom voices have no reference audio — the
    model generates directly from speaker + (optional) instruct at call time.
    """
    user = get_current_user(request)
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "name is required")

    # Validate speaker against upstream list
    speakers_resp = await tts_json("GET", "/v1/speakers")
    valid_ids = {s["id"] for s in speakers_resp.get("speakers", [])}
    if req.speaker not in valid_ids:
        raise HTTPException(400, f"Unknown speaker {req.speaker!r}")

    voice_id = str(uuid.uuid4())[:8]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    instruct = (req.instruct or "").strip()

    db = get_db()
    db.execute(
        "INSERT INTO voices (id, user_id, name, language, source, speaker, instruct, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (voice_id, user["id"], name, req.language, "custom",
         req.speaker, instruct, created_at),
    )
    db.commit()

    return {
        "status": "created", "voice_id": voice_id, "name": name,
        "speaker": req.speaker, "instruct": instruct, "language": req.language,
    }


def _resolve_custom_voice(voice_id: str, user_id: int | None = None) -> dict:
    """Look up a voice row and return {speaker, instruct, language} — raises if
    the voice is missing or not source='custom'. If ``user_id`` is given, scopes
    the lookup to voices owned by that user."""
    db = get_db()
    if user_id is not None:
        r = db.execute(
            "SELECT source, speaker, instruct, language FROM voices WHERE id=? AND user_id=?",
            (voice_id, user_id),
        ).fetchone()
    else:
        r = db.execute(
            "SELECT source, speaker, instruct, language FROM voices WHERE id=?",
            (voice_id,),
        ).fetchone()
    if not r:
        raise HTTPException(404, "Voice not found")
    source = (r[0] or "").lower()
    if source != "custom":
        raise HTTPException(
            403,
            f"Voice {voice_id!r} has source='{source}', not a custom-voice preset",
        )
    return {"speaker": r[1] or "", "instruct": r[2] or "", "language": r[3] or "English"}


class CustomSpeechRequest(BaseModel):
    """OpenAI-shaped TTS request targeting saved custom-voice presets."""
    model: str = "qwen3-tts-custom"
    input: str
    voice: str
    response_format: str = "wav"


@app.post("/v1/audio/speech/custom")
async def openai_tts_custom(req: CustomSpeechRequest, request: Request):
    """OpenAI-shaped TTS endpoint — custom voices only.

    The ``voice`` field must be a voice_id of a saved custom-voice preset.
    Language is inherited from the saved preset; speaker + instruct are
    applied server-side.
    """
    user = get_current_user(request, allow_anonymous=True)
    _require_text_limit(req.input, "input")
    response_format = _validate_audio_format(
        req.response_format, allowed=TTS_RESPONSE_FORMATS
    )
    preset = _resolve_custom_voice(req.voice, user_id=user["id"])

    async def work_maker(tts_url):
        status, headers, body = await tts_request(
            "POST", "/v1/audio/speech/custom", url=tts_url,
            json={
                "text": req.input,
                "speaker": preset["speaker"],
                "language": preset["language"],
                "instruct": preset["instruct"] or None,
                "response_format": response_format,
            },
        )
        if status >= 400:
            raise HTTPException(status, body.decode(errors="replace"))
        content_type = headers.get("Content-Type", headers.get("content-type", "audio/wav"))
        return {"body": body, "media_type": content_type}

    result = await _run_tts_job("OpenAI TTS (custom)", work_maker)
    return Response(content=result["body"], media_type=result["media_type"])


# ---------------------------------------------------------------------------
# Speech Generation (unified)
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    text: str
    voice_id: str = "default"
    model_id: str | None = None
    engine: str | None = None
    language: str | None = None
    response_format: str = "wav"


def _verify_voice_exists(voice_id: str, user_id: int | None = None):
    """Ensure voice exists (or is a built-in like 'default'). If ``user_id``
    is provided, scopes the lookup to that user's voices."""
    if voice_id in ("default", ""):
        return
    db = get_db()
    if user_id is not None:
        r = db.execute(
            "SELECT id FROM voices WHERE id=? AND (user_id=? OR user_id IS NULL)",
            (voice_id, user_id),
        ).fetchone()
    else:
        r = db.execute("SELECT id FROM voices WHERE id=?", (voice_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Voice not found")


def _verify_voice_source(voice_id: str, allowed_sources: tuple[str, ...], user_id: int | None = None) -> None:
    """Ensure voice exists AND its source is in ``allowed_sources``.

    ``default`` (built-in base voice) is always accepted for ``clone`` scope.
    If ``user_id`` is provided, scopes the lookup to that user's voices.
    Raises 404 if missing, 403 if the source is not permitted here.
    """
    if voice_id in ("default", ""):
        if "clone" in allowed_sources:
            return
        raise HTTPException(403, "Default voice not allowed on this endpoint")
    db = get_db()
    if user_id is not None:
        r = db.execute(
            "SELECT source FROM voices WHERE id=? AND (user_id=? OR user_id IS NULL)",
            (voice_id, user_id),
        ).fetchone()
    else:
        r = db.execute("SELECT source FROM voices WHERE id=?", (voice_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Voice not found")
    source = (r[0] or "clone").lower()
    if source not in allowed_sources:
        raise HTTPException(
            403,
            f"Voice '{voice_id}' has source='{source}', "
            f"but this endpoint only accepts: {', '.join(allowed_sources)}",
        )


def _voice_source(voice_id: str, user_id: int | None = None) -> str:
    """Return the stored voice source for routing generation endpoints."""
    if voice_id in ("default", ""):
        return "clone"
    db = get_db()
    if user_id is not None:
        row = db.execute(
            "SELECT source FROM voices WHERE id=? AND (user_id=? OR user_id IS NULL)",
            (voice_id, user_id),
        ).fetchone()
    else:
        row = db.execute("SELECT source FROM voices WHERE id=?", (voice_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Voice not found")
    return (row[0] or "clone").lower()


def _custom_tts_payload(
    *,
    text: str,
    voice_id: str,
    response_format: str,
    user_id: int,
    language_override: str | None = None,
) -> dict:
    preset = _resolve_custom_voice(voice_id, user_id=user_id)
    return {
        "text": text,
        "speaker": preset["speaker"],
        "language": language_override or preset["language"],
        "instruct": preset["instruct"] or None,
        "response_format": response_format,
    }


async def _tts_generate_for_voice(
    *,
    tts_url: str,
    text: str,
    voice_id: str,
    source: str,
    response_format: str,
    user_id: int,
    model_id: str | None = None,
    language: str | None = None,
) -> tuple[int, dict, bytes]:
    response_format = _validate_audio_format(
        response_format, allowed=TTS_RESPONSE_FORMATS
    )
    if source == "custom":
        return await tts_request(
            "POST",
            "/v1/audio/speech/custom",
            url=tts_url,
            json=_custom_tts_payload(
                text=text,
                voice_id=voice_id,
                response_format=response_format,
                user_id=user_id,
                language_override=language,
            ),
        )

    payload = {
        "input": text,
        "voice": voice_id,
        "response_format": response_format,
    }
    if model_id:
        payload["model_id"] = model_id
    if language:
        payload["language"] = language
    status, headers, body = await tts_request(
        "POST", "/v1/audio/speech", url=tts_url, json=payload
    )
    if status < 400 and voice_id not in ("default", ""):
        h_lower = {k.lower(): v for k, v in headers.items()}
        actual_voice = h_lower.get("x-voice")
        if actual_voice and actual_voice != voice_id:
            raise HTTPException(
                502,
                f"TTS returned voice {actual_voice!r} for requested voice {voice_id!r}",
            )
    return status, headers, body


@app.post("/api/generate/stream")
async def generate_speech_stream(req: GenerateRequest, request: Request):
    """Stream speech generation via SSE while the GPU queue job is running."""
    user = get_current_user(request)
    _require_text_limit(req.text)
    _verify_voice_exists(req.voice_id, user_id=user["id"])
    source = _voice_source(req.voice_id, user_id=user["id"])
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def work_maker(tts_url):
        if source == "custom":
            status, headers, body = await _tts_generate_for_voice(
                tts_url=tts_url,
                text=req.text,
                voice_id=req.voice_id,
                source=source,
                response_format="wav",
                user_id=user["id"],
                language=req.language,
            )
            if status >= 400:
                await event_queue.put(
                    _sse_event("error", {"error": body.decode(errors="replace")})
                )
                return
            h_lower = {k.lower(): v for k, v in headers.items()}
            duration = float(h_lower.get("x-audio-duration", "0") or 0)
            chunk = {
                "index": 0,
                "total": 1,
                "audio": base64.b64encode(body).decode("ascii"),
                "duration": round(duration, 2),
                "text": req.text[:80],
            }
            done = {
                "total_duration": round(duration, 2),
                "generation_time": float(h_lower.get("x-generation-time", "0") or 0),
                "rtf": float(h_lower.get("x-rtf", "0") or 0),
                "model": h_lower.get("x-model", "1.7b-custom"),
                "voice": req.voice_id,
                "chunks": 1,
            }
            await event_queue.put(_sse_event("chunk", chunk))
            await event_queue.put(_sse_event("done", done))
            return

        payload = {
            "input": req.text,
            "voice": req.voice_id,
            "response_format": "wav",
        }
        if req.model_id:
            payload["model_id"] = req.model_id
        if req.language:
            payload["language"] = req.language

        timeout = aiohttp.ClientTimeout(total=600, sock_connect=30, sock_read=600)
        async with _http_session().post(
            f"{tts_url}/v1/audio/speech/stream", json=payload, timeout=timeout
        ) as resp:
            if resp.status >= 400:
                body = await resp.read()
                await event_queue.put(
                    _sse_event("error", {"error": body.decode(errors="replace")})
                )
                return
            buf = b""
            async for chunk in resp.content.iter_any():
                buf += chunk
                while b"\n\n" in buf:
                    event, buf = buf.split(b"\n\n", 1)
                    await event_queue.put(event.decode(errors="replace") + "\n\n")
            if buf.strip():
                await event_queue.put(buf.decode(errors="replace") + "\n\n")

    async def worker():
        try:
            await _run_tts_job("TTS Stream", work_maker)
        except Exception as exc:
            logger.exception("TTS stream failed")
            await event_queue.put(_sse_event("error", {"error": str(exc)}))
        finally:
            await event_queue.put(None)

    async def event_stream():
        task = asyncio.create_task(worker())
        try:
            yield _sse_event(
                "progress",
                {"stage": "queued", "progress": 0, "message": "TTS stream queued"},
            )
            while True:
                ev = await event_queue.get()
                if ev is None:
                    break
                yield ev
            await task
        except asyncio.CancelledError:
            task.cancel()
            raise

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/generate")
async def generate_speech(req: GenerateRequest, request: Request):
    """Generate speech using a stored voice."""
    user = get_current_user(request)
    _require_text_limit(req.text)
    _verify_voice_exists(req.voice_id, user_id=user["id"])
    source = _voice_source(req.voice_id, user_id=user["id"])
    selected_url, selected_engine = _select_tts_backend(req.model_id, req.engine)

    async def work_maker(tts_url):
        status, headers, body = await _tts_generate_for_voice(
            tts_url=tts_url,
            text=req.text,
            voice_id=req.voice_id,
            source=source,
            response_format=req.response_format,
            user_id=user["id"],
            model_id=req.model_id,
            language=req.language,
        )
        if status >= 400:
            raise HTTPException(status, body.decode(errors="replace"))
        resp_headers = {k: v for k, v in headers.items() if k.lower().startswith("x-")}
        content_type = headers.get("Content-Type", headers.get("content-type", "audio/wav"))
        return {"body": body, "media_type": content_type, "headers": resp_headers}

    result = await _run_tts_job("TTS Generate", work_maker, tts_url=selected_url)
    return Response(content=result["body"], media_type=result["media_type"], headers=result["headers"])


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------
class BenchmarkRequest(BaseModel):
    text: str
    voice_ids: list[str] = ["default"]
    model_ids: list[str] = ["1.7b-base"]
    runs_per_combo: int = 1


@app.post("/api/benchmark/run")
async def run_benchmark(req: BenchmarkRequest, request: Request):
    """Run a benchmark across model × voice combinations."""
    user = get_current_user(request)
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text is required")
    _require_text_limit(text)
    voice_ids = list(dict.fromkeys(v.strip() for v in req.voice_ids if v.strip()))
    model_ids = list(dict.fromkeys(m.strip() for m in req.model_ids if m.strip()))
    if not voice_ids:
        raise HTTPException(400, "at least one voice_id is required")
    if not model_ids:
        raise HTTPException(400, "at least one model_id is required")
    if len(voice_ids) > 20:
        raise HTTPException(400, "at most 20 voices can be benchmarked at once")
    if len(model_ids) > 8:
        raise HTTPException(400, "at most 8 models can be benchmarked at once")
    if req.runs_per_combo < 1 or req.runs_per_combo > 20:
        raise HTTPException(400, "runs_per_combo must be between 1 and 20")
    for voice_id in voice_ids:
        _verify_voice_source(voice_id, allowed_sources=("clone", "design"), user_id=user["id"])

    async def work():
        results = []
        db = get_db()
        for model_id in model_ids:
            try:
                switch_result = await tts_json("POST", "/v1/models/load", json={"model_id": model_id})
                load_time = switch_result.get("load_time_s", 0)
            except Exception as e:
                results.append({"model_id": model_id, "error": str(e)})
                continue

            for voice_id in voice_ids:
                for run_idx in range(req.runs_per_combo):
                    try:
                        status, headers, body = await tts_request(
                            "POST", "/v1/audio/speech",
                            json={"input": text, "voice": voice_id,
                                  "model_id": model_id, "response_format": "wav"},
                        )
                        if status >= 400:
                            results.append({
                                "model_id": model_id, "voice_id": voice_id,
                                "run": run_idx + 1, "error": body.decode(errors="replace"),
                            })
                            continue

                        h_lower = {k.lower(): v for k, v in headers.items()}
                        audio_dur = float(h_lower.get("x-audio-duration", 0))
                        gen_time = float(h_lower.get("x-generation-time", 0))
                        rtf = float(h_lower.get("x-rtf", 0))

                        result = {
                            "model_id": model_id,
                            "voice_id": voice_id,
                            "run": run_idx + 1,
                            "audio_duration": audio_dur,
                            "generation_time": gen_time,
                            "rtf": rtf,
                            "load_time": load_time if run_idx == 0 else 0,
                        }
                        results.append(result)

                        db.execute(
                            "INSERT INTO benchmarks "
                            "(user_id, voice_id, model_id, text, audio_duration, generation_time, rtf) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (user["id"], voice_id, model_id, text, audio_dur, gen_time, rtf),
                        )
                    except Exception as e:
                        results.append({
                            "model_id": model_id, "voice_id": voice_id,
                            "run": run_idx + 1, "error": str(e),
                        })
        db.commit()
        return {"results": results}

    _, future = await gpu_queue.submit("tts", "Benchmark", work)
    return await future


@app.get("/api/benchmark/results")
async def get_benchmark_results(request: Request, limit: int = 50):
    user = get_current_user(request)
    limit = max(1, min(int(limit), 200))
    db = get_db()
    rows = db.execute(
        "SELECT id, voice_id, model_id, text, audio_duration, generation_time, rtf, created_at "
        "FROM benchmarks WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user["id"], limit),
    ).fetchall()
    return {
        "results": [
            {"id": r[0], "voice_id": r[1], "model_id": r[2], "text": r[3],
             "audio_duration": r[4], "generation_time": r[5], "rtf": r[6], "created_at": r[7]}
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------
@app.get("/api/languages")
async def list_languages():
    return {"languages": [
        "Chinese", "English", "Japanese", "Korean", "German",
        "French", "Russian", "Portuguese", "Spanish", "Italian",
    ]}


# ---------------------------------------------------------------------------
# Transcription (STT)
# ---------------------------------------------------------------------------
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts", ".m4v"}


def _convert_to_wav(data: bytes, suffix: str) -> bytes:
    """Convert audio/video to 16kHz mono WAV using ffmpeg (sync, for run_in_executor)."""
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as inf:
        inf.write(data)
        in_path = inf.name
    out_path = in_path + ".wav"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-ar", "16000", "-ac", "1", "-t", "1800", out_path],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg: {result.stderr.decode(errors='replace')[:500]}")
        return Path(out_path).read_bytes()
    finally:
        Path(in_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


def _download_media_url(url: str) -> bytes:
    """Download supported media URLs as WAV (sync, for run_in_executor)."""
    import subprocess
    import shutil
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    out_template = os.path.join(tmp_dir, "audio.%(ext)s")
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-playlist", "-x", "--audio-format", "wav",
             "--audio-quality", "0", "--max-filesize", f"{_mb(MAX_TRANSCRIBE_UPLOAD_BYTES)}M",
             "--postprocessor-args",
             "ffmpeg:-ar 16000 -ac 1", "-o", out_template, url],
            capture_output=True, timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp: {result.stderr.decode(errors='replace')[:500]}")
        for f in Path(tmp_dir).iterdir():
            if f.suffix == ".wav":
                return f.read_bytes()
        raise RuntimeError("yt-dlp produced no WAV output")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _transcribe_wav_bytes(wav_bytes: bytes) -> dict:
    """Send WAV bytes to ASR via the native /v1/audio/transcriptions endpoint."""
    timeout = aiohttp.ClientTimeout(total=600, sock_connect=60, sock_read=600)
    form = aiohttp.FormData()
    form.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
    form.add_field("model", "Qwen/Qwen3-ASR-0.6B")
    async with _http_session().post(
        f"{ASR_URL}/v1/audio/transcriptions", data=form, timeout=timeout
    ) as resp:
        if resp.status >= 400:
            body = await resp.read()
            raise RuntimeError(f"ASR error {resp.status}: {body.decode(errors='replace')[:500]}")
        data = await resp.json()
        return {"text": _clean_asr_text(data.get("text", ""))}


def _clean_asr_text(raw: str) -> str:
    text = (raw or "").strip()
    if "<asr_text>" in text:
        text = text.split("<asr_text>", 1)[1]
    for marker in ("</asr_text>", "<|endoftext|>"):
        text = text.replace(marker, "")
    return text.strip()


@app.post("/api/transcribe")
async def transcribe(
    request: Request,
    file: UploadFile | None = File(None),
    url: str = Form(""),
):
    """Transcribe audio/video files or supported media URLs."""
    get_current_user(request)  # auth check
    loop = asyncio.get_event_loop()

    if url.strip():
        if not _is_supported_media_url(url.strip()):
            raise HTTPException(400, "Only YouTube and Vimeo URLs are supported")
        try:
            wav_bytes = await loop.run_in_executor(None, _download_media_url, url.strip())
        except Exception as e:
            raise HTTPException(400, f"Download failed: {e}")

    elif file:
        raw = await _read_upload_limited(
            file, MAX_TRANSCRIBE_UPLOAD_BYTES, "Transcription upload"
        )
        filename = file.filename or "upload.wav"
        suffix = Path(filename).suffix.lower()

        if suffix == ".wav":
            wav_bytes = raw
        else:
            try:
                wav_bytes = await loop.run_in_executor(None, _convert_to_wav, raw, suffix)
            except Exception as e:
                raise HTTPException(500, f"Conversion failed: {e}")
    else:
        raise HTTPException(400, "Provide either a file or a URL")

    async def work():
        return await _transcribe_wav_bytes(wav_bytes)

    _, future = await gpu_queue.submit("asr", "Transcribe", work)
    return await future


# ---------------------------------------------------------------------------
# TTS Health proxy
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    try:
        tts_health = await tts_json("GET", "/health")
    except Exception:
        tts_health = {"status": "unreachable"}
    gpu_resources = await get_resource_status()
    return {"api": "ok", "tts": tts_health, "gpu_resources": gpu_resources}


# ---------------------------------------------------------------------------
# Music Generation (ACE-Step)
# ---------------------------------------------------------------------------
class MusicGenerateRequest(BaseModel):
    prompt: str
    engine: str = "acestep"
    lyrics: str = ""
    negative_prompt: str = ""
    audio_duration: int = 60
    bpm: int | None = None
    key_scale: str | None = None
    time_signature: str | None = None
    thinking: bool = True
    audio_format: str = "wav"
    batch_size: int = 1
    seed: int | None = None
    instrumental: bool = False
    loopable: bool = False
    normalize_lufs: float | None = None
    fade_ms: int = 0


async def _music_request(method: str, path: str, **kwargs) -> tuple[int, bytes]:
    timeout = aiohttp.ClientTimeout(total=120, sock_connect=30)
    start = time.perf_counter()
    status = 0
    try:
        async with _http_session().request(
            method, f"{MUSIC_URL}{path}", timeout=timeout, **kwargs
        ) as resp:
            status = resp.status
            body = await resp.read()
            return resp.status, body
    finally:
        labels = {"path": path, "status": status or "error"}
        inc("vocarium_music_requests_total", labels=labels)
        observe("vocarium_music_seconds", time.perf_counter() - start, labels)


def _normalize_music_audio_path(raw_path: str, *, strict: bool = True) -> str | None:
    value = unquote((raw_path or "").strip())
    if not value:
        if strict:
            raise HTTPException(400, "path is required")
        return None

    parsed = urlparse(value)
    if parsed.query and parsed.path.endswith("/v1/audio"):
        query_path = (parse_qs(parsed.query).get("path") or [""])[0]
        value = unquote(query_path.strip())
    elif value.startswith("/v1/audio?"):
        query_path = (parse_qs(value.split("?", 1)[1]).get("path") or [""])[0]
        value = unquote(query_path.strip())

    if not value or "\x00" in value or len(value) > 2048:
        if strict:
            raise HTTPException(400, "invalid audio path")
        return None
    return value


def _extract_music_audio_paths(payload: object) -> list[str]:
    paths: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            file_ref = value.get("file")
            if isinstance(file_ref, str):
                path = _normalize_music_audio_path(file_ref, strict=False)
                if path:
                    paths.append(path)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    visit(json.loads(stripped))
                except json.JSONDecodeError:
                    return

    visit(payload)
    return sorted(set(paths))


def _record_music_task(
    user_id: int,
    task_id: str,
    status: str,
    file_paths: list[str] | None = None,
) -> None:
    db = get_db()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    if file_paths is None:
        db.execute(
            "INSERT INTO music_tasks (task_id, user_id, status, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at "
            "WHERE music_tasks.user_id=excluded.user_id",
            (task_id, user_id, status, now),
        )
    else:
        db.execute(
            "INSERT INTO music_tasks (task_id, user_id, status, file_paths, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "status=excluded.status, file_paths=excluded.file_paths, updated_at=excluded.updated_at "
            "WHERE music_tasks.user_id=excluded.user_id",
            (task_id, user_id, status, json.dumps(file_paths), now),
        )
    owner = db.execute(
        "SELECT user_id FROM music_tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if not owner or owner[0] != user_id:
        db.rollback()
        raise RuntimeError("music task ownership conflict")
    if file_paths is not None:
        db.execute(
            "DELETE FROM music_task_files WHERE task_id=? AND user_id=?",
            (task_id, user_id),
        )
        db.executemany(
            "INSERT OR IGNORE INTO music_task_files (task_id, user_id, path, created_at) "
            "VALUES (?, ?, ?, ?)",
            [(task_id, user_id, path, now) for path in sorted(set(file_paths))],
        )
    db.commit()


def _require_owned_music_tasks(user_id: int, task_ids: list[str]) -> None:
    placeholders = ",".join("?" for _ in task_ids)
    rows = get_db().execute(
        f"SELECT task_id FROM music_tasks WHERE user_id=? AND task_id IN ({placeholders})",
        (user_id, *task_ids),
    ).fetchall()
    owned = {row[0] for row in rows}
    if owned != set(task_ids):
        raise HTTPException(404, "Music task not found")


def _sync_music_tasks_from_poll(user_id: int, poll: dict) -> None:
    for task in poll.get("data") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            continue
        status_code = task.get("status")
        status = "completed" if status_code == 1 else "failed" if status_code == 2 else "running"
        paths = _extract_music_audio_paths(task)
        _record_music_task(user_id, task_id, status, paths if paths else None)


def _require_owned_music_audio_path(user_id: int, path: str) -> None:
    row = get_db().execute(
        "SELECT 1 FROM music_task_files WHERE user_id=? AND path=? LIMIT 1",
        (user_id, path),
    ).fetchone()
    if row:
        return
    raise HTTPException(404, "Music audio not found")


@app.post("/api/music/generate")
async def music_generate(req: MusicGenerateRequest, request: Request):
    """Submit music generation, hold GPU lock until complete, return result."""
    user = get_current_user(request)
    engine = _validate_engine(req.engine, SUPPORTED_MUSIC_ENGINES)
    if not req.prompt.strip():
        raise HTTPException(400, "prompt is required")
    if len(req.prompt) > 2000:
        raise HTTPException(413, "prompt too long (max 2000 characters)")
    if len(req.lyrics) > 10000:
        raise HTTPException(413, "lyrics too long (max 10000 characters)")
    if len(req.negative_prompt) > 2000:
        raise HTTPException(413, "negative_prompt too long (max 2000 characters)")
    if req.audio_duration < 10 or req.audio_duration > 300:
        raise HTTPException(400, "audio_duration must be between 10 and 300 seconds")
    if req.batch_size < 1 or req.batch_size > 4:
        raise HTTPException(400, "batch_size must be between 1 and 4")
    audio_format = _validate_audio_format(
        req.audio_format, allowed=MUSIC_RESPONSE_FORMATS, field="audio_format"
    )
    normalize_lufs = _validate_lufs(req.normalize_lufs)
    fade_ms = _validate_fade_ms(req.fade_ms)

    prompt = req.prompt
    if req.instrumental and "instrumental" not in prompt.casefold():
        prompt = f"{prompt}, instrumental"
    if req.loopable and "loop" not in prompt.casefold():
        prompt = f"{prompt}, seamless loop"
    negative_prompt = _merge_negative_prompt(
        req.negative_prompt,
        "vocals, singing, speech" if req.instrumental else "",
    )
    vocarium_options = {
        "engine": engine,
        "negative_prompt": req.negative_prompt,
        "instrumental": req.instrumental,
        "loopable": req.loopable,
        "normalize_lufs": normalize_lufs,
        "fade_ms": fade_ms,
    }

    payload = {
        "prompt": prompt,
        "lyrics": req.lyrics,
        "audio_duration": req.audio_duration,
        "thinking": req.thinking,
        "model": "acestep-v15-turbo",
        "inference_steps": 8,
        "batch_size": req.batch_size,
        "audio_format": audio_format,
    }
    if req.bpm is not None:
        payload["bpm"] = req.bpm
    if req.key_scale is not None:
        payload["key_scale"] = req.key_scale
    if req.time_signature is not None:
        payload["time_signature"] = req.time_signature
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if req.seed is not None:
        payload["seed"] = req.seed
        payload["use_random_seed"] = False
    else:
        payload["use_random_seed"] = True

    async def work():
        # Submit task
        status, body = await _music_request("POST", "/release_task", json=payload)
        if status >= 400:
            raise HTTPException(status, body.decode(errors="replace"))
        submit_result = json.loads(body)
        task_id = submit_result.get("data", {}).get("task_id")
        if not task_id:
            return {
                "submit": submit_result,
                "vocarium_options": vocarium_options,
            }  # no task_id means immediate result or error
        _record_music_task(user["id"], task_id, "submitted")

        # Poll until complete (holds GPU lock)
        for _ in range(300):  # max ~10 min (2s * 300)
            await asyncio.sleep(2)
            s, b = await _music_request("POST", "/query_result", json={"task_id_list": [task_id]})
            if s >= 400:
                continue
            poll = json.loads(b)
            tasks = poll.get("data", [])
            if not tasks:
                continue
            task = tasks[0]
            if task.get("status") == 1:  # success
                _sync_music_tasks_from_poll(user["id"], poll)
                return {
                    "submit": submit_result,
                    "result": poll,
                    "vocarium_options": vocarium_options,
                }
            if task.get("status") == 2:  # failed
                _record_music_task(user["id"], task_id, "failed")
                raise HTTPException(500, "Music generation failed")
        raise HTTPException(504, "Music generation timed out")

    _, future = await gpu_queue.submit("music", "Music Generate", work)
    return await future


class MusicStatusRequest(BaseModel):
    task_ids: list[str]


@app.post("/api/music/status")
async def music_status(req: MusicStatusRequest, request: Request):
    """Poll for music generation task status."""
    user = get_current_user(request)
    task_ids = [tid.strip() for tid in req.task_ids if tid and tid.strip()]
    if not task_ids:
        raise HTTPException(400, "at least one task_id is required")
    if len(task_ids) > 20:
        raise HTTPException(400, "at most 20 task_ids can be queried at once")
    if any(len(tid) > 128 or "\x00" in tid for tid in task_ids):
        raise HTTPException(400, "invalid task_id")
    _require_owned_music_tasks(user["id"], task_ids)
    status, body = await _music_request(
        "POST", "/query_result",
        json={"task_id_list": task_ids},
    )
    if status >= 400:
        raise HTTPException(status, body.decode(errors="replace"))
    poll = json.loads(body)
    _sync_music_tasks_from_poll(user["id"], poll)
    return poll


@app.get("/api/music/audio")
async def music_audio(path: str, request: Request):
    """Download generated music audio file."""
    user = get_current_user(request)
    path = _normalize_music_audio_path(path)
    assert path is not None
    _require_owned_music_audio_path(user["id"], path)
    status, body = await _music_request("GET", "/v1/audio", params={"path": path})
    if status >= 400:
        raise HTTPException(status, body.decode(errors="replace"))
    # Guess content type from path
    if path.endswith(".mp3"):
        ct = "audio/mpeg"
    elif path.endswith(".wav"):
        ct = "audio/wav"
    elif path.endswith(".flac"):
        ct = "audio/flac"
    else:
        ct = "audio/mpeg"
    return Response(content=body, media_type=ct)


@app.post("/api/music/enhance")
async def music_enhance(request: Request):
    """Enhance prompt/lyrics using ACE-Step's LM."""
    get_current_user(request)  # auth check
    body = await request.body()
    if len(body) > MAX_MUSIC_ENHANCE_BODY_BYTES:
        raise HTTPException(
            413,
            f"request body too large (max {_size_label(MAX_MUSIC_ENHANCE_BODY_BYTES)})",
        )
    status, resp_body = await _music_request(
        "POST", "/format_input",
        data=body, headers={"Content-Type": "application/json"},
    )
    if status >= 400:
        raise HTTPException(status, resp_body.decode(errors="replace"))
    return json.loads(resp_body)


@app.get("/api/music/health")
async def music_health():
    """Check ACE-Step backend health."""
    try:
        status, body = await _music_request("GET", "/health")
        return json.loads(body)
    except Exception:
        return {"status": "unreachable", "backend_running": False}


# ---------------------------------------------------------------------------
# Sound Effects (MMAudio)
# ---------------------------------------------------------------------------
async def _sfx_request(method: str, path: str, **kwargs) -> tuple[int, bytes]:
    timeout_seconds = SFX_GENERATE_TIMEOUT_SECONDS if path == "/generate" else 30
    timeout = aiohttp.ClientTimeout(
        total=timeout_seconds,
        sock_connect=30,
        sock_read=timeout_seconds,
    )
    start = time.perf_counter()
    status = 0
    try:
        async with _http_session().request(
            method, f"{SFX_URL}{path}", timeout=timeout, **kwargs
        ) as resp:
            status = resp.status
            body = await resp.read()
            return resp.status, body
    finally:
        labels = {"path": path, "status": status or "error"}
        inc("vocarium_sfx_requests_total", labels=labels)
        observe("vocarium_sfx_seconds", time.perf_counter() - start, labels)


@app.post("/api/sfx/generate")
async def sfx_generate(request: Request):
    """Generate a sound effect from a text prompt. Returns WAV audio."""
    get_current_user(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body")
    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    if len(prompt) > 1000:
        raise HTTPException(413, "prompt too long (max 1000 characters)")
    engine = (body.get("engine") or "mmaudio").strip().lower()
    engine = _validate_engine(engine, SUPPORTED_SFX_ENGINES)
    negative_prompt = (body.get("negative_prompt") or "").strip()
    if len(negative_prompt) > 1000:
        raise HTTPException(413, "negative_prompt too long (max 1000 characters)")
    no_speech = _coerce_bool(body.get("no_speech", False), "no_speech")
    no_music = _coerce_bool(body.get("no_music", False), "no_music")

    try:
        duration = float(body.get("duration", 8.0))
        cfg_strength = float(body.get("cfg_strength", 4.5))
        num_steps = int(body.get("num_steps", 25))
        normalize_lufs = (
            None
            if body.get("normalize_lufs") is None
            else float(body.get("normalize_lufs"))
        )
        fade_ms = int(body.get("fade_ms", 0))
    except (TypeError, ValueError):
        raise HTTPException(
            400,
            "duration, cfg_strength, num_steps, normalize_lufs, and fade_ms must be numeric",
        )
    if duration < 1 or duration > 30:
        raise HTTPException(400, "duration must be between 1 and 30 seconds")
    if cfg_strength < 1 or cfg_strength > 10:
        raise HTTPException(400, "cfg_strength must be between 1 and 10")
    if num_steps < 1 or num_steps > 100:
        raise HTTPException(400, "num_steps must be between 1 and 100")
    normalize_lufs = _validate_lufs(normalize_lufs)
    fade_ms = _validate_fade_ms(fade_ms)
    negative_prompt = _merge_negative_prompt(
        negative_prompt,
        "speech, voice, vocals, talking" if no_speech else "",
        "music, melody, song, vocals" if no_music else "",
    )
    vocarium_options = {
        "engine": engine,
        "no_speech": no_speech,
        "no_music": no_music,
        "normalize_lufs": normalize_lufs,
        "fade_ms": fade_ms,
    }

    sfx_params = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "duration": duration,
        "cfg_strength": cfg_strength,
        "num_steps": num_steps,
        "seed": body.get("seed"),
    }

    async def work():
        try:
            status, resp_body = await _sfx_request("POST", "/generate", json=sfx_params)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                504,
                f"SFX backend timed out after {SFX_GENERATE_TIMEOUT_SECONDS}s while loading or generating audio",
            ) from exc
        except aiohttp.ClientError as exc:
            raise HTTPException(
                502,
                f"SFX backend connection failed: {exc}",
            ) from exc
        if status >= 400:
            raise HTTPException(status, detail=resp_body.decode(errors="replace"))
        return resp_body

    _, future = await gpu_queue.submit("sfx", "SFX Generate", work)
    wav_bytes = await future
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            "Content-Disposition": "attachment; filename=sfx.wav",
            "X-Vocarium-Options": json.dumps(vocarium_options),
        },
    )


@app.get("/api/sfx/health")
async def sfx_health():
    """Check MMAudio backend health."""
    try:
        status, body = await _sfx_request("GET", "/health")
        return json.loads(body)
    except Exception:
        return {"status": "unreachable", "model_loaded": False}


# ---------------------------------------------------------------------------
# OpenAI-compatible endpoints (for OpenWebUI, etc.)
# ---------------------------------------------------------------------------

class OpenAISpeechRequest(BaseModel):
    """OpenAI-compatible TTS request format."""
    model: str = "qwen3-tts"
    input: str
    voice: str = "default"
    response_format: str = "wav"
    engine: str | None = None
    speed: float = 1.0  # ignored, kept for compat


def _openai_voice_lookup_key(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", (value or "").strip()).casefold()


def _resolve_openai_tts_voice(voice: str, user_id: int | None = None) -> str:
    """Resolve OpenAI-compatible voice input to a concrete Vocarium voice id.

    Persona names belong in the caller's script/persona fields. The
    OpenAI-compatible voice field should be a concrete Vocarium voice id, but
    some clients send the displayed voice name back instead of the listed id.
    Stored user voices win over persona aliases.
    """
    cleaned = (voice or "").strip()
    if not cleaned:
        return "default"

    lookup_key = _openai_voice_lookup_key(cleaned)
    db = get_db()
    if user_id is not None:
        rows = db.execute("SELECT id, name FROM voices WHERE user_id=?", (user_id,)).fetchall()
        rows += db.execute("SELECT id, name FROM voices WHERE user_id IS NULL").fetchall()
    else:
        rows = db.execute("SELECT id, name FROM voices").fetchall()

    for voice_id, name in rows:
        if cleaned == (voice_id or ""):
            return voice_id
    for voice_id, name in rows:
        if lookup_key == _openai_voice_lookup_key(name or ""):
            return voice_id

    normalized = re.sub(r"[\s_-]+", " ", cleaned).casefold()
    compact = re.sub(r"[\s_-]+", "", cleaned).casefold()
    if (
        normalized in OPENAI_TTS_DEFAULT_VOICE_PERSONA_ALIASES
        or compact in OPENAI_TTS_DEFAULT_VOICE_PERSONA_ALIASES
    ):
        return "default"
    return cleaned


async def _openai_speech_proxy(
    req: OpenAISpeechRequest,
    description: str,
    *,
    voice_override: str | None = None,
) -> Response:
    """Shared TTS proxy used by both /v1/audio/speech endpoints."""
    _require_text_limit(req.input, "input")
    response_format = _validate_audio_format(
        req.response_format, allowed=TTS_RESPONSE_FORMATS
    )
    requested_voice = (req.voice or "").strip()
    voice = voice_override or _normalize_openai_tts_voice(req.voice)
    selected_url, selected_engine = _select_tts_backend(req.model, req.engine)
    payload = {
        "input": req.input,
        "voice": voice,
        "response_format": response_format,
    }
    # Map OpenAI model names to internal model_id
    model_map = {"tts-1": "1.7b-base", "tts-1-hd": "1.7b-base"}
    if req.model in model_map:
        payload["model_id"] = model_map[req.model]
    elif req.model not in ("qwen3-tts", ""):
        payload["model_id"] = req.model

    async def work_maker(tts_url):
        status, headers, body = await tts_request(
            "POST", "/v1/audio/speech", url=selected_url, json=payload
        )
        if status >= 400:
            raise HTTPException(status, body.decode(errors="replace"))
        if voice not in ("default", ""):
            h_lower = {k.lower(): v for k, v in headers.items()}
            actual_voice = h_lower.get("x-voice")
            if actual_voice and actual_voice != voice:
                raise HTTPException(
                    502,
                    f"TTS returned voice {actual_voice!r} for requested voice {voice!r}",
                )
        content_type = headers.get("Content-Type", headers.get("content-type", "audio/wav"))
        return {"body": body, "media_type": content_type, "voice": voice}

    result = await _run_tts_job(description, work_maker)
    headers = {}
    headers["X-TTS-Engine"] = selected_engine
    if result.get("voice") and result["voice"] != requested_voice:
        headers["X-Voice"] = result["voice"]
        headers["X-Requested-Voice"] = requested_voice
    return Response(content=result["body"], media_type=result["media_type"], headers=headers)


@app.post("/v1/audio/speech")
async def openai_tts(req: OpenAISpeechRequest, request: Request):
    """OpenAI-compatible TTS endpoint — base model + cloned voices only.

    This is the endpoint Canto (and other OpenAI-compatible clients) consume.
    Designed voices live behind ``/v1/audio/speech/designed``.
    """
    user = get_current_user(request, allow_anonymous=True)
    voice = _resolve_openai_tts_voice(req.voice, user_id=user["id"])
    _verify_voice_source(voice, allowed_sources=("clone",), user_id=user["id"])
    return await _openai_speech_proxy(
        req,
        "OpenAI TTS (base/clone)",
        voice_override=voice,
    )


@app.post("/v1/audio/speech/designed")
async def openai_tts_designed(req: OpenAISpeechRequest, request: Request):
    """OpenAI-compatible TTS endpoint — designed voices only.

    Exposes the design-model voices (used by the podcast generator) as an
    OpenAI-compatible endpoint. Cloned and default base voices are rejected.
    """
    user = get_current_user(request, allow_anonymous=True)
    voice = _resolve_openai_tts_voice(req.voice, user_id=user["id"])
    _verify_voice_source(voice, allowed_sources=("design",), user_id=user["id"])
    return await _openai_speech_proxy(
        req,
        "OpenAI TTS (designed)",
        voice_override=voice,
    )


@app.post("/v1/audio/transcriptions")
async def openai_stt(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    language: str = Form(""),
    response_format: str = Form("json"),
):
    """Whisper-compatible STT endpoint — works with OpenWebUI."""
    get_current_user(request, allow_anonymous=True)
    loop = asyncio.get_event_loop()

    raw = await _read_upload_limited(
        file, MAX_TRANSCRIBE_UPLOAD_BYTES, "Transcription upload"
    )
    filename = file.filename or "upload.wav"
    suffix = Path(filename).suffix.lower()

    if suffix == ".wav":
        wav_bytes = raw
    else:
        try:
            wav_bytes = await loop.run_in_executor(None, _convert_to_wav, raw, suffix)
        except Exception as e:
            raise HTTPException(500, f"Audio conversion failed: {e}")

    async def work():
        return await _transcribe_wav_bytes(wav_bytes)

    _, future = await gpu_queue.submit("asr", "OpenAI STT", work)
    result = await future

    if response_format == "text":
        return Response(content=result["text"], media_type="text/plain")
    # Default: JSON (OpenAI format)
    return result


@app.get("/v1/models")
async def openai_models():
    """OpenAI-compatible model listing for TTS/STT."""
    models = [
        {"id": "tts-1", "object": "model", "owned_by": "vocarium",
         "description": "Qwen3-TTS 1.7B (GPU)"},
        {"id": "tts-1-hd", "object": "model", "owned_by": "vocarium",
         "description": "Qwen3-TTS 1.7B (GPU)"},
        {"id": "whisper-1", "object": "model", "owned_by": "vocarium",
         "description": "Qwen3-ASR 0.6B"},
    ]
    return {"object": "list", "data": models}


@app.get("/v1/audio/models")
async def openai_audio_models():
    """Open WebUI-compatible TTS model listing."""
    data = await openai_models()
    return {"models": data["data"]}


@app.get("/v1/voices")
async def openai_voices(request: Request, source: str | None = Query(default=None)):
    """List available TTS voices. Optional source filter: clone, design, or custom."""
    user = get_current_user(request, allow_anonymous=True)
    if source and source not in ("clone", "design", "custom"):
        raise HTTPException(400, "source must be clone, design, or custom")
    db = get_db()
    if source:
        rows = db.execute(
            "SELECT id, name, language, source FROM voices WHERE user_id=? AND source=? ORDER BY created_at",
            (user["id"], source),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, name, language, source FROM voices WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
    voices = []
    if source in (None, "clone"):
        voices.append({
            "voice_id": "default",
            "name": "Default",
            "language": "German",
            "source": "clone",
        })
    for r in rows:
        if r[0] == "default" and source in (None, "clone"):
            continue
        voices.append({
            "voice_id": r[0],
            "name": r[1],
            "language": r[2],
            "source": r[3],
        })
    return {"voices": voices}


@app.get("/v1/audio/voices")
async def openai_audio_voices(request: Request):
    """Open WebUI-compatible voice listing for the base /speech endpoint."""
    data = await openai_voices(request, source="clone")
    return {
        "voices": [
            {
                "id": voice["voice_id"],
                "name": voice["name"],
                "language": voice.get("language"),
                "source": voice.get("source"),
            }
            for voice in data.get("voices", [])
        ]
    }


class PersonaRequest(BaseModel):
    name: str
    tagline: str | None = None
    soul: str | None = None
    humour: str | None = None
    warmth: str | None = None
    scriptLength: str | None = None


class PersonaUpdate(BaseModel):
    name: str | None = None
    tagline: str | None = None
    soul: str | None = None
    humour: str | None = None
    warmth: str | None = None
    scriptLength: str | None = None


def _clean_persona_text(
    value: str | None,
    field: str,
    *,
    required: bool = False,
    max_chars: int = 2000,
) -> str:
    cleaned = (value or "").strip()
    if required and not cleaned:
        raise HTTPException(400, f"{field} is required")
    if len(cleaned) > max_chars:
        raise HTTPException(400, f"{field} is too long (max {max_chars} characters)")
    return cleaned


def _persona_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "tagline": row[3] or "",
        "soul": row[2] or "",
        "humour": row[4] or "",
        "warmth": row[5] or row[7] or "",
        "scriptLength": row[6] or "medium",
        "voice": "default",
        "created_at": row[8],
        "updated_at": row[9],
    }


def _get_persona_or_404(persona_id: str, user_id: int) -> dict:
    row = get_db().execute(
        "SELECT id, name, personality, persona_tagline, persona_humour, "
        "persona_warmth, persona_script_length, speaking_style, created_at, updated_at "
        "FROM hosts WHERE id=? AND user_id=?",
        (persona_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Persona not found")
    return _persona_row_to_dict(row)


@app.get("/v1/personas")
async def openai_personas(request: Request):
    """List text/persona styles for OpenAI-compatible SUB/WAVE clients.

    Personas are deliberately separate from TTS voices. OpenAI-compatible
    speech still uses ``voice=default`` for the base voice; clients can use
    persona fields to shape script text before sending it to TTS.
    """
    user = get_current_user(request, allow_anonymous=True)
    rows = get_db().execute(
        "SELECT id, name, personality, persona_tagline, persona_humour, "
        "persona_warmth, persona_script_length, speaking_style, created_at, updated_at "
        "FROM hosts WHERE user_id=? ORDER BY created_at",
        (user["id"],),
    ).fetchall()
    return {"personas": [_persona_row_to_dict(row) for row in rows]}


@app.post("/v1/personas")
async def create_openai_persona(req: PersonaRequest, request: Request):
    user = get_current_user(request, allow_anonymous=True)
    name = _clean_persona_text(req.name, "name", required=True, max_chars=120)
    soul = _clean_persona_text(req.soul, "soul")
    tagline = _clean_persona_text(req.tagline, "tagline", max_chars=500)
    humour = _clean_persona_text(req.humour, "humour", max_chars=500)
    warmth = _clean_persona_text(req.warmth, "warmth", max_chars=500)
    script_length = _clean_persona_text(req.scriptLength, "scriptLength", max_chars=64) or "medium"
    persona_id = f"persona-{uuid.uuid4().hex}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    db = get_db()
    db.execute(
        "INSERT INTO hosts "
        "(id, user_id, name, personality, speaking_style, persona_tagline, "
        "persona_humour, persona_warmth, persona_script_length, voice_id, role, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'host', ?, ?)",
        (
            persona_id,
            user["id"],
            name,
            soul,
            warmth,
            tagline,
            humour,
            warmth,
            script_length,
            now,
            now,
        ),
    )
    db.commit()
    return _get_persona_or_404(persona_id, user["id"])


@app.patch("/v1/personas/{persona_id}")
async def update_openai_persona(
    persona_id: str,
    req: PersonaUpdate,
    request: Request,
):
    user = get_current_user(request, allow_anonymous=True)
    _get_persona_or_404(persona_id, user["id"])
    fields = []
    values = []
    if req.name is not None:
        fields.append("name=?")
        values.append(_clean_persona_text(req.name, "name", required=True, max_chars=120))
    if req.soul is not None:
        fields.append("personality=?")
        values.append(_clean_persona_text(req.soul, "soul"))
    if req.tagline is not None:
        fields.append("persona_tagline=?")
        values.append(_clean_persona_text(req.tagline, "tagline", max_chars=500))
    if req.humour is not None:
        fields.append("persona_humour=?")
        values.append(_clean_persona_text(req.humour, "humour", max_chars=500))
    if req.warmth is not None:
        warmth = _clean_persona_text(req.warmth, "warmth", max_chars=500)
        fields.append("persona_warmth=?")
        values.append(warmth)
        fields.append("speaking_style=?")
        values.append(warmth)
    if req.scriptLength is not None:
        script_length = _clean_persona_text(
            req.scriptLength, "scriptLength", max_chars=64
        ) or "medium"
        fields.append("persona_script_length=?")
        values.append(script_length)
    if not fields:
        return _get_persona_or_404(persona_id, user["id"])
    fields.append("updated_at=?")
    values.append(time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    values.extend([persona_id, user["id"]])
    get_db().execute(
        f"UPDATE hosts SET {', '.join(fields)} WHERE id=? AND user_id=?",
        values,
    )
    get_db().commit()
    return _get_persona_or_404(persona_id, user["id"])


@app.delete("/v1/personas/{persona_id}")
async def delete_openai_persona(persona_id: str, request: Request):
    user = get_current_user(request, allow_anonymous=True)
    _get_persona_or_404(persona_id, user["id"])
    db = get_db()
    db.execute("DELETE FROM hosts WHERE id=? AND user_id=?", (persona_id, user["id"]))
    db.commit()
    return {"status": "deleted", "id": persona_id}


# ---------------------------------------------------------------------------
# LLM Providers (user-configurable)
# ---------------------------------------------------------------------------

class LLMProviderCreate(BaseModel):
    name: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.8
    max_tokens: int = 16384
    provider_type: str = "openai"


class LLMProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    is_active: bool | None = None
    provider_type: str | None = None


def _get_active_llm_provider(user_id: int) -> dict | None:
    """Return the active LLM provider for a user, or None."""
    db = get_db()
    row = db.execute(
        "SELECT id, name, base_url, api_key, model, temperature, max_tokens, provider_type "
        "FROM llm_providers WHERE user_id=? AND is_active=1 ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "base_url": row[2], "api_key": row[3],
        "model": row[4], "temperature": row[5], "max_tokens": row[6],
        "provider_type": row[7],
    }


@app.get("/api/llm/providers")
async def list_llm_providers(request: Request):
    """List all LLM providers for the authenticated user."""
    user = get_current_user(request)
    db = get_db()
    rows = db.execute(
        "SELECT id, name, base_url, model, temperature, max_tokens, is_active, provider_type, created_at "
        "FROM llm_providers WHERE user_id=? ORDER BY is_active DESC, created_at DESC",
        (user["id"],),
    ).fetchall()
    return {
        "providers": [
            {
                "id": r[0], "name": r[1], "base_url": r[2], "model": r[3],
                "temperature": r[4], "max_tokens": r[5], "is_active": bool(r[6]),
                "provider_type": r[7], "created_at": r[8],
            }
            for r in rows
        ]
    }


@app.get("/api/llm/providers/active")
async def get_active_llm_provider(request: Request):
    """Return the currently active LLM provider for the user."""
    user = get_current_user(request)
    provider = _get_active_llm_provider(user["id"])
    if not provider:
        raise HTTPException(404, "No active LLM provider configured")
    provider = {k: v for k, v in provider.items() if k != "api_key"}
    return provider


@app.post("/api/llm/providers")
async def create_llm_provider(req: LLMProviderCreate, request: Request):
    """Create a new LLM provider for the authenticated user."""
    user = get_current_user(request)
    clean = _validate_llm_common(
        name=req.name,
        base_url=req.base_url,
        model=req.model,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        provider_type=req.provider_type,
        require_name=True,
        require_base_url=True,
        require_model=True,
    )
    provider_id = str(uuid.uuid4())
    db = get_db()
    is_first_provider = db.execute(
        "SELECT 1 FROM llm_providers WHERE user_id=? LIMIT 1",
        (user["id"],),
    ).fetchone() is None
    db.execute(
        "INSERT INTO llm_providers "
        "(id, user_id, name, base_url, api_key, model, temperature, max_tokens, provider_type, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            provider_id,
            user["id"],
            clean["name"],
            clean["base_url"],
            (req.api_key or "").strip(),
            clean["model"],
            clean["temperature"],
            clean["max_tokens"],
            clean["provider_type"],
            1 if is_first_provider else 0,
        ),
    )
    db.commit()
    return {
        "id": provider_id,
        "name": clean["name"],
        "status": "created",
        "is_active": is_first_provider,
    }


@app.patch("/api/llm/providers/{provider_id}")
async def update_llm_provider(provider_id: str, req: LLMProviderUpdate, request: Request):
    """Update an LLM provider. Only the owner can update."""
    user = get_current_user(request)
    db = get_db()
    r = db.execute("SELECT id FROM llm_providers WHERE id=? AND user_id=?", (provider_id, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Provider not found")

    clean = _validate_llm_common(
        name=req.name,
        base_url=req.base_url,
        model=req.model,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        provider_type=req.provider_type,
    )
    fields = []
    values = []
    if "name" in clean:
        fields.append("name=?"); values.append(clean["name"])
    if "base_url" in clean:
        fields.append("base_url=?"); values.append(clean["base_url"])
    if req.api_key is not None:
        fields.append("api_key=?"); values.append(req.api_key.strip())
    if "model" in clean:
        fields.append("model=?"); values.append(clean["model"])
    if "temperature" in clean:
        fields.append("temperature=?"); values.append(clean["temperature"])
    if "max_tokens" in clean:
        fields.append("max_tokens=?"); values.append(clean["max_tokens"])
    if req.is_active is not None:
        fields.append("is_active=?"); values.append(1 if req.is_active else 0)
    if "provider_type" in clean:
        fields.append("provider_type=?"); values.append(clean["provider_type"])

    if not fields:
        return {"status": "no changes"}

    fields.append("updated_at=?"); values.append(time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    values.extend([provider_id, user["id"]])
    db.execute(f"UPDATE llm_providers SET {', '.join(fields)} WHERE id=? AND user_id=?", values)

    # If this provider was set to active, deactivate all others for this user
    if req.is_active:
        db.execute(
            "UPDATE llm_providers SET is_active=0 WHERE user_id=? AND id!=?",
            (user["id"], provider_id),
        )
    db.commit()
    return {"status": "updated", "provider_id": provider_id}


@app.delete("/api/llm/providers/{provider_id}")
async def delete_llm_provider(provider_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    r = db.execute(
        "SELECT id, is_active FROM llm_providers WHERE id=? AND user_id=?",
        (provider_id, user["id"]),
    ).fetchone()
    if not r:
        raise HTTPException(404, "Provider not found")
    db.execute("DELETE FROM llm_providers WHERE id=? AND user_id=?", (provider_id, user["id"]))
    if r[1]:
        fallback = db.execute(
            "SELECT id FROM llm_providers WHERE user_id=? ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        if fallback:
            db.execute(
                "UPDATE llm_providers SET is_active=1, updated_at=? WHERE id=? AND user_id=?",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ"), fallback[0], user["id"]),
            )
    db.commit()
    return {"status": "deleted", "provider_id": provider_id}


@app.post("/api/llm/providers/{provider_id}/set-active")
async def set_active_llm_provider(provider_id: str, request: Request):
    """Set a provider as active and deactivate all others for this user."""
    user = get_current_user(request)
    db = get_db()
    r = db.execute("SELECT id FROM llm_providers WHERE id=? AND user_id=?", (provider_id, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Provider not found")
    db.execute("UPDATE llm_providers SET is_active=0 WHERE user_id=?", (user["id"],))
    db.execute(
        "UPDATE llm_providers SET is_active=1, updated_at=? WHERE id=? AND user_id=?",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ"), provider_id, user["id"]),
    )
    db.commit()
    return {"status": "active", "provider_id": provider_id}


@app.post("/api/llm/test")
async def test_llm_provider(request: Request, body: dict):
    """Test an LLM provider configuration by sending a simple completion."""
    get_current_user(request)
    raw = await request.body()
    if len(raw) > MAX_LLM_TEST_BODY_BYTES:
        raise HTTPException(
            413,
            f"request body too large (max {_size_label(MAX_LLM_TEST_BODY_BYTES)})",
        )
    base_url = body.get("base_url", "")
    api_key = body.get("api_key", "")
    model = body.get("model", "")
    base_url = _normalize_http_base_url(base_url)
    model = (model or "").strip()
    if not model:
        raise HTTPException(400, "model is required")

    import aiohttp
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Say 'OK' and nothing else."}],
        "temperature": 0.0,
        "max_tokens": 10,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with _http_session().post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"status": "ok", "response": content.strip()}
            else:
                body_text = await resp.text()
                return {"status": "error", "code": resp.status, "detail": body_text[:200]}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8280)
