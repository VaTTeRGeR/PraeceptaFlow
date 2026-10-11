#!/bin/sh
# Builds dist/PraeceptaFlow/PraeceptaFlow (macOS: dist/PraeceptaFlow.app) with the
# virtual environment from setup.sh.
# Usage: ./build.sh [--onefile]
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python

if [ ! -x "$PY" ]; then
    echo "No virtual environment found. Run ./setup.sh first."
    exit 1
fi

MODE=--onedir
OUT=dist/PraeceptaFlow/PraeceptaFlow
if [ "$1" = "--onefile" ]; then
    MODE=--onefile
    OUT=dist/PraeceptaFlow
fi

if [ "$(uname)" = "Darwin" ]; then
    # PyInstaller deprecates onefile .app bundles and blocks them in 7.0.
    if [ "$MODE" = "--onefile" ]; then
        echo "--onefile is not supported on macOS, the build is an .app bundle."
        exit 1
    fi
    OUT=dist/PraeceptaFlow.app
fi

"$PY" -m PyInstaller --noconfirm --clean --log-level WARN --windowed "$MODE" --name PraeceptaFlow src/main.py
echo "Built $OUT"
