#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "[1/4] create venv"
python3 -m venv .venv

echo "[2/4] install deps"
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "[3/4] ensure data dir"
mkdir -p data

echo "[4/4] done"
echo
echo "Start with:"
echo "./.venv/bin/python app.py"
