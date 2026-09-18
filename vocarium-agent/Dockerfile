FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.145.0
ARG CLAUDE_CODE_VERSION=2.1.220

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 tini \
    && npm install --global "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10003 agent \
    && useradd --uid 10003 --gid agent --create-home --shell /usr/sbin/nologin agent

WORKDIR /app
COPY --chown=10003:10003 runner.py maintenance.py mapping.schema.json episode_context.schema.json alignment.schema.json ./
COPY requirements.toml /etc/codex/requirements.toml

USER 10003:10003
ENV HOME=/home/agent CODEX_HOME=/home/agent/.codex CLAUDE_CONFIG_DIR=/home/agent/.claude CODEX_CLI_VERSION=${CODEX_VERSION} CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION} PYTHONUNBUFFERED=1
EXPOSE 8090
HEALTHCHECK --interval=20s --timeout=4s --start-period=5s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=2)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/runner.py"]
