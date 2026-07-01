FROM nvidia/cuda:13.0.2-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv python3-dev git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# Clone MMAudio
RUN git clone --depth 1 https://github.com/hkchengrex/MMAudio.git /app/mmaudio

WORKDIR /app/mmaudio

# Install PyTorch + torchaudio first, then MMAudio
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir \
        torch==2.11.0+cu130 \
        torchaudio==2.11.0+cu130 \
        torchvision==0.26.0+cu130 \
        --index-url https://download.pytorch.org/whl/cu130 && \
    pip install --no-cache-dir -e . && \
    pip install --no-cache-dir fastapi uvicorn soundfile scipy

# Copy our server
COPY server.py /app/server.py

EXPOSE 8004

CMD ["python", "/app/server.py"]
