"""ASR proxy that starts/stops the vLLM backend on demand.

Starts the qwen-asr-serve process on first request, proxies all
requests to it, and kills it after an idle timeout to free GPU memory.
Dynamically calculates GPU memory utilization based on available VRAM
so it can coexist with TTS on the same GPU.
"""

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8001
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8000"))
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "300"))  # seconds

MODEL = os.environ.get("ASR_MODEL", "Qwen/Qwen3-ASR-0.6B")
MAX_MODEL_LEN = os.environ.get("MAX_MODEL_LEN", "4096")

# Minimum free VRAM (MiB) needed to start. Model ~1.2GB + KV cache ~0.5GB + overhead
MIN_FREE_MIB = 2500

process: subprocess.Popen | None = None
last_used: float = 0.0
lock = threading.Condition(threading.RLock())
idle_timer: threading.Timer | None = None
active_requests = 0
backend_ready = False
backend_starting = False
last_start_error: str | None = None
_http_client: httpx.AsyncClient | None = None

app = FastAPI(title="ASR Proxy")


def _get_free_vram_mib() -> int:
    """Query free GPU memory via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            # Take first GPU (NVIDIA_VISIBLE_DEVICES maps to index 0 inside container)
            return int(result.stdout.decode().strip().split("\n")[0])
    except Exception:
        pass
    return 0


def _get_total_vram_mib() -> int:
    """Query total GPU memory via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            return int(result.stdout.decode().strip().split("\n")[0])
    except Exception:
        pass
    return 12288  # fallback: 12GB


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=30.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


def start_backend():
    global process, backend_ready
    if process is not None and process.poll() is None:
        return

    backend_ready = False

    free_mib = _get_free_vram_mib()
    total_mib = _get_total_vram_mib()

    if free_mib < MIN_FREE_MIB:
        raise RuntimeError(
            f"Not enough free VRAM to start ASR: {free_mib} MiB free, need {MIN_FREE_MIB} MiB. "
            f"TTS may be using too much GPU memory."
        )

    # Calculate gpu_memory_utilization from available VRAM.
    # TTS stays loaded (coexistence mode) so we only take what's free.
    # Keep 500MB safety margin for CUDA runtime.
    safe_bytes = max(free_mib - 500, MIN_FREE_MIB - 500)
    gpu_mem_util = min(0.45, max(0.10, safe_bytes / total_mib))
    gpu_mem_util_str = f"{gpu_mem_util:.2f}"

    print(f"Starting vLLM backend for {MODEL}... "
          f"(free={free_mib}MiB, total={total_mib}MiB, util={gpu_mem_util_str})",
          flush=True)

    process = subprocess.Popen(
        [
            "qwen-asr-serve", MODEL,
            "--gpu-memory-utilization", gpu_mem_util_str,
            "--max-model-len", MAX_MODEL_LEN,
            "--max-num-seqs", "8",
            "--host", BACKEND_HOST,
            "--port", str(BACKEND_PORT),
        ],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    # Wait for backend to be ready
    for i in range(120):
        time.sleep(2)
        if process.poll() is not None:
            exit_code = process.returncode
            stop_backend()
            raise RuntimeError(f"vLLM process died with code {exit_code}")
        try:
            r = httpx.get(f"http://{BACKEND_HOST}:{BACKEND_PORT}/v1/models", timeout=2)
            if r.status_code == 200:
                backend_ready = True
                print(f"vLLM backend ready after {(i+1)*2}s", flush=True)
                return
        except httpx.HTTPError:
            pass
    stop_backend()
    raise RuntimeError("vLLM backend did not become ready in 240s")


def stop_backend():
    global process, backend_ready
    backend_ready = False
    if process is None or process.poll() is not None:
        process = None
        return
    print("Stopping vLLM backend to free GPU memory...", flush=True)
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    process = None
    print("vLLM backend stopped. GPU memory freed.", flush=True)


def schedule_unload():
    global idle_timer
    if IDLE_TIMEOUT <= 0:
        return  # Never unload
    if idle_timer is not None:
        idle_timer.cancel()

    def _check():
        with lock:
            if process is not None and process.poll() is None:
                elapsed = time.time() - last_used
                if active_requests == 0 and backend_ready and elapsed >= IDLE_TIMEOUT:
                    stop_backend()

    idle_timer = threading.Timer(IDLE_TIMEOUT, _check)
    idle_timer.daemon = True
    idle_timer.start()


def acquire_backend() -> None:
    global active_requests, backend_starting, last_used, last_start_error
    with lock:
        waited_for_start = False
        while backend_starting:
            waited_for_start = True
            lock.wait()
        if waited_for_start and last_start_error:
            raise RuntimeError(last_start_error)
        needs_start = not (
            backend_ready and process is not None and process.poll() is None
        )
        # Reserve the lifecycle while starting or claiming the ready backend,
        # so a manual unload cannot slip in before active_requests increments.
        backend_starting = True
    if needs_start:
        try:
            start_backend()
        except Exception as exc:
            with lock:
                last_start_error = str(exc)
                backend_starting = False
                lock.notify_all()
            raise
    with lock:
        last_start_error = None
        active_requests += 1
        last_used = time.time()
        backend_starting = False
        lock.notify_all()


def release_backend() -> None:
    global active_requests, last_used
    with lock:
        active_requests = max(0, active_requests - 1)
        last_used = time.time()
    schedule_unload()


def _unload_if_idle() -> dict:
    with lock:
        if active_requests > 0:
            return {
                "status": "busy",
                "was_running": process is not None and process.poll() is None,
                "active_requests": active_requests,
            }
        if backend_starting:
            return {
                "status": "busy",
                "was_running": process is not None and process.poll() is None,
                "active_requests": active_requests,
                "backend_starting": True,
            }
        was_running = process is not None and process.poll() is None
        stop_backend()
        return {"status": "unloaded", "was_running": was_running}


@app.get("/health")
async def health():
    running = process is not None and process.poll() is None
    return {
        "status": "ok",
        "service": "qwen3-asr-proxy",
        "backend_running": running,
        "backend_ready": backend_ready,
        "backend_starting": backend_starting,
        "active_requests": active_requests,
        "last_start_error": last_start_error,
        "model": MODEL,
        "idle_timeout": IDLE_TIMEOUT,
    }


@app.post("/unload")
async def unload():
    """Unload vLLM backend to free GPU memory for other services."""
    return await asyncio.to_thread(_unload_if_idle)


@app.on_event("shutdown")
async def shutdown():
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    await asyncio.to_thread(acquire_backend)

    url = f"http://{BACKEND_HOST}:{BACKEND_PORT}/{path}"
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)

    try:
        resp = await _client().request(
            method=request.method,
            url=url,
            content=body,
            headers=headers,
            params=list(request.query_params.multi_items()),
        )
    finally:
        await asyncio.to_thread(release_backend)

    # Filter hop-by-hop and server headers to avoid duplicates with uvicorn
    skip = {"server", "date", "transfer-encoding", "connection", "content-length", "content-encoding"}
    fwd_headers = {k: v for k, v in resp.headers.items() if k.lower() not in skip}

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=fwd_headers,
    )


if __name__ == "__main__":
    timeout_msg = f"{IDLE_TIMEOUT}s" if IDLE_TIMEOUT > 0 else "disabled (never unload)"
    print(f"ASR proxy starting on port {LISTEN_PORT}. "
          f"Backend will start on first request. "
          f"Idle timeout: {timeout_msg}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT)
