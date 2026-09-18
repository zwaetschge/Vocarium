#!/usr/bin/env bash
# Pre-fetch the model weights Vocarium needs so the first request does not
# wait on a download.
#
#   models/                    Whisper HF cache (HF_HOME of whisper-stt):
#                                Systran/faster-whisper-large-v3   (profile german)
#                                Flix-AI/flix-swissgerman-full     (profile swiss,
#                                converted to CTranslate2 on first use)
#   llama-embeddings/models/   bge-m3 GGUF for the CPU embedding service
#
# OmniVoice pulls its own weights into ${OMNIVOICE_HF_CACHE} on first start;
# Kikiri voices are checked-in fine-tunes under kikiri-tts/models/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${WHISPER_MODEL_DIR:-$ROOT/models}"
WHISPER_MODELS=("Systran/faster-whisper-large-v3" "${WHISPER_SWISS_MODEL:-Flix-AI/flix-swissgerman-full}")

mkdir -p "$CACHE_DIR"
echo "==> Whisper cache: $CACHE_DIR"
for repo in "${WHISPER_MODELS[@]}"; do
  echo "    pulling $repo"
  docker run --rm -e HF_HOME=/models -v "$CACHE_DIR:/models" python:3.11-slim sh -c \
    "pip install --quiet --no-cache-dir 'huggingface_hub[cli]' >/dev/null && \
     python -c \"from huggingface_hub import snapshot_download; snapshot_download('$repo')\""
done

echo "==> Embedding model"
"$ROOT/scripts/download-embedding-model.sh"
echo "==> Done. OmniVoice downloads its weights on first container start."
