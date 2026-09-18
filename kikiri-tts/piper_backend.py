"""Piper (VITS/ONNX) inference for the Vocarium fallback speech engine.

Why a hand-rolled backend instead of the ``piper-tts`` package: upstream
phonemizes through ``piper_phonemize``, a compiled C++ library with no
maintained wheel for this image's Python. The German voices Vocarium ships have
a ``phoneme_id_map`` made exclusively of single codepoints, so espeak-ng via
phonemizer-fork — already installed here for the Kokoro frontend — plus a
per-codepoint lookup reproduces the very same id sequence with no new native
dependency beyond onnxruntime.

One model file can carry many speakers (``de_DE-mls-medium`` has 236), which is
the whole reason the fallback bank can offer dozens of presets from ~470 MB on
disk instead of ~330 MB per voice like the Kokoro fine-tunes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

PAD, BOS, EOS = "_", "^", "$"

# Piper was trained on single sentences; whole paragraphs drift in prosody and
# start dropping words past roughly this length.
MAX_SENTENCE_CHARS = max(80, int(os.environ.get("PIPER_MAX_SENTENCE_CHARS", "320")))
SENTENCE_GAP_SECONDS = float(os.environ.get("PIPER_SENTENCE_GAP", "0.18"))
ONNX_THREADS = max(1, int(os.environ.get("PIPER_ONNX_THREADS", "4")))

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")

_espeak_lock = threading.Lock()
_espeak_backends: dict[str, object] = {}


def _espeak_backend(voice: str):
    """Lazily build (and cache) a phonemizer backend for an espeak voice."""
    with _espeak_lock:
        cached = _espeak_backends.get(voice)
        if cached is not None:
            return cached
        import espeakng_loader
        from phonemizer.backend import EspeakBackend
        from phonemizer.backend.espeak.wrapper import EspeakWrapper

        EspeakWrapper.set_library(espeakng_loader.get_library_path())
        EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
        backend = EspeakBackend(
            voice,
            preserve_punctuation=True,
            with_stress=True,
            language_switch="remove-flags",
        )
        _espeak_backends[voice] = backend
        return backend


def split_sentences(text: str) -> list[str]:
    parts: list[str] = []
    for raw in _SENTENCE_SPLIT.split(text.strip()):
        chunk = raw.strip()
        while len(chunk) > MAX_SENTENCE_CHARS:
            cut = chunk.rfind(" ", 0, MAX_SENTENCE_CHARS)
            if cut <= 0:
                cut = MAX_SENTENCE_CHARS
            head = chunk[:cut].strip()
            if head:
                parts.append(head)
            chunk = chunk[cut:].strip()
        if chunk:
            parts.append(chunk)
    return parts or [text.strip()]


def phoneme_ids(phonemes: str, id_map: dict[str, list[int]]) -> np.ndarray:
    """Reproduce piper's ``phonemes_to_ids``: BOS, then every phoneme padded."""
    pad = id_map[PAD][0]
    ids: list[int] = [id_map[BOS][0], pad]
    for char in phonemes:
        mapped = id_map.get(char)
        if not mapped:
            continue
        ids.append(mapped[0])
        ids.append(pad)
    ids.append(id_map[EOS][0])
    return np.asarray(ids, dtype=np.int64)


