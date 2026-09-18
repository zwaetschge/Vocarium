FROM python:3.12-slim-bookworm

ARG KIKIRI_COMMIT=a12d0410e89841e6f3c09958ae5c072f90ae1d49
ARG MISAKI_COMMIT=9cda9268309160120fffee216280a9ca83ac4644

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        espeak-ng \
        git \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir \
        torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install --no-cache-dir -r /app/requirements.txt \
    && git clone --recursive https://github.com/semidark/kikiri-tts.git /tmp/kikiri-tts \
    && cd /tmp/kikiri-tts \
    && git checkout "$KIKIRI_COMMIT" \
    && git submodule update --init --recursive \
    && python -m pip install --no-cache-dir --no-deps /tmp/kikiri-tts \
    && python -m pip install --no-cache-dir --force-reinstall --no-deps \
        "git+https://github.com/semidark/misaki.git@$MISAKI_COMMIT" \
    && rm -rf /tmp/kikiri-tts /root/.cache/pip

# The pinned Kokoro pipeline eagerly imports the English frontend (and spaCy)
# even for German-only workers. Keep that optional path lazy in this CPU image.
RUN sed -i '1i from __future__ import annotations' /usr/local/lib/python3.12/site-packages/kokoro/pipeline.py \
    && sed -i 's/from misaki import en, espeak/from misaki import espeak/' /usr/local/lib/python3.12/site-packages/kokoro/pipeline.py \
    && sed -i "/if lang_code in 'ab':/a\\            from misaki import en" /usr/local/lib/python3.12/site-packages/kokoro/pipeline.py

COPY server.py /app/server.py
COPY piper_backend.py /app/piper_backend.py
# Curated fallback voice bank. The ONNX weights themselves stay outside the
# image (bind-mounted at /piper-models) -- 468 MB has no business in a layer.
COPY voices.json /app/voices.json

ENV KIKIRI_MODEL_PATH=/models/model.pth \
    KIKIRI_VOICE_PATH=/models/default.pt \
    KIKIRI_CONFIG_PATH=/models/config.json \
    KIKIRI_PIPER_MODELS_DIR=/piper-models \
    KIKIRI_PIPER_CATALOG=/app/voices.json \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Kein root zur Laufzeit; die Modellverzeichnisse sind ohnehin read-only.
RUN groupadd -g 1000 kikiri && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin kikiri \
    && chown -R 1000:1000 /app
USER kikiri

EXPOSE 8881
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8881", "--workers", "1"]
