#!/usr/bin/env bash
# Vocarium installer — idempotent, safe to re-run.
#
# Steps:
#   1. Verify prerequisites (Docker, Compose v2, NVIDIA Container Toolkit).
#   2. Bootstrap .env from .env.example if missing.
#   3. Pre-pull the Qwen3 TTS + ASR weights (skip with --skip-models).
#   4. Build container images.
#   5. Start the stack and run a smoke test.
#
# Usage:
#   scripts/install.sh                    # full install
#   scripts/install.sh --skip-models      # don't pre-download weights
#   scripts/install.sh --prod             # use docker-compose.prod.yml overlay
#   scripts/install.sh --no-start         # build but don't `up`
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

SKIP_MODELS=0
PROD=0
NO_START=0

for arg in "$@"; do
  case "$arg" in
    --skip-models) SKIP_MODELS=1 ;;
    --prod)        PROD=1 ;;
    --no-start)    NO_START=1 ;;
    --help|-h)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown flag: $arg"; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
bold "[1/5] Checking prerequisites"

command -v docker >/dev/null 2>&1 || die "Docker not found in PATH."
ok "docker: $(docker --version)"

if docker compose version >/dev/null 2>&1; then
  ok "compose: $(docker compose version --short)"
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  warn "Using legacy docker-compose; v2 (`docker compose`) recommended."
  COMPOSE=(docker-compose)
else
  die "Docker Compose v2 not found. Install: https://docs.docker.com/compose/install/"
fi

# NVIDIA Container Toolkit — required for runtime: nvidia.
if docker info 2>/dev/null | grep -qi 'Runtimes:.*nvidia'; then
  ok "nvidia container runtime detected"
else
  warn "nvidia runtime not found in 'docker info'."
  warn "If this is a CPU-only host, the GPU services will fail to start."
  warn "Install: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
fi

# Quick GPU sanity check (non-fatal).
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_COUNT="$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l | tr -d ' ')"
  ok "$GPU_COUNT GPU(s) visible to host"
else
  warn "nvidia-smi not on PATH (non-fatal if NVIDIA toolkit is configured)."
fi

# ---------------------------------------------------------------------------
# 2. .env bootstrap
# ---------------------------------------------------------------------------
bold "[2/5] Configuration"

if [ ! -f .env ]; then
  cp .env.example .env
  ok "Created .env from .env.example. Review it before next run."
else
  ok ".env already exists — leaving it alone."
fi

# ---------------------------------------------------------------------------
# 3. Pre-pull models
# ---------------------------------------------------------------------------
bold "[3/5] Models"
if [ "$SKIP_MODELS" -eq 1 ]; then
  warn "Skipping model download (--skip-models). First container start will"
  warn "download them lazily — expect a slow startup the first time."
else
  bash "$HERE/download-models.sh"
fi

# ---------------------------------------------------------------------------
# 4. Build images
# ---------------------------------------------------------------------------
bold "[4/5] Building images"

COMPOSE_FILES=(-f docker-compose.yml)
if [ "$PROD" -eq 1 ]; then
  COMPOSE_FILES+=(-f docker-compose.prod.yml)
  ok "Production overlay enabled (internal services hidden from host)."
fi

# Build the UI separately first so its tsc errors surface clearly.
"${COMPOSE[@]}" "${COMPOSE_FILES[@]}" build vocarium-ui
"${COMPOSE[@]}" "${COMPOSE_FILES[@]}" build

# ---------------------------------------------------------------------------
# 5. Start
# ---------------------------------------------------------------------------
bold "[5/5] Starting stack"

if [ "$NO_START" -eq 1 ]; then
  warn "Skipping startup (--no-start). Run yourself:"
  echo "  ${COMPOSE[*]} ${COMPOSE_FILES[*]} up -d"
  exit 0
fi

"${COMPOSE[@]}" "${COMPOSE_FILES[@]}" up -d

# Smoke test — wait up to 60s for the gateway to respond.
PORT="$(grep -E '^VOCARIUM_UI_PORT=' .env | cut -d= -f2)"
PORT="${PORT:-3100}"
API_PORT="$(grep -E '^VOCARIUM_API_PORT=' .env | cut -d= -f2)"
API_PORT="${API_PORT:-8280}"

printf "Waiting for vocarium-api on http://localhost:%s ..." "$API_PORT"
for _ in $(seq 1 30); do
  if curl -sf "http://localhost:$API_PORT/api/health" >/dev/null 2>&1; then
    echo " ready"
    ok "Vocarium is running."
    echo
    echo "  UI:  http://localhost:$PORT"
    echo "  API: http://localhost:$API_PORT/api/health"
    echo
    echo "Tail logs:   ${COMPOSE[*]} ${COMPOSE_FILES[*]} logs -f vocarium-api"
    echo "Stop:        ${COMPOSE[*]} ${COMPOSE_FILES[*]} down"
    exit 0
  fi
  printf "."
  sleep 2
done
echo
warn "Gateway didn't respond within 60s. Check logs:"
echo "  ${COMPOSE[*]} ${COMPOSE_FILES[*]} logs vocarium-api"
exit 1
