"""Vocarium Kikiri fine-tune trainer.

Owns the GPU pipeline that turns an audiobook directory into a servable Kikiri
voice. Jobs are queued and executed one at a time — there is a single GPU, and a
second concurrent run would evict the first one's weights.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

JOBS_DIR = Path(os.environ.get("TRAINER_JOBS_DIR", "/jobs"))
LIBRARY_DIR = Path(os.environ.get("TRAINER_LIBRARY_DIR", "/library"))
PUBLISH_DIR = Path(os.environ.get("TRAINER_PUBLISH_DIR", "/models"))
BASE_CHECKPOINT = Path(os.environ.get("KIKIRI_BASE_CHECKPOINT", "/base/stage1_raw.pth"))
WORKSPACE = Path(os.environ.get("KIKIRI_WORKSPACE", "/workspace"))
ASR_URL = os.environ.get("ASR_URL", "http://whisper-stt:8000/v1/audio/transcriptions")
AUDIO_SUFFIXES = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".wav"}
MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

app = FastAPI(title="Vocarium Kikiri Trainer", version="1.0.0")
queue: asyncio.Queue[str] = asyncio.Queue()
running: dict[str, asyncio.subprocess.Process] = {}


class JobRequest(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    source_path: str
    owner: str = "unknown"
    max_hours: float = Field(default=12.0, gt=0.5, le=60.0)
    epochs: int = Field(default=4, ge=1, le=20)
    batch_size: int = Field(default=2, ge=1, le=16)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().casefold()).strip("-")
    return slug[:40] or "voice"


def resolve_source(raw: str) -> Path:
    """Keep job sources inside the mounted audiobook library."""
    library = LIBRARY_DIR.resolve()
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else library / candidate).resolve()
    if resolved != library and library not in resolved.parents:
        raise HTTPException(400, "source_path must live inside the mounted audiobook library")
    if not resolved.is_dir():
        raise HTTPException(404, f"Source directory not found: {raw}")
    if not any(path.suffix.casefold() in AUDIO_SUFFIXES for path in resolved.iterdir()):
        raise HTTPException(400, "Source directory holds no supported audio files")
    return resolved


def read_state(job_id: str) -> dict[str, object]:
    state = JOBS_DIR / job_id / "state.json"
    if not state.is_file():
        raise HTTPException(404, f"Unknown job {job_id}")
    try:
        return json.loads(state.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "running", "stages": [], "spec": {}, "result": {}}


def job_request(job_id: str) -> dict[str, object]:
    """The immutable creation request — the authority on who owns a job.

    ``state.json`` is rewritten by the pipeline on every progress tick, so it must
    not be the source of truth for ownership.
    """
    path = JOBS_DIR / job_id / "request.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def job_summary(job_id: str) -> dict[str, object]:
    state = read_state(job_id)
    created = job_request(job_id)
    stages = state.get("stages", [])
    done = sum(float(stage.get("progress", 0)) for stage in stages)
    active = next((stage for stage in stages if stage.get("status") == "running"), None)
    # Live pipeline values win, except for the fields only the creation request
    # can answer for — ownership above all.
    spec = {**created, **state.get("spec", {})}
    for key in ("owner", "name", "source_path"):
        if key in created:
            spec[key] = created[key]
    return {
        "id": job_id,
        "status": state.get("status", "unknown"),
        "error": state.get("error", ""),
        "spec": spec,
        "stage": active.get("name") if active else None,
        "stage_detail": active.get("detail", "") if active else "",
        "progress": round(done / len(stages), 4) if stages else 0.0,
        "stages": stages,
        "result": state.get("result", {}),
        "updated_at": state.get("updated_at"),
    }


async def worker() -> None:
    while True:
        job_id = await queue.get()
        directory = JOBS_DIR / job_id
        try:
            spec = json.loads((directory / "request.json").read_text(encoding="utf-8"))
            command = [
                sys.executable,
                str(Path(__file__).resolve().parent / "pipeline" / "run.py"),
                str(directory),
                "--source", spec["source_path"],
                "--speaker", spec["speaker"],
                "--model-id", spec["model_id"],
                "--display-name", spec["name"],
                "--max-hours", str(spec["max_hours"]),
                "--epochs", str(spec["epochs"]),
                "--batch-size", str(spec["batch_size"]),
                "--asr-url", ASR_URL,
                "--base-checkpoint", str(BASE_CHECKPOINT),
                "--workspace", str(WORKSPACE),
                "--publish-to", str(PUBLISH_DIR),
            ]
            logger.info("Starting job %s (%s)", job_id, spec["model_id"])
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            running[job_id] = process
            code = await process.wait()
            logger.info("Job %s finished with code %s", job_id, code)
        except Exception:
            logger.exception("Job %s crashed", job_id)
            state = directory / "state.json"
            if state.is_file():
                payload = json.loads(state.read_text(encoding="utf-8"))
                payload["status"] = "failed"
                payload["error"] = "Trainer crashed while running the job"
                state.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            running.pop(job_id, None)
            queue.task_done()


@app.on_event("startup")
async def startup() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    # A job that was mid-flight when the service died is resumable: every stage
    # skips work it already produced, so simply queue it again.
    for directory in sorted(JOBS_DIR.iterdir()) if JOBS_DIR.is_dir() else []:
        state = directory / "state.json"
        if state.is_file() and json.loads(state.read_text(encoding="utf-8")).get("status") == "running":
            logger.info("Requeueing interrupted job %s", directory.name)
            await queue.put(directory.name)
    asyncio.create_task(worker())


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "healthy",
        "queued": queue.qsize(),
        "running": list(running),
        "base_checkpoint": BASE_CHECKPOINT.is_file(),
        "library": str(LIBRARY_DIR),
    }


@app.get("/library")
def library(path: str = Query(default="")) -> dict[str, object]:
    """Browse the mounted audiobook library so the UI can pick a source."""
    root = LIBRARY_DIR.resolve()
    target = (root / path).resolve() if path else root
    if target != root and root not in target.parents:
        raise HTTPException(400, "path escapes the library root")
    if not target.is_dir():
        raise HTTPException(404, "Directory not found")
    entries = []
    for child in sorted(target.iterdir(), key=lambda item: item.name.casefold()):
        if not child.is_dir():
            continue
        audio = [item for item in child.iterdir() if item.suffix.casefold() in AUDIO_SUFFIXES]
        entries.append(
            {
                "name": child.name,
                "path": str(child.relative_to(root)),
                "audio_files": len(audio),
                "has_children": any(item.is_dir() for item in child.iterdir()),
            }
        )
    return {"path": str(target.relative_to(root)) if target != root else "", "entries": entries}


@app.get("/jobs")
def list_jobs() -> dict[str, object]:
    if not JOBS_DIR.is_dir():
        return {"jobs": []}
    jobs = []
    for directory in sorted(JOBS_DIR.iterdir(), reverse=True):
        if (directory / "state.json").is_file():
            jobs.append(job_summary(directory.name))
    return {"jobs": jobs}


@app.post("/jobs", status_code=201)
async def create_job(request: JobRequest) -> dict[str, object]:
    source = resolve_source(request.source_path)
    model_id = slugify(request.name)
    if not MODEL_ID_PATTERN.match(model_id):
        raise HTTPException(400, "name must contain at least three alphanumeric characters")
    if (PUBLISH_DIR / model_id).is_dir():
        raise HTTPException(409, f"A published voice named {model_id!r} already exists")
    for existing in list_jobs()["jobs"]:
        if existing["spec"].get("model_id") == model_id and existing["status"] in ("running", "queued"):
            raise HTTPException(409, f"A job for {model_id!r} is already in flight")

    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    directory = JOBS_DIR / job_id
    directory.mkdir(parents=True)
    payload = {
        "name": request.name,
        "owner": request.owner,
        "source_path": str(source),
        "speaker": model_id,
        "model_id": model_id,
        "max_hours": request.max_hours,
        "epochs": request.epochs,
        "batch_size": request.batch_size,
    }
    (directory / "request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "state.json").write_text(
        json.dumps(
            {
                "status": "queued",
                "error": "",
                "spec": payload,
                "stages": [
                    {"name": name, "status": "pending", "progress": 0.0, "detail": ""}
                    for name in ("segment", "transcribe", "finalize", "train", "export")
                ],
                "result": {},
                "updated_at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    await queue.put(job_id)
    return job_summary(job_id)


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, object]:
    return job_summary(job_id)


@app.get("/jobs/{job_id}/log")
def get_log(job_id: str, stage: str = Query(default="train"), lines: int = Query(default=120, ge=1, le=2000)) -> dict[str, object]:
    log = JOBS_DIR / job_id / "logs" / f"{Path(stage).name}.log"
    if not log.is_file():
        return {"stage": stage, "lines": []}
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    return {"stage": stage, "lines": tail}


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, object]:
    process = running.get(job_id)
    if process is None:
        raise HTTPException(409, "Job is not running")
    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    state = JOBS_DIR / job_id / "state.json"
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["status"] = "cancelled"
    state.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": job_id, "status": "cancelled"}


@app.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict[str, object]:
    """Re-queue a job that failed or was cancelled.

    Every stage skips work it already produced, so a retry picks up where the run
    stopped — a failed transcription does not re-segment the whole book.
    """
    if job_id in running:
        raise HTTPException(409, "Job is already running")
    directory = JOBS_DIR / job_id
    if not (directory / "request.json").is_file():
        raise HTTPException(404, f"Unknown job {job_id}")
    state = read_state(job_id)
    if state.get("status") == "completed":
        raise HTTPException(409, "Job already completed")
    state["status"] = "queued"
    state["error"] = ""
    for stage in state.get("stages", []):
        if stage.get("status") in ("failed", "running"):
            stage["status"] = "pending"
            stage["progress"] = 0.0
            stage["detail"] = ""
    state["updated_at"] = time.time()
    (directory / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    await queue.put(job_id)
    return job_summary(job_id)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, purge_model: bool = Query(default=False)) -> dict[str, object]:
    if job_id in running:
        raise HTTPException(409, "Cancel the job before deleting it")
    directory = JOBS_DIR / job_id
    if not directory.is_dir():
        raise HTTPException(404, f"Unknown job {job_id}")
    state = read_state(job_id)
    model_id = str(state.get("spec", {}).get("model_id", ""))
    shutil.rmtree(directory)
    if purge_model and model_id and (PUBLISH_DIR / model_id).is_dir():
        shutil.rmtree(PUBLISH_DIR / model_id)
    return {"id": job_id, "deleted": True, "model_purged": purge_model}
