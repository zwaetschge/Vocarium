"""MMAudio Sound Effects Server.

On-demand model loading with /unload endpoint for GPU sharing.
Generates sound effects from text prompts using MMAudio large_44k_v2.
"""

import io
import gc
import hashlib
import os
from pathlib import Path
import shutil
import asyncio
import threading
import time

import requests
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = FastAPI(title="MMAudio SFX Server")

# Global state
model_loaded = False
net = None
fm = None
feature_utils = None
seq_cfg = None
rng = None
lock = threading.Lock()
last_activity = time.time()

DEVICE = "cuda"
DTYPE = torch.bfloat16
VARIANT = "large_44k_v2"
MODEL_CACHE_DIR = Path(os.environ.get("MMAUDIO_MODEL_CACHE_DIR", "/root/.cache/mmaudio"))
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "600"))  # seconds
MODEL_WEIGHT_HINTS = [
    {
        "name": "mmaudio_large_44k_v2.pth",
        "approx_size": "4.12 GB",
        "purpose": "MMAudio large 44 kHz SFX generation model",
    }
]
RESTART_ON_UNLOAD = os.environ.get("RESTART_ON_UNLOAD", "1").lower() not in {
    "0",
    "false",
    "no",
}
exit_scheduled = False
model_loading = False
model_load_started_at: float | None = None
last_load_error: str | None = None


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_model_file(path: Path, url: str, expected_md5: str) -> None:
    """Download a model file resumably and publish it only after verification."""
    if path.exists() and _file_md5(path) == expected_md5:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(f"{path.suffix}.part")
    if partial.exists() and _file_md5(partial) == expected_md5:
        partial.replace(path)
        return
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))

    try:
        for attempt in range(1, 4):
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with session.get(url, headers=headers, stream=True, timeout=(30, 300)) as response:
                    response.raise_for_status()
                    append = offset > 0 and response.status_code == 206
                    mode = "ab" if append else "wb"
                    with partial.open(mode) as target:
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk:
                                target.write(chunk)
                if _file_md5(partial) != expected_md5:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError(f"Checksum mismatch for {path.name}")
                partial.replace(path)
                return
            except (OSError, requests.RequestException, RuntimeError):
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
    finally:
        session.close()


def _prepare_model_files(model_cfg) -> None:
    """Move MMAudio's required weights into the persistent cache and ensure them."""
    from mmaudio.utils.download_utils import links

    link_by_name = {item["name"]: item for item in links}
    paths = [
        ("model_path", "weights"),
        ("vae_path", "ext_weights"),
        ("bigvgan_16k_path", "ext_weights"),
        ("synchformer_ckpt", "ext_weights"),
    ]
    for attribute, directory in paths:
        original = getattr(model_cfg, attribute)
        if original is None:
            continue
        original = Path(original)
        cached = MODEL_CACHE_DIR / directory / original.name
        cached.parent.mkdir(parents=True, exist_ok=True)
        if not cached.exists() and original.exists():
            shutil.copy2(original, cached)
        setattr(model_cfg, attribute, cached)
        link = link_by_name[original.name]
        _download_model_file(cached, link["url"], link["md5"])


def _has_model_state() -> bool:
    """Return true if any model object is still referenced by the process."""
    return any(obj is not None for obj in (net, fm, feature_utils, seq_cfg, rng))


async def _exit_after_response(reason: str):
    await asyncio.sleep(0.5)
    print(f"MMAudio process exiting after unload ({reason})", flush=True)
    os._exit(0)


def _schedule_process_exit(reason: str) -> bool:
    """Restart the server process so CUDA contexts held by dependencies die."""
    global exit_scheduled
    if not RESTART_ON_UNLOAD or exit_scheduled:
        return False
    exit_scheduled = True
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_exit_after_response(reason))
    except RuntimeError:
        timer = threading.Timer(0.5, lambda: os._exit(0))
        timer.daemon = True
        timer.start()
    return True


