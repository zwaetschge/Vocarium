"""Vocarium OmniVoice TTS — zero-shot German voice cloning as a fourth engine.

Voices are reference pairs in ``VOICES_DIR``: ``<Name>.wav`` (3–10 s clean
speech) plus ``<Name>.txt`` (its exact transcript). Each pair is turned into a
cached ``VoiceClonePrompt`` at startup, so requests skip the reference encoding.
Dropping in a new pair and restarting the service publishes a new voice.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from pathlib import Path

import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

MODEL_ID = os.environ.get("OMNIVOICE_MODEL", "k2-fsa/OmniVoice")
VOICES_DIR = Path(os.environ.get("OMNIVOICE_VOICES_DIR", "/voices"))
DEVICE = os.environ.get("OMNIVOICE_DEVICE", "cuda")
LANGUAGE = os.environ.get("OMNIVOICE_LANGUAGE", "de")
MAX_TEXT_CHARS = int(os.environ.get("OMNIVOICE_MAX_TEXT_CHARS", "4096"))

app = FastAPI(title="Vocarium OmniVoice TTS", version="1.0.0")
generation_lock = asyncio.Lock()
model = None
prompts: dict[str, object] = {}


class SpeechRequest(BaseModel):
    model: str = "omnivoice"
    input: str = Field(min_length=1)
    voice: str
    response_format: str = "wav"


def load_voices() -> None:
    prompts.clear()
    for wav in sorted(VOICES_DIR.glob("*.wav")):
        transcript = wav.with_suffix(".txt")
        if not transcript.is_file():
            logger.warning("Skipping %s: no matching .txt transcript", wav.name)
            continue
        started = time.perf_counter()
        prompts[wav.stem] = model.create_voice_clone_prompt(
            ref_audio=str(wav),
            ref_text=transcript.read_text(encoding="utf-8").strip(),
        )
        logger.info("Voice %r ready in %.1fs", wav.stem, time.perf_counter() - started)
    # Die Prompt-Encodierungen hinterlassen reservierte Allocator-Blöcke in
    # Peak-Größe; ohne Freigabe wächst die nvidia-smi-Belegung mit jeder
    # registrierten Stimme (~5 GB → 7+ GB bei 17 Stimmen).
    torch.cuda.empty_cache()


@app.on_event("startup")
def startup() -> None:
    global model
    from omnivoice import OmniVoice

    started = time.perf_counter()
    model = OmniVoice.from_pretrained(MODEL_ID, device_map=DEVICE, dtype=torch.float16)
    logger.info("Model loaded in %.1fs", time.perf_counter() - started)
    load_voices()
    if not prompts:
        logger.error("No voices found under %s", VOICES_DIR)


def synthesize(voice: str, text: str) -> tuple[bytes, float]:
    audios = model.generate(
        text=text,
        language=LANGUAGE,
        voice_clone_prompt=prompts[voice],
    )
    buffer = io.BytesIO()
    sf.write(buffer, audios[0], model.sampling_rate, format="WAV", subtype="PCM_16")
    # Aktivierungs-Reservierungen sofort zurückgeben — die 3060 teilt sich
    # das VRAM mit Whisper-STT; Kosten sind Mikrosekunden pro Request.
    torch.cuda.empty_cache()
    return buffer.getvalue(), len(audios[0]) / model.sampling_rate


def validate(request: SpeechRequest) -> tuple[str, str]:
    text = request.input.strip()
    if not text:
        raise HTTPException(400, "Input text must not be empty")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(413, f"Input text exceeds {MAX_TEXT_CHARS} characters")
    if request.response_format.strip().lower() != "wav":
        raise HTTPException(400, "OmniVoice currently serves response_format='wav' only")
    if request.voice not in prompts:
        raise HTTPException(404, f"Unknown voice {request.voice!r}")
    return request.voice, text


def split_sentences(text: str, min_chars: int = 60) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?…:])\s+", text.strip()) if p.strip()]
    merged: list[str] = []
    for part in parts:
        # The first chunk stays a single sentence — it bounds time-to-first-audio.
        if len(merged) > 1 and (len(merged[-1]) < min_chars or len(part) < min_chars // 2):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged or [text.strip()]


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "healthy" if prompts else "unavailable",
        "engine": "omnivoice",
        "model": MODEL_ID,
        "voices": sorted(prompts),
        "device": DEVICE,
    }


@app.get("/v1/audio/voices")
def voices() -> dict[str, object]:
    return {
        "voices": [
            {
                "voice_id": name,
                "name": name,
                "type": "omnivoice-clone",
                "gender": None,
                # The reference pair is the voice. Publishing its transcript and
                # availability lets the gateway offer a real preview instead of
                # having to synthesise one first.
                "ref_text": _reference_text(name),
                "has_reference": (VOICES_DIR / f"{name}.wav").is_file(),
            }
            for name in sorted(prompts)
        ]
    }


def _reference_text(name: str) -> str | None:
    path = VOICES_DIR / f"{name}.txt"
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


@app.get("/v1/audio/voices/{name}/reference")
def reference_audio(name: str) -> FileResponse:
    """Serve the clone's own reference recording.

    Cheaper and more honest than synthesising a sample: this is literally the
    audio the clone was built from.
    """
    if name not in prompts:
        raise HTTPException(404, f"Unknown voice {name!r}")
    wav_path = VOICES_DIR / f"{name}.wav"
    if not wav_path.is_file():
        raise HTTPException(404, f"No reference audio for {name!r}")
    return FileResponse(wav_path, media_type="audio/wav")


@app.post("/v1/audio/voices")
async def register_voice(
    name: str = Form(...),
    ref_text: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, object]:
    """Register a new clone voice at runtime: persist the pair, build the prompt.

    The pair on disk is the durable form — a restart rebuilds the prompt from it.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    if not safe:
        raise HTTPException(400, "name must contain letters or digits")
    if safe in prompts:
        raise HTTPException(409, f"Voice {safe!r} already exists")
    text = ref_text.strip()
    if not text:
        raise HTTPException(400, "ref_text is required")
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "Reference audio exceeds 50 MB")

    wav_path = VOICES_DIR / f"{safe}.wav"
    wav_path.write_bytes(data)
    try:
        async with generation_lock:
            prompt = await asyncio.to_thread(
                model.create_voice_clone_prompt,
                ref_audio=str(wav_path),
                ref_text=text,
            )
    except Exception as exc:
        wav_path.unlink(missing_ok=True)
        logger.exception("Voice registration failed")
        raise HTTPException(400, f"Could not build a clone prompt: {exc}") from exc
    wav_path.with_suffix(".txt").write_text(text, encoding="utf-8")
    prompts[safe] = prompt
    logger.info("Voice %r registered at runtime", safe)
    return {"status": "created", "voice_id": safe}


