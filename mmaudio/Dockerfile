FROM nvidia/cuda:12.6.0-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone MMAudio
RUN git clone --depth 1 https://github.com/hkchengrex/MMAudio.git /app/mmaudio

WORKDIR /app/mmaudio

# Install PyTorch + torchaudio first, then MMAudio
RUN pip3 install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cu124 && \
    pip3 install --no-cache-dir -e . && \
    pip3 install --no-cache-dir fastapi uvicorn soundfile scipy

# Copy our server
COPY server.py /app/server.py

EXPOSE 8004

CMD ["python3", "/app/server.py"]
