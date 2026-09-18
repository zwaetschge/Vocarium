#!/usr/bin/env bash
# Runs the vocarium-api test-suite inside the API image. The host has no
# pytest and `docker exec` may be blocked by a socket proxy; `docker run`
# with the repo bind-mounted works everywhere the stack itself works.
# The whole repo is mounted because several tests read docker-compose.yml,
# the UI sources and the worker servers next to the API.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${VOCARIUM_API_IMAGE:-vocarium-vocarium-api:latest}"
docker run --rm -t \
  -v "$PWD:/repo:ro" \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPYCACHEPREFIX=/tmp/pycache -e HOME=/tmp \
  -w /repo/vocarium-api "$IMAGE" \
  sh -c 'pip install --quiet --no-cache-dir --user pytest >/dev/null && python -m pytest -q -p no:cacheprovider "$@"' -- "$@"
