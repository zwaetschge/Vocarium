#!/usr/bin/env python3
"""Probe every speaker of a multi-speaker Piper model and score it.

Curating from the upstream sample mp3s would need an mp3 decoder and would
measure a file someone else rendered. Rendering here instead measures exactly
what Vocarium will serve, and the metrics (mean F0, voiced fraction, duration
ratio) are enough to sort a 236-speaker dump into "usable, female" /
"usable, male" / "broken".

Run inside a container that has onnxruntime:
    python scripts/analyze-piper-mls-speakers.py \
        --model /piper-models/de_DE-mls-medium.onnx --out /work/.piper-tmp/mls.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

SENTENCE = "Guten Tag, ich lese Ihnen dieses Kapitel gerne vor."


def mean_f0(wave: np.ndarray, rate: int) -> tuple[float, float]:
    """Autocorrelation pitch track. Returns (median F0 Hz, voiced fraction)."""
    frame = int(rate * 0.04)
    hop = int(rate * 0.02)
    lo, hi = int(rate / 400), int(rate / 60)  # 60..400 Hz search window
    pitches: list[float] = []
    voiced = 0
    frames = 0
    for start in range(0, max(0, wave.size - frame), hop):
        chunk = wave[start : start + frame]
        if float(np.sqrt((chunk**2).mean())) < 0.02:
            continue
        frames += 1
        chunk = chunk - chunk.mean()
        corr = np.correlate(chunk, chunk, mode="full")[frame - 1 :]
        if corr[0] <= 0:
            continue
        corr = corr / corr[0]
        window = corr[lo:hi]
        if window.size == 0:
            continue
        peak = int(np.argmax(window)) + lo
        if corr[peak] < 0.3:  # unvoiced or noise
            continue
        voiced += 1
        pitches.append(rate / peak)
    if not pitches:
        return 0.0, 0.0
    return float(np.median(pitches)), voiced / max(1, frames)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kikiri-tts"))
    sys.path.insert(0, "/app")
    from piper_backend import PiperEngine

    model_path = Path(args.model)
    engine = PiperEngine(model_path.parent)
    engine.discover()
    model_id = model_path.name[: -len(".onnx")]
    model = engine.models[model_id]
    total = model.num_speakers if not args.limit else min(args.limit, model.num_speakers)
    print(f"{model_id}: {total} speakers @ {model.sample_rate} Hz", flush=True)

    results = []
    started = time.time()
    for sid in range(total):
        try:
            wave, rate = engine.synthesize(
                model_id, SENTENCE, speaker_id=sid, target_rate=model.sample_rate
            )
        except Exception as exc:  # a broken speaker must not stop the sweep
            results.append({"speaker_id": sid, "error": str(exc)})
            continue
        f0, voiced = mean_f0(wave, rate)
        results.append(
            {
                "speaker_id": sid,
                "duration": round(wave.size / rate, 3),
                "rms": round(float(np.sqrt((wave**2).mean())), 4),
                "f0": round(f0, 1),
                "voiced": round(voiced, 3),
            }
        )
        if (sid + 1) % 20 == 0:
            elapsed = time.time() - started
            print(f"  {sid + 1}/{total}  {elapsed:.0f}s", flush=True)

    Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
    ok = [r for r in results if "error" not in r]
    print(f"done: {len(ok)}/{len(results)} rendered in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
