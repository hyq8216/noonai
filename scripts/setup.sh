#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
BOOTSTRAP_PYTHON="${NOON_BOOTSTRAP_PYTHON:-python3}"
"$BOOTSTRAP_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required; set NOON_BOOTSTRAP_PYTHON to your Python 3.12 executable"'
"$BOOTSTRAP_PYTHON" -m venv .venv
.venv/bin/python -c 'import sys; assert sys.version_info >= (3, 11), "Existing .venv uses an unsupported Python; recreate the development venv"' 
if [[ "$(uname -s)" == Linux ]] && [[ ! -f /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc ]]; then
  if [[ "$EUID" -eq 0 ]]; then
    apt-get update
    apt-get install -y fonts-noto-cjk fonts-dejavu-core
  else
    sudo apt-get update
    sudo apt-get install -y fonts-noto-cjk fonts-dejavu-core
  fi
fi
.venv/bin/python -m pip install -r workbench/requirements.lock
.venv/bin/python -m pip check
npm ci --prefix scripts/browser
if [[ "$(uname -s)" == Linux ]]; then
  npx --prefix scripts/browser playwright install --with-deps chromium
else
  npx --prefix scripts/browser playwright install chromium
fi
.venv/bin/python scripts/doctor.py
