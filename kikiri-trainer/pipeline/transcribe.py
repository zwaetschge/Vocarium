#!/usr/bin/env python3
"""Resumably transcribe segmented WAV files through an OpenAI ASR endpoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import httpx


def clean_text(value: str) -> str:
    if "<asr_text>" in value:
        value = value.rsplit("<asr_text>", 1)[1]
    value = re.sub(r"\s+", " ", value).strip()
    return value


def preflight(client: httpx.Client, url: str) -> None:
    """Fail fast when the ASR service is not up.

    Without this, an unreachable endpoint burns three retries per clip across the
    whole manifest — hours of churn that ends in a dataset of error rows. The
    stage is resumable, so exiting now costs nothing.
    """
    origin = url.split("/v1/")[0].rstrip("/")
    try:
        response = client.get(f"{origin}/v1/models", timeout=30.0)
        response.raise_for_status()
    except Exception as exc:
        raise SystemExit(
            f"ASR endpoint {origin} is not reachable ({type(exc).__name__}: {exc}). "
            "Start the ASR worker and retry this job; completed stages are kept."
        )


def load_completed(path: Path) -> set[str]:
    completed: set[str] = set()
    if not path.exists():
        return completed
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") == "ok":
                completed.add(row["clip"])
    return completed


def append_row(path: Path, row: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("clips", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--url", default="http://whisper-stt:8000/v1/audio/transcriptions")
    parser.add_argument("--model", default="german")
    parser.add_argument("--language", default="de")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.manifest.read_text(encoding="utf-8").splitlines() if line]
    if args.limit is not None:
        rows = rows[: args.limit]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    completed = load_completed(args.output)
    pending = [row for row in rows if row["clip"] not in completed]
    print(f"total={len(rows)} completed={len(completed)} pending={len(pending)}", flush=True)
    started = time.monotonic()

    timeout = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=20.0)
    consecutive_failures = 0
    if not pending:
        print("nothing to transcribe", flush=True)
        return

    with httpx.Client(timeout=timeout) as client:
        preflight(client, args.url)
        for index, segment in enumerate(pending, start=1):
            clip_name = segment["clip"]
            clip_path = args.clips / clip_name
            last_error = "unknown error"
            for attempt in range(1, args.retries + 1):
                try:
                    with clip_path.open("rb") as audio:
                        response = client.post(
                            args.url,
                            data={"model": args.model, "language": args.language},
                            files={"file": (clip_name, audio, "audio/wav")},
                        )
                    response.raise_for_status()
                    payload = response.json()
                    text = clean_text(str(payload.get("text", "")))
                    if not text:
                        raise ValueError("empty transcription")
                    duration = float(segment["duration_seconds"])
                    words = len(text.split())
                    row = {
                        **segment,
                        "text": text,
                        "words": words,
                        "words_per_second": round(words / duration, 3),
                        "status": "ok",
                    }
                    append_row(args.output, row)
                    break
                except Exception as exc:  # retries are intentionally broad for long jobs
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < args.retries:
                        time.sleep(2**attempt)
                consecutive_failures = 0
            else:
                append_row(args.output, {**segment, "status": "error", "error": last_error})
                consecutive_failures += 1
                if consecutive_failures >= 10:
                    raise SystemExit(
                        f"Aborting after {consecutive_failures} consecutive ASR failures. "
                        f"Last error: {last_error}"
                    )

            elapsed = time.monotonic() - started
            rate = index / elapsed if elapsed else 0.0
            eta_minutes = (len(pending) - index) / rate / 60 if rate else 0.0
            print(
                f"[{index}/{len(pending)}] {clip_name} "
                f"rate={rate:.2f}/s eta={eta_minutes:.1f}m",
                flush=True,
            )


if __name__ == "__main__":
    main()
