#!/bin/sh
set -eu

CONF=/tmp/tinyproxy.conf
cp /etc/tinyproxy/tinyproxy.conf "$CONF"

if [ -n "${NO_UPSTREAM:-}" ]; then
    IFS=,
    for entry in $NO_UPSTREAM; do
        entry="${entry#"${entry%%[! ]*}"}"
        entry="${entry%"${entry##*[! ]}"}"
        [ -n "$entry" ] || continue
        case "$entry" in
            */*) printf 'Upstream none "%s"\n' "$entry" >> "$CONF" ;;
            .*) printf 'Upstream none "%s"\nUpstream none "%s"\n' "$entry" "${entry#.}" >> "$CONF" ;;
            *) printf 'Upstream none "%s"\nUpstream none ".%s"\n' "$entry" "$entry" >> "$CONF" ;;
        esac
    done
    unset IFS
    printf 'reaching %s without the upstream\n' "$NO_UPSTREAM" >&2
fi

if [ -n "${UPSTREAM_PROXY:-}" ]; then
    printf 'Upstream http %s\n' "$UPSTREAM_PROXY" >> "$CONF"
    printf 'forwarding to upstream proxy %s\n' "${UPSTREAM_PROXY##*@}" >&2
else
    printf 'no upstream proxy set, going out directly\n' >&2
fi

if [ -n "${HOST_PORTS:-}" ]; then
    if ! command -v socat >/dev/null 2>&1; then
        printf 'socat is missing, rebuild the proxy image; host ports %s are not forwarded\n' "$HOST_PORTS" >&2
    else
        IFS=,
        for port in $HOST_PORTS; do
            socat TCP-LISTEN:"$port",fork TCP:"$HOST_TARGET":"$port" &
        done
        unset IFS
        printf 'forwarding ports %s to %s\n' "$HOST_PORTS" "$HOST_TARGET" >&2
    fi
fi

exec /usr/bin/tinyproxy -d -c "$CONF"
