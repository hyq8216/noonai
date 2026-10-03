#!/bin/zsh
cd "${0:A:h}"
if [[ -x .venv/bin/python3 ]]; then
  exec .venv/bin/python3 server.py
elif [[ -x /Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 ]]; then
  exec /Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 server.py
else
  exec python3 server.py
fi
