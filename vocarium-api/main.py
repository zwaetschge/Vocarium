"""Vocarium API Gateway.

Central voice management API. Stores voice metadata in SQLite,
proxies generation requests to Qwen3-TTS, and orchestrates
cloning / design / benchmark workflows.
"""

import asyncio
import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Response, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import init_db, get_db, get_or_create_user, backfill_hosts_for_all_users
from gpu_queue import gpu_queue, register_unloaders
from podcast.routes import create_podcast_router

TTS_URL = os.environ.get("TTS_URL", "http://qwen3-tts:8880")
TTS_URL_2 = os.environ.get("TTS_URL_2", "http://qwen3-tts-2:8880")
ASR_URL = os.environ.get("ASR_URL", "http://qwen3-asr:8000")
MUSIC_URL = os.environ.get("MUSIC_URL", "http://acestep:8003")
SFX_URL = os.environ.get("SFX_URL", "http://mmaudio:8004")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))

# `true` allows OpenAI-style endpoints to fall back to a shared "api" user
# when no Remote-User header is present. Disable for multi-user deployments.
ALLOW_ANONYMOUS = os.environ.get("ALLOW_ANONYMOUS", "true").lower() in ("1", "true", "yes")

# Comma-separated list, or "*" for all (only safe in dev). Set per-deployment.
_cors = os.environ.get("CORS_ORIGINS", "*").strip()
CORS_ORIGINS = ["*"] if _cors == "*" else [o.strip() for o in _cors.split(",") if o.strip()]

DATA_DIR.mkdir(parents=True, exist_ok=True)
VOICES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Vocarium API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Audio-Duration", "X-Generation-Time", "X-RTF", "X-Model", "X-Voice", "X-Chunks"],
)


# ---------------------------------------------------------------------------
# Auth: forward-auth via Remote-User / X-Forwarded-User header
# ---------------------------------------------------------------------------
def get_current_user(request: Request, allow_anonymous: bool = False) -> dict:
    """Extract authenticated user from the upstream identity proxy.

    Reads ``Remote-User`` (Authelia) or ``X-Forwarded-User`` (oauth2-proxy etc.).
    Auto-creates the user on first login. When ``allow_anonymous=True`` AND
    the global ``ALLOW_ANONYMOUS`` flag is set, requests without a header are
    routed to a shared ``api`` user — convenient for single-user OpenAI-style
    integrations (e.g. OpenWebUI). Disable in any multi-user deployment by
    setting ``ALLOW_ANONYMOUS=false``.
    """
    username = (
        request.headers.get("Remote-User")
        or request.headers.get("X-Forwarded-User")
        or ""
    ).strip()
    if not username and allow_anonymous and ALLOW_ANONYMOUS:
        return get_or_create_user("api")
    if not username:
        raise HTTPException(401, "Not authenticated — Remote-User header missing")
    return get_or_create_user(username)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _unload_music():
    """Tell ACE-Step proxy to unload backend, freeing GPU 1 VRAM."""
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{MUSIC_URL}/unload") as resp:
                if resp.status < 400:
                    data = await resp.json()
                    if data.get("was_running"):
                        print("Unloaded ACE-Step to free GPU 1 for SFX", flush=True)
    except Exception:
        pass


async def _unload_sfx():
    """Tell MMAudio to unload model, freeing GPU 1 VRAM."""
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{SFX_URL}/unload") as resp:
                if resp.status < 400:
                    data = await resp.json()
                    if data.get("was_loaded"):
                        print("Unloaded MMAudio to free GPU 1 for music", flush=True)
    except Exception:
        pass


async def tts_request(method: str, path: str, *, url: str | None = None, **kwargs) -> tuple[int, dict, bytes]:
    target = url or TTS_URL
    timeout = aiohttp.ClientTimeout(total=600, sock_connect=30, sock_read=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, f"{target}{path}", **kwargs) as resp:
            body = await resp.read()
            # Normalize header keys to exact case for X- headers
            headers = {}
            for k, v in resp.headers.items():
                headers[k] = v
            return resp.status, headers, body


