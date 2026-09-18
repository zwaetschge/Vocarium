#!/usr/bin/env python3
"""Back-transcribe synthesised speech and report a normalised German word error rate.

Intelligibility is the one quality signal that can be measured without listening:
if ASR recovers the prompt, the voice is at least articulate. It says nothing about
naturalness, so treat a low WER as a floor, not a verdict.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import httpx

SENTENCES = [
    "Hallo, dies ist ein verständlicher deutscher Testsatz mit der Standardstimme.",
    "Schön, dass du da bist. Die Bücher liegen auf dem großen Tisch.",
    "Ich mache mich auf den Weg nach Aachen, um auch nachts wach zu sein.",
    "Zwei weiße Zwerge zwängen sich zwischen zwei Zweige.",
    "Warum hast du das getan? Das ist ja unglaublich!",
    "Das kostet genau einhundertdreiundzwanzig Millionen Euro.",
]


def normalize(text: str) -> list[str]:
    text = text.casefold().replace("ß", "ss")
    return re.sub(r"[^a-zäöü0-9]+", " ", text).split()


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, expected in enumerate(reference, 1):
        current = [row]
        for column, actual in enumerate(hypothesis, 1):
            current.append(
                min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (expected != actual))
            )
        previous = current
    return previous[-1]


def clean(text: str) -> str:
    if "<asr_text>" in text:
        text = text.rsplit("<asr_text>", 1)[1]
    return re.sub(r"\s+", " ", text).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("voice")
    parser.add_argument("--api", default="http://vocarium-api:8280")
    parser.add_argument("--asr", default="http://whisper-stt:8000")
    parser.add_argument("--user", default="zwaetschge")
    args = parser.parse_args()

    results, total_edits, total_words, audio_seconds, wall = [], 0, 0, 0.0, 0.0
    with httpx.Client(timeout=httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=20.0)) as client:
        for index, sentence in enumerate(SENTENCES, start=1):
            speech = client.post(
                f"{args.api}/api/generate",
                headers={"Remote-User": args.user},
                json={"text": sentence, "voice_id": args.voice},
            )
            speech.raise_for_status()
            duration = float(speech.headers.get("x-audio-duration", 0) or 0)
            inference = float(speech.headers.get("x-inference-seconds", 0) or 0)
            audio_seconds += duration
            wall += inference

            asr = client.post(
                f"{args.asr}/v1/audio/transcriptions",
                files={"file": (f"{index}.wav", speech.content, "audio/wav")},
                data={"model": "german", "language": "de"},
            )
            asr.raise_for_status()
            hypothesis = clean(str(asr.json().get("text", "")))
            reference_words = normalize(sentence)
            edits = edit_distance(reference_words, normalize(hypothesis))
            total_edits += edits
            total_words += len(reference_words)
            results.append(
                {
                    "reference": sentence,
                    "hypothesis": hypothesis,
                    "edits": edits,
                    "words": len(reference_words),
                    "wer": round(edits / len(reference_words), 4),
                    "seconds": duration,
                }
            )
            print(f"[{index}/{len(SENTENCES)}] wer={edits}/{len(reference_words)}  {hypothesis[:70]}", flush=True)

    summary = {
        "voice": args.voice,
        "utterances": len(results),
        "word_errors": total_edits,
        "reference_words": total_words,
        "wer": round(total_edits / total_words, 4) if total_words else 0,
        "audio_seconds": round(audio_seconds, 2),
        "rtf": round(wall / audio_seconds, 4) if audio_seconds else 0,
        "results": results,
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
