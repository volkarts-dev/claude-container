#!/usr/bin/env sh
SCRIPT="$(readlink -f "$0")"
exec python3 "$(dirname -- "$SCRIPT")/start.py" "$@"