async def tts_json(method: str, path: str, **kwargs) -> dict:
    status, headers, body = await tts_request(method, path, **kwargs)
    if status >= 400:
        detail = body.decode(errors="replace")
        raise HTTPException(status, detail=detail)
    return json.loads(body)


async def _run_tts_job(description: str, work_maker):
    """Run a TTS job on the GPU TTS service via the shared GPU queue.

    work_maker(tts_url) → coroutine that performs the actual TTS work.
    """
    async def work():
        return await work_maker(TTS_URL)

    _, future = await gpu_queue.submit("tts", description, work)
    return await future


# ---------------------------------------------------------------------------
# Podcast router — mounted here so it can capture get_current_user/gpu_queue
# ---------------------------------------------------------------------------
podcast_router, audio_assembler = create_podcast_router(
    get_current_user=get_current_user,
    tts_url=TTS_URL,
    extra_tts_urls=[TTS_URL_2],
    db_getter=get_db,
    gpu_submit=gpu_queue.submit,
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
async def queue_status():
    """Return current GPU queue state."""
    return gpu_queue.get_status()


@app.get("/api/queue/status/{job_id}")
async def queue_job_status(job_id: str):
    """Return status of a specific queued job."""
    info = gpu_queue.get_job_status(job_id)
    if not info:
        raise HTTPException(404, "Job not found")
    return info


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    init_db(DATA_DIR / "vocarium.db")
    # Backfill preset hosts for existing users (idempotent)
    backfill = backfill_hosts_for_all_users()
    if backfill:
        print(f"Backfilled {backfill} preset host(s)", flush=True)
    # Sync voices from TTS filesystem into SQLite if needed
    await _sync_voices_from_tts()
    # Start GPU queue with unload callbacks
    # TTS+ASR stay loaded on GPU 0 (dedicated). Only music/sfx on GPU 1 need unloading.
    register_unloaders({
        "music": _unload_music,
        "sfx": _unload_sfx,
    })
    gpu_queue.start()
    try:
        cleaned = await audio_assembler.clean_stale_jobs()
        if cleaned:
            print(f"Cleaned {cleaned} stale podcast assembly job(s)", flush=True)
    except Exception as exc:
        print(f"Podcast cleanup skipped: {exc}", flush=True)
    print("Vocarium API ready (GPU queue active)", flush=True)


async def _sync_voices_from_tts():
    """Ensure all voices in the shared volume are tracked in SQLite."""
    db = get_db()
    existing_ids = {row[0] for row in db.execute("SELECT id FROM voices").fetchall()}
    for voice_dir in sorted(VOICES_DIR.iterdir()):
        if not voice_dir.is_dir():
            continue
        vid = voice_dir.name
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
async def list_models():
    return await tts_json("GET", "/v1/models")


@app.get("/api/models/current")
async def current_model():
    return await tts_json("GET", "/v1/models/current")


class SwitchModelRequest(BaseModel):
    model_id: str


@app.post("/api/models/switch")
async def switch_model(req: SwitchModelRequest):
    return await tts_json("POST", "/v1/models/load", json={"model_id": req.model_id})


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
            "has_audio": (VOICES_DIR / r[0] / "ref_audio.wav").exists(),
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
        "has_audio": (VOICES_DIR / r[0] / "ref_audio.wav").exists(),
    }


