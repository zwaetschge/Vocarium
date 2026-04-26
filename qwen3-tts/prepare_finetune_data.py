#!/usr/bin/env python3
"""Prepare fine-tuning data from Vocarium voice recordings.

Scans the voices directory and creates a JSONL training file from
all stored voice samples with their transcriptions.

Usage:
    python prepare_finetune_data.py \
        --voices-dir /app/voices \
        --output /data/training.jsonl \
        --voice-id default            # specific voice, or omit for all
"""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Prepare TTS fine-tuning data")
    parser.add_argument("--voices-dir", type=str, default="/app/voices",
                        help="Path to voices directory")
    parser.add_argument("--output", type=str, required=True,
                        help="Output JSONL file path")
    parser.add_argument("--voice-id", type=str, default=None,
                        help="Specific voice ID to export (default: all)")
    parser.add_argument("--min-text-len", type=int, default=10,
                        help="Minimum text length to include (default: 10)")
    args = parser.parse_args()

    voices_dir = Path(args.voices_dir)
    if not voices_dir.exists():
        print(f"ERROR: Voices directory not found: {voices_dir}")
        return

    entries = []
    for voice_dir in sorted(voices_dir.iterdir()):
        if not voice_dir.is_dir():
            continue
        if args.voice_id and voice_dir.name != args.voice_id:
            continue

        meta_file = voice_dir / "metadata.json"
        audio_file = voice_dir / "ref_audio.wav"

        if not meta_file.exists() or not audio_file.exists():
            continue

        meta = json.loads(meta_file.read_text())
        ref_text = meta.get("ref_text", "").strip()
        language = meta.get("language", "English")

        if len(ref_text) < args.min_text_len:
            print(f"  Skipping {voice_dir.name}: text too short ({len(ref_text)} chars)")
            continue

        entries.append({
            "audio": str(audio_file.resolve()),
            "text": ref_text,
            "language": language,
        })
        print(f"  Added: {voice_dir.name} ({language}, {len(ref_text)} chars)")

    if not entries:
        print("No valid entries found.")
        return

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(entries)} entries to {output_path}")
    print(f"\nTo fine-tune, run:")
    print(f"  python finetune.py --data {output_path} --output /app/models/my-voice --epochs 10")


if __name__ == "__main__":
    main()
