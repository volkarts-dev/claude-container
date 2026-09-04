#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat >&2 <<'USAGE'
usage: start.sh [PATH...] [-- CLAUDE_ARG...]

Ensures the egress proxy is running, then starts claude in a container. The
current directory is always mounted and used as the working directory. Each
additional PATH is mounted read-write under /workspace, named after the last
component of that path.

Anything after -- is passed on to claude itself.

Networking
  The claude container runs on an internal container network with no route off
  the host. Its only way out is the tinyproxy container, which sits on that
  network as http://proxy:3128 and on a second, outward-facing network. That
  proxy goes out over the host connection, or forwards to the corporate proxy
  named by CLAUDE_PROXY, except for the hosts named by CLAUDE_NO_PROXY. A proxy
  container left over from an earlier start is reused, and recreated when its
  upstream, that list or either network differs from the one in the current
  environment.

  The proxy image is built on demand; the dev image has to be built beforehand
  with ./build.sh.

Environment
  CONTAINER_ENGINE container frontend, docker or podman (default docker)
  CLAUDE_PROXY     corporate proxy the egress forwards to, e.g.
                   http://proxy.corp:3128 or http://user:pass@host:port; falls
                   back to HTTPS_PROXY/HTTP_PROXY, unset means direct
  CLAUDE_NO_PROXY  comma-separated hosts, domains (.corp.example) and networks
                   (10.0.0.0/8) the proxy reaches directly instead of through
                   the upstream; falls back to NO_PROXY
  CLAUDE_NET       internal network name (default claude-internal)
  CLAUDE_BRIDGE    outward-facing network the proxy reaches the outside on,
                   created when missing (default claude-egress); it has to
                   carry DNS, which the engines' predefined bridge networks do
                   not
  CLAUDE_USERNS    --userns for the claude container; unset picks keep-id under
                   rootless podman and nothing otherwise
  CLAUDE_IMAGE     dev image name (default claude-dev)
  CLAUDE_TAG       dev image tag (default latest)
  CONTAINER_USER   user inside the dev image (default dev)
  PROXY_IMAGE      proxy image name (default claude-proxy)
  PROXY_TAG        proxy image tag (default latest)
  PROXY_CONTAINER  proxy container name (default claude-proxy)
  PROXY_PORT       host port to publish the proxy on; unset publishes nothing
  PROXY_BIND       host address for that port (default 127.0.0.1)
USAGE
}

ENGINE="${CONTAINER_ENGINE:-docker}"
case "$ENGINE" in
    docker) HOST_INTERNAL="host.docker.internal" ;;
    podman) HOST_INTERNAL="host.containers.internal" ;;
    *) printf 'start.sh: unknown container engine: %s\n' "$ENGINE" >&2; exit 1 ;;
esac

IMAGE="${CLAUDE_IMAGE:-claude-dev}"
TAG="${CLAUDE_TAG:-latest}"
CONTAINER_USER="${CONTAINER_USER:-dev}"
CONTAINER_HOME="/home/${CONTAINER_USER}"
NET_NAME="${CLAUDE_NET:-claude-internal}"
BRIDGE_NET="${CLAUDE_BRIDGE:-claude-egress}"
PROXY_IMAGE="${PROXY_IMAGE:-claude-proxy}"
PROXY_TAG="${PROXY_TAG:-latest}"
PROXY_NAME="${PROXY_CONTAINER:-claude-proxy}"
PROXY_PUBLISH="${PROXY_PORT:-}"
PROXY_BIND="${PROXY_BIND:-127.0.0.1}"
PROXY_LISTEN=3128
NO_UPSTREAM="${CLAUDE_NO_PROXY:-${NO_PROXY:-${no_proxy:-}}}"

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
        \[*\]:*) PROXY_HOST="${hostport%%]*}]"; PROXY_PORT_N="${hostport##*]:}" ;;
        \[*\]) PROXY_HOST="$hostport"; PROXY_PORT_N="" ;;
        *:*) PROXY_HOST="${hostport%:*}"; PROXY_PORT_N="${hostport##*:}" ;;
        *) PROXY_HOST="$hostport"; PROXY_PORT_N="" ;;
    esac
    if [ -z "$PROXY_PORT_N" ]; then
        case "$scheme" in
            https) PROXY_PORT_N=443 ;;
            http) PROXY_PORT_N=80 ;;
            *) PROXY_PORT_N=8080 ;;
        esac
        printf 'start.sh: no proxy port given, assuming %s\n' "$PROXY_PORT_N" >&2
    fi
    if [ "$scheme" = "https" ]; then
        printf 'start.sh: warning: tinyproxy talks to the upstream in cleartext, so an https:// proxy will not work\n' >&2
    fi
    [ -n "$PROXY_HOST" ]
}