def resample(wave: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or wave.size == 0:
        return wave
    try:  # soxr gives a proper polyphase filter when the wheel is available
        import soxr

        return np.asarray(soxr.resample(wave, src_rate, dst_rate), dtype=np.float32)
    except Exception:
        # Every Piper rate here (16k / 22.05k) is below the 24k target, so this
        # only ever upsamples — linear interpolation adds mild imaging, never
        # aliasing.
        count = int(round(wave.size * dst_rate / src_rate))
        if count <= 0:
            return wave[:0]
        positions = np.linspace(0.0, wave.size - 1, num=count, dtype=np.float64)
        return np.interp(positions, np.arange(wave.size, dtype=np.float64), wave).astype(np.float32)


@dataclass(frozen=True)
class PiperModel:
    model_id: str
    onnx_path: Path
    config: dict

    @property
    def sample_rate(self) -> int:
        return int(self.config.get("audio", {}).get("sample_rate", 22050))

    @property
    def num_speakers(self) -> int:
        return int(self.config.get("num_speakers", 1))

    @property
    def speaker_id_map(self) -> dict[str, int]:
        return {str(k): int(v) for k, v in (self.config.get("speaker_id_map") or {}).items()}

    @property
    def espeak_voice(self) -> str:
        return str(self.config.get("espeak", {}).get("voice", "de"))

    @property
    def defaults(self) -> tuple[float, float, float]:
        inference = self.config.get("inference") or {}
        return (
            float(inference.get("noise_scale", 0.667)),
            float(inference.get("length_scale", 1.0)),
            float(inference.get("noise_w", 0.8)),
        )


class PiperEngine:
    """Registry + LRU session cache over the Piper ONNX files on disk."""

    def __init__(self, models_dir: Path, max_resident: int = 2) -> None:
        self.models_dir = models_dir
        self.max_resident = max(1, max_resident)
        self.models: dict[str, PiperModel] = {}
        self._sessions: "OrderedDict[str, object]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.models)

    def discover(self) -> dict[str, PiperModel]:
        found: dict[str, PiperModel] = {}
        if self.models_dir.is_dir():
            for config_path in sorted(self.models_dir.glob("*.onnx.json")):
                onnx_path = config_path.with_suffix("")  # strips the trailing .json
                if not onnx_path.is_file():
                    logger.warning("Piper config %s has no matching .onnx", config_path.name)
                    continue
                try:
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    logger.warning("Ignoring unreadable Piper config %s", config_path.name)
                    continue
                model_id = onnx_path.name[: -len(".onnx")]
                found[model_id] = PiperModel(model_id=model_id, onnx_path=onnx_path, config=config)
        with self._lock:
            self.models = found
            for stale in [key for key in self._sessions if key not in found]:
                self._sessions.pop(stale, None)
        return found

    def _session(self, model: PiperModel):
        with self._lock:
            existing = self._sessions.get(model.model_id)
            if existing is not None:
                self._sessions.move_to_end(model.model_id)
                return existing
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = ONNX_THREADS
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        session = onnxruntime.InferenceSession(
            str(model.onnx_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        with self._lock:
            self._sessions[model.model_id] = session
            self._sessions.move_to_end(model.model_id)
            while len(self._sessions) > self.max_resident:
                evicted, _ = self._sessions.popitem(last=False)
                logger.info("Evicted Piper session %s", evicted)
        return session

    def loaded_ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    def synthesize(
        self,
        model_id: str,
        text: str,
        *,
        speaker_id: int = 0,
        speed: float = 1.0,
        target_rate: int = 24000,
    ) -> tuple[np.ndarray, int]:
        model = self.models.get(model_id)
        if model is None:
            raise KeyError(model_id)
        session = self._session(model)
        backend = _espeak_backend(model.espeak_voice)
        id_map = model.config["phoneme_id_map"]
        noise_scale, length_scale, noise_w = model.defaults
        # speed > 1 must shorten the audio, and length_scale stretches it.
        scales = np.asarray(
            [noise_scale, length_scale / max(0.1, speed), noise_w], dtype=np.float32
        )

        sentences = split_sentences(text)
        phonemized = backend.phonemize(sentences, strip=True)
        gap = np.zeros(int(model.sample_rate * SENTENCE_GAP_SECONDS), dtype=np.float32)

        pieces: list[np.ndarray] = []
        for phonemes in phonemized:
            ids = phoneme_ids(phonemes, id_map)
            if ids.size <= 3:  # BOS + PAD + EOS only: nothing pronounceable
                continue
            inputs = {
                "input": ids[None, :],
                "input_lengths": np.asarray([ids.size], dtype=np.int64),
                "scales": scales,
            }
            if model.num_speakers > 1:
                inputs["sid"] = np.asarray([speaker_id], dtype=np.int64)
            audio = session.run(None, inputs)[0]
            pieces.append(np.asarray(audio, dtype=np.float32).reshape(-1))
            pieces.append(gap)

        if not pieces:
            raise RuntimeError("Piper produced no audio for the requested text")
        waveform = np.concatenate(pieces[:-1])  # drop the trailing gap

        # Normalise once over the whole utterance: per-sentence normalisation
        # (what upstream does) makes the level jump between sentences.
        peak = float(np.max(np.abs(waveform))) if waveform.size else 0.0
        if peak > 0:
            waveform = (waveform / peak * 0.95).astype(np.float32)
        return resample(waveform, model.sample_rate, target_rate), target_rate
