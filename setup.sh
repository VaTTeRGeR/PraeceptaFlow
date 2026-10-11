#!/bin/sh
# Creates the virtual environment if missing and installs the build dependencies.
# Usage: ./setup.sh
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python

[ -x "$PY" ] || python3 -m venv .venv

"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements-build.txt
echo "Set up .venv"
