#!/bin/sh
dir="$(dirname "$0")"
exec "$dir/.venv/bin/python" "$dir/src/main.py" "$@"
