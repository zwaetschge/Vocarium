"""Opt-in F5-TTS German worker for Vocarium.

This service intentionally mirrors the small subset of the Qwen TTS worker
contract that Vocarium needs for clone benchmarking. It keeps model loading
lazy so /health remains useful before large checkpoints are downloaded.
"""

import gc
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel


DEVICE = os.environ.get("DEVICE", "cuda")
F5_MODEL_REPO = os.environ.get("F5_MODEL_REPO", "hvoss-techfak/F5-TTS-German")
F5_MODEL_NAME = os.environ.get("F5_MODEL_NAME", "F5TTS_Base")
F5_CKPT_FILE = os.environ.get(
    "F5_CKPT_FILE",
    "hf://hvoss-techfak/F5-TTS-German/model_f5tts_german.pt",
)
F5_VOCAB_FILE = os.environ.get(
    "F5_VOCAB_FILE",
    "hf://hvoss-techfak/F5-TTS-German/vocab.txt",
)
F5_PACKAGE_HINT = os.environ.get("F5_PACKAGE_HINT", "pin if German checkpoint requires it")
VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/app/models"))
MAX_VOICE_UPLOAD_BYTES = int(os.environ.get("MAX_VOICE_UPLOAD_BYTES", str(50 * 1024 * 1024)))
DEFAULT_SEED = int(os.environ.get("F5_DEFAULT_SEED", "12345"))
NFE_STEP = int(os.environ.get("F5_NFE_STEP", "32"))
CFG_STRENGTH = float(os.environ.get("F5_CFG_STRENGTH", "2.0"))
SPEED = float(os.environ.get("F5_SPEED", "1.0"))

os.environ.setdefault("HF_HOME", str(MODELS_DIR / "huggingface"))
VOICES_DIR.mkdir(parents=True, exist_ok=True)
MODELS_DIR.mkdir(parents=True, exist_ok=True)

SUPPORTED_OUTPUT_FORMATS = {"wav", "mp3", "flac", "opus", "aac", "pcm"}
VOICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MODEL_WEIGHT_HINTS = [
    {
        "name": F5_MODEL_REPO,
        "approx_size": "varies by checkpoint and vocoder",
        "purpose": "German F5-TTS voice-cloning checkpoint",
    },
    {
        "name": "F5-TTS package assets",
        "approx_size": "additional vocoder/tokenizer downloads possible",
        "purpose": "inference runtime",
    },
]

app = FastAPI(title="Vocarium F5-TTS German Worker")
model_lock = threading.Lock()
inference_lock = threading.RLock()
f5_model = None
model_loaded = False
model_loading = False
model_load_started_at: float | None = None
last_load_error: str | None = None


class SpeechRequest(BaseModel):
    input: str | None = None
    text: str | None = None
    voice: str = "default"
    response_format: str = "wav"
    model: str = "f5-german"
    engine: str | None = None
    seed: int | None = None


def _voice_dir_for(voice_id: str) -> Path:
    cleaned = (voice_id or "").strip()
    if not VOICE_ID_RE.fullmatch(cleaned):
        raise HTTPException(400, "Invalid voice_id")
    root = VOICES_DIR.resolve()
    target = (root / cleaned).resolve()
    if target == root or root not in target.parents:
        raise HTTPException(400, "Invalid voice_id")
    return target


def _read_voice_meta(voice_id: str) -> dict:
    voice_dir = _voice_dir_for(voice_id)
    meta_file = voice_dir / "metadata.json"
    ref_audio = voice_dir / "ref_audio.wav"
    if not meta_file.exists() or not ref_audio.exists():
        raise HTTPException(404, f"Voice {voice_id!r} has no reference audio")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    return {
        "voice_id": voice_id,
        "ref_audio": str(ref_audio),
        "ref_text": (meta.get("ref_text") or "").strip(),
        "name": meta.get("name") or voice_id,
        "language": meta.get("language") or "German",
        "source": meta.get("source") or "clone",
    }


