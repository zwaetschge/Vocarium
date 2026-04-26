#!/usr/bin/env bash
# Download the HuggingFace models Vocarium needs into a host-side cache.
#
# Models are pulled via huggingface_hub (in a temporary container) so:
#   - users don't need a Python venv on the host
#   - downloads are resumable
#   - the cache layout matches what the runtime containers expect
#
# Why pre-download?
#   - Models are several GB each; doing this up front means `docker compose
#     up` won't sit at 0% for 20 minutes on first launch.
#   - ACE-Step and MMAudio download their weights lazily on first request,
#     so they're skipped here unless --all is passed.
#
# Usage:
#   scripts/download-models.sh           # core: TTS (3 variants) + ASR
#   scripts/download-models.sh --all     # also pre-pull ACE-Step + MMAudio
#   scripts/download-models.sh --tts     # TTS only
#   scripts/download-models.sh --asr     # ASR only
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# shellcheck disable=SC1091
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a

CACHE_DIR="${HF_CACHE_DIR:-$ROOT/models}"
TTS_CACHE_DIR="$ROOT/qwen3-tts/models"

# Resolve to absolute paths so the bind mount works regardless of CWD.
case "$CACHE_DIR" in
  /*) ;;
  *) CACHE_DIR="$ROOT/${CACHE_DIR#./}" ;;
esac

mkdir -p "$CACHE_DIR" "$TTS_CACHE_DIR"

WHAT="core"
case "${1:-}" in
  --all)  WHAT="all" ;;
  --tts)  WHAT="tts" ;;
  --asr)  WHAT="asr" ;;
  --help|-h)
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  "")     ;;
  *) echo "Unknown option: $1"; exit 2 ;;
esac

TTS_MODELS=(
  "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
  "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
  "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
)
ASR_MODELS=("Qwen/Qwen3-ASR-0.6B")

declare -a TARGETS=()
case "$WHAT" in
  core|all) TARGETS=("${TTS_MODELS[@]}" "${ASR_MODELS[@]}") ;;
  tts)      TARGETS=("${TTS_MODELS[@]}") ;;
  asr)      TARGETS=("${ASR_MODELS[@]}") ;;
esac

echo "==> Vocarium model downloader"
echo "    Cache (ASR + shared): $CACHE_DIR"
echo "    Cache (TTS):          $TTS_CACHE_DIR"
echo "    Targets:"
for m in "${TARGETS[@]}"; do echo "      - $m"; done
echo

# Run in a one-shot container so the host doesn't need huggingface_hub.
# Pre-pull the image once for nicer logs.
IMAGE="python:3.11-slim"
docker pull "$IMAGE" >/dev/null

run_dl() {
  local model="$1" cache_target="$2"
  echo "==> $model -> $cache_target"
  docker run --rm \
    -e HF_HOME=/cache \
    -e HF_HUB_ENABLE_HF_TRANSFER=1 \
    -v "$cache_target":/cache \
    -v "$ROOT/scripts/_dl_one.py":/_dl_one.py:ro \
    "$IMAGE" \
    bash -c "pip install --quiet --no-cache-dir 'huggingface_hub[hf_transfer]>=0.24' && python /_dl_one.py '$model'"
}

# Each TTS variant goes into the qwen3-tts/models cache (bind-mounted into
# both TTS containers). ASR goes into the shared cache. ACE-Step and
# MMAudio use their own named volumes — we can't easily pre-populate those
# from the host without complicating things, so we leave them lazy.
for model in "${TARGETS[@]}"; do
  case "$model" in
    Qwen/Qwen3-TTS-*) run_dl "$model" "$TTS_CACHE_DIR" ;;
    Qwen/Qwen3-ASR-*) run_dl "$model" "$CACHE_DIR" ;;
    *) run_dl "$model" "$CACHE_DIR" ;;
  esac
done

if [ "$WHAT" = "all" ]; then
  echo
  echo "==> ACE-Step and MMAudio download their own weights on first request."
  echo "    They run inside their containers, so the cache lives in the"
  echo "    'acestep-cache' / 'mmaudio-cache' Docker volumes. Trigger them"
  echo "    by hitting /api/music/health and /api/sfx/health once the stack"
  echo "    is up."
fi

echo
echo "Done."
