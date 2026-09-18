#!/usr/bin/env python3
"""Split audiobook chapters into pause-aligned PCM WAV training clips.

The source files are never modified. Audio is decoded to mono 24 kHz PCM16,
then cut at the centre of natural pauses close to the requested target length.
Each output clip is recorded in a JSONL manifest for reproducibility.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


SAMPLE_RATE = 24_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1_000


@dataclass(frozen=True)
class Segment:
    clip: str
    source: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float


def natural_key(path: Path) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name))


AUDIO_SUFFIXES = (".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".wav")


def probe_seconds(path: Path) -> float:
    """Container duration without decoding the stream."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        return float(result.stdout.decode().strip())
    except ValueError:
        return 0.0


def select_sources(paths: list[Path], max_hours: float | None) -> list[Path]:
    """Spread the selection evenly across the book instead of taking a prefix.

    A prefix would over-represent one narrative section; an even stride keeps the
    range of narration (calm exposition, dialogue, tension) that a voice model
    needs to generalise.
    """
    if max_hours is None or not paths:
        return paths
    durations = [probe_seconds(path) for path in paths]
    budget = max_hours * 3_600
    if sum(durations) <= budget:
        return paths
    mean = sum(durations) / len(durations)
    keep = max(1, min(len(paths), round(budget / mean) if mean else len(paths)))
    stride = len(paths) / keep
    picked = [paths[min(len(paths) - 1, round(index * stride))] for index in range(keep)]
    # Deduplicate while preserving order; rounding can collide on short books.
    seen: set[Path] = set()
    return [path for path in picked if not (path in seen or seen.add(path))]


def decode(path: Path) -> np.ndarray:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return np.frombuffer(result.stdout, dtype="<i2").copy()


def frame_dbfs(audio: np.ndarray) -> np.ndarray:
    usable = len(audio) // FRAME_SAMPLES * FRAME_SAMPLES
    if usable == 0:
        return np.empty(0, dtype=np.float32)
    frames = audio[:usable].reshape(-1, FRAME_SAMPLES).astype(np.float32)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1.0) / 32768.0)


