#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

IMAGE="${CLAUDE_IMAGE:-claude-dev}"
TAG="${CLAUDE_TAG:-latest}"
CONTAINER_USER="${CONTAINER_USER:-dev}"
NODE_MAJOR="${NODE_MAJOR:-22}"
DOTNET_CHANNEL="${DOTNET_CHANNEL:-10.0}"

docker build \
    --build-arg "USER_UID=$(id -u)" \
    --build-arg "USER_GID=$(id -g)" \
    --build-arg "USERNAME=${CONTAINER_USER}" \
    --build-arg "NODE_MAJOR=${NODE_MAJOR}" \
    --build-arg "DOTNET_CHANNEL=${DOTNET_CHANNEL}" \
    -t "${IMAGE}:${TAG}" \
    "$@" \
    "${SCRIPT_DIR}"

echo "built ${IMAGE}:${TAG}"