resolve_upstream() {
    local url="${CLAUDE_PROXY:-${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}}"
    UPSTREAM=""
    HOST_GATEWAY=0
    [ -n "$url" ] || return 0
    if ! parse_proxy "$url"; then
        printf 'start.sh: cannot parse proxy %s\n' "$url" >&2
        exit 1
    fi
    case "$PROXY_HOST" in
        localhost|127.0.0.1|::1|\[::1\]|host.docker.internal|host.containers.internal)
            PROXY_HOST="$HOST_INTERNAL"
            HOST_GATEWAY=1
            ;;
    esac
    UPSTREAM="${PROXY_CREDS:+${PROXY_CREDS}@}${PROXY_HOST}:${PROXY_PORT_N}"
}

ensure_engine() {
    if ! command -v "$ENGINE" >/dev/null 2>&1; then
        printf 'start.sh: %s is not installed or not on PATH\n' "$ENGINE" >&2
        exit 1
    fi
    if ! "$ENGINE" info >/dev/null 2>&1; then
        printf 'start.sh: cannot talk to the %s engine; is it running?\n' "$ENGINE" >&2
        exit 1
    fi
}

ensure_network() {
    if "$ENGINE" network inspect "$NET_NAME" >/dev/null 2>&1; then
        if [ "$("$ENGINE" network inspect -f '{{.Internal}}' "$NET_NAME")" != "true" ]; then
            printf 'start.sh: network %s exists but is not internal; remove it or set CLAUDE_NET\n' "$NET_NAME" >&2
            exit 1
        fi
    else
        "$ENGINE" network create --internal "$NET_NAME" >/dev/null
        printf 'created internal network %s\n' "$NET_NAME" >&2
    fi
}

ensure_bridge_network() {
    if "$ENGINE" network inspect "$BRIDGE_NET" >/dev/null 2>&1; then
        if [ "$("$ENGINE" network inspect -f '{{.Internal}}' "$BRIDGE_NET")" = "true" ]; then
            printf 'start.sh: network %s is internal; the proxy cannot reach the outside through it\n' "$BRIDGE_NET" >&2
            exit 1
        fi
        if [ "$ENGINE" = "podman" ] && [ "$("$ENGINE" network inspect -f '{{.DNSEnabled}}' "$BRIDGE_NET" 2>/dev/null || true)" = "false" ]; then
            printf 'start.sh: warning: network %s serves no DNS, the proxy will not resolve host names\n' "$BRIDGE_NET" >&2
        fi
    else
        "$ENGINE" network create "$BRIDGE_NET" >/dev/null
        printf 'created outward network %s\n' "$BRIDGE_NET" >&2
    fi
}

wait_proxy() {
    local i
    for i in $(seq 20); do
        if "$ENGINE" exec "$PROXY_NAME" sh -c "http_proxy=http://127.0.0.1:${PROXY_LISTEN} wget -q -O /dev/null http://tinyproxy.stats/" >/dev/null 2>&1; then
            return
        fi
        sleep 0.25
    done
    printf 'start.sh: proxy %s is not answering on port %s; see %s logs %s\n' "$PROXY_NAME" "$PROXY_LISTEN" "$ENGINE" "$PROXY_NAME" >&2
    exit 1
}

ensure_proxy() {
    local args=() described current
    current="$("$ENGINE" inspect -f '{{index .Config.Labels "claude.upstream"}}|{{index .Config.Labels "claude.noupstream"}}|{{index .Config.Labels "claude.network"}}|{{index .Config.Labels "claude.bridge"}}' "$PROXY_NAME" 2>/dev/null || true)"
    if [ "$current" = "${UPSTREAM}|${NO_UPSTREAM}|${NET_NAME}|${BRIDGE_NET}" ]; then
        if [ "$("$ENGINE" inspect -f '{{.State.Running}}' "$PROXY_NAME")" != "true" ]; then
            "$ENGINE" start "$PROXY_NAME" >/dev/null
        fi
        wait_proxy
        return
    fi
    if [ -n "$current" ]; then
        printf 'proxy settings changed, recreating %s\n' "$PROXY_NAME" >&2
    fi
    if ! "$ENGINE" image inspect "${PROXY_IMAGE}:${PROXY_TAG}" >/dev/null 2>&1; then
        CONTAINER_ENGINE="$ENGINE" "${SCRIPT_DIR}/build.sh" proxy
    fi
    "$ENGINE" rm -f "$PROXY_NAME" >/dev/null 2>&1 || true

    args=(
        --name "$PROXY_NAME"
        --restart unless-stopped
        --network "$NET_NAME"
        --network-alias proxy
        --label "claude.upstream=${UPSTREAM}"
        --label "claude.noupstream=${NO_UPSTREAM}"
        --label "claude.network=${NET_NAME}"
        --label "claude.bridge=${BRIDGE_NET}"
        --cap-drop ALL
        --security-opt no-new-privileges
    )
    if [ -n "$UPSTREAM" ]; then
        args+=(-e "UPSTREAM_PROXY=${UPSTREAM}")
    fi
    if [ -n "$NO_UPSTREAM" ]; then
        args+=(-e "NO_UPSTREAM=${NO_UPSTREAM}")
    fi
    if [ "$HOST_GATEWAY" -eq 1 ]; then
        args+=(--add-host "${HOST_INTERNAL}:host-gateway")
    fi
    if [ -n "$PROXY_PUBLISH" ]; then
        args+=(-p "${PROXY_BIND}:${PROXY_PUBLISH}:${PROXY_LISTEN}")
    fi

    # Created, bridged, then started: tinyproxy must never come up on a container
    # that cannot yet resolve its upstream.
    "$ENGINE" create "${args[@]}" "${PROXY_IMAGE}:${PROXY_TAG}" >/dev/null
    "$ENGINE" network connect "$BRIDGE_NET" "$PROXY_NAME"
    "$ENGINE" start "$PROXY_NAME" >/dev/null
    wait_proxy
    described="${UPSTREAM:-direct}"
    printf 'proxy %s serving %s -> %s\n' "$PROXY_NAME" "$NET_NAME" "${described##*@}" >&2
}

