FROM nvidia/cuda:12.6.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv git ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
RUN pip3 install --no-cache-dir uv

WORKDIR /app

# Clone ACE-Step
RUN git clone --depth 1 https://github.com/ACE-Step/ACE-Step-1.5.git /app/acestep

WORKDIR /app/acestep
RUN uv sync

# Copy our proxy wrapper
COPY proxy.py /app/proxy.py
COPY requirements-proxy.txt /app/requirements-proxy.txt
RUN uv pip install --system -r /app/requirements-proxy.txt

EXPOSE 8003

CMD ["python3", "/app/proxy.py"]
