"""Whisper STT service for Vocarium.

Serves Flix-AI/flix-swissgerman-full (whisper-large-v3 finetune, strong on
Swiss German and standard German) via faster-whisper/CTranslate2 in
int8_float16 — ~2.5 GB VRAM, so it coexists with the resident OmniVoice on
the RTX 3060. Lazy-loads on first request, unloads after IDLE_TIMEOUT.

On first start the HF checkpoint is converted once to CTranslate2 format
under $HF_HOME/ct2/.

API (OpenAI-flavoured, superset):
  POST /v1/audio/transcriptions  multipart: file, language?, task?
      -> {text, language, segments: [{start, end, text}], words: [...]}
  GET  /health                   {status, model_loaded}
  POST /unload                   force-free VRAM
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whisper-stt")

FLIX_MODEL_ID = os.environ.get("WHISPER_MODEL", "Flix-AI/flix-swissgerman-full")
# Zwei Profile: "german" = Vanilla large-v3 (zuverlässig für Standarddeutsch —
# das Flix-Finetune driftet dort teils in englische Übersetzungen ab),
# "swiss" = Flix-Finetune für schweizerdeutsche Aufnahmen. Es ist immer nur
# EIN Modell resident; ein Wechsel entlädt das andere.
DEFAULT_PROFILE = os.environ.get("WHISPER_DEFAULT_PROFILE", "german")
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "300"))
DEVICE_INDEX = int(os.environ.get("WHISPER_GPU", "0"))
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8_float16")
HF_HOME = Path(os.environ.get("HF_HOME", "/models"))
CT2_DIR = HF_HOME / "ct2" / FLIX_MODEL_ID.replace("/", "--")

app = FastAPI(title="Vocarium Whisper STT")

_model = None
_model_profile = ""
_lock = asyncio.Lock()
_infer_lock = None
_last_used = 0.0


def _resolve_profile(requested: str) -> str:
    req = (requested or "").strip().lower()
    if req in ("swiss", "schweizerdeutsch", "ch", "flix") or "swiss" in req:
        return "swiss"
    return "german"


def _ensure_ct2_model() -> None:
    """One-time HF → CTranslate2 conversion (CPU, cached on the models volume)."""
    if (CT2_DIR / "model.bin").is_file():
        return
    logger.info("Converting %s to CTranslate2 at %s ...", FLIX_MODEL_ID, CT2_DIR)
    CT2_DIR.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["ct2-transformers-converter", "--model", FLIX_MODEL_ID,
         "--output_dir", str(CT2_DIR), "--quantization", "float16", "--force"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"CT2 conversion failed: {proc.stderr[-800:]}")
    # Das Flix-Repo liefert keine preprocessor_config.json; large-v3 braucht
    # 128 Mel-Bins (Default wäre 80 → "Invalid input features shape").
    import json

    (CT2_DIR / "preprocessor_config.json").write_text(json.dumps({
        "feature_size": 128,
        "sampling_rate": 16000,
        "hop_length": 160,
        "n_fft": 400,
        "chunk_length": 30,
        "feature_extractor_type": "WhisperFeatureExtractor",
        "processor_class": "WhisperProcessor",
    }), encoding="utf-8")
    logger.info("Conversion done")


def _load_model(profile: str):
    global _model, _model_profile, _infer_lock
    import threading

    from faster_whisper import WhisperModel

    if profile == "swiss":
        _ensure_ct2_model()
        source = str(CT2_DIR)
    else:
        source = "large-v3"  # vorgewandelte CT2-Gewichte von Systran
    logger.info("Loading %s (%s, %s) on cuda:%d ...", profile, source, COMPUTE_TYPE, DEVICE_INDEX)
    t0 = time.time()
    _model = WhisperModel(
        source, device="cuda", device_index=DEVICE_INDEX,
        compute_type=COMPUTE_TYPE, download_root=str(HF_HOME / "ct2-vanilla"),
    )
    _model_profile = profile
    _infer_lock = threading.Lock()
    logger.info("Model loaded in %.1fs", time.time() - t0)


def _unload_model():
    global _model, _model_profile
    if _model is None:
        return
    logger.info("Unloading model (%s)", _model_profile)
    _model = None
    _model_profile = ""


async def _ensure_loaded(profile: str):
    global _last_used
    async with _lock:
        if _model is None or _model_profile != profile:
            if _model is not None:
                _unload_model()
            await asyncio.to_thread(_load_model, profile)
        _last_used = time.time()


@app.on_event("startup")
async def _idle_watchdog():
    async def loop():
        while True:
            await asyncio.sleep(30)
            if _model is not None and time.time() - _last_used > IDLE_TIMEOUT:
                async with _lock:
                    if _model is not None and time.time() - _last_used > IDLE_TIMEOUT:
                        _unload_model()

    asyncio.create_task(loop())


def _decode_audio(raw: bytes, suffix: str) -> np.ndarray:
    """Any input container -> 16 kHz mono float32 via ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=suffix or ".bin") as src:
        src.write(raw)
        src.flush()
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", src.name, "-f", "f32le",
             "-ac", "1", "-ar", "16000", "-"],
            capture_output=True,
        )
    if proc.returncode != 0:
        raise HTTPException(400, f"Audio decode failed: {proc.stderr.decode(errors='replace')[:300]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _collect_segments(audio: np.ndarray, language: str | None, task: str, word_timestamps: bool,
                      vad_filter: bool = True):
    segments_iter, info = _model.transcribe(
        audio, language=language, task=task,
        beam_size=5, vad_filter=vad_filter, word_timestamps=word_timestamps,
        condition_on_previous_text=False,
    )
    segments, words, texts = [], [], []
    for seg in segments_iter:
        text = seg.text.strip()
        texts.append(text)
        segments.append({
            "start": round(seg.start, 3), "end": round(seg.end, 3), "text": text,
        })
        for w in seg.words or []:
            words.append({
                "word": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3),
            })
    return segments, words, texts, info