def _load_model():
    """Load MMAudio model into GPU memory."""
    global last_load_error, model_load_started_at, model_loaded, model_loading
    global net, fm, feature_utils, seq_cfg, rng

    model_loading = True
    model_load_started_at = time.time()
    last_load_error = None
    model_loaded = False

    try:
        from mmaudio.eval_utils import all_model_cfg, setup_eval_logging
        from mmaudio.model.networks import get_my_mmaudio
        from mmaudio.model.flow_matching import FlowMatching
        from mmaudio.model.utils.features_utils import FeaturesUtils

        setup_eval_logging()
        print(f"Loading MMAudio {VARIANT}...", flush=True)

        model_cfg = all_model_cfg[VARIANT]
        _prepare_model_files(model_cfg)
        seq_cfg = model_cfg.seq_cfg

        net = get_my_mmaudio(model_cfg.model_name).to(DEVICE, DTYPE).eval()
        net.load_weights(torch.load(model_cfg.model_path, map_location=DEVICE, weights_only=True))

        fm = FlowMatching(min_sigma=0, inference_mode="euler", num_steps=25)

        feature_utils = FeaturesUtils(
            tod_vae_ckpt=model_cfg.vae_path,
            synchformer_ckpt=model_cfg.synchformer_ckpt,
            enable_conditions=True,
            mode=model_cfg.mode,
            bigvgan_vocoder_ckpt=model_cfg.bigvgan_16k_path,
            need_vae_encoder=False,
        )
        feature_utils = feature_utils.to(DEVICE, DTYPE).eval()

        rng = torch.Generator(device=DEVICE)
        model_loaded = True
        print(f"MMAudio {VARIANT} loaded successfully", flush=True)
    except Exception as exc:
        last_load_error = str(exc)
        _unload_model()
        _schedule_process_exit("model load failed")
        raise
    finally:
        model_loading = False


def _unload_model():
    """Unload model from GPU to free VRAM."""
    global net, fm, feature_utils, seq_cfg, rng, model_loaded
    net = None
    fm = None
    feature_utils = None
    seq_cfg = None
    rng = None
    model_loaded = False
    gc.collect()
    if torch.cuda.is_available():
        try:
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        except Exception as exc:
            print(f"MMAudio CUDA cache cleanup failed: {exc}", flush=True)
    print("MMAudio model unloaded, GPU memory freed", flush=True)


def _ensure_loaded():
    """Ensure model is loaded."""
    global last_activity
    last_activity = time.time()
    if not model_loaded:
        _load_model()


async def idle_watcher():
    while True:
        await asyncio.sleep(60)
        if IDLE_TIMEOUT <= 0:
            continue
        with lock:
            if (model_loaded or _has_model_state()) and time.time() - last_activity > IDLE_TIMEOUT:
                print(f"MMAudio idle for {IDLE_TIMEOUT}s, unloading to free GPU", flush=True)
                _unload_model()
                _schedule_process_exit("idle timeout")


