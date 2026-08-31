#!/bin/sh
set -eu

# The config is generated at start so the upstream can change without a rebuild;
# /etc is read-only for nobody, so the copy lives in /tmp.
CONF=/tmp/tinyproxy.conf
cp /etc/tinyproxy/tinyproxy.conf "$CONF"

if [ -n "${UPSTREAM_PROXY:-}" ]; then
    printf 'Upstream http %s\n' "$UPSTREAM_PROXY" >> "$CONF"
    printf 'forwarding to upstream proxy %s\n' "${UPSTREAM_PROXY##*@}" >&2
else
    printf 'no upstream proxy set, going out directly\n' >&2
fi

exec /usr/bin/tinyproxy -d -c "$CONF"