@app.delete("/v1/audio/voices/{name}")
def delete_voice(name: str) -> dict[str, object]:
    if name not in prompts:
        raise HTTPException(404, f"Unknown voice {name!r}")
    prompts.pop(name)
    (VOICES_DIR / f"{name}.wav").unlink(missing_ok=True)
    (VOICES_DIR / f"{name}.txt").unlink(missing_ok=True)
    return {"status": "deleted", "voice_id": name}


@app.post("/v1/audio/speech")
async def speech(request: SpeechRequest) -> Response:
    voice, text = validate(request)
    started = time.perf_counter()
    async with generation_lock:
        audio, duration = await asyncio.to_thread(synthesize, voice, text)
    elapsed = time.perf_counter() - started
    return Response(
        audio,
        media_type="audio/wav",
        headers={
            "X-TTS-Engine": "omnivoice",
            "X-Model": "omnivoice",
            "X-Voice": voice,
            "X-Audio-Duration": f"{duration:.3f}",
            "X-Inference-Seconds": f"{elapsed:.3f}",
        },
    )


@app.post("/v1/audio/speech/stream")
async def speech_stream(request: SpeechRequest) -> StreamingResponse:
    voice, text = validate(request)
    sentences = split_sentences(text)

    async def events():
        started = time.perf_counter()
        total = 0.0
        try:
            for index, sentence in enumerate(sentences):
                async with generation_lock:
                    audio, duration = await asyncio.to_thread(synthesize, voice, sentence)
                total += duration
                chunk = {
                    "index": index,
                    "total": len(sentences),
                    "audio": base64.b64encode(audio).decode("ascii"),
                    "duration": round(duration, 2),
                    "text": sentence[:80],
                }
                yield f"event: chunk\ndata: {json.dumps(chunk)}\n\n"
            elapsed = time.perf_counter() - started
            done = {
                "total_duration": round(total, 2),
                "generation_time": round(elapsed, 2),
                "rtf": round(elapsed / total, 4) if total else 0,
                "model": "omnivoice",
                "voice": voice,
                "chunks": len(sentences),
            }
            yield f"event: done\ndata: {json.dumps(done)}\n\n"
        except Exception as exc:
            logger.exception("OmniVoice streaming failed")
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
