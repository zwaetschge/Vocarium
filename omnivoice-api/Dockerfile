# OmniVoice serving. Reuses the CUDA/PyTorch layers of the kikiri trainer image;
# build that first (docker compose build kikiri-trainer).
ARG BASE_IMAGE=vocarium-kikiri-trainer:latest
FROM ${BASE_IMAGE}

RUN python -m pip install --no-cache-dir omnivoice fastapi uvicorn soundfile

WORKDIR /srv
COPY server.py /srv/server.py

ENV OMNIVOICE_VOICES_DIR=/voices \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8880
ENTRYPOINT []
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8880", "--workers", "1"]
