"""Qwen3-TTS Multi-Model API server.

Supports Base (voice cloning), VoiceDesign (description-to-voice), and
CustomVoice (6 prebuilt speakers + optional steering `instruct`).
Dynamically switches between models. Voice prompts are regenerated
from stored reference audio when switching Base models.

Shared /app/voices/ volume stores ref audio + metadata per voice.
"""

import argparse
import asyncio
import base64
import gc
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import torch
DEVICE = os.environ.get("DEVICE", "cuda")
DTYPE = torch.float32 if DEVICE == "cpu" else torch.bfloat16
if DEVICE != "cpu":
    torch.set_float32_matmul_precision("high")

from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/app/models"))
os.environ["HF_HOME"] = str(MODELS_DIR / "huggingface")

from faster_qwen3_tts import FasterQwen3TTS

VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
VOICES_DIR.mkdir(parents=True, exist_ok=True)
MAX_VOICE_UPLOAD_BYTES = int(os.environ.get("MAX_VOICE_UPLOAD_BYTES", str(50 * 1024 * 1024)))

REF_AUDIO_DIR = Path("/app/ref_audio")
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "600"))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "1.7b-base")

AVAILABLE_MODELS = {
    "1.7b-base": {
        "path": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "type": "base",
        "params": "1.7B",
    },
    "1.7b-design": {
        "path": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "type": "design",
        "params": "1.7B",
    },
    "1.7b-custom": {
        "path": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "type": "custom",
        "params": "1.7B",
    },
}

# Prebuilt speakers exposed by the CustomVoice model
BUILTIN_SPEAKERS = [
    {"id": "Vivian",    "name": "Vivian",    "gender": "female", "language_hint": "English"},
    {"id": "Serena",    "name": "Serena",    "gender": "female", "language_hint": "English"},
    {"id": "Uncle_Fu",  "name": "Uncle Fu",  "gender": "male",   "language_hint": "Chinese"},
    {"id": "Dylan",     "name": "Dylan",     "gender": "male",   "language_hint": "English"},
    {"id": "Eric",      "name": "Eric",      "gender": "male",   "language_hint": "English"},
    {"id": "Ryan",      "name": "Ryan",      "gender": "male",   "language_hint": "English"},
    {"id": "Aiden",     "name": "Aiden",     "gender": "male",   "language_hint": "English"},
    {"id": "Ono_Anna",  "name": "Ono Anna",  "gender": "female", "language_hint": "Japanese"},
    {"id": "Sohee",     "name": "Sohee",     "gender": "female", "language_hint": "Korean"},
]
BUILTIN_SPEAKER_IDS = {s["id"] for s in BUILTIN_SPEAKERS}

SUPPORTED_LANGUAGES = [
    "Chinese", "English", "Japanese", "Korean", "German",
    "French", "Russian", "Portuguese", "Spanish", "Italian",
]
SUPPORTED_OUTPUT_FORMATS = {"wav", "mp3", "flac", "opus", "aac", "pcm"}
VOICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized == "auto":
        return default
    return normalized in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _voice_dir_for(voice_id: str) -> Path:
    cleaned = (voice_id or "").strip()
    if not VOICE_ID_RE.fullmatch(cleaned):
        raise HTTPException(400, "Invalid voice_id")
    root = VOICES_DIR.resolve()
    target = (root / cleaned).resolve()
    if target == root or root not in target.parents:
        raise HTTPException(400, "Invalid voice_id")
    return target


