from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from kokoro import KModel, KPipeline
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

MODELS_DIR = Path(os.environ.get("KIKIRI_MODELS_DIR", "/models"))
LEGACY_MODEL_PATH = Path(os.environ.get("KIKIRI_MODEL_PATH", "/models/model.pth"))
LEGACY_VOICE_PATH = Path(os.environ.get("KIKIRI_VOICE_PATH", "/models/default.pt"))
LEGACY_CONFIG_PATH = Path(os.environ.get("KIKIRI_CONFIG_PATH", "/models/config.json"))
PIPER_MODELS_DIR = Path(os.environ.get("KIKIRI_PIPER_MODELS_DIR", "/piper-models"))
PIPER_CATALOG_PATH = Path(os.environ.get("KIKIRI_PIPER_CATALOG", "/app/voices.json"))
DEFAULT_MODEL_ID = os.environ.get("KIKIRI_MODEL_ID", "kikiri-bobiverse")
VOICE_ID = os.environ.get("KIKIRI_VOICE_ID", "default")
TORCH_THREADS = max(1, int(os.environ.get("KIKIRI_TORCH_THREADS", "12")))
MAX_TEXT_CHARS = max(1, int(os.environ.get("KIKIRI_MAX_TEXT_CHARS", "5000")))
# Each resident fine-tune costs ~350 MB of RAM. The container memory limit is the
# real constraint, so keep only the recently used ones loaded.
MAX_RESIDENT_MODELS = max(1, int(os.environ.get("KIKIRI_MAX_RESIDENT_MODELS", "2")))
# Piper sessions are far cheaper (~80-230 MB each) and a voice bank is only
# useful if switching between its presets is not a model load every time.
MAX_RESIDENT_PIPER = max(1, int(os.environ.get("KIKIRI_MAX_RESIDENT_PIPER", "3")))
SAMPLE_RATE = 24_000

torch.set_num_threads(TORCH_THREADS)
torch.set_num_interop_threads(max(1, min(4, TORCH_THREADS)))

app = FastAPI(title="Vocarium Kikiri CPU TTS", version="3.0.0")
inference_lock = asyncio.Lock()

try:
    from piper_backend import PiperEngine

    piper = PiperEngine(PIPER_MODELS_DIR, max_resident=MAX_RESIDENT_PIPER)
except Exception:  # onnxruntime missing, or a broken piper-models mount
    logger.exception("Piper fallback backend unavailable; serving Kokoro fine-tunes only")
    piper = None


@dataclass(frozen=True)
class VoiceEntry:
    """One servable voice.

    Two backends live behind the same id space. ``kokoro`` entries are the
    hand-trained German fine-tunes: one directory, one voice. ``piper`` entries
    are presets of the fallback bank, where a single ONNX file can carry
    hundreds of speakers — which is why the registry is keyed by voice and not,
    as it used to be, by model.
    """

    voice_id: str
    display_name: str
    backend: str
    group: str
    gender: str = "neutral"
    description: str = ""
    # kokoro
    model_path: Path | None = None
    voice_path: Path | None = None
    config_path: Path | None = None
    # piper
    piper_model: str | None = None
    speaker_id: int = 0


@dataclass
class LoadedModel:
    pipeline: KPipeline
    voice: torch.Tensor


registry: dict[str, VoiceEntry] = {}
loaded: "OrderedDict[str, LoadedModel]" = OrderedDict()


class SpeechRequest(BaseModel):
    model: str = DEFAULT_MODEL_ID
    input: str = Field(min_length=1)
    voice: str = VOICE_ID
    response_format: str = "wav"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


