"""GPU Queue — serializes GPU operations across configured inference services.

GPU placement is defined in Docker Compose via GPU_TTS_*, GPU_ASR, GPU_MUSIC,
and GPU_SFX. On single-GPU deployments, idle-unload keeps large models from
coexisting in VRAM. Music and SFX evict each other since both need a large
chunk of GPU memory. Jobs are processed FIFO; before each job, only services
that cannot coexist with the incoming service are unloaded.
"""

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Unload callbacks are injected at startup so this module stays decoupled.
_unload_callbacks: dict[str, Callable[[], Coroutine]] = {}

def _gpu_id(name: str, default: str = "0") -> str:
    return (os.environ.get(name, default) or default).strip()


def _service_gpus() -> dict[str, str]:
    gpus = {
        "tts": _gpu_id("GPU_TTS_PRIMARY", _gpu_id("GPU_TTS_1", "0")),
        "asr": _gpu_id("GPU_ASR", "0"),
        "music": _gpu_id("GPU_MUSIC", "0"),
        "sfx": _gpu_id("GPU_SFX", "0"),
    }
    if os.environ.get("TTS_URL_2", "").strip():
        gpus["tts_extra"] = _gpu_id("GPU_TTS_EXTRA", _gpu_id("GPU_TTS_2", "1"))
    return gpus


def _coexisting_services(incoming: str) -> set[str]:
    """Services on different GPU IDs can coexist; same-GPU services conflict."""
    gpus = _service_gpus()
    incoming_gpu = gpus.get(incoming)
    if incoming_gpu is None:
        return {incoming}
    return {
        service
        for service, gpu in gpus.items()
        if service == incoming or gpu != incoming_gpu
    }


def register_unloaders(callbacks: dict[str, Callable[[], Coroutine]]):
    """Register service unload callbacks, e.g. {"tts": _unload_tts, ...}."""
    _unload_callbacks.update(callbacks)


@dataclass
class Job:
    job_id: str
    service_type: str  # "tts" | "asr" | "music" | "sfx"
    description: str
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())
    work_fn: Callable[[], Coroutine] | None = None
    status: str = "queued"  # queued | running | completed | failed
    position: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


class GpuQueue:
    def __init__(self, max_history: int = 50):
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._jobs: dict[str, Job] = {}
        self._current: Job | None = None
        self._max_history = max_history
        self._worker_task: asyncio.Task | None = None
        self._position_counter = 0

    def start(self):
        """Start the background worker. Call from FastAPI startup."""
        self._worker_task = asyncio.create_task(self._worker())

    async def submit(
        self,
        service_type: str,
        description: str,
        work_fn: Callable[[], Coroutine],
    ) -> tuple[str, asyncio.Future]:
        """Submit a GPU job. Returns (job_id, future).

        The caller awaits the future to get the result.
        """
        job_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        job = Job(
            job_id=job_id,
            service_type=service_type,
            description=description,
            future=loop.create_future(),
            work_fn=work_fn,
        )
        self._position_counter += 1
        job.position = self._queue.qsize() + 1  # 1-based, behind current
        self._jobs[job_id] = job
        await self._queue.put(job)
        self._evict_old()
        return job_id, job.future

    def get_status(self) -> dict:
        """Return queue overview."""
        pending = [
            {
                "job_id": j.job_id,
                "position": idx + 1,
                "service_type": j.service_type,
                "description": j.description,
                "created_at": j.created_at,
            }
            for idx, j in enumerate(list(self._queue._queue))
        ]
        current = None
        if self._current:
            j = self._current
            current = {
                "job_id": j.job_id,
                "service_type": j.service_type,
                "description": j.description,
                "started_at": j.started_at,
            }
        return {
            "current": current,
            "queue": pending,
            "queue_length": len(pending),
        }

    def get_job_status(self, job_id: str) -> dict | None:
        """Return status of a specific job."""
        job = self._jobs.get(job_id)
        if not job:
            return None
        # Recalculate position for queued jobs
        position = 0
        if job.status == "queued":
            for idx, j in enumerate(list(self._queue._queue)):
                if j.job_id == job_id:
                    position = idx + 1
                    break
        return {
            "job_id": job.job_id,
            "status": job.status,
            "position": position,
            "service_type": job.service_type,
            "description": job.description,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "error": job.error,
        }

    @property
    def is_busy(self) -> bool:
        """True if a job is currently running or waiting in the queue."""
        return self._current is not None or not self._queue.empty()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    async def _worker(self):
        while True:
            job = await self._queue.get()

            # Skip cancelled jobs
            if job.future.cancelled():
                job.status = "failed"
                job.error = "cancelled"
                job.finished_at = time.time()
                continue

            self._current = job
            job.status = "running"
            job.started_at = time.time()

            # Unload only services that can't coexist with the incoming job
            await self._unload_conflicts(job.service_type)

            try:
                result = await job.work_fn()
                if not job.future.cancelled():
                    job.future.set_result(result)
                job.status = "completed"
            except Exception as e:
                if not job.future.cancelled():
                    job.future.set_exception(e)
                job.status = "failed"
                job.error = str(e)
            finally:
                job.finished_at = time.time()
                self._current = None
                self._queue.task_done()

    async def _unload_conflicts(self, incoming: str):
        """Unload services that can't coexist with the incoming service type."""
        keep = _coexisting_services(incoming)
        tasks = []
        for svc, fn in _unload_callbacks.items():
            if svc not in keep:
                tasks.append(fn())
        if tasks:
            unloading = [s for s in _unload_callbacks if s not in keep]
            logger.info("Unloading %s (incompatible with %s)", unloading, incoming)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for svc, result in zip(unloading, results):
                if isinstance(result, Exception):
                    logger.warning("Unload callback for %s failed: %s", svc, result)

    def _evict_old(self):
        """Remove old completed/failed jobs to bound memory."""
        done = [
            j for j in self._jobs.values()
            if j.status in ("completed", "failed")
        ]
        if len(done) > self._max_history:
            done.sort(key=lambda j: j.finished_at or 0)
            for j in done[: len(done) - self._max_history]:
                self._jobs.pop(j.job_id, None)


# Module-level singleton
gpu_queue = GpuQueue()