async def _read_upload_limited(upload: UploadFile, max_bytes: int, label: str) -> bytes:
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        mb = max(1, max_bytes // (1024 * 1024))
        raise HTTPException(413, f"{label} too large (max {mb} MB)")
    return data


def _default_cuda_graphs_enabled() -> bool:
    if DEVICE == "cpu" or not torch.cuda.is_available():
        return False
    try:
        gpu_name = torch.cuda.get_device_name(0).lower()
    except Exception:
        return False
    # Blackwell / RTX 50xx has shown CUDA graph replay hangs in this stack.
    return not re.search(r"(blackwell|rtx\s*50|5060|5070|5080|5090)", gpu_name)


# The CUDA-graph clone path is the practical default on RTX 30/40 GPUs. The
# important quality fix is non_streaming_mode=True in _generate_voice_clone();
# without it, short German Base/Clone prompts can repeat or stop early.
ENABLE_CUDA_GRAPHS = _env_bool("TTS_ENABLE_CUDA_GRAPHS", _default_cuda_graphs_enabled())
CUDA_GRAPH_MODELS = {"1.7b-base"} if ENABLE_CUDA_GRAPHS else set()

# Clone/base voice identity gets unstable when each sentence is sampled as a
# fresh generation. Keep clone generation deterministic by default; deployments
# that prefer more variation can opt back in via TTS_CLONE_DO_SAMPLE=true.
CLONE_DO_SAMPLE = _env_bool("TTS_CLONE_DO_SAMPLE", False)
CLONE_TEMPERATURE = _env_float("TTS_CLONE_TEMPERATURE", 0.7)
CLONE_TOP_K = _env_int("TTS_CLONE_TOP_K", 20)
CLONE_TOP_P = _env_float("TTS_CLONE_TOP_P", 0.8)
CLONE_XVEC_ONLY = _env_bool("TTS_CLONE_XVEC_ONLY", False)
CUSTOM_DO_SAMPLE = _env_bool("TTS_CUSTOM_DO_SAMPLE", False)
CUSTOM_TEMPERATURE = _env_float("TTS_CUSTOM_TEMPERATURE", 0.7)
CUSTOM_TOP_K = _env_int("TTS_CUSTOM_TOP_K", 20)
CUSTOM_TOP_P = _env_float("TTS_CUSTOM_TOP_P", 0.8)
REF_NORMALIZATION_VERSION = 2
REF_NORMALIZE_PEAK = _env_float("TTS_REF_NORMALIZE_PEAK", 0.85)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
current_model_id: Optional[str] = None
model: Optional[FasterQwen3TTS] = None
use_cuda_graphs: bool = False  # whether current model uses CUDA graph path
voice_prompts: dict = {}       # legacy compat — kept for health endpoint
voice_refs: dict = {}          # {voice_id: {"ref_audio": path, "ref_text": str}}
voice_clone_prompt_cache: dict = {}  # {(model_id, voice_id, xvec_only): prompt}
last_used: float = 0.0
model_lock = threading.Lock()
inference_lock = threading.RLock()
active_requests: int = 0       # concurrent inference counter
idle_timer: Optional[threading.Timer] = None


# ---------------------------------------------------------------------------
# Model lifecycle
# ---------------------------------------------------------------------------
def _do_load_model(model_id: str):
    global model, current_model_id, last_used, use_cuda_graphs

    info = AVAILABLE_MODELS[model_id]
    attn_impl = os.environ.get("ATTN_IMPL", "eager")
    use_cuda_graphs = (DEVICE != "cpu") and (model_id in CUDA_GRAPH_MODELS)
    print(f"Loading model: {info['path']} ({model_id}) attn={attn_impl} "
          f"cuda_graphs={'ON' if use_cuda_graphs else 'OFF'}", flush=True)
    model = FasterQwen3TTS.from_pretrained(
        info["path"],
        device=DEVICE,
        dtype=DTYPE,
    )
    current_model_id = model_id
    last_used = time.time()
    print(f"Model {model_id} loaded ({info['params']} params)", flush=True)


def _do_unload_model():
    global model, current_model_id, last_used
    if model is None:
        return
    print(f"Unloading model {current_model_id} ...", flush=True)
    voice_clone_prompt_cache.clear()
    del model
    model = None
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    current_model_id = None
    print("Model unloaded.", flush=True)


def _trim_silence(audio: np.ndarray, sr: int, threshold: float = 0.03, tail_silence: float = 0.1) -> tuple:
    """Simple energy-based trimming with tail silence."""
    # Find first non-silent frame from start
    energy = np.abs(audio)
    mask = energy > threshold
    start = 0
    if mask.any():
        start = int(np.where(mask)[0][0])
        # Keep a little pre-context
        start = max(0, start - int(sr * 0.05))
    end = len(audio)
    if mask.any():
        end = int(np.where(mask)[0][-1])
        end = min(len(audio), end + int(sr * 0.1))
    audio = audio[start:end]
    silence = np.zeros(int(sr * tail_silence), dtype=np.float32)
    return (np.concatenate([audio, silence]), sr)


def _normalize_reference_wav(audio_path: Path) -> dict:
    """Normalize clone reference audio for stable speaker conditioning."""
    tmp_path = audio_path.with_name(f".{audio_path.stem}.normalize.tmp.wav")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(audio_path),
            "-ar", "24000", "-ac", "1", "-acodec", "pcm_s16le",
            str(tmp_path),
        ],
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.decode(errors="replace")[:500])
    tmp_path.replace(audio_path)

    audio, sr = sf.read(str(audio_path), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)
    audio = np.asarray(audio, dtype=np.float32)
    audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
    if audio.size == 0:
        raise ValueError("reference audio is empty")

    audio = audio - float(np.mean(audio))
    peak_before = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak_before <= 1e-5:
        raise ValueError("reference audio is silent")

    trim_threshold = max(0.004, min(0.03, peak_before * 0.025))
    audio, sr = _trim_silence(audio, sr, threshold=trim_threshold, tail_silence=0.25)
    peak_after_trim = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak_after_trim > 1e-5:
        audio = audio * min(4.0, REF_NORMALIZE_PEAK / peak_after_trim)
    audio = np.clip(audio, -0.98, 0.98).astype(np.float32)
    sf.write(str(audio_path), audio, sr, format="WAV", subtype="PCM_16")
    return {
        "version": REF_NORMALIZATION_VERSION,
        "duration_s": round(float(len(audio) / sr), 3),
        "sample_rate": sr,
        "peak_before": round(peak_before, 5),
        "peak_after": round(float(np.max(np.abs(audio))) if audio.size else 0.0, 5),
    }


def _voice_clone_prompt_for(voice_id: str, ref_audio: str, ref_text: str):
    """Build and cache stable speaker conditioning for the current Base model."""
    if model is None or current_model_id is None:
        raise RuntimeError("Model not ready")
    xvec_only = CLONE_XVEC_ONLY or not (ref_text or "").strip()
    key = (current_model_id, voice_id, xvec_only)
    cached = voice_clone_prompt_cache.get(key)
    if cached is not None:
        return cached
    if not hasattr(model.model, "create_voice_clone_prompt"):
        return None
    prompt = model.model.create_voice_clone_prompt(
        ref_audio=ref_audio,
        ref_text=ref_text,
        x_vector_only_mode=xvec_only,
    )
    voice_clone_prompt_cache[key] = prompt
    return prompt


