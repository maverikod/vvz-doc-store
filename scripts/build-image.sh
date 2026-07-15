#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-doc-store:local}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
CONTEXT_DIR="${CONTEXT_DIR:-.}"

docker build -f "$DOCKERFILE" -t "$IMAGE_NAME" "$CONTEXT_DIR"
