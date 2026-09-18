#!/usr/bin/env python3
"""Quality-filter ASR clips and produce Kikiri/StyleTTS2 training lists."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import wave
from collections import Counter
from pathlib import Path

import numpy as np
from misaki import espeak

from kokoro_symbols import dicts


SPEAKER_ID = "0"
SAMPLE_RATE = 24_000
VALIDATION_EVERY_NTH_SOURCE = 20
MIN_VALIDATION_CLIPS = 60
MAX_VALIDATION_CLIPS = 200
OOD_SENTENCES = [
    "Die Bundesrepublik Deutschland ist ein demokratischer Staat.",
    "Morgen wird es regnen, nehmen Sie einen Regenschirm mit.",
    "Der schnelle braune Fuchs springt über den faulen Hund.",
    "Können Sie mir bitte den Weg zum Bahnhof zeigen?",
    "Wissenschaftler haben eine bahnbrechende Entdeckung gemacht.",
    "Das Frühstück war ausgezeichnet, besonders die frischen Brötchen.",
    "Diese Aufgabe erfordert besondere Sorgfalt und Aufmerksamkeit.",
    "Die Zugverbindung zwischen München und Berlin dauert etwa vier Stunden.",
    "Könnten Sie mir erklären, wie dieses Gerät funktioniert?",
    "Bitte vergessen Sie nicht, die Tür abzuschließen, wenn Sie gehen.",
]


def dbfs(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return -120.0
    values = samples.astype(np.float32)
    rms = float(np.sqrt(np.mean(values * values)))
    return 20.0 * math.log10(max(rms, 1.0) / 32768.0)


def longest_quiet_run(quiet: np.ndarray) -> int:
    """Longest contiguous stretch of below-floor frames, in frames."""
    if not len(quiet):
        return 0
    if quiet.all():
        return len(quiet)
    loud = np.flatnonzero(~quiet)
    bounds = np.concatenate(([-1], loud, [len(quiet)]))
    return int((np.diff(bounds) - 1).max())


def audio_metrics(path: Path) -> dict[str, float | int]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").copy()
    edge = min(len(audio), round(sample_rate * 0.04))
    frame = max(1, round(sample_rate * 0.02))
    usable = len(audio) // frame * frame
    frame_rms = np.sqrt(
        np.mean(audio[:usable].reshape(-1, frame).astype(np.float32) ** 2, axis=1)
    ) if usable else np.empty(0)
    frame_levels = 20.0 * np.log10(np.maximum(frame_rms, 1.0) / 32768.0)
    quiet = frame_levels <= -45.0
    return {
        "max_silence_run_seconds": round(longest_quiet_run(quiet) * 0.02, 3),
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "rms_dbfs": round(dbfs(audio), 3),
        "peak_dbfs": round(20.0 * math.log10(max(float(np.max(np.abs(audio))), 1.0) / 32768.0), 3),
        "start_dbfs": round(dbfs(audio[:edge]), 3),
        "end_dbfs": round(dbfs(audio[-edge:]), 3),
        "silence_fraction": round(float(np.mean(frame_levels <= -45.0)), 4) if len(frame_levels) else 1.0,
        "clipping_fraction": round(float(np.mean(np.abs(audio.astype(np.int32)) >= 32760)), 6),
    }


def source_order(manifest_rows: list[dict[str, object]]) -> dict[str, int]:
    """Map each source file to its position in the book.

    The old implementation parsed "Kapitel <n>" out of the filename, which silently
    returned -1 for every other naming scheme and collapsed the validation split.
    Ordering by first appearance in the manifest works for any audiobook.
    """
    order: dict[str, int] = {}
    for row in manifest_rows:
        source = str(row["source"])
        if source not in order:
            order[source] = len(order)
    return order


def clean_training_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("|", ",")).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--speaker", required=True, help="Dataset speaker directory name.")
    parser.add_argument(
        "--max-silence-run-seconds",
        type=float,
        default=2.0,
        help="Reject a clip holding a contiguous silent block longer than this.",
    )
    args = parser.parse_args()
    speaker = args.speaker
    root = args.root.resolve()
    prepared = root / "prepared"
    clips_dir = prepared / "clips"
    manifest_rows = [json.loads(line) for line in (prepared / "segments.jsonl").read_text(encoding="utf-8").splitlines() if line]
    transcripts: dict[str, dict[str, object]] = {}
    for line in (prepared / "transcriptions.jsonl").read_text(encoding="utf-8").splitlines():
        if line:
            row = json.loads(line)
            transcripts[row["clip"]] = row

    missing = [row["clip"] for row in manifest_rows if row["clip"] not in transcripts]
    if missing:
        raise SystemExit(f"Transcriptions incomplete: {len(missing)} clips missing")

    output_audio = root / "dataset" / "audio" / speaker
    training_dir = root / "training"
    output_audio.mkdir(parents=True, exist_ok=True)
    training_dir.mkdir(parents=True, exist_ok=True)
    order = source_order(manifest_rows)
    g2p = espeak.EspeakG2P(language="de")

    kept: list[dict[str, object]] = []
    qa_rows: list[dict[str, object]] = []
    reason_counts: Counter[str] = Counter()
    unknown_symbols: Counter[str] = Counter()

    for segment in manifest_rows:
        clip = str(segment["clip"])
        transcript = transcripts[clip]
        reasons: list[str] = []
        text = clean_training_text(str(transcript.get("text", "")))
        duration = float(segment["duration_seconds"])
        words = len(text.split())
        word_rate = words / duration if duration else 0.0
        chapter = order[str(segment["source"])]
        metrics = audio_metrics(clips_dir / clip)

        if transcript.get("status") != "ok":
            reasons.append("asr_error")
        if not 5.0 <= duration <= 20.0:
            reasons.append("duration")
        if words < 5:
            reasons.append("too_few_words")
        if not 0.65 <= word_rate <= 4.2:
            reasons.append("word_rate")
        if int(metrics["channels"]) != 1 or int(metrics["sample_width"]) != 2 or int(metrics["sample_rate"]) != SAMPLE_RATE:
            reasons.append("audio_format")
        if not -35.0 <= float(metrics["rms_dbfs"]) <= -8.0:
            reasons.append("rms")
        # A high aggregate silence fraction is normal for narration: even accepted
        # clips sit near 0.29, because every inter-word gap and stop closure falls
        # below the -45 dBFS floor. What actually breaks duration modelling is a
        # single long block of dead air with no corresponding token in the text.
        if float(metrics["max_silence_run_seconds"]) > args.max_silence_run_seconds:
            reasons.append("dead_air")
        if float(metrics["silence_fraction"]) > 0.55:
            reasons.append("too_much_silence")
        if float(metrics["clipping_fraction"]) > 0.001:
            reasons.append("clipping")
        if float(metrics["start_dbfs"]) > -28.0 or float(metrics["end_dbfs"]) > -28.0:
            reasons.append("hard_cut")

        phonemes = ""
        if not reasons:
            try:
                phonemes, _ = g2p(text)
                phonemes = phonemes.replace("ʏ", "y")
            except Exception:
                reasons.append("g2p_error")
        if phonemes:
            unknown = sorted(set(phonemes) - set(dicts))
            if unknown:
                reasons.append("unknown_phoneme")
                unknown_symbols.update(unknown)
            if not 50 <= len(phonemes) <= 510:
                reasons.append("phoneme_length")

        for reason in set(reasons):
            reason_counts[reason] += 1
        qa_rows.append(
            {
                **segment,
                "chapter": chapter,
                "text": text,
                "words": words,
                "words_per_second": round(word_rate, 3),
                **metrics,
                "kept": not reasons,
                "reasons": reasons,
            }
        )
        if reasons:
            continue

        destination = output_audio / clip
        source = clips_dir / clip
        if not destination.exists():
            os.link(source, destination)
        kept.append(
            {
                **segment,
                "chapter": chapter,
                "text": text,
                "phonemes": phonemes,
                "relative_wav": f"{speaker}/{clip}",
            }
        )

    train = [row for row in kept if int(row["chapter"]) % VALIDATION_EVERY_NTH_SOURCE != 0]
    validation = [row for row in kept if int(row["chapter"]) % VALIDATION_EVERY_NTH_SOURCE == 0]
    # Selecting a checkpoint by validation loss is only meaningful if the split is
    # big enough to be stable. Striding over source files can land on a handful of
    # short chapters — a 2749-clip run produced 15 validation clips that way — so
    # top the split up with evenly spread clips until it carries some weight.
    target = min(MAX_VALIDATION_CLIPS, max(MIN_VALIDATION_CLIPS, round(len(kept) * 0.05)))
    if kept and len(validation) < target:
        chosen = {row["clip"] for row in validation}
        stride = max(1, len(train) // (target - len(validation)))
        for row in train[::stride]:
            if len(validation) >= target:
                break
            chosen.add(row["clip"])
            validation.append(row)
        train = [row for row in train if row["clip"] not in chosen]

    def write_list(path: Path, rows: list[dict[str, object]]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(f'{row["relative_wav"]}|{row["phonemes"]}|{SPEAKER_ID}\n')

    write_list(training_dir / "train_list.txt", train)
    write_list(training_dir / "val_list.txt", validation)
    with (root / "dataset" / "metadata.csv").open("w", encoding="utf-8") as handle:
        handle.write("filename|text|speaker\n")
        for row in kept:
            handle.write(f'{row["relative_wav"]}|{row["text"]}|{speaker}\n')
    with (root / "dataset" / "phonemes.csv").open("w", encoding="utf-8") as handle:
        handle.write("filename|ipa\n")
        for row in kept:
            handle.write(f'{row["relative_wav"]}|{row["phonemes"]}\n')
    with (prepared / "qa.jsonl").open("w", encoding="utf-8") as handle:
        for row in qa_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (prepared / "accepted.jsonl").open("w", encoding="utf-8") as handle:
        for row in kept:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    ood = []
    for text in OOD_SENTENCES:
        phonemes, _ = g2p(text)
        ood.append(phonemes.replace("ʏ", "y"))
    (training_dir / "OOD_texts.txt").write_text("\n".join(ood) + "\n", encoding="utf-8")

    summary = {
        "input_clips": len(manifest_rows),
        "accepted_clips": len(kept),
        "rejected_clips": len(manifest_rows) - len(kept),
        "accepted_hours": round(sum(float(row["duration_seconds"]) for row in kept) / 3600, 3),
        "train_clips": len(train),
        "validation_clips": len(validation),
        "rejection_reasons": dict(reason_counts.most_common()),
        "unknown_symbols": dict(unknown_symbols),
        "speaker": speaker,
    }
    (prepared / "qa_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