def _clear_voice_clone_prompt(voice_id: str | None = None) -> None:
    if voice_id is None:
        voice_clone_prompt_cache.clear()
        return
    for key in list(voice_clone_prompt_cache.keys()):
        if key[1] == voice_id:
            voice_clone_prompt_cache.pop(key, None)


def _generate_voice_clone(text: str, language: str, voice_id: str,
                          ref_audio: str, ref_text: str,
                          max_new_tokens: int = 800) -> tuple:
    """Generate voice clone using CUDA graphs (fast) or fallback (compatible)."""
    with inference_lock:
        voice_clone_prompt = _voice_clone_prompt_for(voice_id, ref_audio, ref_text)
        xvec_only = CLONE_XVEC_ONLY or not (ref_text or "").strip()
        if use_cuda_graphs:
            kwargs = {
                "text": text,
                "language": language,
                "max_new_tokens": max_new_tokens,
                "non_streaming_mode": True,
                "temperature": CLONE_TEMPERATURE,
                "top_k": CLONE_TOP_K,
                "top_p": CLONE_TOP_P,
                "do_sample": CLONE_DO_SAMPLE,
                "repetition_penalty": 1.05,
                "xvec_only": xvec_only,
            }
            if voice_clone_prompt is not None:
                kwargs["voice_clone_prompt"] = voice_clone_prompt
            else:
                kwargs["ref_audio"] = ref_audio
                kwargs["ref_text"] = ref_text
            return model.generate_voice_clone(**kwargs)
        kwargs = {
            "text": text,
            "language": language,
            "max_new_tokens": max_new_tokens,
            "non_streaming_mode": True,
            "eos_token_id": [2150, 2157],
            "temperature": CLONE_TEMPERATURE,
            "top_k": CLONE_TOP_K,
            "top_p": CLONE_TOP_P,
            "do_sample": CLONE_DO_SAMPLE,
            "repetition_penalty": 1.05,
            "x_vector_only_mode": xvec_only,
        }
        if voice_clone_prompt is not None:
            kwargs["voice_clone_prompt"] = voice_clone_prompt
        else:
            kwargs["ref_audio"] = ref_audio
            kwargs["ref_text"] = ref_text
        return model.model.generate_voice_clone(**kwargs)