def discover_kokoro() -> dict[str, VoiceEntry]:
    """Index every fine-tune directory under /models.

    Layout is one directory per fine-tune (model.pth + default.pt + config.json,
    optionally meta.json for the display name). The flat layout the first release
    shipped is still honoured so existing deployments keep working.
    """
    entries: dict[str, VoiceEntry] = {}
    if MODELS_DIR.is_dir():
        for directory in sorted(MODELS_DIR.iterdir()):
            if not directory.is_dir():
                continue
            model_path = directory / "model.pth"
            voice_path = directory / "default.pt"
            config_path = directory / "config.json"
            if not (model_path.is_file() and voice_path.is_file() and config_path.is_file()):
                continue
            display_name = directory.name
            gender = "neutral"
            description = ""
            meta_path = directory / "meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    display_name = str(meta.get("display_name", display_name))
                    gender = str(meta.get("gender", gender))
                    description = str(meta.get("description", description))
                except json.JSONDecodeError:
                    logger.warning("Ignoring unreadable meta.json in %s", directory)
            entries[directory.name] = VoiceEntry(
                voice_id=directory.name,
                display_name=display_name,
                backend="kokoro",
                group="kikiri",
                gender=gender,
                description=description,
                model_path=model_path,
                voice_path=voice_path,
                config_path=config_path,
            )

    if LEGACY_MODEL_PATH.is_file() and LEGACY_VOICE_PATH.is_file() and LEGACY_CONFIG_PATH.is_file():
        entries.setdefault(
            DEFAULT_MODEL_ID,
            VoiceEntry(
                voice_id=DEFAULT_MODEL_ID,
                display_name=DEFAULT_MODEL_ID,
                backend="kokoro",
                group="kikiri",
                model_path=LEGACY_MODEL_PATH,
                voice_path=LEGACY_VOICE_PATH,
                config_path=LEGACY_CONFIG_PATH,
            ),
        )
    return entries


def discover_piper() -> dict[str, VoiceEntry]:
    """Build the fallback bank from voices.json, or auto-enumerate without it.

    The catalog is curated (see scripts/build-fallback-voice-catalog.py) because
    de_DE-mls-medium alone has 236 speakers and most of them are not worth
    offering. Without a catalog the bank degrades to one preset per model file,
    which keeps the container useful after a bare `docker run`.
    """
    if piper is None:
        return {}
    models = piper.discover()
    if not models:
        return {}

    entries: dict[str, VoiceEntry] = {}
    catalog: list[dict] = []
    if PIPER_CATALOG_PATH.is_file():
        try:
            catalog = json.loads(PIPER_CATALOG_PATH.read_text(encoding="utf-8")).get("voices", [])
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Ignoring unreadable Piper catalog %s", PIPER_CATALOG_PATH)

    for item in catalog:
        model_id = str(item.get("model", ""))
        model = models.get(model_id)
        if model is None:
            logger.warning("Catalog voice %s names missing model %s", item.get("id"), model_id)
            continue
        speaker_id = int(item.get("speaker_id", 0))
        if speaker_id >= model.num_speakers:
            logger.warning("Catalog voice %s: speaker %d out of range", item.get("id"), speaker_id)
            continue
        voice_id = str(item["id"])
        entries[voice_id] = VoiceEntry(
            voice_id=voice_id,
            display_name=str(item.get("name") or voice_id),
            backend="piper",
            group="fallback",
            gender=str(item.get("gender", "neutral")),
            description=str(item.get("description", "")),
            piper_model=model_id,
            speaker_id=speaker_id,
        )

    if not entries:
        for model_id in sorted(models):
            voice_id = f"fallback-{model_id}"
            entries[voice_id] = VoiceEntry(
                voice_id=voice_id,
                display_name=model_id,
                backend="piper",
                group="fallback",
                description="Auto-erkannte Fallback-Stimme (kein Katalog gefunden)",
                piper_model=model_id,
            )
    return entries


def refresh_registry() -> dict[str, VoiceEntry]:
    global registry
    merged = discover_kokoro()
    for voice_id, entry in discover_piper().items():
        # A hand-trained fine-tune always outranks a bank preset of the same id.
        merged.setdefault(voice_id, entry)
    registry = merged
    for voice_id in list(loaded):
        if voice_id not in registry:
            loaded.pop(voice_id, None)
            logger.info("Dropped %s — no longer present on disk", voice_id)
    return registry


