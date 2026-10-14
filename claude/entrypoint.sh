#!/bin/sh
set -eu

# A virtual X server, so GUI programs and tests run headless without xvfb-run.
# DISPLAY can be preset (-e DISPLAY=:N) to pick another display number.
export DISPLAY="${DISPLAY:-:99}"
SOCKET="/tmp/.X11-unix/X${DISPLAY#:}"

if [ ! -S "$SOCKET" ]; then
    Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
    tries=0
    while [ ! -S "$SOCKET" ] && [ "$tries" -lt 50 ]; do
        sleep 0.1
        tries=$((tries + 1))
    done
    [ -S "$SOCKET" ] || printf 'Xvfb did not come up on %s, see /tmp/xvfb.log\n' "$DISPLAY" >&2
fi

if [ -n "${CLAUDE_HOST_PORTS:-}" ]; then
    IFS=,
    for port in $CLAUDE_HOST_PORTS; do
        socat TCP4-LISTEN:"$port",bind=127.0.0.1,fork TCP:proxy:"$port" >>/tmp/socat.log 2>&1 &
        socat TCP6-LISTEN:"$port",bind=[::1],ipv6only,fork TCP:proxy:"$port" >>/tmp/socat.log 2>&1 &
    done
    unset IFS
fi

exec "$@"