def _list_voice_refs() -> list[dict]:
    voices = []
    for voice_dir in sorted(VOICES_DIR.iterdir()):
        if not voice_dir.is_dir() or not VOICE_ID_RE.fullmatch(voice_dir.name):
            continue
        meta_file = voice_dir / "metadata.json"
        ref_audio = voice_dir / "ref_audio.wav"
        if not meta_file.exists() or not ref_audio.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        voices.append({
            "voice_id": voice_dir.name,
            "name": meta.get("name") or voice_dir.name,
            "language": meta.get("language") or "German",
            "source": meta.get("source") or "clone",
            "has_audio": True,
        })
    return voices


def _validate_response_format(fmt: str) -> str:
    value = (fmt or "wav").strip().lower()
    if value not in SUPPORTED_OUTPUT_FORMATS:
        raise HTTPException(
            400,
            f"response_format must be one of: {', '.join(sorted(SUPPORTED_OUTPUT_FORMATS))}",
        )
    return value


async def _read_upload_limited(upload: UploadFile, max_bytes: int, label: str) -> bytes:
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        mb = max(1, max_bytes // (1024 * 1024))
        raise HTTPException(413, f"{label} too large (max {mb} MB)")
    return data


def _convert_audio_bytes_to_wav(audio_bytes: bytes, audio_path: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", "pipe:0",
            "-ar", "24000", "-ac", "1", "-acodec", "pcm_s16le",
            str(audio_path),
        ],
        input=audio_bytes,
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[:500])


def _audio_to_format(audio: np.ndarray, sr: int, fmt: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if fmt == "flac":
        sf.write(buf, audio, sr, format="FLAC")
        buf.seek(0)
        return buf.read(), "audio/flac"
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    if fmt in ("wav", "pcm"):
        return buf.read(), "audio/wav"
    ffmpeg_fmt = {"mp3": "mp3", "opus": "opus", "aac": "adts"}.get(fmt)
    result = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-f", ffmpeg_fmt, "pipe:1"],
        input=buf.read(),
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[:500])
    return result.stdout, {"mp3": "audio/mpeg", "opus": "audio/opus", "aac": "audio/aac"}[fmt]


def _resolve_hf_path(path: str) -> str:
    value = (path or "").strip()
    if not value.startswith("hf://"):
        return value
    try:
        from huggingface_hub import hf_hub_download
    except Exception as exc:
        raise RuntimeError(
            "huggingface_hub is required to resolve hf:// model paths."
        ) from exc
    remainder = value.removeprefix("hf://").strip("/")
    parts = remainder.split("/", 2)
    if len(parts) != 3 or not all(parts):
        raise RuntimeError(f"Invalid hf:// path: {value}")
    repo_id = f"{parts[0]}/{parts[1]}"
    filename = parts[2]
    return hf_hub_download(repo_id=repo_id, filename=filename, cache_dir=str(MODELS_DIR))


def _load_f5_model():
    global f5_model, last_load_error, model_loaded, model_loading, model_load_started_at
    with model_lock:
        if f5_model is not None:
            return f5_model
        model_loading = True
        model_load_started_at = time.time()
        last_load_error = None
        try:
            try:
                from f5_tts.api import F5TTS
            except Exception as exc:
                raise RuntimeError(
                    "F5-TTS dependencies are not installed. Build the f5-tts "
                    "Docker profile or install the f5-tts package in this worker."
                ) from exc
            f5_model = F5TTS(
                model=F5_MODEL_NAME,
                ckpt_file=_resolve_hf_path(F5_CKPT_FILE),
                vocab_file=_resolve_hf_path(F5_VOCAB_FILE),
                device=DEVICE,
            )
            model_loaded = True
            return f5_model
        except Exception as exc:
            last_load_error = str(exc)
            model_loaded = False
            raise
        finally:
            model_loading = False


