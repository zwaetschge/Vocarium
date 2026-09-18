# Shared CUDA + PyTorch base for the GPU serving images (omnivoice-tts,
# whisper-stt).
#
# These layers used to be inherited from the kikiri trainer, which in turn
# inherited them from qwen3-tts. Both are retired, so the chain now starts
# here: nothing in this image is specific to a single engine, and the
# consumers add only their own packages on top.
#
# It is a build-only service (Compose profile "build"); `docker compose up`
# never starts it. Build it before its consumers:
#   docker compose build gpu-base omnivoice-tts whisper-stt
FROM nvidia/cuda:13.0.2-cudnn-runtime-ubuntu24.04

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev \
    git ffmpeg libsndfile1 sox curl && \
    rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

# The venv path is part of the contract: whisper-stt extends LD_LIBRARY_PATH
# with /app/venv/lib/python3.12/site-packages/nvidia/... to find its CUDA-12
# CTranslate2 wheels.
RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir \
    torch torchaudio --index-url https://download.pytorch.org/whl/cu130 && \
    pip install --no-cache-dir soundfile numpy scipy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["python", "-c", "print('gpu-base is a build-only image')"]
