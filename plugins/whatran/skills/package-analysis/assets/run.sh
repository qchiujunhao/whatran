#!/usr/bin/env bash
# Python orchestrator template. Adapt the commands for another runtime.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p results

BASE_PYTHON="${PYTHON:-python3}"
rm -rf .venv
"$BASE_PYTHON" -m venv .venv
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt

.venv/bin/python steps/01_STEP.py
# .venv/bin/python steps/02_STEP.py
# .venv/bin/python steps/03_STEP.py
