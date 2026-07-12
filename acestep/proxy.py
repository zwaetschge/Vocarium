"""ACE-Step on-demand proxy.

Starts the ACE-Step API server on first request and provides an /unload
endpoint to kill it and free GPU memory for TTS/ASR.
Same pattern as the ASR proxy.
"""

import asyncio
import json
import os
import signal
import subprocess
import threading
import time

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

app = FastAPI(title="ACE-Step Proxy")

BACKEND_PORT = 8010  # internal port for the actual ACE-Step server
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "600"))  # 10 min default

# ACE-Step config
CONFIG_PATH = os.environ.get("ACESTEP_CONFIG_PATH", "acestep-v15-turbo")
LM_MODEL_PATH = os.environ.get("ACESTEP_LM_MODEL_PATH", "acestep-5Hz-lm-0.6B")
LM_BACKEND = os.environ.get("ACESTEP_LM_BACKEND", "pt")
STARTUP_TIMEOUT_SECONDS = int(os.environ.get("ACESTEP_STARTUP_TIMEOUT_SECONDS", "120"))
QUERY_RESULT_TIMEOUT_SECONDS = int(os.environ.get("ACESTEP_QUERY_RESULT_TIMEOUT_SECONDS", "600"))
MODEL_WEIGHT_HINTS = [
    {
        "name": "Qwen3 Embedding",
        "approx_size": "1.19 GB",
        "purpose": "text conditioning",
    },
    {
        "name": CONFIG_PATH,
        "approx_size": "4.79 GB",
        "purpose": "ACE-Step diffusion model",
    },
    {
        "name": LM_MODEL_PATH,
        "approx_size": "3.71 GB for 1.7B, smaller for 0.6B",
        "purpose": "lyrics/prompt language model",
    },
]

process: subprocess.Popen | None = None
lock = threading.Lock()
last_activity = time.time()
backend_starting = False
backend_started_at: float | None = None
last_start_error: str | None = None
active_requests = 0
_http_session: aiohttp.ClientSession | None = None


def _session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


