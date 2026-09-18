# Kikiri fine-tune trainer.
#
# Reuses the CUDA + PyTorch layers of the qwen3-tts image, so build that service
# first (`docker compose build qwen3-tts`). Only the trainer needs the StyleTTS2
# checkout; the CPU serving image stays slim.
ARG BASE_IMAGE=vocarium-qwen3-tts:latest
FROM ${BASE_IMAGE}

ARG KIKIRI_COMMIT=a12d0410e89841e6f3c09958ae5c072f90ae1d49
ARG MISAKI_COMMIT=9cda9268309160120fffee216280a9ca83ac4644

USER root

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        espeak-ng \
        ffmpeg \
        git \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
        Cython==3.1.3 \
        einops-exts==0.0.4 \
        espeakng-loader==0.2.4 \
        fastapi==0.115.6 \
        httpx==0.28.1 \
        matplotlib==3.10.6 \
        munch==4.0.0 \
        nltk==3.9.1 \
        phonemizer-fork==3.3.2 \
        tensorboard==2.20.0 \
        uvicorn==0.34.0 \
    && python -m pip install --no-cache-dir \
        git+https://github.com/resemble-ai/monotonic_align.git@c6e5e6cb19882164027eb6e35118e841eed9298e \
        git+https://github.com/semidark/misaki.git@${MISAKI_COMMIT}

RUN git clone --recursive https://github.com/semidark/kikiri-tts.git /workspace \
    && cd /workspace \
    && git checkout "${KIKIRI_COMMIT}" \
    && git submodule update --init --recursive

COPY patches /app/patches
# See patches/ungate_timbre.py: upstream ties timbre learning to the SLM
# adversarial phase, which cannot run here. Without this a fine-tune silently
# produces a voice that never learns how its speaker sounds.
RUN python /app/patches/ungate_timbre.py

WORKDIR /app
COPY server.py /app/server.py
COPY pipeline /app/pipeline
COPY assets /app/assets

ENV PYTHONPATH=/workspace/StyleTTS2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KIKIRI_WORKSPACE=/workspace \
    TRAINER_JOBS_DIR=/jobs \
    TRAINER_LIBRARY_DIR=/library \
    TRAINER_PUBLISH_DIR=/models

EXPOSE 8206
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8206", "--workers", "1"]