def _infer_f5(
    text: str,
    ref_audio: str,
    ref_text: str,
    seed: int,
) -> tuple[np.ndarray, int]:
    model = _load_f5_model()
    with inference_lock:
        if hasattr(model, "infer"):
            wav, sr, _ = model.infer(
                ref_file=ref_audio,
                ref_text=ref_text,
                gen_text=text,
                nfe_step=NFE_STEP,
                cfg_strength=CFG_STRENGTH,
                speed=SPEED,
                seed=seed,
                show_info=lambda *_args, **_kwargs: None,
                progress=None,
            )
            return np.asarray(wav, dtype=np.float32), int(sr)
        if hasattr(model, "generate"):
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                model.generate(
                    text=text,
                    ref_audio=ref_audio,
                    ref_text=ref_text,
                    output_path=tmp_path,
                )
                wav, sr = sf.read(tmp_path, dtype="float32", always_2d=False)
                return np.asarray(wav, dtype=np.float32), int(sr)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
    raise RuntimeError("Installed F5-TTS package exposes neither infer() nor generate()")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "engine": "f5",
        "model": "f5-german",
        "model_repo": F5_MODEL_REPO,
        "model_name": F5_MODEL_NAME,
        "ckpt_file": F5_CKPT_FILE,
        "vocab_file": F5_VOCAB_FILE,
        "package_hint": F5_PACKAGE_HINT,
        "device": DEVICE,
        "model_loaded": model_loaded,
        "model_loading": model_loading,
        "model_load_started_at": model_load_started_at,
        "last_load_error": last_load_error,
        "voices_loaded": [voice["voice_id"] for voice in _list_voice_refs()],
        "first_load": {
            "may_download": not model_loaded,
            "model_weight_hints": MODEL_WEIGHT_HINTS,
        },
    }


@app.post("/unload")
async def unload():
    global f5_model, model_loaded, model_load_started_at
    was_loaded = f5_model is not None
    with model_lock:
        f5_model = None
        model_loaded = False
        model_load_started_at = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return {"status": "unloaded", "was_loaded": was_loaded, "model_id": "f5-german"}


@app.get("/v1/models")
async def list_models():
    return {
        "models": [{
            "id": "f5-german",
            "path": F5_MODEL_REPO,
            "type": "clone",
            "params": "F5",
            "loaded": model_loaded,
        }]
    }


@app.get("/v1/voices")
async def list_voices():
    return {"voices": _list_voice_refs()}


@app.post("/v1/voices/register")
async def register_voice(
    voice_id: str = Form(...),
    ref_text: str = Form(...),
    language: str = Form("German"),
    name: str = Form(""),
    ref_audio: UploadFile = File(...),
):
    voice_dir = _voice_dir_for(voice_id)
    voice_dir.mkdir(parents=True, exist_ok=True)
    audio_path = voice_dir / "ref_audio.wav"
    audio_bytes = await _read_upload_limited(
        ref_audio, MAX_VOICE_UPLOAD_BYTES, "Reference audio"
    )
    try:
        _convert_audio_bytes_to_wav(audio_bytes, audio_path)
    except Exception as exc:
        shutil.rmtree(voice_dir, ignore_errors=True)
        raise HTTPException(400, f"Audio conversion failed: {exc}") from exc
    meta = {
        "name": name or voice_id,
        "ref_text": (ref_text or "").strip(),
        "language": language or "German",
        "source": "clone",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (voice_dir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"status": "registered", "voice_id": voice_id, "prompt_loaded": True}


@app.delete("/v1/voices/{voice_id}")
async def delete_voice(voice_id: str):
    shutil.rmtree(_voice_dir_for(voice_id), ignore_errors=True)
    return {"status": "deleted", "voice_id": voice_id}


@app.post("/v1/audio/speech")
async def create_speech(req: SpeechRequest):
    text = (req.input or req.text or "").strip()
    if not text:
        raise HTTPException(400, "input is required")
    fmt = _validate_response_format(req.response_format)
    voice = (req.voice or "default").strip()
    meta = _read_voice_meta(voice)
    if not meta["ref_text"]:
        raise HTTPException(400, f"Voice {voice!r} has no reference text")
    seed = req.seed if req.seed is not None else DEFAULT_SEED
    t0 = time.perf_counter()
    try:
        audio, sr = _infer_f5(text, meta["ref_audio"], meta["ref_text"], seed)
        body, media_type = _audio_to_format(audio, sr, fmt)
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc)
        status = 503 if "F5-TTS dependencies are not installed" in message else 500
        raise HTTPException(status, message) from exc
    duration = float(len(audio) / sr) if sr else 0.0
    elapsed = time.perf_counter() - t0
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "X-TTS-Engine": "f5",
            "X-Model": "f5-german",
            "X-Voice": voice,
            "X-Audio-Duration": f"{duration:.3f}",
            "X-Generation-Time": f"{elapsed:.3f}",
            "X-RTF": f"{(elapsed / duration):.3f}" if duration > 0 else "0",
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8885)
