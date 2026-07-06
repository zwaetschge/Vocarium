FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    ffmpeg \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# F5-TTS German checkpoints may be version-sensitive. Override
# F5_TTS_PACKAGE in the build environment if a specific pin is required.
ARG F5_TTS_PACKAGE=f5-tts
RUN pip install --upgrade pip setuptools wheel && \
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124 && \
    pip install fastapi uvicorn soundfile numpy python-multipart && \
    pip install "${F5_TTS_PACKAGE}"

COPY server.py /app/server.py

EXPOSE 8885
CMD ["python", "/app/server.py"]
