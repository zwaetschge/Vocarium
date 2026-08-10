FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv ffmpeg libsndfile1 git curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir \
      torch==2.8.0 torchaudio==2.8.0 \
      --index-url https://download.pytorch.org/whl/cu128 && \
    pip install --no-cache-dir \
      dots.tts==0.2.1 \
      fastapi==0.116.1 \
      "uvicorn[standard]==0.35.0" \
      python-multipart==0.0.20 \
      soundfile==0.13.1

COPY server.py /app/server.py

ENV VOICES_DIR=/app/voices \
    DOTS_TTS_MODEL=dots-studio/dots.tts-soar \
    DOTS_TTS_MODEL_REVISION=e3520f75254d0020a0406db31c51a79d00d22d55 \
    DOTS_TTS_PRECISION=bfloat16 \
    DOTS_TTS_NUM_STEPS=10 \
    IDLE_TIMEOUT=120 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8890

CMD ["python", "server.py", "--port", "8890"]