@app.on_event("startup")
async def startup():
    asyncio.create_task(idle_watcher())
    print(f"MMAudio SFX server ready (model loads on first request, idle timeout={IDLE_TIMEOUT}s)", flush=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    prompt: str
    negative_prompt: str = ""
    duration: float = 8.0
    cfg_strength: float = 4.5
    num_steps: int = 25
    seed: int | None = None


def _generate_sfx_blocking(req: GenerateRequest) -> tuple[bytes, float, int]:
    """Load and run MMAudio off the FastAPI event loop."""
    from mmaudio.eval_utils import generate
    import soundfile as sf

    with lock:
        try:
            _ensure_loaded()
        except Exception as exc:
            _schedule_process_exit("model load failed")
            raise RuntimeError(f"MMAudio model load failed: {exc}") from exc

        try:
            seq_cfg.duration = req.duration
            fm.num_steps = req.num_steps
            net.update_seq_lengths(
                seq_cfg.latent_seq_len, seq_cfg.clip_seq_len, seq_cfg.sync_seq_len
            )

            if req.seed is not None:
                rng.manual_seed(req.seed)
            else:
                rng.manual_seed(int(time.time() * 1000) % (2**32))

            start = time.time()
            with torch.no_grad():
                audios = generate(
                    clip_video=None,
                    sync_video=None,
                    text=[req.prompt],
                    negative_text=[req.negative_prompt] if req.negative_prompt else [""],
                    feature_utils=feature_utils,
                    net=net,
                    fm=fm,
                    rng=rng,
                    cfg_strength=req.cfg_strength,
                )
            gen_time = time.time() - start
            sampling_rate = seq_cfg.sampling_rate
            audio = audios.float().detach().cpu()[0]  # may be 1D or 2D

        except Exception as exc:
            _unload_model()
            _schedule_process_exit("generation failed")
            raise RuntimeError(f"Generation failed: {exc}") from exc

    if audio.dim() == 1:
        audio = audio.unsqueeze(0)  # (1, samples)
    elif audio.dim() > 2:
        audio = audio.squeeze()
        if audio.dim() == 1:
            audio = audio.unsqueeze(0)

    buf = io.BytesIO()
    sf.write(buf, audio.numpy().T, sampling_rate, format="WAV")
    return buf.getvalue(), gen_time, sampling_rate


@app.post("/generate")
async def generate_sfx(req: GenerateRequest):
    """Generate a sound effect from a text prompt. Returns WAV audio."""
    if not req.prompt.strip():
        raise HTTPException(400, "Prompt is required")
    if len(req.prompt) > 1000 or len(req.negative_prompt) > 1000:
        raise HTTPException(413, "Prompt fields must be at most 1000 characters")
    if req.duration < 1 or req.duration > 30:
        raise HTTPException(400, "Duration must be between 1 and 30 seconds")
    if req.cfg_strength < 1 or req.cfg_strength > 10:
        raise HTTPException(400, "cfg_strength must be between 1 and 10")
    if req.num_steps < 1 or req.num_steps > 100:
        raise HTTPException(400, "num_steps must be between 1 and 100")

    try:
        wav_bytes, gen_time, sampling_rate = await asyncio.to_thread(
            _generate_sfx_blocking, req
        )
    except Exception as exc:
        status = 503 if "model load failed" in str(exc).lower() else 500
        raise HTTPException(status, str(exc)) from exc

    from fastapi.responses import Response
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            "X-Generation-Time": f"{gen_time:.2f}",
            "X-Audio-Duration": f"{req.duration:.1f}",
            "X-Sample-Rate": str(sampling_rate),
        },
    )


@app.post("/unload")
async def unload():
    """Unload model to free GPU memory."""
    acquired = lock.acquire(blocking=False)
    if not acquired:
        return {
            "status": "busy",
            "was_loaded": model_loaded,
            "had_state": _has_model_state(),
            "model_loading": model_loading,
            "restart_scheduled": False,
        }
    try:
        was_loaded = model_loaded
        had_state = _has_model_state()
        restart_scheduled = False
        if was_loaded or had_state:
            _unload_model()
            restart_scheduled = _schedule_process_exit("manual unload")
        return {
            "status": "unloaded",
            "was_loaded": was_loaded,
            "had_state": had_state,
            "restart_scheduled": restart_scheduled,
        }
    finally:
        lock.release()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_loaded": model_loaded,
        "model_loading": model_loading,
        "model_load_started_at": model_load_started_at,
        "last_load_error": last_load_error,
        "has_model_state": _has_model_state(),
        "variant": VARIANT,
        "idle_timeout": IDLE_TIMEOUT,
        "restart_on_unload": RESTART_ON_UNLOAD,
        "exit_scheduled": exit_scheduled,
        "first_load": {
            "may_download": not model_loaded,
            "model_weight_hints": MODEL_WEIGHT_HINTS,
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8004)
