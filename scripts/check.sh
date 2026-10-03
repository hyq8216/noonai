#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/runtime.sh
PYTHON="${NOON_PYTHON:-.venv/bin/python}"
"$PYTHON" scripts/doctor.py
"$PYTHON" -m unittest discover -s workbench/tests -v
"$PYTHON" scripts/smoke.py
for script in workbench/static/*.js; do node --check "$script"; done
node scripts/browser/smoke.cjs
