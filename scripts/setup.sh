#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install -r workbench/requirements.lock
.venv/bin/python -m pip check
npm ci --prefix scripts/browser
npx --prefix scripts/browser playwright install chromium
.venv/bin/python scripts/doctor.py
