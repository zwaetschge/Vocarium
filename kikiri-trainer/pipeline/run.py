#!/usr/bin/env python3
"""Run a full Kikiri fine-tune: segment → transcribe → QA → train → export.

Every stage writes into one job directory and is skipped when its output already
exists, so a job survives a restart of the trainer service or the box. Progress is
mirrored into ``state.json`` after every parsed line, which is what the API polls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent
ASSETS = PIPELINE.parent / "assets"

PROGRESS_PATTERNS = {
    "segment": re.compile(r"^\[(\d+)/(\d+)\]"),
    "transcribe": re.compile(r"^\[(\d+)/(\d+)\]"),
    "train": re.compile(r"Epoch \[(\d+)/(\d+)\], Step \[(\d+)/(\d+)\]"),
}


@dataclass
class Stage:
    name: str
    status: str = "pending"
    progress: float = 0.0
    detail: str = ""
    started_at: float | None = None
    finished_at: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "progress": round(self.progress, 4),
            "detail": self.detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class Job:
    directory: Path
    spec: dict[str, object]
    stages: list[Stage] = field(default_factory=list)
    status: str = "running"
    error: str = ""
    result: dict[str, object] = field(default_factory=dict)

    def write(self) -> None:
        payload = {
            "status": self.status,
            "error": self.error,
            "spec": self.spec,
            "stages": [stage.as_dict() for stage in self.stages],
            "result": self.result,
            "updated_at": time.time(),
        }
        temporary = self.directory / "state.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.directory / "state.json")


def run_stage(job: Job, stage: Stage, command: list[str], cwd: Path | None = None) -> None:
    stage.status = "running"
    stage.started_at = time.time()
    job.write()
    log = job.directory / "logs" / f"{stage.name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    pattern = PROGRESS_PATTERNS.get(stage.name)
    last_write = 0.0

    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(command)}\n")
        handle.flush()
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert process.stdout is not None
        for line in process.stdout:
            handle.write(line)
            handle.flush()
            match = pattern.search(line) if pattern else None
            if match:
                numbers = [int(value) for value in match.groups()]
                if stage.name == "train":
                    epoch, epochs, step, steps = numbers
                    stage.progress = ((epoch - 1) * steps + step) / max(1, epochs * steps)
                    stage.detail = f"Epoche {epoch}/{epochs}, Schritt {step}/{steps}"
                else:
                    done, total = numbers[0], numbers[1]
                    stage.progress = done / max(1, total)
                    stage.detail = f"{done}/{total}"
            now = time.time()
            if now - last_write > 2.0:
                last_write = now
                job.write()
        code = process.wait()

    if code != 0:
        stage.status = "failed"
        stage.finished_at = time.time()
        job.status = "failed"
        job.error = f"Stage {stage.name} exited with code {code}. See logs/{stage.name}.log"
        job.write()
        raise SystemExit(code)

    stage.status = "completed"
    stage.progress = 1.0
    stage.finished_at = time.time()
    job.write()


def skip_stage(job: Job, stage: Stage, reason: str) -> None:
    stage.status = "completed"
    stage.progress = 1.0
    stage.detail = reason
    stage.finished_at = time.time()
    job.write()


def release_asr(url: str) -> None:
    """Free the ASR worker's VRAM before the training stage claims the GPU.

    On a single-GPU box the gap between transcription and training is shorter than
    the ASR idle-unload timeout, so without this the trainer can hit an OOM while
    vLLM still holds the card.
    """
    origin = url.split("/v1/")[0].rstrip("/")
    request = urllib.request.Request(f"{origin}/unload", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
        print(f"released ASR at {origin}", flush=True)
    except Exception as exc:
        # Not fatal: the worker may already be idle, or unload may be unsupported.
        print(f"ASR unload at {origin} failed ({type(exc).__name__}: {exc})", flush=True)


def render_config(job: Job, speaker: str, epochs: int, batch_size: int) -> Path:
    template = (ASSETS / "train_second.template.yml").read_text(encoding="utf-8")
    directory = job.directory
    replacements = {
        r"^epochs: .*$": f"epochs: {epochs}",
        r"^epochs_2nd: .*$": f"epochs_2nd: {epochs}",
        r"^batch_size: .*$": f"batch_size: {batch_size}",
        r'^log_dir: .*$': f'log_dir: "{directory}/training/logs"',
        r'^  train_data: .*$': f'  train_data: "{directory}/training/train_list.txt"',
        r'^  val_data: .*$': f'  val_data: "{directory}/training/val_list.txt"',
        r'^  root_path: .*$': f'  root_path: "{directory}/dataset/audio"',
        r'^  OOD_data: .*$': f'  OOD_data: "{directory}/training/OOD_texts.txt"',
    }
    for pattern, replacement in replacements.items():
        template = re.sub(pattern, replacement, template, count=1, flags=re.MULTILINE)
    path = directory / "training" / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template, encoding="utf-8")
    return path


def best_checkpoint(log_dir: Path) -> tuple[Path | None, str]:
    """Pick the epoch with the lowest validation loss, not simply the last one.

    Validation loss is not monotone across epochs, so the final checkpoint is not
    reliably the best one. Falls back to the last checkpoint when the log cannot
    be matched up with the files on disk.
    """
    checkpoints = sorted(log_dir.glob("epoch_2nd_*.pth"))
    if not checkpoints:
        return None, "no checkpoint"
    log = log_dir / "train.log"
    if log.is_file():
        losses = [
            float(match)
            for match in re.findall(r"Validation loss: ([0-9.]+)", log.read_text(encoding="utf-8", errors="replace"))
        ]
        scored = list(zip(checkpoints, losses))
        if scored:
            chosen, loss = min(scored, key=lambda item: item[1])
            return chosen, f"validation loss {loss:.4f} ({chosen.name})"
    return checkpoints[-1], f"last checkpoint ({checkpoints[-1].name})"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--speaker", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--display-name")
    parser.add_argument("--max-hours", type=float, default=12.0)
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--asr-url", default=os.environ.get("ASR_URL", "http://whisper-stt:8000/v1/audio/transcriptions"))
    parser.add_argument("--base-checkpoint", type=Path, default=Path(os.environ.get("KIKIRI_BASE_CHECKPOINT", "/base/stage1_raw.pth")))
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("KIKIRI_WORKSPACE", "/workspace")))
    parser.add_argument("--publish-to", type=Path, help="Model directory served by kikiri-tts.")
    args = parser.parse_args()

    directory = args.job_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    prepared = directory / "prepared"
    training = directory / "training"
    log_dir = training / "logs"

    request_path = directory / "request.json"
    created = (
        json.loads(request_path.read_text(encoding="utf-8"))
        if request_path.is_file()
        else {}
    )
    job = Job(
        directory=directory,
        spec={
            **created,
            "source": str(args.source),
            "speaker": args.speaker,
            "model_id": args.model_id,
            "display_name": args.display_name or args.model_id,
            "max_hours": args.max_hours,
            "epochs": args.epochs,
            "batch_size": args.batch_size,
        },
        stages=[Stage(name) for name in ("segment", "transcribe", "finalize", "train", "export")],
    )
    stages = {stage.name: stage for stage in job.stages}
    job.write()

    if (prepared / "segments.jsonl").exists():
        skip_stage(job, stages["segment"], "bereits segmentiert")
    else:
        run_stage(
            job,
            stages["segment"],
            [sys.executable, str(PIPELINE / "segment.py"), str(args.source), str(prepared), "--max-hours", str(args.max_hours)],
        )

    run_stage(
        job,
        stages["transcribe"],
        [
            sys.executable,
            str(PIPELINE / "transcribe.py"),
            str(prepared / "segments.jsonl"),
            str(prepared / "clips"),
            str(prepared / "transcriptions.jsonl"),
            "--url",
            args.asr_url,
        ],
    )

    release_asr(args.asr_url)

    run_stage(
        job,
        stages["finalize"],
        [sys.executable, str(PIPELINE / "finalize.py"), str(directory), "--speaker", args.speaker],
    )
    summary_path = prepared / "qa_summary.json"
    if summary_path.exists():
        job.result["dataset"] = json.loads(summary_path.read_text(encoding="utf-8"))
        job.write()

    log_dir.mkdir(parents=True, exist_ok=True)
    first_stage = log_dir / "first_stage.pth"
    if not first_stage.exists():
        if not args.base_checkpoint.is_file():
            raise SystemExit(f"Base checkpoint missing: {args.base_checkpoint}")
        try:
            os.link(args.base_checkpoint, first_stage)
        except OSError:
            first_stage.symlink_to(args.base_checkpoint)

    config = render_config(job, args.speaker, args.epochs, args.batch_size)
    run_stage(
        job,
        stages["train"],
        # train_finetune.py, not train_second.py: only the former trains the
        # acoustic modules (decoder/style_encoder — the speaker's timbre) from
        # step one, together with discriminators loaded from the base checkpoint.
        [sys.executable, "train_finetune.py", "--config_path", str(config)],
        cwd=args.workspace / "StyleTTS2",
    )

    checkpoint, reason = best_checkpoint(log_dir)
    if checkpoint is None:
        raise SystemExit("Training produced no epoch checkpoint")
    print(f"exporting {checkpoint.name} — selected by {reason}", flush=True)
    job.result["selected_checkpoint"] = reason
    target = args.publish_to / args.model_id if args.publish_to else directory / "model"
    run_stage(
        job,
        stages["export"],
        [
            sys.executable,
            str(PIPELINE / "export.py"),
            str(checkpoint),
            str(target),
            "--audio-dir",
            str(directory / "dataset" / "audio" / args.speaker),
            "--workspace",
            str(args.workspace),
            "--model-id",
            args.model_id,
            "--display-name",
            args.display_name or args.model_id,
        ],
    )

    job.status = "completed"
    job.result["model_dir"] = str(target)
    job.result["checkpoint"] = str(checkpoint)
    job.write()
    print(json.dumps(job.result, ensure_ascii=False))


if __name__ == "__main__":
    main()
