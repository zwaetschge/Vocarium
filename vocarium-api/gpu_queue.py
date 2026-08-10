"""GPU Queue — serializes GPU operations across configured inference services.

GPU placement is defined in Docker Compose via GPU_TTS_*, GPU_DOTS_TTS,
GPU_ASR, GPU_MUSIC, and GPU_SFX. On single-GPU deployments, idle-unload keeps large models from
coexisting in VRAM. Music and SFX evict each other since both need a large
chunk of GPU memory. Jobs are processed FIFO; before each job, only services
that cannot coexist with the incoming service are unloaded.
"""

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from metrics import inc, observe, set_gauge
from request_context import current_request_id, current_user_id

logger = logging.getLogger(__name__)

# Unload callbacks are injected at startup so this module stays decoupled.
_unload_callbacks: dict[str, Callable[[], Coroutine]] = {}
_job_recorder: Callable[[dict[str, Any]], None] | None = None
_quota_checker: Callable[[int, str], dict[str, Any]] | None = None
_cancel_checker: Callable[[str], bool] | None = None


class GpuResourceError(RuntimeError):
    """Raised when a queued GPU job is blocked by the resource guard."""

    def __init__(self, service_type: str, decision: dict[str, Any]):
        self.service_type = service_type
        self.decision = decision
        selected = decision.get("selected") or {}
        reasons = selected.get("reasons") or decision.get("reasons") or ["GPU unavailable"]
        reason_text = "; ".join(str(r) for r in reasons)
        super().__init__(f"{service_type} blocked by GPU resource guard: {reason_text}")


class QueueQuotaError(RuntimeError):
    """Raised when a user has too many active GPU jobs."""

    def __init__(self, user_id: int, service_type: str, decision: dict[str, Any]):
        self.user_id = user_id
        self.service_type = service_type
        self.decision = decision
        message = decision.get("message") or "GPU queue quota exceeded"
        super().__init__(str(message))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _csv(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in (os.environ.get(name, default) or "").split(",") if v.strip()]


def _service_need_mib(service_type: str) -> int:
    kind = "tts" if service_type == "tts_extra" else service_type
    defaults = {
        # Qwen3-TTS 1.7B uses about 4.7 GiB on the RTX 3060. This is a total
        # footprint estimate, not additional free memory required beside an
        # already-running TTS CUDA context.
        "tts": 5500,
        "dots": 9000,
        "asr": 2500,
        "music": 8000,
        "sfx": 7000,
    }
    default = defaults.get(kind, 0)
    if default <= 0:
        return 0
    return _env_int(f"GPU_ESTIMATE_MIB_{kind.upper()}", default)


def _service_memory_tolerance_mib(service_type: str) -> int:
    """Allow for small gputasks/driver accounting differences.

    The TTS 1.7B worker is measured at roughly 4.7 GiB on the RTX 3060, while
    the conservative configured estimate remains 5.5 GiB. A 512 MiB tolerance
    prevents false denials without weakening the estimate by a full GiB.
    """
    kind = "tts" if service_type == "tts_extra" else service_type
    default = 512 if kind == "tts" else 0
    return max(0, _env_int(f"GPU_GUARD_MEMORY_TOLERANCE_MIB_{kind.upper()}", default))

def _gpu_id(name: str, default: str = "0") -> str:
    return (os.environ.get(name, default) or default).strip()


def _service_gpus() -> dict[str, str]:
    gpus = {
        "tts": _gpu_id("GPU_TTS_PRIMARY", _gpu_id("GPU_TTS_1", "0")),
        "dots": _gpu_id("GPU_DOTS_TTS", "0"),
        "asr": _gpu_id("GPU_ASR", "0"),
        "music": _gpu_id("GPU_MUSIC", "0"),
        "sfx": _gpu_id("GPU_SFX", "0"),
    }
    if os.environ.get("TTS_URL_2", "").strip():
        gpus["tts_extra"] = _gpu_id("GPU_TTS_EXTRA", _gpu_id("GPU_TTS_2", "1"))
    return gpus


