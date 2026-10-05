#!/usr/bin/env bash
# Source from repository scripts: setup exports do not survive a cloud task.
NOON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export npm_config_cache="$NOON_ROOT/.cloud-runtime/cache/npm"
export PIP_CACHE_DIR="$NOON_ROOT/.cloud-runtime/cache/pip"
NOON_PREPARED_BROWSERS="$(dirname "$NOON_ROOT")/.noonai-assets/playwright"
if [[ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ]]; then
  if [[ -d "$NOON_PREPARED_BROWSERS" ]]; then
    export PLAYWRIGHT_BROWSERS_PATH="$NOON_PREPARED_BROWSERS"
  else
    export PLAYWRIGHT_BROWSERS_PATH="$NOON_ROOT/.cloud-runtime/cache/playwright"
  fi
fi
mkdir -p "$npm_config_cache" "$PIP_CACHE_DIR" "$PLAYWRIGHT_BROWSERS_PATH"
NOON_NODE_BIN="$NOON_ROOT/.cloud-runtime/tools/node_modules/.bin"
if [[ -x "$NOON_NODE_BIN/node" ]]; then
  export PATH="$NOON_NODE_BIN:$PATH"
fi
if ! command -v node >/dev/null || [[ "$(node -p 'process.versions.node.split(".")[0]')" != 22 ]]; then
  if [[ "${NOON_INSTALL_RUNTIME:-0}" != 1 ]] || ! command -v npm >/dev/null; then
    echo 'Node.js 22 is required. Run bash scripts/setup.sh with npm available.' >&2
    return 1
  fi
  npm install --prefix "$NOON_ROOT/.cloud-runtime/tools" --no-audit --no-fund node@22.23.3
  export PATH="$NOON_NODE_BIN:$PATH"
fi
