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

process: subprocess.Popen | None = None
lock = threading.Lock()
last_activity = time.time()


def start_backend():
    """Start the ACE-Step API server as a subprocess."""
    global process
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
    global process
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
    raise RuntimeError("ACE-Step backend did not start in time")


async def ensure_backend():
    """Ensure the backend is running. Start it if not."""
    global last_activity
    last_activity = time.time()
    with lock:
        if process is None or process.poll() is not None:
            start_backend()
    await wait_for_backend()


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
                if time.time() - last_activity > IDLE_TIMEOUT:
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
    with lock:
        was_running = process is not None and (process.poll() is None if process else False)
        stop_backend()
    return {"status": "unloaded", "was_running": was_running}


@app.get("/health")
async def health():
    """Health check — reports if backend is running."""
    running = process is not None and process.poll() is None
    return {"status": "ok", "backend_running": running}


@app.post("/release_task")
async def release_task(request: Request):
    """Proxy to ACE-Step /release_task — starts backend if needed."""
    await ensure_backend()
    body = await request.body()
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{BACKEND_URL}/release_task", data=body,
                                headers={"Content-Type": "application/json"}) as resp:
            data = await resp.read()
            return Response(content=data, status_code=resp.status,
                            media_type=resp.content_type)


@app.post("/query_result")
async def query_result(request: Request):
    """Proxy to ACE-Step /query_result."""
    global last_activity
    last_activity = time.time()
    if process is None or process.poll() is not None:
        raise HTTPException(503, "ACE-Step backend not running")
    body = await request.body()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{BACKEND_URL}/query_result", data=body,
                                headers={"Content-Type": "application/json"}) as resp:
            data = await resp.read()
            return Response(content=data, status_code=resp.status,
                            media_type=resp.content_type)


@app.get("/v1/audio")
async def get_audio(path: str):
    """Proxy to ACE-Step /v1/audio — download generated audio."""
    global last_activity
    last_activity = time.time()
    if process is None or process.poll() is not None:
        raise HTTPException(503, "ACE-Step backend not running")
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{BACKEND_URL}/v1/audio", params={"path": path}) as resp:
            if resp.status >= 400:
                body = await resp.read()
                raise HTTPException(resp.status, body.decode(errors="replace"))
            data = await resp.read()
            ct = resp.headers.get("Content-Type", "audio/mpeg")
            return Response(content=data, media_type=ct)


@app.post("/format_input")
async def format_input(request: Request):
    """Proxy to ACE-Step /format_input — enhance caption/lyrics via LM."""
    await ensure_backend()
    body = await request.body()
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{BACKEND_URL}/format_input", data=body,
                                headers={"Content-Type": "application/json"}) as resp:
            data = await resp.read()
            return Response(content=data, status_code=resp.status,
                            media_type=resp.content_type)


@app.get("/v1/models")
async def list_models():
    """Proxy to ACE-Step /v1/models."""
    if process is None or process.poll() is not None:
        return {"models": [], "backend_running": False}
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{BACKEND_URL}/v1/models") as resp:
            data = await resp.read()
            return Response(content=data, status_code=resp.status,
                            media_type=resp.content_type)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
