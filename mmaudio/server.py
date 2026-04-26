"""MMAudio Sound Effects Server.

On-demand model loading with /unload endpoint for GPU sharing.
Generates sound effects from text prompts using MMAudio large_44k_v2.
"""

import io
import gc
import threading
import time

import torch
import torchaudio
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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
IDLE_TIMEOUT = 600  # 10 min


def _load_model():
    """Load MMAudio model into GPU memory."""
    global net, fm, feature_utils, seq_cfg, rng, model_loaded

    from mmaudio.eval_utils import all_model_cfg, setup_eval_logging
    from mmaudio.model.networks import get_my_mmaudio
    from mmaudio.model.flow_matching import FlowMatching
    from mmaudio.model.utils.features_utils import FeaturesUtils

    setup_eval_logging()
    print(f"Loading MMAudio {VARIANT}...", flush=True)

    model_cfg = all_model_cfg[VARIANT]
    model_cfg.download_if_needed()
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
        torch.cuda.empty_cache()
    print("MMAudio model unloaded, GPU memory freed", flush=True)


def _ensure_loaded():
    """Ensure model is loaded."""
    global last_activity
    last_activity = time.time()
    if not model_loaded:
        _load_model()


# ---------------------------------------------------------------------------
# Idle watcher
# ---------------------------------------------------------------------------
import asyncio


async def idle_watcher():
    while True:
        await asyncio.sleep(60)
        if IDLE_TIMEOUT <= 0:
            continue
        with lock:
            if model_loaded and time.time() - last_activity > IDLE_TIMEOUT:
                print(f"MMAudio idle for {IDLE_TIMEOUT}s, unloading to free GPU", flush=True)
                _unload_model()


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


@app.post("/generate")
async def generate_sfx(req: GenerateRequest):
    """Generate a sound effect from a text prompt. Returns WAV audio."""
    with lock:
        _ensure_loaded()

    if req.duration < 1 or req.duration > 30:
        raise HTTPException(400, "Duration must be between 1 and 30 seconds")

    from mmaudio.eval_utils import generate

    try:
        with lock:
            # Update duration
            seq_cfg.duration = req.duration
            net.update_seq_lengths(
                seq_cfg.latent_seq_len, seq_cfg.clip_seq_len, seq_cfg.sync_seq_len
            )

            # Set seed
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

        audio = audios.float().cpu()[0]  # may be 1D or 2D
        if audio.dim() == 1:
            audio = audio.unsqueeze(0)  # (1, samples)
        elif audio.dim() > 2:
            audio = audio.squeeze()
            if audio.dim() == 1:
                audio = audio.unsqueeze(0)

        # Encode to WAV
        buf = io.BytesIO()
        torchaudio.save(buf, audio, seq_cfg.sampling_rate, format="wav")
        wav_bytes = buf.getvalue()

        from fastapi.responses import Response
        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={
                "X-Generation-Time": f"{gen_time:.2f}",
                "X-Audio-Duration": f"{req.duration:.1f}",
                "X-Sample-Rate": str(seq_cfg.sampling_rate),
            },
        )

    except Exception as e:
        raise HTTPException(500, f"Generation failed: {e}")


@app.post("/unload")
async def unload():
    """Unload model to free GPU memory."""
    with lock:
        was_loaded = model_loaded
        if model_loaded:
            _unload_model()
    return {"status": "unloaded", "was_loaded": was_loaded}


@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": model_loaded, "variant": VARIANT}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8004)
