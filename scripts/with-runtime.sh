#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/runtime.sh"
cd "$NOON_ROOT"
exec "$@"