# Silero-VAD stuft Gesang über Musik als "keine Sprache" ein: ein ganzer Song
# ergab 0 Segmente bzw. einen einzigen Satz. Unterhalb dieser Abdeckung wird
# ohne VAD wiederholt; Whisper selbst kommt mit Gesang gut zurecht.
VAD_MIN_COVERAGE = float(os.getenv("VAD_MIN_COVERAGE", "0.2"))


def _run(audio: np.ndarray, language: str | None, task: str, vad_filter: bool):
    try:
        return _collect_segments(audio, language, task, True, vad_filter)
    except IndexError as error:
        # faster-whisper's find_alignment() crashes when a decoded segment
        # yields no alignable text tokens (empty boolean index). Segment
        # timestamps are still valid, so retry without word alignment.
        logger.warning("Word-timestamp alignment failed (%s); retrying without word timestamps", error)
        return _collect_segments(audio, language, task, False, vad_filter)


def _coverage(segments: list[dict], audio: np.ndarray) -> float:
    total = len(audio) / 16000
    if total <= 0:
        return 1.0
    return sum(max(0.0, s["end"] - s["start"]) for s in segments) / total


def _transcribe(audio: np.ndarray, language: str | None, task: str, vad: str = "auto") -> dict:
    """vad: "auto" (VAD, Rückfall ohne VAD bei geringer Abdeckung), "on", "off"."""
    with _infer_lock:
        used_vad = vad != "off"
        segments, words, texts, info = _run(audio, language, task, used_vad)
        if vad == "auto" and used_vad:
            coverage = _coverage(segments, audio)
            if coverage < VAD_MIN_COVERAGE and len(audio) / 16000 > 5:
                logger.info("VAD kept only %.0f%% of %.0fs audio; retrying without VAD",
                            coverage * 100, len(audio) / 16000)
                segments, words, texts, info = _run(audio, language, task, False)
                used_vad = False
    return {
        "text": " ".join(texts).strip(),
        "language": info.language,
        "segments": segments,
        "words": words,
        "vad": used_vad,
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "profiles": {"german": "large-v3", "swiss": FLIX_MODEL_ID},
        "default_profile": DEFAULT_PROFILE,
        "compute_type": COMPUTE_TYPE,
        "model_loaded": _model is not None,
        "loaded_profile": _model_profile,
        "idle_timeout": IDLE_TIMEOUT,
    }


@app.get("/v1/models")
async def list_models():
    """OpenAI-kompatible Modell-Liste.

    Clients zaehlen darueber die Profile auf; als billiger Preflight taugt er
    ausserdem, weil er kein Modell laedt.
    """
    return {
        "object": "list",
        "data": [
            {"id": "german", "object": "model", "owned_by": "whisper-stt"},
            {"id": "swiss", "object": "model", "owned_by": "whisper-stt"},
        ],
    }


@app.post("/unload")
async def unload():
    async with _lock:
        was_loaded = _model is not None
        _unload_model()
    return {"ok": True, "was_running": was_loaded}


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    language: str = Form(""),
    task: str = Form("transcribe"),
    model: str = Form(""),  # "german" (default) | "swiss" für das Flix-Finetune
    vad: str = Form("auto"),  # "auto" | "on" | "off" – Sprachfilter; auto fällt bei Gesang zurück
):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty upload")
    if len(raw) > 200 * 1024 * 1024:
        raise HTTPException(413, "Upload larger than 200 MB")
    suffix = os.path.splitext(file.filename or "")[1].lower()
    audio = await asyncio.to_thread(_decode_audio, raw, suffix)
    if not len(audio):
        raise HTTPException(400, "No audio samples decoded")

    profile = _resolve_profile(model or DEFAULT_PROFILE)
    await _ensure_loaded(profile)
    lang = language.strip().lower() or None
    # Whisper erwartet ISO-Kürzel; Vocarium reicht teils Klartext durch.
    aliases = {"german": "de", "deutsch": "de", "schweizerdeutsch": "de", "english": "en",
               "french": "fr", "französisch": "fr", "italian": "it", "italienisch": "it"}
    lang = aliases.get(lang, lang)
    if lang is None:
        # Ohne Vorgabe driftet das Finetune bei Auto-Detection ins Englische
        # (übersetzt statt transkribiert). Default deshalb Deutsch; echte
        # Auto-Detection nur auf ausdrücklichen Wunsch (language=auto).
        lang = "de"
    elif lang == "auto":
        lang = None
    vad_mode = vad.strip().lower() if vad.strip().lower() in ("auto", "on", "off") else "auto"
    result = await asyncio.to_thread(_transcribe, audio, lang, task, vad_mode)
    global _last_used
    _last_used = time.time()
    return {
        **result,
        "model": FLIX_MODEL_ID if profile == "swiss" else "large-v3",
        "profile": profile,
        "duration": round(len(audio) / 16000, 2),
    }
