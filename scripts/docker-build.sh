#!/usr/bin/env bash
# Build and run with all env vars from .env
# Usage: ./scripts/docker-build.sh [--build-only|--run-only]

set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${ENV_FILE:-.env}"
IMAGE_TAG="${IMAGE_TAG:-intelligent-bl-extractor:latest}"
BUILD_ONLY=false
RUN_ONLY=false

for arg in "$@"; do
  case "$arg" in
    --build-only) BUILD_ONLY=true ;;
    --run-only) RUN_ONLY=true ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy .env.example to .env first." >&2
  exit 1
fi

API_PORT=$(grep -E '^\s*API_PORT\s*=' "$ENV_FILE" | tail -1 | cut -d= -f2 | tr -d ' \r' || true)
API_PORT="${API_PORT:-8000}"

echo "Env file: $ENV_FILE | Port: $API_PORT"

if [[ "$RUN_ONLY" != true ]]; then
  echo "Building $IMAGE_TAG ..."
  docker build -t "$IMAGE_TAG" .
fi

if [[ "$BUILD_ONLY" != true ]]; then
  echo "Starting with docker compose ..."
  docker compose --env-file "$ENV_FILE" up -d --build
  echo "Docs:   http://localhost:${API_PORT}/docs"
  echo "Health: http://localhost:${API_PORT}/health"
fi