ensure_engine

if ! "$ENGINE" image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1; then
    printf 'start.sh: image %s:%s is missing; run %s/build.sh\n' "$IMAGE" "$TAG" "$SCRIPT_DIR" >&2
    exit 1
fi

CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "${CONFIG_DIR}"
if [ ! -f "${CONFIG_DIR}/.claude.json" ] && [ -f "$HOME/.claude.json" ]; then
    cp "$HOME/.claude.json" "${CONFIG_DIR}/.claude.json"
fi

resolve_upstream
ensure_network
ensure_bridge_network
ensure_proxy

in_proxy="http://proxy:${PROXY_LISTEN}"
no_proxy_val="localhost,127.0.0.1,::1,proxy"

# Rootless podman maps the invoking user to root inside, which would leave the
# bind-mounted workspace owned by the wrong uid for the container user.
if [ -n "${CLAUDE_USERNS+x}" ]; then
    USERNS="${CLAUDE_USERNS}"
elif [ "$ENGINE" = "podman" ] && [ "$("$ENGINE" info -f '{{.Host.Security.Rootless}}' 2>/dev/null || true)" = "true" ]; then
    USERNS="keep-id"
else
    USERNS=""
fi

args=(
    --rm
    --network "${NET_NAME}"
    --cap-drop NET_ADMIN
    --cap-drop NET_RAW
    --security-opt no-new-privileges
    -v "${CONFIG_DIR}:${CONTAINER_HOME}/.claude"
    -e "CLAUDE_CONFIG_DIR=${CONTAINER_HOME}/.claude"
    -e "HTTP_PROXY=${in_proxy}"
    -e "HTTPS_PROXY=${in_proxy}"
    -e "http_proxy=${in_proxy}"
    -e "https_proxy=${in_proxy}"
    -e "NO_PROXY=${no_proxy_val}"
    -e "no_proxy=${no_proxy_val}"
)

if [ -n "$USERNS" ]; then
    args+=(--userns "$USERNS")
fi

pwd_abs="$(pwd -P)"
assign_name "$(basename -- "${pwd_abs}")"
workdir_name="${MOUNT_NAME}"
args+=(-v "${pwd_abs}:/workspace/${workdir_name}")
printf 'mount %s -> /workspace/%s (workdir)\n' "${pwd_abs}" "${workdir_name}" >&2

for p in ${paths[@]+"${paths[@]}"}; do
    if ! abs="$(resolve_path "$p")"; then
        printf 'start.sh: no such path: %s\n' "$p" >&2
        exit 1
    fi
    assign_name "$(basename -- "${abs}")"
    args+=(-v "${abs}:/workspace/${MOUNT_NAME}")
    printf 'mount %s -> /workspace/%s\n' "${abs}" "${MOUNT_NAME}" >&2
done

args+=(-w "/workspace/${workdir_name}")

# The terminal type and COLORTERM decide what the programs inside are willing to
# emit; without them TERM falls back to plain xterm and 24-bit color is dropped.
# The image carries ncurses-term, so exotic entries like xterm-kitty resolve.
if [ -t 0 ]; then
    args+=(-it)
    for var in TERM COLORTERM TERM_PROGRAM TERM_PROGRAM_VERSION; do
        if [ -n "${!var:-}" ]; then
            args+=(-e "${var}")
        fi
    done
fi

if [ -f "$HOME/.gitconfig" ]; then
    args+=(-v "$HOME/.gitconfig:${CONTAINER_HOME}/.gitconfig:ro")
fi

for var in ANTHROPIC_API_KEY ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX; do
    if [ -n "${!var:-}" ]; then
        args+=(-e "${var}")
    fi
done

exec "$ENGINE" run "${args[@]}" "${IMAGE}:${TAG}" claude ${claude_args[@]+"${claude_args[@]}"}