def _sanitize_ref_text(ref_text: str | None) -> str:
    """Remove ASR control prefixes before using text as a clone reference."""
    text = (ref_text or "").strip()
    text = re.sub(r"^\s*language\s+[A-Za-z]+\s*<asr_text>\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*<asr_text>\s*", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _write_metadata(meta_file: Path, meta: dict) -> None:
    meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _convert_audio_bytes_to_wav(audio_bytes: bytes, audio_path: Path) -> dict:
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
    return _normalize_reference_wav(audio_path)


def _normalize_voice_ref_if_needed(voice_dir: Path, meta: dict) -> dict:
    norm = meta.get("ref_audio_normalization") or {}
    if norm.get("version") == REF_NORMALIZATION_VERSION:
        return meta
    ref_audio = voice_dir / "ref_audio.wav"
    stats = _normalize_reference_wav(ref_audio)
    meta["ref_audio_normalization"] = stats
    _write_metadata(voice_dir / "metadata.json", meta)
    print(f"  Normalized ref_audio for voice '{voice_dir.name}'", flush=True)
    return meta


def _load_all_voice_refs():
    """Scan /app/voices/ and index ref audio paths + text for every stored voice."""
    voice_refs.clear()
    voice_prompts.clear()
    if not VOICES_DIR.exists():
        return
    for voice_dir in sorted(VOICES_DIR.iterdir()):
        if not voice_dir.is_dir():
            continue
        if not VOICE_ID_RE.fullmatch(voice_dir.name):
            print(f"  Skipping unsafe voice directory '{voice_dir.name}'", flush=True)
            continue
        ref_audio = voice_dir / "ref_audio.wav"
        meta_file = voice_dir / "metadata.json"
        if not ref_audio.exists() or not meta_file.exists():
            continue
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        ref_text = _sanitize_ref_text(meta.get("ref_text", ""))
        if ref_text != (meta.get("ref_text", "") or ""):
            meta["ref_text"] = ref_text
            _write_metadata(meta_file, meta)
            print(f"  Sanitized ref_text for voice '{voice_dir.name}'", flush=True)
        try:
            meta = _normalize_voice_ref_if_needed(voice_dir, meta)
        except Exception as exc:
            print(f"  Warning: failed to normalize voice '{voice_dir.name}': {exc}", flush=True)
        vid = voice_dir.name
        voice_refs[vid] = {
            "ref_audio": str(ref_audio),
            "ref_text": ref_text,
        }
        voice_prompts[vid] = True  # for health endpoint compat
        print(f"  Indexed voice '{vid}'", flush=True)


def _schedule_unload():
    global idle_timer
    if IDLE_TIMEOUT <= 0:
        return  # Never unload
    if idle_timer is not None:
        idle_timer.cancel()

    def _check():
        global active_requests
        with model_lock:
            # Only unload if no requests are running AND timeout has passed
            if active_requests == 0 and model is not None and (time.time() - last_used) >= IDLE_TIMEOUT:
                _do_unload_model()

    idle_timer = threading.Timer(IDLE_TIMEOUT, _check)
    idle_timer.daemon = True
    idle_timer.start()


def ensure_model(model_id: str | None = None):
    global last_used
    target = model_id or current_model_id or DEFAULT_MODEL
    if target not in AVAILABLE_MODELS:
        raise ValueError(f"Unknown model: {target}")
    with model_lock:
        if (
            model is not None
            and current_model_id != target
            and active_requests > 0
        ):
            raise RuntimeError(
                f"Model {current_model_id} is busy; cannot switch to {target}"
            )
        if model is None or current_model_id != target:
            if model is not None:
                _do_unload_model()
            _do_load_model(target)
        last_used = time.time()
    _schedule_unload()


# ---------------------------------------------------------------------------
# Init default voice on first run
# ---------------------------------------------------------------------------
def _init_default_voice():
    """Seed a 'default' voice from a baked-in sample if one is bundled.

    The repo ships no reference audio by default — users either upload their
    own (Clone tab) or pick a prebuilt CustomVoice speaker. To bundle a
    default sample, drop a ``voice.wav`` + ``voice.txt`` (transcript) pair
    into ``qwen3-tts/ref_audio/`` before building the image.
    """
    if any(VOICES_DIR.iterdir()):
        return
    default_audio = REF_AUDIO_DIR / "voice.wav"
    default_text_file = REF_AUDIO_DIR / "voice.txt"
    if not default_audio.exists():
        return
    ref_text = ""
    if default_text_file.exists():
        ref_text = default_text_file.read_text(encoding="utf-8").strip()
    voice_dir = VOICES_DIR / "default"
    voice_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(default_audio, voice_dir / "ref_audio.wav")
    meta = {
        "name": "Default",
        "ref_text": ref_text,
        "language": os.environ.get("DEFAULT_VOICE_LANGUAGE", "English"),
        "source": "clone",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_metadata(voice_dir / "metadata.json", meta)
    print("Initialized default voice from baked-in ref audio", flush=True)


# ---------------------------------------------------------------------------
# Audio encoding
# ---------------------------------------------------------------------------
def audio_to_format(audio: np.ndarray, sr: int, fmt: str) -> tuple[bytes, str]:
    if fmt not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError(
            f"Unsupported response_format {fmt!r}. "
            f"Allowed: {sorted(SUPPORTED_OUTPUT_FORMATS)}"
        )
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
    if ffmpeg_fmt:
        extra = ["-ab", "192k"] if fmt == "mp3" else []
        result = subprocess.run(
            ["ffmpeg", "-i", "pipe:0", "-f", ffmpeg_fmt] + extra + ["pipe:1"],
            input=buf.read(), capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg error: {result.stderr.decode()}")
        ct = {"mp3": "audio/mpeg", "opus": "audio/opus", "aac": "audio/aac"}[fmt]
        return result.stdout, ct

    buf.seek(0)
    return buf.read(), "audio/wav"


def _validate_response_format(fmt: str) -> str:
    normalized = (fmt or "wav").strip().lower()
    if normalized not in SUPPORTED_OUTPUT_FORMATS:
        raise HTTPException(
            400,
            f"response_format must be one of: {', '.join(sorted(SUPPORTED_OUTPUT_FORMATS))}",
        )
    return normalized


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Qwen3-TTS Multi-Model API")


@app.on_event("startup")
async def startup():
    _init_default_voice()
    # Index every voice on disk so /v1/audio/speech finds them after a fresh
    # boot. Without this, voice_refs is empty until each voice is re-registered
    # via /v1/voices/register and generation fails with "No voices loaded".
    _load_all_voice_refs()
    print(f"TTS server ready. Models: {list(AVAILABLE_MODELS.keys())}. "
          f"Idle timeout: {IDLE_TIMEOUT}s. "
          f"Voices indexed: {len(voice_refs)}", flush=True)


# ---- Health & Info --------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "current_model": current_model_id,
        "model_loaded": model is not None,
        "active_requests": active_requests,
        "available_models": {k: v["path"] for k, v in AVAILABLE_MODELS.items()},
        "voices_loaded": list(voice_prompts.keys()),
        "languages": SUPPORTED_LANGUAGES,
        "idle_timeout": IDLE_TIMEOUT,
        "cuda_graphs_enabled": ENABLE_CUDA_GRAPHS,
        "cuda_graph_models": sorted(CUDA_GRAPH_MODELS),
        "clone_sampling": {
            "do_sample": CLONE_DO_SAMPLE,
            "temperature": CLONE_TEMPERATURE,
            "top_k": CLONE_TOP_K,
            "top_p": CLONE_TOP_P,
            "xvec_only": CLONE_XVEC_ONLY,
        },
        "custom_sampling": {
            "do_sample": CUSTOM_DO_SAMPLE,
            "temperature": CUSTOM_TEMPERATURE,
            "top_k": CUSTOM_TOP_K,
            "top_p": CUSTOM_TOP_P,
        },
        "voice_clone_prompt_cache": [
            {"model": key[0], "voice": key[1], "xvec_only": key[2]}
            for key in voice_clone_prompt_cache.keys()
        ],
    }


# ---- Model Management -----------------------------------------------------

@app.post("/unload")
async def unload():
    """Unload current model to free GPU memory for other services."""
    with model_lock:
        if active_requests > 0:
            return {
                "status": "busy",
                "was_loaded": model is not None,
                "model_id": current_model_id,
                "active_requests": active_requests,
            }
        was_loaded = model is not None
        mid = current_model_id
        _do_unload_model()
    return {"status": "unloaded", "was_loaded": was_loaded, "model_id": mid}


@app.get("/v1/models")
async def list_models():
    return {
        "models": [
            {
                "id": mid,
                "path": info["path"],
                "type": info["type"],
                "params": info["params"],
                "loaded": current_model_id == mid,
            }
            for mid, info in AVAILABLE_MODELS.items()
        ]
    }


@app.get("/v1/models/current")
async def current_model_info():
    if current_model_id is None:
        return {"model": None, "loaded": False}
    return {"model": current_model_id, "loaded": True, **AVAILABLE_MODELS[current_model_id]}


class ModelLoadRequest(BaseModel):
    model_id: str


@app.post("/v1/models/load")
async def load_model_endpoint(request: ModelLoadRequest):
    if request.model_id not in AVAILABLE_MODELS:
        raise HTTPException(400, f"Unknown model. Available: {list(AVAILABLE_MODELS.keys())}")
    t0 = time.time()
    with model_lock:
        if (
            model is not None
            and current_model_id != request.model_id
            and active_requests > 0
        ):
            raise HTTPException(
                409,
                f"Model {current_model_id} is busy; cannot switch to {request.model_id}",
            )
        if model is not None and current_model_id != request.model_id:
            _do_unload_model()
        if model is None:
            _do_load_model(request.model_id)
    _schedule_unload()
    return {
        "status": "loaded",
        "model_id": request.model_id,
        "load_time_s": round(time.time() - t0, 2),
        "voices_loaded": list(voice_prompts.keys()),
    }


# ---- Voice Management -----------------------------------------------------

@app.get("/v1/voices")
async def list_voices():
    voices = []
    for voice_dir in sorted(VOICES_DIR.iterdir()):
        if not voice_dir.is_dir():
            continue
        meta_file = voice_dir / "metadata.json"
        if not meta_file.exists():
            continue
        meta = json.loads(meta_file.read_text())
        meta["voice_id"] = voice_dir.name
        meta["prompt_loaded"] = voice_dir.name in voice_prompts
        voices.append(meta)
    return {"voices": voices}


@app.post("/v1/voices/register")
async def register_voice(
    voice_id: str = Form(...),
    ref_text: str = Form(...),
    language: str = Form("German"),
    name: str = Form(""),
    ref_audio: UploadFile = File(...),
):
    """Register a new voice from reference audio + transcription."""
    ref_text = _sanitize_ref_text(ref_text)
    voice_dir = _voice_dir_for(voice_id)
    voice_dir.mkdir(parents=True, exist_ok=True)

    audio_path = voice_dir / "ref_audio.wav"
    audio_bytes = await _read_upload_limited(
        ref_audio, MAX_VOICE_UPLOAD_BYTES, "Reference audio"
    )

    try:
        normalization = await asyncio.to_thread(_convert_audio_bytes_to_wav, audio_bytes, audio_path)
    except Exception as exc:
        raise HTTPException(500, f"Audio conversion failed: {exc}") from exc

    meta = {
        "name": name or voice_id,
        "ref_text": ref_text,
        "language": language,
        "source": "clone",
        "ref_audio_normalization": normalization,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    await asyncio.to_thread(_write_metadata, voice_dir / "metadata.json", meta)

    # Index the voice ref for generation
    _clear_voice_clone_prompt(voice_id)
    voice_refs[voice_id] = {"ref_audio": str(audio_path), "ref_text": ref_text}
    voice_prompts[voice_id] = True

    return {"status": "registered", "voice_id": voice_id, "prompt_loaded": voice_id in voice_refs}


@app.post("/v1/voices/register-designed")
async def register_designed_voice(
    voice_id: str = Form(...),
    name: str = Form(""),
    language: str = Form("English"),
    design_prompt: str = Form(""),
    ref_text: str = Form(""),
    ref_audio: UploadFile = File(...),
):
    """Register a voice from audio generated by the Voice Designer."""
    ref_text = _sanitize_ref_text(ref_text)
    voice_dir = _voice_dir_for(voice_id)
    voice_dir.mkdir(parents=True, exist_ok=True)

    audio_path = voice_dir / "ref_audio.wav"
    audio_bytes = await _read_upload_limited(
        ref_audio, MAX_VOICE_UPLOAD_BYTES, "Reference audio"
    )
    try:
        normalization = await asyncio.to_thread(_convert_audio_bytes_to_wav, audio_bytes, audio_path)
    except Exception as exc:
        raise HTTPException(500, f"Audio conversion failed: {exc}") from exc

    meta = {
        "name": name or voice_id,
        "ref_text": ref_text,
        "language": language,
        "source": "design",
        "design_prompt": design_prompt,
        "ref_audio_normalization": normalization,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    await asyncio.to_thread(_write_metadata, voice_dir / "metadata.json", meta)

    # Index the voice ref for generation
    _clear_voice_clone_prompt(voice_id)
    voice_refs[voice_id] = {"ref_audio": str(audio_path), "ref_text": ref_text}
    voice_prompts[voice_id] = True

    return {"status": "registered", "voice_id": voice_id, "prompt_loaded": voice_id in voice_refs}


@app.delete("/v1/voices/{voice_id}")
async def delete_voice(voice_id: str):
    voice_dir = _voice_dir_for(voice_id)
    if not voice_dir.exists():
        raise HTTPException(404, "Voice not found")
    shutil.rmtree(voice_dir)
    voice_prompts.pop(voice_id, None)
    voice_refs.pop(voice_id, None)
    _clear_voice_clone_prompt(voice_id)
    return {"status": "deleted", "voice_id": voice_id}


@app.get("/v1/voices/{voice_id}/audio")
async def get_voice_audio(voice_id: str):
    """Return the reference audio for a voice."""
    audio_path = _voice_dir_for(voice_id) / "ref_audio.wav"
    if not audio_path.exists():
        raise HTTPException(404, "Voice audio not found")
    return FileResponse(audio_path, media_type="audio/wav")


# ---- Speech Generation ----------------------------------------------------

class SpeechRequest(BaseModel):
    input: str
    voice: str = "default"
    model_id: str | None = None
    response_format: str = "wav"
    language: str | None = None
    max_new_tokens: int | None = None


CHUNK_CHAR_THRESHOLD = 200  # texts longer than this get chunked
MAX_CHUNK_CHARS = 200       # target max chars per chunk


def _split_at_clause(text: str, max_len: int) -> list[str]:
    """Split a long sentence at clause boundaries (commas, semicolons, quotes)."""
    # Try splitting at clause-level punctuation
    parts = re.split(r'(?<=[,;:\"\"\'])\s+', text)
    if len(parts) <= 1:
        # Hard split at word boundary near midpoint
        mid = max_len
        idx = text.rfind(' ', 0, mid)
        if idx <= 0:
            idx = text.find(' ', mid)
        if idx <= 0:
            return [text]
        return [text[:idx].strip(), text[idx:].strip()]

    chunks = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) + 1 > max_len:
            chunks.append(current.strip())
            current = part
        else:
            current = f"{current} {part}" if current else part
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _split_text_to_chunks(text: str) -> list[str]:
    """Split long text into chunks at paragraph/sentence/clause boundaries."""
    text = text.strip()
    if len(text) <= CHUNK_CHAR_THRESHOLD:
        return [text]

    # First split on double newlines (paragraphs)
    paragraphs = re.split(r'\n\s*\n', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks = []
    for para in paragraphs:
        if len(para) <= MAX_CHUNK_CHARS:
            chunks.append(para)
        else:
            # Split long paragraphs at sentence boundaries
            sentences = re.split(r'(?<=[.!?…])\s+', para)
            current = ""
            for sent in sentences:
                if current and len(current) + len(sent) + 1 > MAX_CHUNK_CHARS:
                    chunks.append(current.strip())
                    current = sent
                else:
                    current = f"{current} {sent}" if current else sent
            if current.strip():
                chunks.append(current.strip())

    # Second pass: split any remaining oversized chunks at clause boundaries
    final = []
    for chunk in chunks:
        if len(chunk) <= MAX_CHUNK_CHARS:
            final.append(chunk)
        else:
            final.extend(_split_at_clause(chunk, MAX_CHUNK_CHARS))

    return final if final else [text]


def _clone_token_limit(text: str, override: int | None = None) -> int:
    if override is not None:
        return override
    return min(800, max(180, int(len(text) * 2.0)))


@app.post("/v1/audio/speech")
async def create_speech(request: SpeechRequest):
    if not request.input.strip():
        raise HTTPException(400, "Input text is empty")
    response_format = _validate_response_format(request.response_format)

    target_model = request.model_id or current_model_id or DEFAULT_MODEL
    if AVAILABLE_MODELS.get(target_model, {}).get("type") != "base":
        raise HTTPException(400, "Speech generation requires a Base model (1.7b-base)")

    voice_id = request.voice
    if voice_id not in voice_refs:
        available = list(voice_refs.keys())
        if not available:
            raise HTTPException(400, "No voices loaded. Register a voice first.")
        raise HTTPException(
            404,
            f"Unknown voice {voice_id!r}. Available voices: {available}",
        )

    ensure_model(target_model)

    # Resolve language
    language = "English"
    if request.language:
        for lang in SUPPORTED_LANGUAGES:
            if lang.lower() == request.language.lower():
                language = lang
                break
    else:
        meta_file = VOICES_DIR / voice_id / "metadata.json"
        if meta_file.exists():
            language = json.loads(meta_file.read_text()).get("language", "English")

    chunks = _split_text_to_chunks(request.input)
    n_chunks = len(chunks)
    vref = voice_refs[voice_id]

    global active_requests, last_used
    with model_lock:
        if model is None:
            raise HTTPException(503, "Model not ready")
        active_requests += 1
    try:
        loop = asyncio.get_event_loop()

        def _run_generate():
            t0 = time.time()
            audio_parts = []
            sr = 24000
            # Half-second silence between chunks
            silence = np.zeros(int(sr * 0.4), dtype=np.float32)

            # Voice cloning uses shared model-side conditioning state. Keep all
            # chunks for one request contiguous so concurrent requests cannot
            # swap speakers between sentences.
            with inference_lock:
                for i, chunk in enumerate(chunks):
                    chunk_len = len(chunk)
                    token_limit = _clone_token_limit(chunk, request.max_new_tokens)
                    print(f"[chunk {i+1}/{n_chunks}] {chunk_len} chars → {token_limit} tokens: "
                          f"{chunk[:60]}{'…' if len(chunk) > 60 else ''}", flush=True)
                    wavs, sr = _generate_voice_clone(
                        text=chunk, language=language, voice_id=voice_id,
                        ref_audio=vref["ref_audio"],
                        ref_text=vref["ref_text"],
                        max_new_tokens=token_limit,
                    )
                    audio_parts.append(wavs[0])
                    if i < n_chunks - 1:
                        audio_parts.append(silence)

            gen_time = time.time() - t0
            audio = np.concatenate(audio_parts)
            audio_dur = len(audio) / sr
            rtf = gen_time / audio_dur if audio_dur > 0 else 0
            cg = "CG" if use_cuda_graphs else "fallback"
            print(f"[{voice_id}/{language}] {audio_dur:.1f}s in {gen_time:.1f}s "
                  f"(RTF {rtf:.2f}x) chunks={n_chunks} model={current_model_id} [{cg}]", flush=True)
            return audio, sr, audio_dur, gen_time, n_chunks, rtf

        audio, sr, audio_dur, gen_time, n_chunks, rtf = await loop.run_in_executor(None, _run_generate)

    except Exception as e:
        raise HTTPException(500, f"Generation failed: {e}")
    finally:
        with model_lock:
            active_requests -= 1
            last_used = time.time()
        _schedule_unload()

    audio_bytes, content_type = await asyncio.to_thread(
        audio_to_format, audio, sr, response_format
    )
    return Response(
        content=audio_bytes, media_type=content_type,
        headers={
            "X-Audio-Duration": str(round(audio_dur, 2)),
            "X-Generation-Time": str(round(gen_time, 2)),
            "X-RTF": str(round(rtf, 4)),
            "X-Model": current_model_id or "",
            "X-Voice": voice_id,
            "X-Chunks": str(n_chunks),
        },
    )


# ---- Streaming Speech Generation (SSE, chunk-by-chunk) --------------------

@app.post("/v1/audio/speech/stream")
async def create_speech_stream(request: SpeechRequest):
    """Stream speech generation via SSE. Each text chunk is sent as a
    base64-encoded WAV event as soon as it's generated."""
    if not request.input.strip():
        raise HTTPException(400, "Input text is empty")
    response_format = _validate_response_format(request.response_format)
    if response_format != "wav":
        raise HTTPException(400, "Streaming speech returns base64 WAV chunks; use response_format='wav'")

    target_model = request.model_id or current_model_id or DEFAULT_MODEL
    if AVAILABLE_MODELS.get(target_model, {}).get("type") != "base":
        raise HTTPException(400, "Speech generation requires a Base model")

    voice_id = request.voice
    if voice_id not in voice_refs:
        available = list(voice_refs.keys())
        if not available:
            raise HTTPException(400, "No voices loaded. Register a voice first.")
        raise HTTPException(
            404,
            f"Unknown voice {voice_id!r}. Available voices: {available}",
        )

    ensure_model(target_model)

    language = "English"
    if request.language:
        for lang in SUPPORTED_LANGUAGES:
            if lang.lower() == request.language.lower():
                language = lang
                break
    else:
        meta_file = VOICES_DIR / voice_id / "metadata.json"
        if meta_file.exists():
            language = json.loads(meta_file.read_text()).get("language", "English")

    chunks = _split_text_to_chunks(request.input)
    n_chunks = len(chunks)
    vref = voice_refs[voice_id]

    global active_requests, last_used
    with model_lock:
        if model is None:
            raise HTTPException(503, "Model not ready")
        active_requests += 1

    def _gen_chunk(i: int, chunk: str, token_limit: int):
        """Run one chunk generation in an executor thread."""
        with model_lock:
            if model is None:
                raise RuntimeError("Model not ready")
        return _generate_voice_clone(
            text=chunk, language=language, voice_id=voice_id,
            ref_audio=vref["ref_audio"],
            ref_text=vref["ref_text"],
            max_new_tokens=token_limit,
        )

    async def generate_sse():
        global active_requests, last_used
        t0 = time.time()
        loop = asyncio.get_running_loop()
        event_queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _run_stream_request():
            total_audio_dur = 0.0
            sr = 24000
            try:
                # Keep one streaming request's clone chunks contiguous for the same
                # reason as non-streaming generation: the model-side speaker state
                # is not safe to interleave between cloned voices.
                with inference_lock:
                    for i, chunk in enumerate(chunks):
                        chunk_len = len(chunk)
                        token_limit = _clone_token_limit(chunk, request.max_new_tokens)
                        print(f"[stream chunk {i+1}/{n_chunks}] {chunk_len} chars → {token_limit} tokens", flush=True)

                        wavs, sr = _gen_chunk(i, chunk, token_limit)
                        audio = wavs[0]
                        audio_dur = len(audio) / sr
                        total_audio_dur += audio_dur

                        # Encode chunk as WAV bytes → base64
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
                print(f"[stream/{voice_id}] {total_audio_dur:.1f}s in {gen_time:.1f}s "
                      f"(RTF {rtf:.2f}x) chunks={n_chunks} [{cg}]", flush=True)

                done_data = {
                    "total_duration": round(total_audio_dur, 2),
                    "generation_time": round(gen_time, 2),
                    "rtf": round(rtf, 4),
                    "model": current_model_id or "",
                    "voice": voice_id,
                    "chunks": n_chunks,
                }
                loop.call_soon_threadsafe(
                    event_queue.put_nowait,
                    f"event: done\ndata: {json.dumps(done_data)}\n\n",
                )
            except Exception as e:
                loop.call_soon_threadsafe(
                    event_queue.put_nowait,
                    f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n",
                )
            finally:
                loop.call_soon_threadsafe(event_queue.put_nowait, None)

        try:
            task = loop.run_in_executor(None, _run_stream_request)
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                yield event
            await task
        finally:
            with model_lock:
                active_requests -= 1
                last_used = time.time()
            _schedule_unload()

    return StreamingResponse(generate_sse(), media_type="text/event-stream")


# ---- Voice Design ----------------------------------------------------------

class DesignRequest(BaseModel):
    text: str
    description: str
    language: str = "English"
    response_format: str = "wav"
    max_new_tokens: int | None = None


@app.get("/v1/speakers")
async def list_speakers():
    """Return the prebuilt speakers usable with the CustomVoice model."""
    return {"speakers": BUILTIN_SPEAKERS}


@app.post("/v1/audio/design")
async def design_voice(request: DesignRequest):
    """Generate speech with a novel voice from a text description.
    Requires the 1.7b-design model (loaded automatically)."""
    if not request.text.strip():
        raise HTTPException(400, "Text is empty")
    if not request.description.strip():
        raise HTTPException(400, "Voice description is empty")
    response_format = _validate_response_format(request.response_format)

    ensure_model("1.7b-design")

    global active_requests, last_used
    with model_lock:
        if model is None:
            raise HTTPException(503, "Model not ready")
        active_requests += 1
    try:
        t0 = time.time()
        text_len = len(request.text.strip())
        token_limit = request.max_new_tokens or min(800, max(150, int(text_len * 1.5)))
        loop = asyncio.get_event_loop()

        def _gen():
            with inference_lock:
                return model.model.generate_voice_design(
                    text=request.text,
                    language=request.language,
                    instruct=request.description,
                    max_new_tokens=token_limit,
                    eos_token_id=[2150, 2157],
                    repetition_penalty=1.05,
                )

        wavs, sr = await loop.run_in_executor(None, _gen)
        gen_time = time.time() - t0
        audio = wavs[0]
        audio_dur = len(audio) / sr
        print(f"[design/{request.language}] {audio_dur:.1f}s in {gen_time:.1f}s", flush=True)
    except Exception as e:
        raise HTTPException(500, f"Voice design failed: {e}")
    finally:
        try:
            with model_lock:
                active_requests -= 1
                last_used = time.time()
            _schedule_unload()
        except Exception:
            pass

    audio_bytes, content_type = await asyncio.to_thread(
        audio_to_format, audio, sr, response_format
    )
    return Response(
        content=audio_bytes, media_type=content_type,
        headers={
            "X-Audio-Duration": str(round(audio_dur, 2)),
            "X-Generation-Time": str(round(gen_time, 2)),
        },
    )


# ---- Custom Voice (prebuilt speakers + optional steering) -----------------

class CustomVoiceRequest(BaseModel):
    text: str
    speaker: str
    language: str = "English"
    instruct: str | None = None
    response_format: str = "wav"
    max_new_tokens: int | None = None


@app.post("/v1/audio/speech/custom")
async def custom_voice(request: CustomVoiceRequest):
    """Generate speech with a prebuilt speaker. Optional `instruct` steers
    delivery (e.g. 'excited whisper'). Requires the 1.7b-custom model."""
    if not request.text.strip():
        raise HTTPException(400, "Text is empty")
    if request.speaker not in BUILTIN_SPEAKER_IDS:
        raise HTTPException(
            400,
            f"Unknown speaker {request.speaker!r}. "
            f"Available: {sorted(BUILTIN_SPEAKER_IDS)}",
        )
    response_format = _validate_response_format(request.response_format)

    ensure_model("1.7b-custom")

    global active_requests, last_used
    with model_lock:
        if model is None:
            raise HTTPException(503, "Model not ready")
        active_requests += 1
    try:
        t0 = time.time()
        text_len = len(request.text.strip())
        token_limit = request.max_new_tokens or min(800, max(150, int(text_len * 1.5)))
        kwargs = dict(
            text=request.text,
            speaker=request.speaker,
            language=request.language,
            max_new_tokens=token_limit,
            eos_token_id=[2150, 2157],
            non_streaming_mode=True,
            do_sample=CUSTOM_DO_SAMPLE,
            temperature=CUSTOM_TEMPERATURE,
            top_k=CUSTOM_TOP_K,
            top_p=CUSTOM_TOP_P,
            repetition_penalty=1.08,
        )
        if request.instruct and request.instruct.strip():
            kwargs["instruct"] = request.instruct.strip()

        # Run blocking inference in executor to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        def _gen():
            with inference_lock:
                return model.model.generate_custom_voice(**kwargs)
        wavs, sr = await loop.run_in_executor(None, _gen)
        gen_time = time.time() - t0
        audio = wavs[0]
        audio_dur = len(audio) / sr
        print(
            f"[custom/{request.speaker}/{request.language}] "
            f"{audio_dur:.1f}s in {gen_time:.1f}s "
            f"instruct={'yes' if kwargs.get('instruct') else 'no'}",
            flush=True,
        )
    except Exception as e:
        raise HTTPException(500, f"Custom voice generation failed: {e}")
    finally:
        with model_lock:
            active_requests -= 1
            last_used = time.time()
        _schedule_unload()

    audio_bytes, content_type = await asyncio.to_thread(
        audio_to_format, audio, sr, response_format
    )
    return Response(
        content=audio_bytes, media_type=content_type,
        headers={
            "X-Audio-Duration": str(round(audio_dur, 2)),
            "X-Generation-Time": str(round(gen_time, 2)),
            "X-Speaker": request.speaker,
            "X-Model": current_model_id or "",
        },
    )


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
