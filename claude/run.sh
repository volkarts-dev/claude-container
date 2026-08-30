#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat >&2 <<'USAGE'
usage: run.sh [PATH...] [-- CLAUDE_ARG...]

Starts claude in a container. The current directory is always mounted and used
as the working directory. Each additional PATH is mounted read-write under
/workspace, named after the last component of that path.

Anything after -- is passed on to claude itself.

Networking
  The container runs on an internal Docker network with no route off the host.
  Its only way out is a relay container that forwards to the external HTTP
  proxy named by CLAUDE_PROXY (falling back to HTTPS_PROXY/HTTP_PROXY from the
  environment). Without a proxy the container has no network access at all.

Environment
  CLAUDE_PROXY      upstream proxy, e.g. http://proxy.corp:3128 or user:pass@host:port
  CLAUDE_NO_PROXY   extra no_proxy entries for inside the container
  CLAUDE_NET        internal network name (default claude-egress)
  CLAUDE_RELAY      relay container name (default claude-egress-relay)
USAGE
}

IMAGE="${CLAUDE_IMAGE:-claude-dev}"
TAG="${CLAUDE_TAG:-latest}"
CONTAINER_USER="${CONTAINER_USER:-dev}"
CONTAINER_HOME="/home/${CONTAINER_USER}"
NET_NAME="${CLAUDE_NET:-claude-egress}"
RELAY_NAME="${CLAUDE_RELAY:-claude-egress-relay}"
RELAY_PORT=3128

paths=()
claude_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --) shift; claude_args=("$@"); break ;;
        -h|--help) usage; exit 0 ;;
        *) paths+=("$1"); shift ;;
    esac
done

resolve_path() {
    if command -v realpath >/dev/null 2>&1; then
        realpath -e -- "$1"
    elif [ -d "$1" ]; then
        (cd -- "$1" && pwd -P)
    elif [ -e "$1" ]; then
        printf '%s/%s\n' "$(cd -- "$(dirname -- "$1")" && pwd -P)" "$(basename -- "$1")"
    else
        return 1
    fi
}

used_names=()
# Sets MOUNT_NAME to an unused /workspace subdirectory name; must not run in a
# subshell, since it records the name it hands out.
assign_name() {
    local base="$1" candidate i=2
    [ "$base" = "/" ] && base="root"
    MOUNT_NAME="$base"
    while :; do
        for candidate in ${used_names[@]+"${used_names[@]}"}; do
            if [ "$candidate" = "$MOUNT_NAME" ]; then
                MOUNT_NAME="${base}-${i}"
                i=$((i + 1))
                continue 2
            fi
        done
        break
    done
    used_names+=("$MOUNT_NAME")
}

# Splits a proxy URL into PROXY_CREDS (may be empty), PROXY_HOST and PROXY_PORT.
parse_proxy() {
    local url="$1" scheme rest hostport
    scheme=""
    case "$url" in
        *://*) scheme="${url%%://*}"; rest="${url#*://}" ;;
        *) rest="$url" ;;
    esac
    rest="${rest%%/*}"
    PROXY_CREDS=""
    case "$rest" in
        *@*) PROXY_CREDS="${rest%@*}"; hostport="${rest##*@}" ;;
        *) hostport="$rest" ;;
    esac
    case "$hostport" in
        \[*\]:*) PROXY_HOST="${hostport%%]*}]"; PROXY_PORT="${hostport##*]:}" ;;
        \[*\]) PROXY_HOST="$hostport"; PROXY_PORT="" ;;
        *:*) PROXY_HOST="${hostport%:*}"; PROXY_PORT="${hostport##*:}" ;;
        *) PROXY_HOST="$hostport"; PROXY_PORT="" ;;
    esac
    if [ -z "$PROXY_PORT" ]; then
        case "$scheme" in
            https) PROXY_PORT=443 ;;
            http) PROXY_PORT=80 ;;
            *) PROXY_PORT=8080 ;;
        esac
        printf 'run.sh: no proxy port given, assuming %s\n' "$PROXY_PORT" >&2
    fi
    if [ "$scheme" = "https" ]; then
        printf 'run.sh: warning: the relay forwards raw TCP, so a TLS-terminating proxy will fail certificate validation\n' >&2
    fi
    [ -n "$PROXY_HOST" ]
}

# Creates the internal network if missing, and refuses a same-named one that
# would let traffic out.
ensure_network() {
    if docker network inspect "$NET_NAME" >/dev/null 2>&1; then
        if [ "$(docker network inspect -f '{{.Internal}}' "$NET_NAME")" != "true" ]; then
            printf 'run.sh: network %s exists but is not internal; remove it or set CLAUDE_NET\n' "$NET_NAME" >&2
            exit 1
        fi
    else
        docker network create --internal "$NET_NAME" >/dev/null
        printf 'created internal network %s\n' "$NET_NAME" >&2
    fi
}

