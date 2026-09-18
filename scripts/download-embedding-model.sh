#!/usr/bin/env bash
# Fetch the CPU embedding model for the `embeddings` service (llama.cpp).
#
# bge-m3 is multilingual and 1024-dimensional -- the same shape the retired
# Ollama backend produced, so existing embedding caches stay valid.
set -euo pipefail

REPO="${EMBEDDING_GGUF_REPO:-gpustack/bge-m3-GGUF}"
FILE="${EMBEDDING_GGUF:-bge-m3-Q8_0.gguf}"
DEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/llama-embeddings/models"
DEST="$DEST_DIR/$FILE"

if [[ -s "$DEST" ]]; then
    echo "Already present: $DEST"
    exit 0
fi

mkdir -p "$DEST_DIR"
echo "Downloading $REPO/$FILE (~610 MB) ..."
curl -sSL --fail -o "$DEST.part" \
    "https://huggingface.co/$REPO/resolve/main/$FILE"
mv "$DEST.part" "$DEST"
echo "Saved to $DEST"