def default_model_id() -> str:
    if DEFAULT_MODEL_ID in registry:
        return DEFAULT_MODEL_ID
    for voice_id, entry in sorted(registry.items()):
        if entry.backend == "kokoro":
            return voice_id
    return next(iter(sorted(registry)), DEFAULT_MODEL_ID)


def load_model(voice_id: str) -> LoadedModel:
    entry = registry.get(voice_id)
    if entry is None:
        raise HTTPException(404, f"Unknown model {voice_id!r}")
    if entry.backend != "kokoro":
        raise HTTPException(400, f"{voice_id!r} is not a Kokoro fine-tune")
    existing = loaded.get(voice_id)
    if existing is not None:
        loaded.move_to_end(voice_id)
        return existing

    started = time.perf_counter()
    model = KModel(
        repo_id="hexgrad/Kokoro-82M",
        config=str(entry.config_path),
        model=str(entry.model_path),
    ).to("cpu").eval()
    pipeline = KPipeline(lang_code="d", repo_id="hexgrad/Kokoro-82M", model=model)
    voice = torch.load(entry.voice_path, map_location="cpu", weights_only=True)
    if tuple(voice.shape) != (510, 1, 256):
        raise HTTPException(500, f"Unexpected voicepack shape {tuple(voice.shape)} for {voice_id!r}")

    loaded[voice_id] = LoadedModel(pipeline=pipeline, voice=voice)
    loaded.move_to_end(voice_id)
    while len(loaded) > MAX_RESIDENT_MODELS:
        evicted, _ = loaded.popitem(last=False)
        logger.info("Evicted %s to stay within %d resident models", evicted, MAX_RESIDENT_MODELS)
    logger.info("Loaded %s on CPU in %.2fs", voice_id, time.perf_counter() - started)
    return loaded[voice_id]


@app.on_event("startup")
def startup() -> None:
    refresh_registry()
    if not registry:
        logger.error("No Kikiri models found under %s", MODELS_DIR)
        return
    kokoro = [v for v in registry.values() if v.backend == "kokoro"]
    fallback = [v for v in registry.values() if v.backend == "piper"]
    logger.info(
        "Discovered %d fine-tune(s) and %d fallback preset(s)", len(kokoro), len(fallback)
    )
    try:
        load_model(default_model_id())
    except Exception:
        logger.exception("Preloading the default model failed; it will load on first request")


def synthesize(voice_id: str, text: str, speed: float) -> tuple[bytes, float]:
    entry = registry.get(voice_id)
    if entry is None:
        raise HTTPException(404, f"Unknown model {voice_id!r}")

    if entry.backend == "piper":
        if piper is None:
            raise HTTPException(503, "Piper fallback backend is unavailable")
        waveform, rate = piper.synthesize(
            entry.piper_model,
            text,
            speaker_id=entry.speaker_id,
            speed=speed,
            target_rate=SAMPLE_RATE,
        )
    else:
        runtime = loaded.get(voice_id) or load_model(voice_id)
        chunks: list[np.ndarray] = []
        with torch.inference_mode():
            for _, _, audio in runtime.pipeline(text, voice=runtime.voice, speed=speed):
                chunks.append(np.asarray(audio, dtype=np.float32).reshape(-1))
        if not chunks:
            raise RuntimeError("Kikiri returned no audio")
        waveform, rate = np.concatenate(chunks), SAMPLE_RATE

    output = io.BytesIO()
    sf.write(output, waveform, rate, format="WAV", subtype="PCM_16")
    return output.getvalue(), len(waveform) / rate


