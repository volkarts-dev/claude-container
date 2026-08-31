#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat >&2 <<'USAGE'
usage: build.sh [TARGET...] [-- DOCKER_BUILD_ARG...]

Builds the container images. TARGET is claude or proxy; without one both are
built.

Anything after -- is passed on to docker build.

Environment
  CLAUDE_IMAGE     dev image name (default claude-dev)
  CLAUDE_TAG       dev image tag (default latest)
  CONTAINER_USER   user inside the dev image (default dev)
  NODE_MAJOR       Node.js major version (default 22)
  DOTNET_CHANNEL   .NET channel (default 10.0)
  PROXY_IMAGE      proxy image name (default claude-proxy)
  PROXY_TAG        proxy image tag (default latest)
USAGE
}

CLAUDE_IMAGE="${CLAUDE_IMAGE:-claude-dev}"
CLAUDE_TAG="${CLAUDE_TAG:-latest}"
CONTAINER_USER="${CONTAINER_USER:-dev}"
NODE_MAJOR="${NODE_MAJOR:-22}"
DOTNET_CHANNEL="${DOTNET_CHANNEL:-10.0}"
PROXY_IMAGE="${PROXY_IMAGE:-claude-proxy}"
PROXY_TAG="${PROXY_TAG:-latest}"

targets=()
build_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --) shift; build_args=("$@"); break ;;
        -h|--help) usage; exit 0 ;;
        claude|proxy) targets+=("$1"); shift ;;
        *) printf 'build.sh: unknown target: %s\n' "$1" >&2; usage; exit 1 ;;
    esac
done
[ ${#targets[@]} -eq 0 ] && targets=(claude proxy)

build_claude() {
    docker build \
        --build-arg "USER_UID=$(id -u)" \
        --build-arg "USER_GID=$(id -g)" \
        --build-arg "USERNAME=${CONTAINER_USER}" \
        --build-arg "NODE_MAJOR=${NODE_MAJOR}" \
        --build-arg "DOTNET_CHANNEL=${DOTNET_CHANNEL}" \
        -t "${CLAUDE_IMAGE}:${CLAUDE_TAG}" \
        ${build_args[@]+"${build_args[@]}"} \
        "${SCRIPT_DIR}/claude"
    printf 'built %s:%s\n' "${CLAUDE_IMAGE}" "${CLAUDE_TAG}" >&2
}

build_proxy() {
    docker build \
        -t "${PROXY_IMAGE}:${PROXY_TAG}" \
        ${build_args[@]+"${build_args[@]}"} \
        "${SCRIPT_DIR}/proxy"
    printf 'built %s:%s\n' "${PROXY_IMAGE}" "${PROXY_TAG}" >&2
}

for target in "${targets[@]}"; do
    "build_${target}"
done