def silence_centres(dbfs: np.ndarray, threshold_db: float, minimum_ms: int) -> list[int]:
    minimum_frames = max(1, math.ceil(minimum_ms / FRAME_MS))
    quiet = dbfs <= threshold_db
    centres: list[int] = []
    run_start: int | None = None
    for index, is_quiet in enumerate(np.append(quiet, False)):
        if is_quiet and run_start is None:
            run_start = index
        elif not is_quiet and run_start is not None:
            if index - run_start >= minimum_frames:
                centres.append((run_start + index) * FRAME_SAMPLES // 2)
            run_start = None
    return centres


def active_bounds(dbfs: np.ndarray, threshold_db: float) -> tuple[int, int]:
    active = np.flatnonzero(dbfs > threshold_db)
    if len(active) == 0:
        return 0, 0
    padding = SAMPLE_RATE * 120 // 1_000
    start = max(0, int(active[0] * FRAME_SAMPLES) - padding)
    end = min(len(dbfs) * FRAME_SAMPLES, int((active[-1] + 1) * FRAME_SAMPLES) + padding)
    return start, end


def choose_boundaries(
    start: int,
    end: int,
    pauses: list[int],
    target_seconds: float,
    minimum_seconds: float,
    maximum_seconds: float,
) -> list[int]:
    target = round(target_seconds * SAMPLE_RATE)
    minimum = round(minimum_seconds * SAMPLE_RATE)
    maximum = round(maximum_seconds * SAMPLE_RATE)
    boundaries = [start]
    pause_index = 0

    while end - boundaries[-1] > maximum:
        current = boundaries[-1]
        low = current + minimum
        high = min(end, current + maximum)
        while pause_index < len(pauses) and pauses[pause_index] < low:
            pause_index += 1
        candidates: list[int] = []
        cursor = pause_index
        while cursor < len(pauses) and pauses[cursor] <= high:
            candidates.append(pauses[cursor])
            cursor += 1
        if candidates:
            cut = min(candidates, key=lambda point: abs(point - (current + target)))
        else:
            # A hard cut is rare in narrated speech, but keeps pathological
            # music/noise sections from producing oversized training samples.
            cut = min(current + target, high)
        boundaries.append(cut)

    if end - boundaries[-1] < minimum and len(boundaries) > 1:
        if end - boundaries[-2] <= maximum:
            boundaries.pop()
    boundaries.append(end)
    return boundaries


def write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(audio.astype("<i2", copy=False).tobytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--target-seconds", type=float, default=15.0)
    parser.add_argument("--minimum-seconds", type=float, default=5.0)
    parser.add_argument("--maximum-seconds", type=float, default=20.0)
    parser.add_argument("--silence-db", type=float, default=-42.0)
    parser.add_argument("--silence-ms", type=int, default=180)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--max-hours",
        type=float,
        help="Cap the source material, sampled evenly across the book.",
    )
    parser.add_argument(
        "--exclude-pattern",
        default=r"(?i)\b(vorspann|abspann|inschrift|impressum|credits?)\b",
        help="Regex of source filenames to skip (front/back matter).",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    clips_dir = output / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    inputs = sorted(
        (path for path in source.iterdir() if path.suffix.casefold() in AUDIO_SUFFIXES),
        key=natural_key,
    )
    if args.exclude_pattern:
        excluded = re.compile(args.exclude_pattern)
        inputs = [path for path in inputs if not excluded.search(path.name)]
    inputs = select_sources(inputs, args.max_hours)
    if args.limit is not None:
        inputs = inputs[: args.limit]
    if not inputs:
        raise SystemExit(f"No audio files found in {source}")
    print(f"sources={len(inputs)}", flush=True)

    segments: list[Segment] = []
    clip_number = 1
    for source_number, path in enumerate(inputs, start=1):
        audio = decode(path)
        levels = frame_dbfs(audio)
        start, end = active_bounds(levels, args.silence_db)
        if end - start < args.minimum_seconds * SAMPLE_RATE:
            continue
        pauses = silence_centres(levels, args.silence_db, args.silence_ms)
        boundaries = choose_boundaries(
            start,
            min(end, len(audio)),
            pauses,
            args.target_seconds,
            args.minimum_seconds,
            args.maximum_seconds,
        )
        for left, right in zip(boundaries, boundaries[1:]):
            if right <= left:
                continue
            clip_name = f"clip_{clip_number:06d}.wav"
            write_wav(clips_dir / clip_name, audio[left:right])
            segments.append(
                Segment(
                    clip=clip_name,
                    source=path.name,
                    start_seconds=round(left / SAMPLE_RATE, 3),
                    end_seconds=round(right / SAMPLE_RATE, 3),
                    duration_seconds=round((right - left) / SAMPLE_RATE, 3),
                )
            )
            clip_number += 1
        print(
            f"[{source_number}/{len(inputs)}] {path.name}: "
            f"{len(boundaries) - 1} clips",
            flush=True,
        )

    manifest = output / "segments.jsonl"
    with manifest.open("w", encoding="utf-8") as handle:
        for segment in segments:
            handle.write(json.dumps(asdict(segment), ensure_ascii=False) + "\n")

    durations = [segment.duration_seconds for segment in segments]
    summary = {
        "source_files": len(inputs),
        "clips": len(segments),
        "hours": round(sum(durations) / 3_600, 3),
        "minimum_seconds": round(min(durations), 3) if durations else None,
        "mean_seconds": round(float(np.mean(durations)), 3) if durations else None,
        "maximum_seconds": round(max(durations), 3) if durations else None,
        "hard_limit_seconds": args.maximum_seconds,
        "sample_rate": SAMPLE_RATE,
    }
    with (output / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