def resolve_request(request: SpeechRequest) -> tuple[str, str]:
    text = request.input.strip()
    if not text:
        raise HTTPException(400, "Input text must not be empty")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(413, f"Input text exceeds {MAX_TEXT_CHARS} characters")
    if request.response_format.strip().lower() != "wav":
        raise HTTPException(400, "Kikiri currently supports response_format='wav' only")

    requested = (request.model or "").strip()
    voice = (request.voice or VOICE_ID).strip()
    if requested in ("", "kikiri"):
        requested = default_model_id()
    if requested not in registry:
        refresh_registry()
    if requested not in registry:
        # The gateway addresses voices through `model`; a bare client that only
        # sets `voice` should still reach the same bank.
        if voice in registry:
            requested = voice
        else:
            raise HTTPException(404, f"Unknown model {requested!r}")

    # Every registry entry carries exactly one voice. Accept the generic name,
    # the id itself, or the display name so callers can address it either way.
    entry = registry[requested]
    if voice not in (VOICE_ID, entry.voice_id, entry.display_name) and voice not in registry:
        raise HTTPException(400, f"Unknown voice {voice!r} for model {requested!r}")
    return requested, text


def _voice_payload(entry: VoiceEntry) -> dict[str, object]:
    return {
        "id": entry.voice_id,
        "object": "model",
        "owned_by": "vocarium",
        "description": entry.display_name,
        "voice": VOICE_ID,
        "loaded": entry.voice_id in loaded
        or (
            entry.backend == "piper"
            and piper is not None
            and entry.piper_model in piper.loaded_ids()
        ),
        "backend": entry.backend,
        "group": entry.group,
        "gender": entry.gender,
        "notes": entry.description,
    }


@app.get("/health")
def health() -> dict[str, object]:
    fallback = [v for v in registry.values() if v.backend == "piper"]
    return {
        "status": "healthy" if registry else "unavailable",
        "engine": "kikiri",
        "model": default_model_id(),
        "models": sorted(registry),
        "loaded": list(loaded),
        "voice": VOICE_ID,
        "device": "cpu",
        "torch_threads": TORCH_THREADS,
        "finetunes": sorted(v.voice_id for v in registry.values() if v.backend == "kokoro"),
        "fallback_voices": len(fallback),
        "fallback_available": piper is not None and bool(fallback),
        "fallback_loaded": piper.loaded_ids() if piper is not None else [],
    }


@app.get("/v1/models")
def models() -> dict[str, object]:
    refresh_registry()
    return {
        "object": "list",
        "data": [
            _voice_payload(entry)
            for entry in sorted(registry.values(), key=lambda item: (item.group != "kikiri", item.voice_id))
        ],
    }


@app.post("/v1/audio/speech")
async def speech(request: SpeechRequest) -> Response:
    model_id, text = resolve_request(request)
    started = time.perf_counter()
    try:
        async with inference_lock:
            audio, duration = await asyncio.to_thread(synthesize, model_id, text, request.speed)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Kikiri synthesis failed")
        raise HTTPException(500, "Kikiri synthesis failed") from exc
    elapsed = time.perf_counter() - started
    return Response(
        audio,
        media_type="audio/wav",
        headers={
            "X-TTS-Engine": "kikiri",
            "X-Model": model_id,
            "X-Backend": registry[model_id].backend,
            "X-Voice": VOICE_ID,
            "X-Audio-Duration": f"{duration:.3f}",
            "X-Inference-Seconds": f"{elapsed:.3f}",
        },
    )


@app.post("/v1/audio/speech/stream")
async def speech_stream(request: SpeechRequest) -> StreamingResponse:
    model_id, text = resolve_request(request)

    async def events():
        started = time.perf_counter()
        try:
            async with inference_lock:
                audio, duration = await asyncio.to_thread(synthesize, model_id, text, request.speed)
            elapsed = time.perf_counter() - started
            chunk = {
                "index": 0,
                "total": 1,
                "audio": base64.b64encode(audio).decode("ascii"),
                "duration": round(duration, 2),
                "text": text[:80],
            }
            done = {
                "total_duration": round(duration, 2),
                "generation_time": round(elapsed, 2),
                "rtf": round(elapsed / duration, 4) if duration else 0,
                "model": model_id,
                "voice": VOICE_ID,
                "chunks": 1,
            }
            yield f"event: chunk\ndata: {json.dumps(chunk)}\n\n"
            yield f"event: done\ndata: {json.dumps(done)}\n\n"
        except Exception as exc:
            logger.exception("Kikiri streaming synthesis failed")
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