@app.delete("/api/voices/{voice_id}")
async def delete_voice(voice_id: str, request: Request):
    user = get_current_user(request)
    db = get_db()
    r = db.execute("SELECT id FROM voices WHERE id=? AND user_id=?", (voice_id, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Voice not found")
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
    audio_path = VOICES_DIR / voice_id / "ref_audio.wav"
    if not audio_path.exists():
        raise HTTPException(404, "Audio not found")
    return Response(content=audio_path.read_bytes(), media_type="audio/wav")


# ---------------------------------------------------------------------------
# Voice Registration helper
# ---------------------------------------------------------------------------
async def _register_voice_on_tts(
    voice_id: str, name: str, language: str, ref_text: str,
    audio_bytes: bytes, *, endpoint: str = "/v1/voices/register",
    design_prompt: str | None = None,
):
    """Register a voice on the TTS service."""
    try:
        form = aiohttp.FormData()
        form.add_field("voice_id", voice_id)
        form.add_field("ref_text", ref_text)
        form.add_field("language", language)
        form.add_field("name", name)
        if design_prompt:
            form.add_field("design_prompt", design_prompt)
        form.add_field("ref_audio", audio_bytes,
                       filename="ref_audio.wav", content_type="audio/wav")
        status, _, body = await tts_request("POST", endpoint, url=TTS_URL, data=form)
        if status >= 400:
            print(f"TTS registration warning: {body.decode()}", flush=True)
    except Exception as e:
        print(f"TTS registration failed: {e}", flush=True)


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
    voice_id = str(uuid.uuid4())[:8]
    voice_dir = VOICES_DIR / voice_id
    voice_dir.mkdir(parents=True, exist_ok=True)

    audio_bytes = await ref_audio.read()
    audio_path = voice_dir / "ref_audio.wav"

    # Convert to WAV
    if ref_audio.filename and not ref_audio.filename.lower().endswith(".wav"):
        import subprocess
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0", "-ar", "24000", "-ac", "1", str(audio_path)],
            input=audio_bytes, capture_output=True,
        )
        if result.returncode != 0:
            raise HTTPException(500, f"Audio conversion failed")
    else:
        audio_path.write_bytes(audio_bytes)

    # Auto-transcribe if no ref_text provided (always attempt as fallback)
    if not ref_text.strip():
        try:
            ref_text = await _transcribe_audio(audio_path)
        except Exception as e:
            print(f"Auto-transcription failed: {e}", flush=True)
            ref_text = ""

    if not ref_text.strip():
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
    (voice_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

    # Register on all TTS instances (GPU 0 + GPU 1 fallback)
    await _register_voice_on_tts(
        voice_id, name, language, ref_text, audio_path.read_bytes(),
    )

    # Save to DB
    db = get_db()
    db.execute(
        "INSERT INTO voices (id, user_id, name, language, source, ref_text, created_at) VALUES (?,?,?,?,?,?,?)",
        (voice_id, user["id"], name, language, "clone", ref_text, meta["created_at"]),
    )
    db.commit()

    return {"status": "created", "voice_id": voice_id, "name": name}


async def _transcribe_audio(audio_path: Path) -> str:
    """Send audio to ASR for transcription via GPU queue."""
    audio_bytes = audio_path.read_bytes()

    async def work():
        form = aiohttp.FormData()
        form.add_field("file", audio_bytes, filename="audio.wav", content_type="audio/wav")
        form.add_field("model", "Qwen/Qwen3-ASR-0.6B")
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{ASR_URL}/v1/audio/transcriptions", data=form) as resp:
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

    async def work_maker(tts_url):
        status, headers, audio_bytes = await tts_request(
            "POST", "/v1/audio/design", url=tts_url,
            json={"text": req.text, "description": req.description,
                  "language": req.language, "response_format": "wav"},
        )
        if status >= 400:
            raise HTTPException(status, audio_bytes.decode(errors="replace"))

        voice_id = str(uuid.uuid4())[:8]
        voice_dir = VOICES_DIR / voice_id
        voice_dir.mkdir(parents=True, exist_ok=True)

        audio_path = voice_dir / "ref_audio.wav"
        audio_path.write_bytes(audio_bytes)

        meta = {
            "name": req.name,
            "ref_text": req.text,
            "language": req.language,
            "source": "design",
            "design_prompt": req.description,
            "user_id": user["id"],
            "username": user["username"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (voice_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

        # Register on both TTS instances so either GPU can use this voice
        await _register_voice_on_tts(
            voice_id, req.name, req.language, req.text, audio_bytes,
            endpoint="/v1/voices/register-designed", design_prompt=req.description,
        )

        db = get_db()
        db.execute(
            "INSERT INTO voices (id, user_id, name, language, source, design_prompt, ref_text, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (voice_id, user["id"], req.name, req.language, "design", req.description, req.text, meta["created_at"]),
        )
        db.commit()
        return {"status": "created", "voice_id": voice_id, "name": req.name}

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
        (voice_id, user["id"], req.name, req.language, "custom",
         req.speaker, instruct, created_at),
    )
    db.commit()

    return {
        "status": "created", "voice_id": voice_id, "name": req.name,
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
    preset = _resolve_custom_voice(req.voice, user_id=user["id"])

    async def work_maker(tts_url):
        status, headers, body = await tts_request(
            "POST", "/v1/audio/speech/custom", url=tts_url,
            json={
                "text": req.input,
                "speaker": preset["speaker"],
                "language": preset["language"],
                "instruct": preset["instruct"] or None,
                "response_format": req.response_format,
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
    language: str | None = None
    response_format: str = "wav"


def _verify_voice_exists(voice_id: str, user_id: int | None = None):
    """Ensure voice exists (or is a built-in like 'default'). If ``user_id``
    is provided, scopes the lookup to that user's voices."""
    if voice_id in ("default", ""):
        return
    db = get_db()
    if user_id is not None:
        r = db.execute("SELECT id FROM voices WHERE id=? AND user_id=?", (voice_id, user_id)).fetchone()
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
        r = db.execute("SELECT source FROM voices WHERE id=? AND user_id=?", (voice_id, user_id)).fetchone()
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


@app.post("/api/generate/stream")
async def generate_speech_stream(req: GenerateRequest, request: Request):
    """Stream speech generation via SSE — queued for GPU access, buffered then streamed."""
    user = get_current_user(request)
    _verify_voice_exists(req.voice_id, user_id=user["id"])
    payload = {
        "input": req.text,
        "voice": req.voice_id,
        "response_format": "wav",
    }
    if req.model_id:
        payload["model_id"] = req.model_id
    if req.language:
        payload["language"] = req.language

    async def work_maker(tts_url):
        """Buffer entire SSE response inside queue to hold GPU lock."""
        events = []
        timeout = aiohttp.ClientTimeout(total=600, sock_connect=30, sock_read=600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{tts_url}/v1/audio/speech/stream", json=payload
            ) as resp:
                if resp.status >= 400:
                    body = await resp.read()
                    error_msg = json.dumps({"error": body.decode(errors="replace")})
                    events.append(f"event: error\ndata: {error_msg}\n\n")
                    return events
                buf = b""
                async for chunk in resp.content.iter_any():
                    buf += chunk
                    while b"\n\n" in buf:
                        event, buf = buf.split(b"\n\n", 1)
                        events.append(event.decode(errors="replace") + "\n\n")
                if buf.strip():
                    events.append(buf.decode(errors="replace") + "\n\n")
        return events

    events = await _run_tts_job("TTS Stream", work_maker)

    async def replay_sse():
        for ev in events:
            yield ev

    return StreamingResponse(replay_sse(), media_type="text/event-stream")


@app.post("/api/generate")
async def generate_speech(req: GenerateRequest, request: Request):
    """Generate speech using a stored voice."""
    user = get_current_user(request)
    _verify_voice_exists(req.voice_id, user_id=user["id"])
    payload = {
        "input": req.text,
        "voice": req.voice_id,
        "response_format": req.response_format,
    }
    if req.model_id:
        payload["model_id"] = req.model_id
    if req.language:
        payload["language"] = req.language

    async def work_maker(tts_url):
        status, headers, body = await tts_request("POST", "/v1/audio/speech", url=tts_url, json=payload)
        if status >= 400:
            raise HTTPException(status, body.decode(errors="replace"))
        resp_headers = {k: v for k, v in headers.items() if k.lower().startswith("x-")}
        content_type = headers.get("Content-Type", headers.get("content-type", "audio/wav"))
        return {"body": body, "media_type": content_type, "headers": resp_headers}

    result = await _run_tts_job("TTS Generate", work_maker)
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
    get_current_user(request)  # auth check

    async def work():
        results = []
        db = get_db()
        for model_id in req.model_ids:
            try:
                switch_result = await tts_json("POST", "/v1/models/load", json={"model_id": model_id})
                load_time = switch_result.get("load_time_s", 0)
            except Exception as e:
                results.append({"model_id": model_id, "error": str(e)})
                continue

            for voice_id in req.voice_ids:
                for run_idx in range(req.runs_per_combo):
                    try:
                        status, headers, body = await tts_request(
                            "POST", "/v1/audio/speech",
                            json={"input": req.text, "voice": voice_id,
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
                            "INSERT INTO benchmarks (voice_id, model_id, text, audio_duration, generation_time, rtf) "
                            "VALUES (?,?,?,?,?,?)",
                            (voice_id, model_id, req.text, audio_dur, gen_time, rtf),
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
async def get_benchmark_results(limit: int = 50):
    db = get_db()
    rows = db.execute(
        "SELECT id, voice_id, model_id, text, audio_duration, generation_time, rtf, created_at "
        "FROM benchmarks ORDER BY created_at DESC LIMIT ?", (limit,)
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


def _download_youtube(url: str) -> bytes:
    """Download audio from YouTube URL as WAV (sync, for run_in_executor)."""
    import subprocess
    import shutil
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    out_template = os.path.join(tmp_dir, "audio.%(ext)s")
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-playlist", "-x", "--audio-format", "wav",
             "--audio-quality", "0", "--postprocessor-args",
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
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{ASR_URL}/v1/audio/transcriptions", data=form) as resp:
            if resp.status >= 400:
                body = await resp.read()
                raise RuntimeError(f"ASR error {resp.status}: {body.decode(errors='replace')[:500]}")
            data = await resp.json()
            return {"text": data.get("text", "").strip()}


@app.post("/api/transcribe")
async def transcribe(
    request: Request,
    file: UploadFile | None = File(None),
    url: str = Form(""),
):
    """Transcribe audio/video files or YouTube URLs."""
    get_current_user(request)  # auth check
    loop = asyncio.get_event_loop()

    if url.strip():
        try:
            wav_bytes = await loop.run_in_executor(None, _download_youtube, url.strip())
        except Exception as e:
            raise HTTPException(400, f"Download failed: {e}")

    elif file:
        raw = await file.read()
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
    return {"api": "ok", "tts": tts_health}


# ---------------------------------------------------------------------------
# Music Generation (ACE-Step)
# ---------------------------------------------------------------------------
class MusicGenerateRequest(BaseModel):
    prompt: str
    lyrics: str = ""
    audio_duration: int = 60
    bpm: int | None = None
    key_scale: str | None = None
    time_signature: str | None = None
    thinking: bool = True
    audio_format: str = "wav"
    batch_size: int = 1
    seed: int | None = None


async def _music_request(method: str, path: str, **kwargs) -> tuple[int, bytes]:
    timeout = aiohttp.ClientTimeout(total=120, sock_connect=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, f"{MUSIC_URL}{path}", **kwargs) as resp:
            body = await resp.read()
            return resp.status, body


@app.post("/api/music/generate")
async def music_generate(req: MusicGenerateRequest, request: Request):
    """Submit music generation, hold GPU lock until complete, return result."""
    get_current_user(request)  # auth check

    payload = {
        "prompt": req.prompt,
        "lyrics": req.lyrics,
        "audio_duration": req.audio_duration,
        "thinking": req.thinking,
        "model": "acestep-v15-turbo",
        "inference_steps": 8,
        "batch_size": req.batch_size,
        "audio_format": req.audio_format,
    }
    if req.bpm is not None:
        payload["bpm"] = req.bpm
    if req.key_scale is not None:
        payload["key_scale"] = req.key_scale
    if req.time_signature is not None:
        payload["time_signature"] = req.time_signature
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
            return submit_result  # no task_id means immediate result or error

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
                return {"submit": submit_result, "result": poll}
            if task.get("status") == 2:  # failed
                raise HTTPException(500, "Music generation failed")
        raise HTTPException(504, "Music generation timed out")

    _, future = await gpu_queue.submit("music", "Music Generate", work)
    return await future


class MusicStatusRequest(BaseModel):
    task_ids: list[str]


@app.post("/api/music/status")
async def music_status(req: MusicStatusRequest, request: Request):
    """Poll for music generation task status."""
    get_current_user(request)  # auth check
    status, body = await _music_request(
        "POST", "/query_result",
        json={"task_id_list": req.task_ids},
    )
    if status >= 400:
        raise HTTPException(status, body.decode(errors="replace"))
    return json.loads(body)


@app.get("/api/music/audio")
async def music_audio(path: str, request: Request):
    """Download generated music audio file."""
    get_current_user(request)  # auth check
    status, body = await _music_request("GET", f"/v1/audio?path={path}")
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
    timeout = aiohttp.ClientTimeout(total=300, sock_connect=30, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, f"{SFX_URL}{path}", **kwargs) as resp:
            body = await resp.read()
            return resp.status, body


@app.post("/api/sfx/generate")
async def sfx_generate(request: Request):
    """Generate a sound effect from a text prompt. Returns WAV audio."""
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")

    sfx_params = {
        "prompt": prompt,
        "negative_prompt": body.get("negative_prompt", ""),
        "duration": body.get("duration", 8.0),
        "cfg_strength": body.get("cfg_strength", 4.5),
        "num_steps": body.get("num_steps", 25),
        "seed": body.get("seed"),
    }

    async def work():
        status, resp_body = await _sfx_request("POST", "/generate", json=sfx_params)
        if status >= 400:
            raise HTTPException(status, detail=resp_body.decode(errors="replace"))
        return resp_body

    _, future = await gpu_queue.submit("sfx", "SFX Generate", work)
    wav_bytes = await future
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=sfx.wav"},
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
    speed: float = 1.0  # ignored, kept for compat


async def _openai_speech_proxy(req: OpenAISpeechRequest, description: str) -> Response:
    """Shared TTS proxy used by both /v1/audio/speech endpoints."""
    payload = {
        "input": req.input,
        "voice": req.voice,
        "response_format": req.response_format,
    }
    # Map OpenAI model names to internal model_id
    model_map = {"tts-1": "1.7b-base", "tts-1-hd": "1.7b-base"}
    if req.model in model_map:
        payload["model_id"] = model_map[req.model]
    elif req.model not in ("qwen3-tts", ""):
        payload["model_id"] = req.model

    async def work_maker(tts_url):
        status, headers, body = await tts_request("POST", "/v1/audio/speech", url=tts_url, json=payload)
        if status >= 400:
            raise HTTPException(status, body.decode(errors="replace"))
        content_type = headers.get("Content-Type", headers.get("content-type", "audio/wav"))
        return {"body": body, "media_type": content_type}

    result = await _run_tts_job(description, work_maker)
    return Response(content=result["body"], media_type=result["media_type"])


@app.post("/v1/audio/speech")
async def openai_tts(req: OpenAISpeechRequest, request: Request):
    """OpenAI-compatible TTS endpoint — base model + cloned voices only.

    This is the endpoint Canto (and other OpenAI-compatible clients) consume.
    Designed voices live behind ``/v1/audio/speech/designed``.
    """
    user = get_current_user(request, allow_anonymous=True)
    _verify_voice_source(req.voice, allowed_sources=("clone",), user_id=user["id"])
    return await _openai_speech_proxy(req, "OpenAI TTS (base/clone)")


@app.post("/v1/audio/speech/designed")
async def openai_tts_designed(req: OpenAISpeechRequest, request: Request):
    """OpenAI-compatible TTS endpoint — designed voices only.

    Exposes the design-model voices (used by the podcast generator) as an
    OpenAI-compatible endpoint. Cloned and default base voices are rejected.
    """
    user = get_current_user(request, allow_anonymous=True)
    _verify_voice_source(req.voice, allowed_sources=("design",), user_id=user["id"])
    return await _openai_speech_proxy(req, "OpenAI TTS (designed)")


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

    raw = await file.read()
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
    return {
        "object": "list",
        "data": [
            {"id": "tts-1", "object": "model", "owned_by": "vocarium",
             "description": "Qwen3-TTS 1.7B (GPU)"},
            {"id": "tts-1-hd", "object": "model", "owned_by": "vocarium",
             "description": "Qwen3-TTS 1.7B (GPU)"},
            {"id": "whisper-1", "object": "model", "owned_by": "vocarium",
             "description": "Qwen3-ASR 0.6B"},
        ],
    }


@app.get("/v1/voices")
async def openai_voices(request: Request, source: str | None = Query(default=None)):
    """List available TTS voices. Optional ``source`` filter (``clone`` | ``design`` )."""
    user = get_current_user(request, allow_anonymous=True)
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
    for r in rows:
        voices.append({
            "voice_id": r[0],
            "name": r[1],
            "language": r[2],
            "source": r[3],
        })
    return {"voices": voices}


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
    return provider


@app.post("/api/llm/providers")
async def create_llm_provider(req: LLMProviderCreate, request: Request):
    """Create a new LLM provider for the authenticated user."""
    user = get_current_user(request)
    provider_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        "INSERT INTO llm_providers (id, user_id, name, base_url, api_key, model, temperature, max_tokens, provider_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (provider_id, user["id"], req.name, req.base_url, req.api_key, req.model, req.temperature, req.max_tokens, req.provider_type),
    )
    db.commit()
    return {"id": provider_id, "name": req.name, "status": "created"}


@app.patch("/api/llm/providers/{provider_id}")
async def update_llm_provider(provider_id: str, req: LLMProviderUpdate, request: Request):
    """Update an LLM provider. Only the owner can update."""
    user = get_current_user(request)
    db = get_db()
    r = db.execute("SELECT id FROM llm_providers WHERE id=? AND user_id=?", (provider_id, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Provider not found")

    fields = []
    values = []
    if req.name is not None:
        fields.append("name=?"); values.append(req.name)
    if req.base_url is not None:
        fields.append("base_url=?"); values.append(req.base_url)
    if req.api_key is not None:
        fields.append("api_key=?"); values.append(req.api_key)
    if req.model is not None:
        fields.append("model=?"); values.append(req.model)
    if req.temperature is not None:
        fields.append("temperature=?"); values.append(req.temperature)
    if req.max_tokens is not None:
        fields.append("max_tokens=?"); values.append(req.max_tokens)
    if req.is_active is not None:
        fields.append("is_active=?"); values.append(1 if req.is_active else 0)
    if req.provider_type is not None:
        fields.append("provider_type=?"); values.append(req.provider_type)

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
    r = db.execute("SELECT id FROM llm_providers WHERE id=? AND user_id=?", (provider_id, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Provider not found")
    db.execute("DELETE FROM llm_providers WHERE id=? AND user_id=?", (provider_id, user["id"]))
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
    db.execute("UPDATE llm_providers SET is_active=1, updated_at=? WHERE id=?", (time.strftime("%Y-%m-%dT%H:%M:%SZ"), provider_id))
    db.commit()
    return {"status": "active", "provider_id": provider_id}


@app.post("/api/llm/test")
async def test_llm_provider(request: Request, body: dict):
    """Test an LLM provider configuration by sending a simple completion."""
    user = get_current_user(request)
    base_url = body.get("base_url", "")
    api_key = body.get("api_key", "")
    model = body.get("model", "")
    if not base_url or not model:
        raise HTTPException(400, "base_url and model are required")

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
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
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
