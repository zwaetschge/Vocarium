"""Lazy dots.tts voice-cloning worker for Vocarium.

The worker reads Vocarium's existing clone directories directly. Each voice is
expected to contain ``ref_audio.wav`` and ``metadata.json`` with ``ref_text``.
No voice data is copied or registered into a second database.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel


VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
MODEL_ID = os.environ.get("DOTS_TTS_MODEL", "dots-studio/dots.tts-soar")
MODEL_REVISION = os.environ.get("DOTS_TTS_MODEL_REVISION", "").strip() or None
PRECISION = os.environ.get("DOTS_TTS_PRECISION", "bfloat16")
NUM_STEPS = max(1, int(os.environ.get("DOTS_TTS_NUM_STEPS", "10")))
GUIDANCE_SCALE = float(os.environ.get("DOTS_TTS_GUIDANCE_SCALE", "1.2"))
SPEAKER_SCALE = float(os.environ.get("DOTS_TTS_SPEAKER_SCALE", "1.5"))
IDLE_TIMEOUT = max(0, int(os.environ.get("IDLE_TIMEOUT", "120")))
MAX_TEXT_CHARS = max(1, int(os.environ.get("MAX_TEXT_CHARS", "4000")))
VOICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

app = FastAPI(title="Vocarium dots.tts Worker")
logger = logging.getLogger(__name__)
runtime: Any | None = None
active_requests = 0
last_used = 0.0
last_load_error: str | None = None
model_lock = threading.Lock()
inference_lock = threading.Lock()
idle_timer: threading.Timer | None = None


class SpeechRequest(BaseModel):
    input: str
    voice: str
    language: str | None = None
    response_format: str = "wav"
    seed: int = 42


def _voice_dir(voice_id: str) -> Path:
    cleaned = (voice_id or "").strip()
    if not VOICE_ID_RE.fullmatch(cleaned):
        raise ValueError("Invalid voice ID")
    root = VOICES_DIR.resolve()
    target = (root / cleaned).resolve()
    if target == root or root not in target.parents:
        raise ValueError("Invalid voice ID")
    return target


def _clone_prompt(voice_id: str) -> tuple[Path, str, str | None]:
    voice_dir = _voice_dir(voice_id)
    audio_path = voice_dir / "ref_audio.wav"
    metadata_path = voice_dir / "metadata.json"
    if not audio_path.is_file():
        raise ValueError(f"Clone voice {voice_id!r} has no reference audio")
    if not metadata_path.is_file():
        raise ValueError(f"Clone voice {voice_id!r} has no metadata")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if (metadata.get("source") or "clone").strip().lower() != "clone":
        raise ValueError("dots.tts only supports cloned voices")
    ref_text = (metadata.get("ref_text") or "").strip()
    if not ref_text:
        raise ValueError(f"Clone voice {voice_id!r} has no reference transcript")
    language = (metadata.get("language") or "").strip() or None
    return audio_path, ref_text, language


def _load_runtime() -> Any:
    global runtime, last_load_error
    with model_lock:
        if runtime is not None:
            return runtime
        try:
            from dots_tts.runtime import DotsTtsRuntime

            logger.info("Loading dots.tts model: %s", MODEL_ID)
            runtime = DotsTtsRuntime.from_pretrained(
                MODEL_ID,
                revision=MODEL_REVISION,
                precision=PRECISION,
                optimize=False,
                warmup_on_optimize=False,
            )
            last_load_error = None
            logger.info("dots.tts model loaded")
            return runtime
        except Exception as exc:
            last_load_error = str(exc)
            raise


def _cancel_idle_timer() -> None:
    global idle_timer
    if idle_timer is not None:
        idle_timer.cancel()
        idle_timer = None


def _unload_if_idle() -> dict[str, Any]:
    global runtime, idle_timer
    with model_lock:
        idle_timer = None
        if active_requests > 0:
            return {"status": "busy", "active_requests": active_requests}
        was_loaded = runtime is not None
        runtime = None
    if was_loaded:
        gc.collect()
        torch.cuda.empty_cache()
        logger.info("dots.tts model unloaded")
    return {"status": "ok", "was_loaded": was_loaded}


def _schedule_unload() -> None:
    global idle_timer
    _cancel_idle_timer()
    if IDLE_TIMEOUT <= 0:
        return
    idle_timer = threading.Timer(IDLE_TIMEOUT, _unload_if_idle)
    idle_timer.daemon = True
    idle_timer.start()


def _generate_blocking(req: SpeechRequest) -> tuple[bytes, dict[str, str]]:
    global active_requests, last_used
    with model_lock:
        _cancel_idle_timer()
        active_requests += 1
    try:
        audio_path, ref_text, stored_language = _clone_prompt(req.voice)
        model = _load_runtime()
        from dots_tts.utils.util import seed_everything

        with inference_lock:
            seed_everything(req.seed)
            result = model.generate(
                text=req.input.strip(),
                prompt_audio_path=str(audio_path),
                prompt_text=ref_text,
                language=req.language or stored_language or "auto_detect",
                num_steps=NUM_STEPS,
                guidance_scale=GUIDANCE_SCALE,
                speaker_scale=SPEAKER_SCALE,
                normalize_text=False,
            )
        audio = result["audio"].detach().float().cpu().squeeze().numpy()
        sample_rate = int(result["sample_rate"])
        output = io.BytesIO()
        sf.write(output, audio, sample_rate, format="WAV", subtype="PCM_16")
        duration = len(audio) / sample_rate if sample_rate else 0.0
        generation_time = float(result.get("time_used") or 0.0)
        headers = {
            "X-Audio-Duration": f"{duration:.3f}",
            "X-Generation-Time": f"{generation_time:.3f}",
            "X-RTF": f"{generation_time / duration if duration else 0.0:.4f}",
            "X-Model": MODEL_ID,
            "X-Voice": req.voice,
            "X-TTS-Engine": "dots",
        }
        return output.getvalue(), headers
    finally:
        with model_lock:
            active_requests -= 1
            last_used = time.time()
            _schedule_unload()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "engine": "dots",
        "model": MODEL_ID,
        "model_loaded": runtime is not None,
        "active_requests": active_requests,
        "idle_timeout": IDLE_TIMEOUT,
        "last_load_error": last_load_error,
    }


@app.post("/unload")
async def unload() -> dict[str, Any]:
    with model_lock:
        if active_requests > 0:
            return {"status": "busy", "active_requests": active_requests}
    return await asyncio.to_thread(_unload_if_idle)


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest) -> Response:
    if not req.input.strip():
        raise HTTPException(400, "input is required")
    if len(req.input) > MAX_TEXT_CHARS:
        raise HTTPException(413, f"input too long (max {MAX_TEXT_CHARS} characters)")
    if req.response_format.lower() != "wav":
        raise HTTPException(400, "dots.tts currently supports response_format=wav")
    try:
        body, headers = await asyncio.to_thread(_generate_blocking, req)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except torch.cuda.OutOfMemoryError as exc:
        raise HTTPException(503, "dots.tts ran out of GPU memory") from exc
    return Response(body, media_type="audio/wav", headers=headers)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8890)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