# Starts (or recreates, when the upstream changed) the only container bridging
# the internal network to the outside world.
ensure_relay() {
    local upstream="$1" host="$2" port="$3" want running extra=()
    want="${host}:${port}"
    if [ "$(docker inspect -f '{{index .Config.Labels "claude.upstream"}}' "$RELAY_NAME" 2>/dev/null || true)" = "$want" ]; then
        running="$(docker inspect -f '{{.State.Running}}' "$RELAY_NAME")"
        if [ "$running" != "true" ]; then
            docker start "$RELAY_NAME" >/dev/null
        fi
        return
    fi
    docker rm -f "$RELAY_NAME" >/dev/null 2>&1 || true
    case "$host" in
        localhost|127.0.0.1|::1|\[::1\]|host.docker.internal)
            host="host.docker.internal"
            extra+=(--add-host "host.docker.internal:host-gateway")
            ;;
    esac
    # Created, bridged, then started: socat must never come up on a container
    # that cannot yet resolve the upstream.
    docker create \
        --name "$RELAY_NAME" \
        --restart unless-stopped \
        --network "$NET_NAME" \
        --network-alias proxy \
        --label "claude.upstream=${want}" \
        --cap-drop ALL \
        --security-opt no-new-privileges \
        ${extra[@]+"${extra[@]}"} \
        "${IMAGE}:${TAG}" \
        socat "TCP-LISTEN:${RELAY_PORT},fork,reuseaddr" "TCP:${host}:${port}" >/dev/null
    docker network connect bridge "$RELAY_NAME"
    docker start "$RELAY_NAME" >/dev/null
    printf 'relay %s -> %s\n' "$RELAY_NAME" "$upstream" >&2
}

# Everything Claude Code persists lives in one directory mount; CLAUDE_CONFIG_DIR
# keeps .claude.json inside it instead of at $HOME, where an atomic rewrite would
# break a single-file bind mount.
CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "${CONFIG_DIR}"
if [ ! -f "${CONFIG_DIR}/.claude.json" ] && [ -f "$HOME/.claude.json" ]; then
    cp "$HOME/.claude.json" "${CONFIG_DIR}/.claude.json"
fi

ensure_network

args=(
    --rm
    --network "${NET_NAME}"
    --cap-drop NET_ADMIN
    --cap-drop NET_RAW
    -v "${CONFIG_DIR}:${CONTAINER_HOME}/.claude"
    -e "CLAUDE_CONFIG_DIR=${CONTAINER_HOME}/.claude"
)

upstream="${CLAUDE_PROXY:-${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}}"
if [ -n "$upstream" ]; then
    if ! parse_proxy "$upstream"; then
        printf 'run.sh: cannot parse proxy %s\n' "$upstream" >&2
        exit 1
    fi
    ensure_relay "$upstream" "$PROXY_HOST" "$PROXY_PORT"
    in_proxy="http://${PROXY_CREDS:+${PROXY_CREDS}@}proxy:${RELAY_PORT}"
    no_proxy_val="localhost,127.0.0.1,::1,proxy${CLAUDE_NO_PROXY:+,${CLAUDE_NO_PROXY}}"
    args+=(
        -e "HTTP_PROXY=${in_proxy}"
        -e "HTTPS_PROXY=${in_proxy}"
        -e "http_proxy=${in_proxy}"
        -e "https_proxy=${in_proxy}"
        -e "NO_PROXY=${no_proxy_val}"
        -e "no_proxy=${no_proxy_val}"
    )
else
    printf 'run.sh: no CLAUDE_PROXY set; the container will have no network access\n' >&2
fi

pwd_abs="$(pwd -P)"
assign_name "$(basename -- "${pwd_abs}")"
workdir_name="${MOUNT_NAME}"
args+=(-v "${pwd_abs}:/workspace/${workdir_name}")
printf 'mount %s -> /workspace/%s (workdir)\n' "${pwd_abs}" "${workdir_name}" >&2

for p in ${paths[@]+"${paths[@]}"}; do
    if ! abs="$(resolve_path "$p")"; then
        printf 'run.sh: no such path: %s\n' "$p" >&2
        exit 1
    fi
    assign_name "$(basename -- "${abs}")"
    args+=(-v "${abs}:/workspace/${MOUNT_NAME}")
    printf 'mount %s -> /workspace/%s\n' "${abs}" "${MOUNT_NAME}" >&2
done

args+=(-w "/workspace/${workdir_name}")

if [ -t 0 ]; then
    args+=(-it)
fi

if [ -f "$HOME/.gitconfig" ]; then
    args+=(-v "$HOME/.gitconfig:${CONTAINER_HOME}/.gitconfig:ro")
fi

if [ -n "${SSH_AUTH_SOCK:-}" ] && [ -S "${SSH_AUTH_SOCK}" ]; then
    args+=(-v "${SSH_AUTH_SOCK}:/ssh-agent" -e "SSH_AUTH_SOCK=/ssh-agent")
fi

for var in ANTHROPIC_API_KEY ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX GH_TOKEN GITHUB_TOKEN; do
    if [ -n "${!var:-}" ]; then
        args+=(-e "${var}")
    fi
done

exec docker run "${args[@]}" "${IMAGE}:${TAG}" claude ${claude_args[@]+"${claude_args[@]}"}