def _gputasks_base_urls() -> list[str]:
    configured = _csv("GPUTASKS_URL")
    # The local deployment exposes gpu-task-manager on the host LAN address.
    # Keep this as a fallback so API containers can still reach it even when
    # the app is not on the same Docker network.
    fallback = "http://gpu-task-manager:3000"
    urls = configured or [fallback]
    return [u.rstrip("/") for u in urls if u.strip()]


def _fetch_gputasks_status_sync() -> dict[str, Any]:
    errors: list[str] = []
    for base in _gputasks_base_urls():
        url = f"{base}/api/v1/status"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status >= 400:
                    errors.append(f"{url}: HTTP {resp.status}")
                    continue
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and "gpus" in data:
                    return data
                if isinstance(data, dict) and isinstance(data.get("data"), dict):
                    inner = data["data"]
                    if "gpus" in inner:
                        return inner
                errors.append(f"{url}: response has no gpus")
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors) or "gputasks URL not configured")


async def _fetch_gputasks_status() -> dict[str, Any]:
    return await asyncio.to_thread(_fetch_gputasks_status_sync)


def _gpu_memory(gpu: dict[str, Any]) -> dict[str, int]:
    memory = gpu.get("memory") or {}
    return {
        "free": int(memory.get("free") or gpu.get("memoryFree") or 0),
        "used": int(memory.get("used") or gpu.get("memoryUsed") or 0),
        "total": int(memory.get("total") or gpu.get("memoryTotal") or 0),
    }


def _process_container_name(proc: dict[str, Any]) -> str:
    container = proc.get("container") or {}
    return str(
        container.get("name")
        or proc.get("containerName")
        or container.get("id")
        or ""
    )


