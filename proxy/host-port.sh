#!/bin/sh
set -eu

id="$1-$$"
printf '%s\n' "$id" > /tmp/host-ports/requests
exec socat - UNIX-LISTEN:"/tmp/host-ports/$id.sock",unlink-early,unlink-close,accept-timeout=30