def start_backend():
    """Start the ACE-Step API server as a subprocess."""
    global process, backend_started_at, last_start_error
    if process is not None and process.poll() is None:
        return  # already running

    cmd = [
        "uv", "run", "acestep-api",
        "--host", "127.0.0.1",
        "--port", str(BACKEND_PORT),
        "--lm-model-path", LM_MODEL_PATH,
        "--init-llm",
    ]

    print(f"Starting ACE-Step backend: {' '.join(cmd)}", flush=True)
    backend_started_at = time.time()
    last_start_error = None
    process = subprocess.Popen(
        cmd,
        cwd="/app/acestep",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Stream logs in background
    def log_output():
        if process and process.stdout:
            for line in process.stdout:
                print(f"[acestep] {line.decode(errors='replace').rstrip()}", flush=True)

    threading.Thread(target=log_output, daemon=True).start()


def stop_backend():
    """Stop the ACE-Step API server to free GPU memory."""
    global process, backend_started_at
    if process is None:
        return
    if process.poll() is None:
        print("Stopping ACE-Step backend...", flush=True)
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    process = None
    backend_started_at = None
    print("ACE-Step backend stopped, GPU memory freed", flush=True)


async def wait_for_backend(timeout: float = 120):
    """Wait until the backend is ready (health check passes)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            exit_code = process.returncode
            raise RuntimeError(f"ACE-Step backend died with code {exit_code}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{BACKEND_URL}/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        print("ACE-Step backend ready", flush=True)
                        return
        except Exception:
            pass
        await asyncio.sleep(2)
    raise RuntimeError(
        "ACE-Step backend did not start in time. First run may still be "
        f"downloading model weights: {MODEL_WEIGHT_HINTS}"
    )


async def ensure_backend():
    """Ensure the backend is running. Start it if not."""
    global backend_starting, last_activity, last_start_error
    last_activity = time.time()
    backend_starting = True
    try:
        with lock:
            if process is None or process.poll() is not None:
                start_backend()
        await wait_for_backend(STARTUP_TIMEOUT_SECONDS)
    except Exception as exc:
        last_start_error = str(exc)
        raise
    finally:
        backend_starting = False


async def _acquire_backend() -> None:
    global active_requests, last_activity
    await ensure_backend()
    with lock:
        active_requests += 1
        last_activity = time.time()


def _release_backend() -> None:
    global active_requests, last_activity
    with lock:
        active_requests = max(0, active_requests - 1)
        last_activity = time.time()


def _unload_if_idle() -> dict:
    with lock:
        if active_requests > 0:
            return {
                "status": "busy",
                "was_running": process is not None and process.poll() is None,
                "active_requests": active_requests,
            }
        was_running = process is not None and process.poll() is None
        stop_backend()
        return {"status": "unloaded", "was_running": was_running}


# ---------------------------------------------------------------------------
# Idle shutdown
# ---------------------------------------------------------------------------
async def idle_watcher():
    """Periodically check if the backend has been idle too long."""
    while True:
        await asyncio.sleep(60)
        if IDLE_TIMEOUT <= 0:
            continue
        with lock:
            if process is not None and process.poll() is None:
                if active_requests == 0 and time.time() - last_activity > IDLE_TIMEOUT:
                    print(f"ACE-Step idle for {IDLE_TIMEOUT}s, stopping to free GPU", flush=True)
                    stop_backend()


@app.on_event("startup")
async def startup():
    asyncio.create_task(idle_watcher())
    print(f"ACE-Step proxy ready (backend starts on first request, idle timeout={IDLE_TIMEOUT}s)", flush=True)


# ---------------------------------------------------------------------------
# Proxy endpoints
# ---------------------------------------------------------------------------
@app.post("/unload")
async def unload():
    """Unload ACE-Step backend to free GPU memory."""
    return await asyncio.to_thread(_unload_if_idle)


@app.on_event("shutdown")
async def shutdown():
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    _http_session = None


@app.get("/health")
async def health():
    """Health check — reports if backend is running."""
    running = process is not None and process.poll() is None
    return {
        "status": "ok",
        "backend_running": running,
        "backend_starting": backend_starting,
        "backend_started_at": backend_started_at,
        "last_start_error": last_start_error,
        "active_requests": active_requests,
        "first_load": {
            "may_download": not running,
            "startup_timeout_seconds": STARTUP_TIMEOUT_SECONDS,
            "model_weight_hints": MODEL_WEIGHT_HINTS,
        },
    }


@app.post("/release_task")
async def release_task(request: Request):
    """Proxy to ACE-Step /release_task — starts backend if needed."""
    await _acquire_backend()
    try:
        body = await request.body()
        timeout = aiohttp.ClientTimeout(total=60)
        async with _session().post(f"{BACKEND_URL}/release_task", data=body,
                                   headers={"Content-Type": "application/json"},
                                   timeout=timeout) as resp:
            data = await resp.read()
            return Response(content=data, status_code=resp.status,
                            media_type=resp.content_type)
    finally:
        _release_backend()


@app.post("/query_result")
async def query_result(request: Request):
    """Proxy to ACE-Step /query_result."""
    await _acquire_backend()
    try:
        body = await request.body()
        timeout = aiohttp.ClientTimeout(total=QUERY_RESULT_TIMEOUT_SECONDS)
        async with _session().post(f"{BACKEND_URL}/query_result", data=body,
                                   headers={"Content-Type": "application/json"},
                                   timeout=timeout) as resp:
            data = await resp.read()
            return Response(content=data, status_code=resp.status,
                            media_type=resp.content_type)
    finally:
        _release_backend()


@app.get("/v1/audio")
async def get_audio(path: str):
    """Proxy to ACE-Step /v1/audio — download generated audio."""
    await _acquire_backend()
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with _session().get(f"{BACKEND_URL}/v1/audio", params={"path": path},
                                  timeout=timeout) as resp:
            if resp.status >= 400:
                body = await resp.read()
                raise HTTPException(resp.status, body.decode(errors="replace"))
            data = await resp.read()
            ct = resp.headers.get("Content-Type", "audio/mpeg")
            return Response(content=data, media_type=ct)
    finally:
        _release_backend()


@app.post("/format_input")
async def format_input(request: Request):
    """Proxy to ACE-Step /format_input — enhance caption/lyrics via LM."""
    await _acquire_backend()
    try:
        body = await request.body()
        timeout = aiohttp.ClientTimeout(total=60)
        async with _session().post(f"{BACKEND_URL}/format_input", data=body,
                                   headers={"Content-Type": "application/json"},
                                   timeout=timeout) as resp:
            data = await resp.read()
            return Response(content=data, status_code=resp.status,
                            media_type=resp.content_type)
    finally:
        _release_backend()


@app.get("/v1/models")
async def list_models():
    """Proxy to ACE-Step /v1/models."""
    await _acquire_backend()
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with _session().get(f"{BACKEND_URL}/v1/models", timeout=timeout) as resp:
            data = await resp.read()
            return Response(content=data, status_code=resp.status,
                            media_type=resp.content_type)
    finally:
        _release_backend()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
