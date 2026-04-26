FROM nvidia/cuda:13.0.2-cudnn-runtime-ubuntu24.04

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    git ffmpeg libsndfile1 sox curl && \
    rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir \
    torch torchaudio --index-url https://download.pytorch.org/whl/cu130 && \
    pip install --no-cache-dir \
    faster-qwen3-tts \
    fastapi \
    "uvicorn[standard]" \
    soundfile \
    numpy \
    scipy

COPY server.py /app/server.py

# Optional: bake in a default reference voice. Drop voice.wav + voice.txt
# into qwen3-tts/ref_audio/ before building. The directory exists but may
# be empty — server.py handles both cases.
RUN mkdir -p /app/ref_audio
COPY ref_audio/ /app/ref_audio/

ENV MODELS_DIR=/app/models
ENV VOICES_DIR=/app/voices
VOLUME /app/models
VOLUME /app/voices

EXPOSE 8880

CMD ["python", "server.py", "--port", "8880"]
