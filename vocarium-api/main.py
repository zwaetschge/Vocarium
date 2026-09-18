"""Vocarium API Gateway.

Central voice management API. Stores voice metadata in SQLite,
proxies generation requests to the speech engines (OmniVoice, Kikiri),
and orchestrates cloning / benchmark workflows.
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import shutil
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Response, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import user_settings
from access_control import is_admin_username
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
    retire_legacy_qwen_hosts_for_all_users,
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
from health_public import build_public_health
from metrics import inc, observe, render_prometheus, route_path_label
from podcast.routes import create_podcast_router
from request_context import request_id_var, user_id_var
from url_security import URLValidationError, normalize_http_base_url

logger = logging.getLogger(__name__)
# Uvicorn konfiguriert nur seine eigenen Logger; ohne Root-Handler
# verschwindet jedes logger.info() des Gateways spurlos.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

API_INSTANCE_ID = os.environ.get("VOCARIUM_INSTANCE_ID") or socket.gethostname()
# Qwen3-TTS is retired (2026-08-28). Empty is the supported default: the
# backend selector raises 503 rather than dialling a host that is not there.
# Set it back to http://qwen3-tts:8880 (plus QWEN_TTS_ENABLED=true and the
# Compose service) to resurrect the engine.
TTS_URL = os.environ.get("TTS_URL", "").strip()
KIKIRI_TTS_URL = os.environ.get("KIKIRI_TTS_URL", "http://kikiri-tts:8881").strip()
# Kikiri is the default speech engine: it runs on CPU, so it never queues behind
# GPU work. Qwen stays available as the fallback for everything Kikiri cannot do
# (cloned voices, voice design, non-WAV output) and can always be forced with
# engine="qwen".
DEFAULT_TTS_ENGINE = os.environ.get("DEFAULT_TTS_ENGINE", "omnivoice").strip().lower()
KIKIRI_MODEL_CACHE_SECONDS = float(os.environ.get("KIKIRI_MODEL_CACHE_SECONDS", "30"))
# VibeVoice-Realtime: retired together with Qwen (its clones were migrated to
# OmniVoice). Empty default, otherwise every voice resolution pays a DNS lookup
# against a host that no longer exists.
VIBEVOICE_TTS_URL = os.environ.get("VIBEVOICE_TTS_URL", "").strip()
# OmniVoice: zero-shot cloning from short reference audio, resident on GPU 0.
OMNIVOICE_TTS_URL = os.environ.get("OMNIVOICE_TTS_URL", "http://omnivoice-tts:8880").strip()
# Qwen TTS retirement switch: with the worker gone, its DB voices (clones,
# designs, prebuilt custom speakers) would clutter the picker as dead entries.
# The rows stay in the database — flip this back on to resurrect them.
QWEN_TTS_ENABLED = os.environ.get("QWEN_TTS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
# Optional second TTS replica (set when running with COMPOSE_PROFILES=dual-gpu).
# Empty/unset means single-GPU mode — all TTS goes through TTS_URL.
TTS_URL_2 = os.environ.get("TTS_URL_2", "").strip()
EXTRA_TTS_URLS = [TTS_URL_2] if TTS_URL_2 else []
# Einziger STT-Dienst (faster-whisper large-v3, lazy + Idle-Unload).
WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper-stt:8000").strip()
# ACE-Step ist aus dem Compose-Stack raus, deshalb ist der Default leer:
# ein gesetzter Wert wuerde die Podcast-Musikbruecke gegen einen toten Host
# verdrahten, statt Musiksegmente sauber zu ueberspringen. Wer Musik wieder
# will, setzt MUSIC_URL explizit.
MUSIC_URL = os.environ.get("MUSIC_URL", "").strip()
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
SUPPORTED_TTS_ENGINES = {"qwen", "kikiri", "vibevoice", "omnivoice"}
SUPPORTED_MUSIC_ENGINES = {"acestep"}
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
ALLOW_ANONYMOUS = os.environ.get("ALLOW_ANONYMOUS", "false").lower() in ("1", "true", "yes")
VOCARIUM_ADMIN_USERS = os.environ.get("VOCARIUM_ADMIN_USERS", "")


def _load_proxy_secret() -> str:
    """Nachweis, dass ``Remote-User`` wirklich vom Identity-Proxy stammt.

    Jeder Container im selben Docker-Netz kann den Header setzen. Ist ein
    Secret konfiguriert, zaehlt der Header nur noch zusammen mit
    ``X-Vocarium-Proxy-Secret``; Nginx in vocarium-ui haengt ihn an.
    """
    value = os.environ.get("VOCARIUM_PROXY_SECRET", "").strip()
    if value:
        return value
    secret_file = Path(os.environ.get("VOCARIUM_PROXY_SECRET_FILE", "/app/data/proxy-secret"))
    try:
        return secret_file.read_text("utf-8").strip()
    except OSError:
        return ""


VOCARIUM_PROXY_SECRET = _load_proxy_secret()

# Comma-separated list, or "*" for all (only safe in dev). Set per-deployment.
# Ohne Angabe gibt es kein CORS: die UI ist same-origin hinter Nginx.
_cors = os.environ.get("CORS_ORIGINS", "").strip()
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
        route_path = route_path_label(request.scope.get("route"))
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
    if username and VOCARIUM_PROXY_SECRET:
        presented = request.headers.get("X-Vocarium-Proxy-Secret", "")
        if not secrets.compare_digest(presented, VOCARIUM_PROXY_SECRET):
            # Ein Identitaets-Header ohne Proxy-Nachweis ist ein Spoof-Versuch
            # oder ein Direktzugriff; beides zaehlt wie "kein Header".
            username = ""
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


def _require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if not is_admin_username(user["username"], VOCARIUM_ADMIN_USERS):
        raise HTTPException(403, "Administrator access required")
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


_kikiri_models_cache: tuple[float, dict[str, dict[str, str]]] = (0.0, {})


async def _kikiri_models(*, force: bool = False) -> dict[str, dict[str, str]]:
    """Map Kikiri model id → metadata, refreshed on a short TTL.

    A fine-tune dropped into kikiri-tts/models/ has to show up without restarting
    the API, but the listing is on the hot path of every speech request, so it is
    cached rather than fetched per call.

    The value carries ``name``/``group``/``gender``/``backend``. Everything
    Kikiri serves is the CPU fallback for when OmniVoice cannot run; ``group``
    only separates the two Kokoro fine-tunes (``kikiri``) from the Piper preset
    bank (``fallback``) so pickers can sort the better ones first.
    """
    global _kikiri_models_cache
    if not KIKIRI_TTS_URL:
        return {}
    fetched_at, cached = _kikiri_models_cache
    if not force and cached and time.time() - fetched_at < KIKIRI_MODEL_CACHE_SECONDS:
        return cached
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with _http_session().get(f"{KIKIRI_TTS_URL}/v1/models", timeout=timeout) as response:
            response.raise_for_status()
            payload = await response.json()
        models = {
            str(item["id"]): {
                "name": str(item.get("description") or item["id"]),
                "group": str(item.get("group") or "kikiri"),
                "gender": str(item.get("gender") or "neutral"),
                "backend": str(item.get("backend") or "kokoro"),
                "notes": str(item.get("notes") or ""),
            }
            for item in payload.get("data", [])
            if item.get("id")
        }
    except Exception as exc:
        logger.warning(
            "Kikiri model listing failed (%r); keeping %d cached models",
            exc, len(cached),
        )
        _kikiri_models_cache = (time.time(), cached)
        return cached
    _kikiri_models_cache = (time.time(), models)
    return models


_vibevoice_models_cache: tuple[float, dict[str, str]] = (0.0, {})


async def _vibevoice_models(*, force: bool = False) -> dict[str, str]:
    """Map VibeVoice voice id → display name, refreshed on a short TTL.

    Only the scanned ``.pt`` voice prompts count ("vibevoice-native"); the
    OpenAI-name aliases would duplicate them and mean nothing to users.
    """
    global _vibevoice_models_cache
    if not VIBEVOICE_TTS_URL:
        return {}
    fetched_at, cached = _vibevoice_models_cache
    if not force and cached and time.time() - fetched_at < KIKIRI_MODEL_CACHE_SECONDS:
        return cached
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with _http_session().get(f"{VIBEVOICE_TTS_URL}/v1/audio/voices", timeout=timeout) as response:
            response.raise_for_status()
            payload = await response.json()
        models = {
            str(item["voice_id"]): str(item.get("name") or item["voice_id"])
            for item in payload.get("voices", [])
            if item.get("voice_id") and item.get("type") == "vibevoice-native"
        }
    except Exception as exc:
        logger.warning(
            "VibeVoice voice listing failed (%r); keeping %d cached voices",
            exc, len(cached),
        )
        # Stale beats empty: the worker blocks for seconds while generating,
        # and a timed-out refresh must not make its voices vanish mid-request.
        _vibevoice_models_cache = (time.time(), cached)
        return cached
    _vibevoice_models_cache = (time.time(), models)
    return models


_omnivoice_models_cache: tuple[float, dict[str, str]] = (0.0, {})
# Referenztranskript je Klonstimme, aus derselben Antwort wie die Namensliste.
# Getrennt gehalten, damit `_omnivoice_models` seinen id→name-Vertrag behält,
# den `_is_omnivoice_voice` und die Auto-Weiche mitbenutzen.
_omnivoice_reference_text: dict[str, str] = {}


async def _omnivoice_models(*, force: bool = False) -> dict[str, str]:
    """Map OmniVoice voice id → display name, same contract as the other caches."""
    global _omnivoice_models_cache
    if not OMNIVOICE_TTS_URL:
        return {}
    fetched_at, cached = _omnivoice_models_cache
    if not force and cached and time.time() - fetched_at < KIKIRI_MODEL_CACHE_SECONDS:
        return cached
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with _http_session().get(f"{OMNIVOICE_TTS_URL}/v1/audio/voices", timeout=timeout) as response:
            response.raise_for_status()
            payload = await response.json()
        models = {
            str(item["voice_id"]): str(item.get("name") or item["voice_id"])
            for item in payload.get("voices", [])
            if item.get("voice_id")
        }
        _omnivoice_reference_text.clear()
        _omnivoice_reference_text.update({
            str(item["voice_id"]): str(item["ref_text"]).strip()
            for item in payload.get("voices", [])
            if item.get("voice_id") and item.get("ref_text")
        })
    except Exception as exc:
        logger.warning(
            "OmniVoice voice listing failed (%r); keeping %d cached voices",
            exc, len(cached),
        )
        _omnivoice_models_cache = (time.time(), cached)
        return cached
    _omnivoice_models_cache = (time.time(), models)
    return models


def _is_omnivoice_voice(voice_id: str) -> bool:
    return bool(voice_id) and voice_id in _omnivoice_models_cache[1]


def _is_vibevoice_voice(voice_id: str) -> bool:
    return bool(voice_id) and voice_id in _vibevoice_models_cache[1]


def _is_kikiri_voice(voice_id: str) -> bool:
    """Whether the id names a published Kikiri fine-tune.

    Reads the cached listing rather than the network so the sync validation
    helpers stay sync; callers on the speech path refresh it first.
    """
    return bool(voice_id) and voice_id in _kikiri_models_cache[1]


def _engine_voice(engine: str, voice: str | None, selected_model: str | None) -> str:
    """Stimme, die der gewaehlte Engine-Container wirklich kennt.

    Kikiri adressiert Stimmen ueber ``model``; OmniVoice kennt kein
    ``default`` und bekommt die Stimme, die ``_select_tts_backend`` fuer
    diesen Fall bereits bestimmt hat.
    """
    if engine == "kikiri":
        return "default"
    cleaned = (voice or "").strip()
    if engine == "omnivoice" and cleaned in ("", "default") and selected_model:
        return selected_model
    return cleaned or "default"


def _upstream_detail(body: bytes) -> str:
    """Fehlertext eines Engine-Containers ohne doppelte JSON-Huelle."""
    text = body.decode(errors="replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
        return parsed["detail"]
    return text


async def _select_tts_backend(
    req_model: str | None,
    req_engine: str | None,
    *,
    voice_id: str | None = None,
    voice_source: str | None = None,
    response_format: str | None = None,
) -> tuple[str, str, str | None]:
    """Pick the speech backend and, for Kikiri, the concrete fine-tune.

    Returns ``(url, engine, kikiri_model_id)``. Selection is voice-driven:
    OmniVoice is the main engine, Kikiri is the CPU fallback bank, and a voice
    id picks its own engine. Qwen is the historical catch-all and is retired by
    default, so that last branch raises 503 unless TTS_URL is configured again.
    """
    requested_engine = (req_engine or "").strip().lower()
    if requested_engine:
        requested_engine = _validate_engine(requested_engine, SUPPORTED_TTS_ENGINES)
    model_name = (req_model or "").strip()
    voice = (voice_id or "").strip()

    if requested_engine == "qwen":
        if not TTS_URL:
            raise HTTPException(503, "Qwen TTS is retired (TTS_URL is not configured)")
        return TTS_URL, "qwen", None

    vibevoice = await _vibevoice_models() if VIBEVOICE_TTS_URL else {}

    def resolve_vibevoice(name: str) -> str | None:
        if not name:
            return None
        if name in vibevoice:
            return name
        lowered = name.casefold()
        for vid, display in vibevoice.items():
            if lowered in (vid.casefold(), display.casefold()):
                return vid
        return None

    vv_chosen = resolve_vibevoice(voice) or resolve_vibevoice(model_name)
    vv_exact = voice if voice in vibevoice else (model_name if model_name in vibevoice else None)
    if requested_engine == "vibevoice":
        if not VIBEVOICE_TTS_URL:
            raise HTTPException(503, "VibeVoice TTS is not configured")
        if not vibevoice:
            raise HTTPException(503, "VibeVoice TTS has no voices available")
        if voice_source == "custom":
            raise HTTPException(400, "VibeVoice serves its own cloned voice prompts, not Qwen custom voices")
        if vv_chosen is None:
            raise HTTPException(404, f"Unknown VibeVoice voice {voice or model_name!r}")
        return VIBEVOICE_TTS_URL, "vibevoice", vv_chosen

    omnivoice = await _omnivoice_models() if OMNIVOICE_TTS_URL else {}

    def resolve_omnivoice(name: str) -> str | None:
        if not name:
            return None
        if name in omnivoice:
            return name
        lowered = name.casefold()
        for vid, display in omnivoice.items():
            if lowered in (vid.casefold(), display.casefold()):
                return vid
        return None

    ov_chosen = resolve_omnivoice(voice) or resolve_omnivoice(model_name)
    ov_exact = voice if voice in omnivoice else (model_name if model_name in omnivoice else None)
    if requested_engine == "omnivoice":
        if not OMNIVOICE_TTS_URL:
            raise HTTPException(503, "OmniVoice TTS is not configured")
        if not omnivoice:
            raise HTTPException(503, "OmniVoice TTS has no voices available")
        if voice_source == "custom":
            raise HTTPException(400, "OmniVoice serves its own cloned voices, not Qwen custom voices")
        if ov_chosen is None:
            raise HTTPException(404, f"Unknown OmniVoice voice {voice or model_name!r}")
        return OMNIVOICE_TTS_URL, "omnivoice", ov_chosen

    kikiri_supported = (
        voice_source != "custom"
        and (response_format or "wav").strip().lower() == "wav"
    )
    models = await _kikiri_models() if KIKIRI_TTS_URL else {}

    def resolve(name: str) -> str | None:
        if not name:
            return None
        if name in models:
            return name
        lowered = name.casefold()
        for model_id, meta in models.items():
            if lowered in (model_id.casefold(), meta["name"].casefold()):
                return model_id
        return None

    chosen = resolve(model_name) or resolve(voice)
    if model_name.casefold() == "kikiri" and models:
        chosen = chosen or next(iter(sorted(models)))

    if requested_engine == "kikiri":
        if not KIKIRI_TTS_URL:
            raise HTTPException(503, "Kikiri TTS is not configured")
        if not models:
            raise HTTPException(503, "Kikiri TTS has no models available")
        if not kikiri_supported:
            raise HTTPException(
                400,
                "Kikiri serves fine-tuned voices as WAV only — use engine='qwen' "
                "for cloned voices or other formats",
            )
        if chosen is None:
            raise HTTPException(404, f"Unknown Kikiri voice {voice or model_name!r}")
        return KIKIRI_TTS_URL, "kikiri", chosen

    # Automatic selection matches exact voice ids only: "David" (OmniVoice) and
    # "david" (a Kikiri fine-tune) are different voices, and a fuzzy match here
    # would silently reroute one user's voice to another engine.
    kikiri_exact = voice if voice in models else (model_name if model_name in models else None)
    if kikiri_exact is None and chosen is not None and model_name.casefold() == "kikiri":
        kikiri_exact = chosen

    wants_wav = (response_format or "wav").strip().lower() == "wav"
    # OmniVoice serves WAV only; other formats used to fall through to Qwen.
    omnivoice_ok = ov_exact is not None and voice_source != "custom" and wants_wav
    kikiri_ok = kikiri_exact is not None and kikiri_supported

    if voice == "default" and not QWEN_TTS_ENABLED and not omnivoice_ok and not kikiri_ok:
        # The built-in "default" voice belongs to no engine and would dead-end
        # in retired Qwen. It goes to the main engine; the CPU fallback only
        # catches it when OmniVoice has nothing to serve.
        if omnivoice and voice_source != "custom" and wants_wav:
            return OMNIVOICE_TTS_URL, "omnivoice", next(iter(sorted(omnivoice)))
        if models and kikiri_supported:
            return KIKIRI_TTS_URL, "kikiri", next(iter(sorted(models)))

    # OmniVoice is the main engine, Kikiri the CPU fallback for when OmniVoice
    # cannot run. Voice ids decide the engine on their own, so this order only
    # settles a genuine tie -- and DEFAULT_TTS_ENGINE=kikiri can invert it when
    # the GPU is deliberately kept out of the loop.
    if omnivoice_ok and DEFAULT_TTS_ENGINE != "kikiri":
        return OMNIVOICE_TTS_URL, "omnivoice", ov_exact
    if kikiri_ok:
        return KIKIRI_TTS_URL, "kikiri", kikiri_exact
    if omnivoice_ok:
        return OMNIVOICE_TTS_URL, "omnivoice", ov_exact
    if vv_exact is not None and voice_source != "custom":
        return VIBEVOICE_TTS_URL, "vibevoice", vv_exact
    if not TTS_URL:
        # Qwen used to absorb everything the other engines declined. With it
        # retired, saying so beats posting to an empty URL.
        raise HTTPException(
            503,
            f"No speech engine serves voice {voice or model_name or 'default'!r} "
            f"in format {(response_format or 'wav')!r} (Qwen is retired)",
        )
    return TTS_URL, "qwen", None


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
    if not url:
        # Engine retired / not configured — nothing to unload.
        return
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
    """Tell Whisper to drop its model, freeing shared GPU VRAM."""
    if WHISPER_URL:
        await _post_unload(WHISPER_URL, "Whisper-STT", "was_running")


async def _unload_music():
    """Tell ACE-Step proxy to unload backend, freeing shared GPU VRAM."""
    await _post_unload(MUSIC_URL, "ACE-Step", "was_running")


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


async def _run_tts_job(
    description: str,
    work_maker,
    *,
    tts_url: str = TTS_URL,
    engine: str = "qwen",
):
    """Run a TTS job on the GPU TTS service via the shared GPU queue.

    work_maker(tts_url) → coroutine that performs the actual TTS work.
    Kikiri runs on CPU and therefore bypasses the queue entirely.
    """
    if engine in ("kikiri", "vibevoice", "omnivoice"):
        return await work_maker(tts_url)

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
)
app.include_router(podcast_router)

# ---------------------------------------------------------------------------
# Audiobooks — Canto core rebuilt on the consolidated engines. The podcast TTS
# bridge already knows how to resolve OmniVoice/Kikiri voices and synthesize
# to a file, which is exactly the contract the audiobook worker needs.
# ---------------------------------------------------------------------------
from audiobooks.routes import create_audiobooks_router

audiobooks_router = create_audiobooks_router(
    get_current_user=get_current_user,
    db_getter=get_db,
    tts_bridge=audio_assembler.tts if hasattr(audio_assembler, "tts") else None,
    data_dir=DATA_DIR,
)
app.include_router(audiobooks_router)
from library import create_library_router
app.include_router(create_library_router(get_current_user=get_current_user, db_getter=get_db))

# ---------------------------------------------------------------------------
# Hörspiele — portierte Szenenklang-Engine. Sie bringt eigene Hintergrund-
# Threads mit, deshalb bekommt sie den Zustandsspeicher (SQLite statt der
# alten state.json) und dieselbe TTS-Brücke wie die Hörbücher.
# ---------------------------------------------------------------------------
from hoerspiele import create_hoerspiele_router, start as hoerspiele_start

hoerspiele_router = create_hoerspiele_router(
    get_current_user=get_current_user,
    db_getter=get_db,
    tts_bridge=audio_assembler.tts if hasattr(audio_assembler, "tts") else None,
    gpu_submit=gpu_queue.submit,
    data_dir=DATA_DIR,
)
app.include_router(hoerspiele_router)


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
    _require_admin(request)
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
    try:
        hoerspiele_start()
    except Exception as exc:  # pragma: no cover - Bereich darf den Start nicht kippen
        logger.error("Hörspiele konnten nicht initialisiert werden: %s", exc)
    interrupted = mark_interrupted_gpu_jobs(worker_id=API_INSTANCE_ID)
    if interrupted:
        logger.warning("Marked %d persisted GPU queue job(s) as interrupted", interrupted)
    def record_gpu_job(job: dict) -> None:
        job["worker_id"] = API_INSTANCE_ID
        upsert_gpu_queue_job(job)

    register_job_recorder(record_gpu_job)
    register_quota_checker(gpu_queue_quota_decision)
    register_cancel_checker(is_gpu_queue_cancel_requested)
    # Alt-Hosts der stillgelegten Qwen-Sprecher zurückziehen (idempotent).
    # Sprecher kommen jetzt aus dem Host-Hub, nicht mehr aus einem Auto-Seed.
    retired = retire_legacy_qwen_hosts_for_all_users()
    if retired:
        logger.info("Retired %d legacy Qwen host(s)", retired)
    # Sync voices from TTS filesystem into SQLite if needed
    await _sync_voices_from_tts()
    # Start GPU queue with unload callbacks. The queue decides conflicts from
    # GPU_TTS_*/GPU_ASR/GPU_MUSIC, so single-GPU and dual-GPU layouts
    # both unload only what can actually collide.
    unloaders = {
        "tts": _unload_tts,
        "asr": _unload_asr,
        "music": _unload_music,
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
    # Model switching is a Qwen concept. With Qwen retired the service is not
    # even in the Compose file, so proxying would 500 on DNS.
    if not QWEN_TTS_ENABLED:
        return {"models": []}
    return await tts_json("GET", "/v1/models")


@app.get("/api/models/current")
async def current_model(request: Request):
    get_current_user(request)
    if not QWEN_TTS_ENABLED:
        return {"id": "", "path": "", "type": "", "params": "", "loaded": False}
    return await tts_json("GET", "/v1/models/current")


class SwitchModelRequest(BaseModel):
    model_id: str


@app.post("/api/models/switch")
async def switch_model(req: SwitchModelRequest, request: Request):
    _require_admin(request)

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
        if not QWEN_TTS_ENABLED:
            continue
        voices.append({
            "id": r[0], "name": r[1], "language": r[2], "source": r[3],
            "design_prompt": r[4], "ref_text": r[5],
            "speaker": r[6], "instruct": r[7], "created_at": r[8],
            "has_audio": _voice_has_audio(r[0]),
        })
    for voice_id, display_name in sorted((await _omnivoice_models(force=True)).items()):
        voices.append({
            "id": voice_id,
            "name": display_name,
            "language": "Auto",
            "source": "omnivoice",
            "design_prompt": None,
            "ref_text": _omnivoice_reference_text.get(voice_id),
            "speaker": None,
            "instruct": None,
            "created_at": None,
            # Das Referenzpaar auf der Platte *ist* die Stimme — es taugt als
            # Vorschau, ohne dafür erst die GPU zu bemühen.
            "has_audio": True,
            "engine": "omnivoice",
        })
    for voice_id, display_name in sorted((await _vibevoice_models(force=True)).items()):
        voices.append({
            "id": voice_id,
            "name": display_name,
            "language": "Auto",
            "source": "vibevoice",
            "design_prompt": None,
            "ref_text": None,
            "speaker": None,
            "instruct": None,
            "created_at": None,
            "has_audio": False,
            "engine": "vibevoice",
        })
    for model_id, meta in sorted((await _kikiri_models(force=True)).items()):
        voices.append({
            "id": model_id,
            "name": meta["name"],
            "language": "German",
            "source": "kikiri",
            "design_prompt": None,
            "ref_text": None,
            "speaker": None,
            "instruct": None,
            "created_at": None,
            "has_audio": False,
            "engine": "kikiri",
            # `fallback` voices only exist for the case where OmniVoice is down;
            # the UI groups on this rather than guessing from the id prefix.
            "group": meta["group"],
            "gender": meta["gender"],
            "backend": meta["backend"],
            "notes": meta["notes"] or None,
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
    if _is_omnivoice_voice(voice_id):
        timeout = aiohttp.ClientTimeout(total=30)
        async with _http_session().delete(
            f"{OMNIVOICE_TTS_URL}/v1/audio/voices/{voice_id}", timeout=timeout
        ) as resp:
            payload = await resp.json()
            if resp.status >= 400:
                raise HTTPException(resp.status, payload.get("detail", "OmniVoice deletion failed"))
        await _omnivoice_models(force=True)
        return {"status": "deleted", "voice_id": voice_id}
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
    if _is_omnivoice_voice(voice_id):
        # OmniVoice-Klone haben keine DB-Zeile; ihr Referenzpaar liegt beim
        # Worker. Durchreichen statt 404, sonst bleibt die Vorschau für alle
        # sechzehn Klonstimmen tot.
        timeout = aiohttp.ClientTimeout(total=30)
        async with _http_session().get(
            f"{OMNIVOICE_TTS_URL}/v1/audio/voices/{voice_id}/reference", timeout=timeout
        ) as resp:
            if resp.status >= 400:
                raise HTTPException(resp.status, "Reference audio not available")
            return Response(await resp.read(), media_type="audio/wav")
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
    return [u for u in (TTS_URL, *EXTRA_TTS_URLS) if u]


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

        if not QWEN_TTS_ENABLED:
            # Qwen is retired: register the clone on OmniVoice instead. The
            # reference pair lives in the OmniVoice voices directory; no DB row —
            # the voice surfaces through the engine listing like all its peers.
            audio_bytes_wav = await asyncio.to_thread(audio_path.read_bytes)
            form = aiohttp.FormData()
            form.add_field("name", name)
            form.add_field("ref_text", ref_text)
            form.add_field("file", audio_bytes_wav, filename="ref.wav", content_type="audio/wav")
            timeout = aiohttp.ClientTimeout(total=180)
            async with _http_session().post(
                f"{OMNIVOICE_TTS_URL}/v1/audio/voices", data=form, timeout=timeout
            ) as resp:
                payload = await resp.json()
                if resp.status >= 400:
                    raise HTTPException(resp.status, payload.get("detail", "OmniVoice registration failed"))
            shutil.rmtree(voice_dir, ignore_errors=True)
            await _omnivoice_models(force=True)
            return {"status": "created", "voice_id": payload["voice_id"], "name": name}

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
    """Transcribe reference audio for voice cloning.

    Whisper verwaltet sein VRAM selbst (lazy laden, Idle-Unload), also ohne
    gpu_queue-Slot.
    """
    if not WHISPER_URL:
        raise HTTPException(503, "Kein STT-Dienst konfiguriert (WHISPER_URL)")
    audio_bytes = await asyncio.to_thread(audio_path.read_bytes)
    result = await _transcribe_wav_whisper(audio_bytes)
    return result.get("text", "").strip()


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
            raise HTTPException(status, _upstream_detail(body))
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
            raise HTTPException(status, _upstream_detail(body))
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
            raise HTTPException(status, _upstream_detail(body))
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
    if _is_kikiri_voice(voice_id) or _is_vibevoice_voice(voice_id) or _is_omnivoice_voice(voice_id):
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
    if "clone" in allowed_sources and (
        _is_omnivoice_voice(voice_id)
        or _is_kikiri_voice(voice_id)
        or _is_vibevoice_voice(voice_id)
    ):
        # Since Qwen was retired, a "cloned voice" is an engine fact, not a
        # database row: OmniVoice owns the clones, Kikiri the fine-tunes. Without
        # this branch every real voice 404s on the OpenAI-compatible endpoint.
        return
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
    if _is_kikiri_voice(voice_id):
        return "kikiri"
    if _is_vibevoice_voice(voice_id):
        return "vibevoice"
    if _is_omnivoice_voice(voice_id):
        return "omnivoice"
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


def _backfill_timing_headers(headers: dict) -> dict:
    """Ergänzt `X-Generation-Time` und `X-RTF`.

    OmniVoice und Kikiri melden nur `X-Inference-Seconds` und
    `X-Audio-Duration`; die beiden abgeleiteten Header stammen noch aus der
    Qwen-Zeit. Ohne diese Ergänzung zeigen Clients dauerhaft 0.
    """
    h_lower = {k.lower(): v for k, v in headers.items()}
    try:
        gen = float(h_lower.get("x-generation-time") or h_lower.get("x-inference-seconds") or 0)
        dur = float(h_lower.get("x-audio-duration") or 0)
    except ValueError:
        return headers
    if gen <= 0:
        return headers
    if "x-generation-time" not in h_lower:
        headers["X-Generation-Time"] = f"{gen:.3f}"
    if "x-rtf" not in h_lower and dur > 0:
        headers["X-RTF"] = f"{gen / dur:.4f}"
    return headers


async def _tts_generate_for_voice_raw(
    *,
    tts_url: str,
    text: str,
    voice_id: str,
    source: str,
    response_format: str,
    user_id: int,
    model_id: str | None = None,
    language: str | None = None,
    engine: str = "qwen",
    kikiri_model: str | None = None,
) -> tuple[int, dict, bytes]:
    response_format = _validate_audio_format(
        response_format, allowed=TTS_RESPONSE_FORMATS
    )
    if engine in ("vibevoice", "omnivoice"):
        return await tts_request(
            "POST",
            "/v1/audio/speech",
            url=tts_url,
            json={
                "model": engine,
                "input": text,
                "voice": kikiri_model or voice_id,
                "response_format": response_format,
            },
        )
    if engine == "kikiri":
        # Every Kikiri fine-tune carries exactly one voice; the model id is what
        # selects the speaker.
        return await tts_request(
            "POST",
            "/v1/audio/speech",
            url=tts_url,
            json={
                "input": text,
                "model": kikiri_model or voice_id,
                "voice": "default",
                "response_format": "wav",
            },
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


async def _tts_generate_for_voice(**kwargs) -> tuple[int, dict, bytes]:
    status, headers, body = await _tts_generate_for_voice_raw(**kwargs)
    if status < 400:
        headers = _backfill_timing_headers(headers)
    return status, headers, body


@app.post("/api/generate/stream")
async def generate_speech_stream(req: GenerateRequest, request: Request):
    """Stream speech generation via SSE while the GPU queue job is running."""
    user = get_current_user(request)
    _require_text_limit(req.text)
    await _kikiri_models()
    await _vibevoice_models()
    await _omnivoice_models()
    _verify_voice_exists(req.voice_id, user_id=user["id"])
    source = _voice_source(req.voice_id, user_id=user["id"])
    selected_url, selected_engine, selected_kikiri_model = await _select_tts_backend(
        req.model_id,
        req.engine,
        voice_id=req.voice_id,
        voice_source=source,
    )
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
                engine=selected_engine,
                kikiri_model=selected_kikiri_model,
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
            "voice": _engine_voice(selected_engine, req.voice_id, selected_kikiri_model),
            "response_format": "wav",
        }
        if selected_engine == "kikiri":
            payload["model"] = selected_kikiri_model
        elif selected_engine in ("vibevoice", "omnivoice"):
            payload["model"] = selected_engine
            payload["voice"] = selected_kikiri_model
        elif req.model_id:
            payload["model_id"] = req.model_id
        if req.language and selected_engine in ("qwen",):
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
            await _run_tts_job(
                "TTS Stream",
                work_maker,
                tts_url=selected_url,
                engine=selected_engine,
            )
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
    await _kikiri_models()
    await _vibevoice_models()
    await _omnivoice_models()
    _verify_voice_exists(req.voice_id, user_id=user["id"])
    source = _voice_source(req.voice_id, user_id=user["id"])
    selected_url, selected_engine, selected_kikiri_model = await _select_tts_backend(
        req.model_id,
        req.engine,
        voice_id=req.voice_id,
        voice_source=source,
        response_format=req.response_format,
    )

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
            engine=selected_engine,
            kikiri_model=selected_kikiri_model,
        )
        if status >= 400:
            raise HTTPException(status, _upstream_detail(body))
        resp_headers = {k: v for k, v in headers.items() if k.lower().startswith("x-")}
        # Not every backend stamps its engine (VibeVoice does not); the caller
        # decided the routing, so the caller states it.
        resp_headers.setdefault("X-TTS-Engine", selected_engine)
        content_type = headers.get("Content-Type", headers.get("content-type", "audio/wav"))
        return {"body": body, "media_type": content_type, "headers": resp_headers}

    result = await _run_tts_job(
        "TTS Generate", work_maker, tts_url=selected_url, engine=selected_engine
    )
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


async def _transcribe_wav_whisper(
    wav_bytes: bytes, model: str = "", language: str = "", vad: str = ""
) -> dict:
    """Einziger STT-Pfad: faster-whisper large-v3 — liefert Text, Sprache und
    Wort-/Segment-Timestamps in einem Aufruf (kein /align nötig).

    `model` wählt das Profil (`german` oder `swiss`), `language` erzwingt eine
    Sprache bzw. schaltet mit `auto` die Erkennung frei. Beide bleiben leer,
    wenn der Aufrufer nichts angibt — dann gelten die Vorgaben des Workers.
    """
    timeout = aiohttp.ClientTimeout(total=1800, sock_connect=30, sock_read=1800)
    form = aiohttp.FormData()
    form.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
    if model.strip():
        form.add_field("model", model.strip())
    if language.strip():
        form.add_field("language", language.strip())
    if vad.strip():
        # "auto" (Vorgabe im Worker), "on", "off": Silero-VAD verwirft Gesang,
        # deshalb kann der Aufrufer den Sprachfilter abschalten.
        form.add_field("vad", vad.strip())
    async with _http_session().post(
        f"{WHISPER_URL}/v1/audio/transcriptions", data=form, timeout=timeout
    ) as resp:
        if resp.status >= 400:
            body = await resp.read()
            raise RuntimeError(
                f"Whisper error {resp.status}: {body.decode(errors='replace')[:500]}"
            )
        data = await resp.json()
    words = [
        {"word": w.get("word", ""), "start": w.get("start", 0.0), "end": w.get("end", 0.0)}
        for w in data.get("words", [])
    ]
    return {
        "text": data.get("text", ""),
        "language": data.get("language", "de"),
        "model": data.get("model", model.strip() or "german"),
        "words": words,
        "segments": data.get("segments", []),
    }


@app.post("/api/transcribe")
async def transcribe(
    request: Request,
    file: UploadFile | None = File(None),
    url: str = Form(""),
    model: str = Form(""),
    language: str = Form(""),
    vad: str = Form(""),
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

    # Whisper verwaltet sein VRAM selbst (lazy laden + Idle-Unload) und
    # braucht deshalb keinen gpu_queue-Slot.
    if not WHISPER_URL:
        raise HTTPException(503, "Kein STT-Dienst konfiguriert (WHISPER_URL)")
    try:
        return await _transcribe_wav_whisper(wav_bytes, model=model, language=language, vad=vad)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Transkription fehlgeschlagen: {exc}")


# ---------------------------------------------------------------------------
# TTS Health proxy
# ---------------------------------------------------------------------------
async def _speech_engine_health() -> dict:
    """Health of whatever engine actually serves speech.

    This block used to be the Qwen worker's /health verbatim. With Qwen retired
    that probe reports "unreachable" forever, which reads as an outage — so fall
    back to OmniVoice, the engine every non-Kikiri voice runs on now.
    """
    if TTS_URL:
        return await tts_json("GET", "/health")
    if not OMNIVOICE_TTS_URL:
        return {"status": "unreachable"}
    timeout = aiohttp.ClientTimeout(total=10)
    async with _http_session().get(
        f"{OMNIVOICE_TTS_URL}/health", timeout=timeout
    ) as resp:
        data = await resp.json()
    voices = data.get("voices")
    return {
        "status": "ok" if data.get("status") in ("ok", "healthy") else "unreachable",
        "model_loaded": bool(voices),
        "voices_loaded": voices,
    }


@app.get("/api/health")
async def health():
    try:
        tts_health = await _speech_engine_health()
    except Exception:
        tts_health = {"status": "unreachable"}
    gpu_resources = await get_resource_status()
    return build_public_health(tts_health, gpu_resources)


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


def _voice_alias_key(value: str) -> str:
    """Vergleichsschluessel fuer Stimmennamen: "Marc-Uwe Kling" == "Marc-Uwe-Kling"."""
    text = (value or "").strip().casefold()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(src, dst)
    return re.sub(r"[^a-z0-9]+", "", text)


def _legacy_clone_alias(voice_id: str, user_id: int | None) -> str | None:
    """OmniVoice-Stimme, die einen stillgelegten Qwen-Klon gleichen Namens ersetzt.

    Clients wie Sub-Wave haben die alten 8-stelligen Klon-IDs gespeichert
    ("83b59aca" = Michael Scott). Die Klone wurden nach OmniVoice migriert und
    heissen dort wie der alte Anzeigename, nur mit Bindestrichen. Solange Qwen
    aus ist, wird der alte Datensatz auf diese Stimme umgebogen.
    """
    if QWEN_TTS_ENABLED or not voice_id or voice_id == "default":
        return None
    _fetched_at, omnivoice = _omnivoice_models_cache
    if not omnivoice or voice_id in omnivoice:
        return None
    db = get_db()
    if user_id is not None:
        row = db.execute(
            "SELECT name, source FROM voices WHERE id=? AND (user_id=? OR user_id IS NULL)",
            (voice_id, user_id),
        ).fetchone()
    else:
        row = db.execute("SELECT name, source FROM voices WHERE id=?", (voice_id,)).fetchone()
    if not row or row[1] not in ("clone", "custom", "design"):
        return None
    wanted = _voice_alias_key(row[0])
    for candidate_id, display in omnivoice.items():
        if wanted and wanted in (_voice_alias_key(candidate_id), _voice_alias_key(display)):
            return candidate_id
    return None


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
            return _legacy_clone_alias(voice_id, user_id) or voice_id
    for voice_id, name in rows:
        if lookup_key == _openai_voice_lookup_key(name or ""):
            return _legacy_clone_alias(voice_id, user_id) or voice_id

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
    voice = voice_override or requested_voice or "default"
    await _kikiri_models()
    await _vibevoice_models()
    await _omnivoice_models()
    # Die Engine-Wahl muss die aufgeloeste Stimme sehen, nicht den rohen
    # Request: sonst landet ein alter Klon-Alias wieder bei der toten Qwen-Engine.
    selected_url, selected_engine, selected_kikiri_model = await _select_tts_backend(
        req.model,
        req.engine,
        voice_id=voice,
        response_format=response_format,
    )
    payload = {
        "input": req.input,
        "voice": _engine_voice(selected_engine, voice, selected_kikiri_model),
        "response_format": response_format,
    }
    if selected_engine == "kikiri":
        payload["model"] = selected_kikiri_model
    # Map OpenAI model names to internal model_id
    model_map = {"tts-1": "1.7b-base", "tts-1-hd": "1.7b-base"}
    if selected_engine == "kikiri":
        pass
    elif req.model in model_map:
        payload["model_id"] = model_map[req.model]
    elif req.model not in ("qwen3-tts", ""):
        payload["model_id"] = req.model

    async def work_maker(tts_url):
        status, headers, body = await tts_request(
            "POST", "/v1/audio/speech", url=selected_url, json=payload
        )
        if status >= 400:
            raise HTTPException(status, _upstream_detail(body))
        if selected_engine != "kikiri" and voice not in ("default", ""):
            h_lower = {k.lower(): v for k, v in headers.items()}
            actual_voice = h_lower.get("x-voice")
            if actual_voice and actual_voice != voice:
                raise HTTPException(
                    502,
                    f"TTS returned voice {actual_voice!r} for requested voice {voice!r}",
                )
        content_type = headers.get("Content-Type", headers.get("content-type", "audio/wav"))
        return {"body": body, "media_type": content_type, "voice": voice}

    result = await _run_tts_job(
        description, work_maker, tts_url=selected_url, engine=selected_engine
    )
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
    # Warm the engine listings first: _verify_voice_source is sync and reads the
    # caches, so a cold cache would reject a perfectly valid OmniVoice voice.
    await _omnivoice_models()
    await _kikiri_models()
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

    if not WHISPER_URL:
        raise HTTPException(503, "Kein STT-Dienst konfiguriert (WHISPER_URL)")
    try:
        result = await _transcribe_wav_whisper(wav_bytes)
    except Exception as exc:
        raise HTTPException(502, f"Transkription fehlgeschlagen: {exc}")

    if response_format == "text":
        return Response(content=result["text"], media_type="text/plain")
    # Default: JSON (OpenAI format)
    return result


@app.get("/v1/models")
async def openai_models():
    """OpenAI-compatible model listing for TTS/STT."""
    models = [
        {"id": "tts-1", "object": "model", "owned_by": "vocarium",
         "description": "OmniVoice zero-shot clones (GPU)"},
        {"id": "tts-1-hd", "object": "model", "owned_by": "vocarium",
         "description": "OmniVoice zero-shot clones (GPU)"},
        {"id": "whisper-1", "object": "model", "owned_by": "vocarium",
         "description": "faster-whisper large-v3 (GPU)"},
    ]
    for model_id, meta in sorted((await _kikiri_models()).items()):
        models.append({
            "id": model_id,
            "object": "model",
            "owned_by": "vocarium",
            "description": f"Kikiri {meta['name']} (CPU)",
        })
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
        # Designed and custom voices belonged to the retired Qwen engine;
        # without it they are unplayable and stay hidden like in /api/voices.
        if r[3] in ("design", "custom") and not QWEN_TTS_ENABLED:
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


# --- Bereichs-Einstellungen -------------------------------------------------

@app.get("/api/settings/prefs/{namespace}")
async def read_settings_prefs(namespace: str, request: Request):
    """Voreinstellungen eines Bereichs lesen."""
    user = get_current_user(request)
    if namespace not in user_settings.SETTINGS_NAMESPACES:
        raise HTTPException(404, f"unknown settings namespace {namespace!r}")
    return {"namespace": namespace, "prefs": user_settings.read(get_db(), user["id"], namespace)}


@app.put("/api/settings/prefs/{namespace}")
async def write_settings_prefs(namespace: str, body: dict, request: Request):
    """Voreinstellungen eines Bereichs schreiben (unbekannte Schlüssel fallen raus)."""
    user = get_current_user(request)
    if namespace not in user_settings.SETTINGS_NAMESPACES:
        raise HTTPException(404, f"unknown settings namespace {namespace!r}")
    prefs = user_settings.write(get_db(), user["id"], namespace, body)
    return {"namespace": namespace, "prefs": prefs}


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


@app.get("/api/llm/providers/{provider_id}/models")
async def list_provider_models(provider_id: str, request: Request) -> dict:
    """Discover models with stored credentials, scoped to the signed-in user."""
    user = get_current_user(request)
    row = get_db().execute(
        "SELECT base_url, api_key FROM llm_providers WHERE id=? AND user_id=?",
        (provider_id, user["id"]),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Provider not found")
    import aiohttp
    headers = {"Authorization": f"Bearer {row[1]}"} if row[1] else {}
    try:
        async with _http_session().get(
            f"{_normalize_http_base_url(row[0])}/models", headers=headers,
            timeout=aiohttp.ClientTimeout(total=25), allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise HTTPException(502, "Provider model catalog unavailable. Check the saved URL and credentials.")
            chunks = []
            size = 0
            while chunk := await response.content.read(65536):
                size += len(chunk)
                if size > 2_000_000:
                    raise HTTPException(502, "Provider model catalog is too large")
                chunks.append(chunk)
            data = json.loads(b"".join(chunks))
            models = sorted({str(item["id"]) for item in data.get("data", [])
                             if isinstance(item, dict) and isinstance(item.get("id"), str) and 0 < len(item["id"]) <= 200})
            if not models:
                raise HTTPException(502, "Provider returned no models; the current selection is unchanged.")
            return {"models": models, "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    except HTTPException:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
        raise HTTPException(502, "Provider model catalog unavailable; the current selection is unchanged.")


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