def _process_haystack(proc: dict[str, Any]) -> str:
    container = proc.get("container") or {}
    parts = [
        container.get("name"),
        container.get("image"),
        proc.get("containerName"),
        proc.get("containerImage"),
        proc.get("command"),
        proc.get("name"),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def _gpu_matches_target(gpu: dict[str, Any], target: str | None) -> bool:
    if target is None:
        return False
    idx = str(gpu.get("index"))
    uuid_value = str(gpu.get("uuid") or "")
    name = str(gpu.get("name") or "").lower()
    target_l = target.lower()
    if target_l.isdigit():
        return idx == target_l
    return (
        uuid_value.lower() == target_l
        or target_l in name
        or (target_l == "3060" and "3060" in name)
        or (target_l == "5060" and "5060" in name)
    )


def _is_protected_gpu(gpu: dict[str, Any]) -> bool:
    idx = str(gpu.get("index"))
    name = str(gpu.get("name") or "").lower()
    protected_ids = set(_csv("GPU_PROTECTED_IDS", "1"))
    protected_names = [v.lower() for v in _csv("GPU_PROTECTED_NAMES", "5060")]
    return idx in protected_ids or any(part and part in name for part in protected_names)


def _gpu_protection_reasons(gpu: dict[str, Any]) -> list[str]:
    if not _is_protected_gpu(gpu):
        return []

    reasons: list[str] = []
    comfy_terms = [v.lower() for v in _csv("GPU_PROTECTED_PROCESS_PATTERNS", "comfyui")]
    allowed_containers = {
        v.lower()
        for v in _csv(
            "GPU_GUARD_ALLOWED_CONTAINERS",
            "qwen3-tts,qwen3-tts-2,qwen3-asr,acestep,mmaudio,vocarium-api,vocarium-api-2",
        )
    }
    unknown_used = 0
    comfy_seen = False
    for proc in gpu.get("processes") or []:
        haystack = _process_haystack(proc)
        if any(term and term in haystack for term in comfy_terms):
            comfy_seen = True
        container_name = _process_container_name(proc).lower()
        if container_name and container_name in allowed_containers:
            continue
        proc_mem = int(proc.get("gpu_memory") or proc.get("gpuMemoryUsed") or 0)
        unknown_used += proc_mem

    if comfy_seen:
        reasons.append("protected GPU has an active ComfyUI process")

    unknown_limit = _env_int("GPU_PROTECTED_UNKNOWN_USED_MIB", 1024)
    if unknown_used > unknown_limit:
        reasons.append(
            f"protected GPU has {unknown_used} MiB used by non-Vocarium processes"
        )

    return reasons


def _service_container_names(service_type: str) -> set[str]:
    return {
        "tts": {"qwen3-tts"},
        "dots": {"dots-tts"},
        "tts_extra": {"qwen3-tts-2"},
        "asr": {"qwen3-asr"},
        "music": {"acestep"},
        "sfx": {"mmaudio"},
    }.get(service_type, set())


def _service_used_mib(gpu: dict[str, Any], service_type: str) -> int:
    containers = _service_container_names(service_type)
    if not containers:
        return 0
    total = 0
    for proc in gpu.get("processes") or []:
        if _process_container_name(proc).lower() in containers:
            total += int(proc.get("gpu_memory") or proc.get("gpuMemoryUsed") or 0)
    return total


def _decision_for_service(
    service_type: str,
    status: dict[str, Any],
) -> dict[str, Any]:
    target = _service_gpus().get(service_type)
    need = _service_need_mib(service_type)
    tolerance = _service_memory_tolerance_mib(service_type)
    candidates: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None

    for gpu in status.get("gpus") or []:
        memory = _gpu_memory(gpu)
        reclaimable = _service_used_mib(gpu, service_type)
        effective_free = memory["free"] + reclaimable
        reasons = _gpu_protection_reasons(gpu)
        if need > 0 and effective_free + tolerance < need:
            reasons.append(
                f"only {memory['free']} MiB free plus {reclaimable} MiB reclaimable, "
                f"estimated need is {need} MiB with {tolerance} MiB telemetry tolerance"
            )
        candidate = {
            "index": gpu.get("index"),
            "name": gpu.get("name"),
            "target": target,
            "free_mib": memory["free"],
            "effective_free_mib": effective_free,
            "reclaimable_mib": reclaimable,
            "memory_tolerance_mib": tolerance,
            "used_mib": memory["used"],
            "total_mib": memory["total"],
            "protected": _is_protected_gpu(gpu),
            "processes": [
                {
                    "pid": p.get("pid"),
                    "name": p.get("name"),
                    "container": _process_container_name(p),
                    "gpu_memory_mib": int(p.get("gpu_memory") or p.get("gpuMemoryUsed") or 0),
                }
                for p in (gpu.get("processes") or [])
            ],
            "allowed": not reasons,
            "reasons": reasons,
        }
        candidates.append(candidate)
        if _gpu_matches_target(gpu, target):
            selected = candidate

    if selected is None:
        return {
            "service_type": service_type,
            "target_gpu": target,
            "estimated_need_mib": need,
            "allowed": False,
            "selected": None,
            "gpus": candidates,
            "reasons": [f"target GPU {target!r} was not reported by gputasks"],
        }

    return {
        "service_type": service_type,
        "target_gpu": target,
        "estimated_need_mib": need,
        "allowed": bool(selected["allowed"]),
        "selected": selected,
        "gpus": candidates,
        "timestamp": status.get("timestamp"),
    }


async def get_resource_status() -> dict[str, Any]:
    """Return gputasks state plus guard decisions for configured services."""
    if not _env_bool("GPU_GUARD_ENABLED", True):
        return {"enabled": False, "service_gpus": _service_gpus()}

    try:
        status = await _fetch_gputasks_status()
    except Exception as exc:
        return {
            "enabled": True,
            "available": False,
            "error": str(exc),
            "service_gpus": _service_gpus(),
        }

    services = sorted(_service_gpus())
    return {
        "enabled": True,
        "available": True,
        "service_gpus": _service_gpus(),
        "decisions": {
            service: _decision_for_service(service, status)
            for service in services
        },
        "timestamp": status.get("timestamp"),
    }


async def require_gpu_resources(service_type: str) -> None:
    """Block queued work when the selected GPU is protected or too full."""
    if not _env_bool("GPU_GUARD_ENABLED", True):
        return
    if _service_need_mib(service_type) <= 0:
        return

    try:
        status = await _fetch_gputasks_status()
    except Exception as exc:
        if _env_bool("GPU_GUARD_FAIL_OPEN", False):
            logger.warning("GPU guard unavailable; allowing %s: %s", service_type, exc)
            return
        decision = {
            "service_type": service_type,
            "allowed": False,
            "selected": None,
            "reasons": [f"gputasks unavailable: {exc}"],
            "service_gpus": _service_gpus(),
        }
        raise GpuResourceError(service_type, decision)

    decision = _decision_for_service(service_type, status)
    if not decision["allowed"]:
        raise GpuResourceError(service_type, decision)


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


def register_job_recorder(callback: Callable[[dict[str, Any]], None] | None):
    """Register a callback that persists queue job state changes."""
    global _job_recorder
    _job_recorder = callback


def register_quota_checker(callback: Callable[[int, str], dict[str, Any]] | None):
    """Register a callback that decides whether a user may enqueue work."""
    global _quota_checker
    _quota_checker = callback


def register_cancel_checker(callback: Callable[[str], bool] | None):
    """Register a callback for cross-process cancellation requests."""
    global _cancel_checker
    _cancel_checker = callback


@dataclass
class Job:
    job_id: str
    service_type: str  # "tts" | "tts_extra" | "dots" | "asr" | "music" | "sfx"
    description: str
    user_id: int | None = None
    request_id: str | None = None
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())
    work_fn: Callable[[], Coroutine] | None = None
    status: str = "queued"  # queued | running | completed | failed | cancelled | cancel_requested
    position: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    heartbeat_at: float | None = None
    error: str | None = None
    last_error: str | None = None
    attempt_count: int = 0
    max_attempts: int = field(default_factory=lambda: max(1, _env_int("GPU_QUEUE_MAX_ATTEMPTS", 1)))
    cancel_requested: bool = False

    def snapshot(self, *, queue_position: int | None = None) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "request_id": self.request_id,
            "service_type": self.service_type,
            "description": self.description,
            "status": self.status,
            "position": self.position if queue_position is None else queue_position,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "heartbeat_at": self.heartbeat_at,
            "error": self.error,
            "last_error": self.last_error,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "cancel_requested": self.cancel_requested,
            "updated_at": time.time(),
        }


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
        *,
        user_id: int | None = None,
        request_id: str | None = None,
        max_attempts: int | None = None,
    ) -> tuple[str, asyncio.Future]:
        """Submit a GPU job. Returns (job_id, future).

        The caller awaits the future to get the result.
        """
        job_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        resolved_user_id = user_id if user_id is not None else current_user_id()
        resolved_request_id = request_id or current_request_id()
        self._enforce_quota(resolved_user_id, service_type)
        job = Job(
            job_id=job_id,
            service_type=service_type,
            description=description,
            user_id=resolved_user_id,
            request_id=resolved_request_id,
            future=loop.create_future(),
            work_fn=work_fn,
            heartbeat_at=time.time(),
        )
        if max_attempts is not None:
            job.max_attempts = max(1, int(max_attempts))
        self._position_counter += 1
        job.position = self._queue.qsize() + 1  # 1-based, behind current
        self._jobs[job_id] = job
        await self._queue.put(job)
        self._record_job(job)
        self._update_gauges()
        self._evict_old()
        return job_id, job.future

    def get_status(self, *, user_id: int | None = None) -> dict:
        """Return queue overview."""
        pending = [
            {
                "job_id": j.job_id,
                "position": idx + 1,
                "service_type": j.service_type,
                "description": j.description,
                "user_id": j.user_id,
                "request_id": j.request_id,
                "created_at": j.created_at,
                "cancel_requested": j.cancel_requested,
            }
            for idx, j in enumerate(list(self._queue._queue))
            if user_id is None or j.user_id == user_id
        ]
        current = None
        if self._current and (user_id is None or self._current.user_id == user_id):
            j = self._current
            current = {
                "job_id": j.job_id,
                "service_type": j.service_type,
                "description": j.description,
                "user_id": j.user_id,
                "request_id": j.request_id,
                "started_at": j.started_at,
                "heartbeat_at": j.heartbeat_at,
                "cancel_requested": j.cancel_requested,
            }
        return {
            "current": current,
            "queue": pending,
            "queue_length": len(pending),
        }

    def get_job_status(self, job_id: str, *, user_id: int | None = None) -> dict | None:
        """Return status of a specific job."""
        job = self._jobs.get(job_id)
        if not job:
            return None
        if user_id is not None and job.user_id != user_id:
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
            "user_id": job.user_id,
            "request_id": job.request_id,
            "service_type": job.service_type,
            "description": job.description,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "heartbeat_at": job.heartbeat_at,
            "error": job.error,
            "last_error": job.last_error,
            "attempt_count": job.attempt_count,
            "max_attempts": job.max_attempts,
            "cancel_requested": job.cancel_requested,
        }

    def cancel_job(self, job_id: str, *, user_id: int | None = None) -> dict | None:
        """Request cancellation for a queued or running in-memory job."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if user_id is not None and job.user_id != user_id:
            return None
        if job.status in ("completed", "failed", "cancelled"):
            return self.get_job_status(job_id, user_id=user_id)

        job.cancel_requested = True
        job.heartbeat_at = time.time()
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = time.time()
            job.error = "cancelled"
            job.future.cancel()
        else:
            job.status = "cancel_requested"
            job.error = "cancel requested"
        self._record_job(job)
        self._update_gauges()
        return self.get_job_status(job_id, user_id=user_id)

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
            if job.future.cancelled() or job.cancel_requested or self._is_cancel_requested(job):
                job.cancel_requested = True
                job.status = "cancelled"
                job.error = "cancelled"
                job.finished_at = time.time()
                job.heartbeat_at = job.finished_at
                self._record_job(job)
                self._update_gauges()
                self._queue.task_done()
                continue

            self._current = job
            job.status = "running"
            job.started_at = time.time()
            job.heartbeat_at = job.started_at
            job.attempt_count += 1
            observe(
                "vocarium_gpu_queue_wait_seconds",
                job.started_at - job.created_at,
                {"service_type": job.service_type},
            )
            self._record_job(job)
            self._update_gauges()
            heartbeat_task = asyncio.create_task(self._heartbeat(job))
            retrying = False

            try:
                # Unload only services that can't coexist with the incoming
                # job, then verify the selected GPU is still available.
                await self._unload_conflicts(job.service_type)
                if self._is_cancel_requested(job):
                    raise asyncio.CancelledError("cancelled")
                await self._require_resources_with_self_unload(job.service_type)
                if self._is_cancel_requested(job):
                    raise asyncio.CancelledError("cancelled")
                result = await job.work_fn()
                if self._is_cancel_requested(job):
                    raise asyncio.CancelledError("cancelled")
                if not job.future.cancelled():
                    job.future.set_result(result)
                job.status = "completed"
            except asyncio.CancelledError as e:
                job.cancel_requested = True
                if not job.future.cancelled():
                    job.future.cancel()
                job.status = "cancelled"
                job.error = str(e) or "cancelled"
            except Exception as e:
                job.last_error = str(e)
                job.error = str(e)
                if job.attempt_count < job.max_attempts and not self._is_cancel_requested(job):
                    retrying = True
                    job.status = "queued"
                    job.position = self._queue.qsize() + 1
                    job.started_at = None
                    job.finished_at = None
                    job.heartbeat_at = time.time()
                    self._record_job(job)
                    await self._queue.put(job)
                    inc("vocarium_gpu_job_retries_total", labels={"service_type": job.service_type})
                    continue
                if not job.future.cancelled():
                    job.future.set_exception(e)
                job.status = "failed"
                inc("vocarium_gpu_job_failures_total", labels={"service_type": job.service_type})
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                if retrying:
                    if self._current is job:
                        self._current = None
                    self._update_gauges()
                    self._queue.task_done()
                    continue

                job.finished_at = time.time()
                job.heartbeat_at = job.finished_at
                if job.started_at is not None:
                    observe(
                        "vocarium_gpu_job_run_seconds",
                        job.finished_at - job.started_at,
                        {"service_type": job.service_type, "status": job.status},
                    )
                self._record_job(job)
                if self._current is job:
                    self._current = None
                self._update_gauges()
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

    async def _require_resources_with_self_unload(self, service_type: str) -> None:
        """Verify GPU resources, unloading the incoming service once if needed.

        gputasks can report VRAM as used without process/container attribution.
        When the incoming service has an idle model already loaded, that makes
        the normal reclaimable-memory calculation too pessimistic. Retrying
        after unloading only the incoming service keeps real external pressure
        blocked while avoiding false denials on repeated same-service jobs.
        """
        try:
            await require_gpu_resources(service_type)
            return
        except GpuResourceError as first_error:
            unload = _unload_callbacks.get(service_type)
            if unload is None or not _env_bool("GPU_GUARD_RETRY_AFTER_SELF_UNLOAD", True):
                raise

            logger.warning(
                "GPU guard denied %s; unloading the service once and rechecking: %s",
                service_type,
                first_error,
            )
            try:
                await unload()
            except Exception as unload_error:
                logger.warning(
                    "Self-unload callback for %s failed after GPU guard denial: %s",
                    service_type,
                    unload_error,
                )
                raise first_error from unload_error

        await require_gpu_resources(service_type)

    def _evict_old(self):
        """Remove old completed/failed jobs to bound memory."""
        done = [
            j for j in self._jobs.values()
            if j.status in ("completed", "failed", "cancelled")
        ]
        if len(done) > self._max_history:
            done.sort(key=lambda j: j.finished_at or 0)
            for j in done[: len(done) - self._max_history]:
                self._jobs.pop(j.job_id, None)

    def _record_job(self, job: Job) -> None:
        if _job_recorder is None:
            return
        try:
            position = 0
            if job.status == "queued":
                for idx, queued in enumerate(list(self._queue._queue)):
                    if queued.job_id == job.job_id:
                        position = idx + 1
                        break
            _job_recorder(job.snapshot(queue_position=position))
        except Exception as exc:
            logger.warning("GPU queue job persistence failed: %s", exc)

    def _update_gauges(self) -> None:
        pending = list(self._queue._queue)
        set_gauge("vocarium_gpu_queue_length", float(len(pending)))
        service_counts: dict[str, int] = {}
        for job in pending:
            service_counts[job.service_type] = service_counts.get(job.service_type, 0) + 1
        for service_type in ("tts", "tts_extra", "dots", "asr", "music", "sfx"):
            set_gauge(
                "vocarium_gpu_queue_length_by_service",
                float(service_counts.get(service_type, 0)),
                {"service_type": service_type},
            )

    async def _heartbeat(self, job: Job) -> None:
        interval = max(1, _env_int("GPU_QUEUE_HEARTBEAT_SECONDS", 10))
        while job.status in ("running", "cancel_requested"):
            await asyncio.sleep(interval)
            if job.status not in ("running", "cancel_requested"):
                break
            job.heartbeat_at = time.time()
            self._record_job(job)

    def _enforce_quota(self, user_id: int | None, service_type: str) -> None:
        if user_id is None or _quota_checker is None:
            return
        decision = _quota_checker(user_id, service_type)
        if not decision.get("allowed", True):
            raise QueueQuotaError(user_id, service_type, decision)

    def _is_cancel_requested(self, job: Job) -> bool:
        if job.cancel_requested:
            return True
        if _cancel_checker is None:
            return False
        try:
            if _cancel_checker(job.job_id):
                job.cancel_requested = True
                return True
        except Exception as exc:
            logger.warning("GPU queue cancel check failed for %s: %s", job.job_id, exc)
        return False


# Module-level singleton
gpu_queue = GpuQueue()
