#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
NOON_INSTALL_RUNTIME=1 source scripts/runtime.sh
BOOTSTRAP_PYTHON="${NOON_BOOTSTRAP_PYTHON:-python3.12}"
"$BOOTSTRAP_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required; set NOON_BOOTSTRAP_PYTHON to your Python 3.12 executable"'
"$BOOTSTRAP_PYTHON" -m venv .venv
.venv/bin/python -c 'import sys; assert sys.version_info >= (3, 11), "Existing .venv uses an unsupported Python; recreate the development venv"' 
if [[ "$(uname -s)" == Linux ]] && [[ ! -f /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc ]]; then
  if [[ "$EUID" -eq 0 ]]; then
    apt-get update
    apt-get install -y fonts-noto-cjk fonts-dejavu-core
  else
    if command -v sudo >/dev/null && sudo -n true 2>/dev/null; then
      sudo -n apt-get update
      sudo -n apt-get install -y fonts-noto-cjk fonts-dejavu-core
    else
      echo 'Missing Noto CJK font. Ask the environment administrator to install fonts-noto-cjk; no sudo is available.' >&2
      exit 1
    fi
  fi
fi
.venv/bin/python -m pip install -r workbench/requirements.lock
.venv/bin/python -m pip check
npm ci --prefix scripts/browser
if [[ "$(uname -s)" == Linux ]] && { [[ "$EUID" -eq 0 ]] || { command -v sudo >/dev/null && sudo -n true 2>/dev/null; }; }; then
  npx --prefix scripts/browser playwright install --with-deps chromium
else
  # New Codex Cloud is an unprivileged Debian VM with system libraries supplied.
  # Do not invoke Playwright's sudo/apt installer there.
  npx --prefix scripts/browser playwright install chromium
fi
.venv/bin/python scripts/doctor.py
node -e 'require("./scripts/browser/node_modules/playwright").chromium.launch({headless:true}).then(b=>b.close()).catch(e=>{console.error("Chromium cannot start; check preinstalled system libraries:",e.message);process.exitCode=1})'
