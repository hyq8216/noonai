#!/usr/bin/env bash
set -euo pipefail
# Refresh dependencies after checkout of a different branch; exports are not required.
bash "$(dirname "$0")/setup.sh"
