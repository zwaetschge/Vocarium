# Whisper STT serving (Flix-AI/flix-swissgerman-full, a whisper-large-v3-turbo
# finetune). Reuses the CUDA/PyTorch layers of the kikiri trainer image; build
# that first (docker compose build kikiri-trainer).
ARG BASE_IMAGE=vocarium-kikiri-trainer:latest
FROM ${BASE_IMAGE}

RUN which ffmpeg || (apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*)
# Upgrade beyond the trainer's pin: the Flix tokenizer_config needs the newer
# extra_special_tokens dict format. Only this image is affected.
# faster-whisper/CTranslate2 serves the model in int8_float16 (~2.5 GB VRAM);
# transformers is only needed for the one-time CT2 conversion.
RUN python -m pip install --no-cache-dir --upgrade transformers accelerate fastapi uvicorn python-multipart faster-whisper
# CTranslate2-Wheels sind gegen CUDA 12 gebaut; die Basis ist CUDA 13. Die
# cu12-Laufzeit kommt als Pip-Wheels dazu und wird per LD_LIBRARY_PATH gefunden.
RUN python -m pip install --no-cache-dir nvidia-cublas-cu12 nvidia-cudnn-cu12
ENV LD_LIBRARY_PATH=/app/venv/lib/python3.12/site-packages/nvidia/cublas/lib:/app/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:${LD_LIBRARY_PATH}

WORKDIR /srv
COPY server.py /srv/server.py

ENV WHISPER_MODEL=Flix-AI/flix-swissgerman-full \
    HF_HOME=/models \
    IDLE_TIMEOUT=300 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
ENTRYPOINT []
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
