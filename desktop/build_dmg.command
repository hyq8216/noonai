#!/bin/bash
set -euo pipefail
TASK_DESKTOP_DIR="$(cd "$(dirname "$0")" && pwd)"
TASK_REPO_DIR="$(dirname "$TASK_DESKTOP_DIR")"
cd "$TASK_REPO_DIR"
if [[ "$(uname -s)" != Darwin ]]; then
  echo '需要在Mac上运行：当前系统不能构建macOS程序或DMG。' >&2
  exit 1
fi
if (( $# > 1 )) || { (( $# == 1 )) && [[ "$1" != --overwrite ]]; }; then
  echo '用法：build_dmg.command [--overwrite]' >&2
  exit 1
fi
TASK_BOOTSTRAP_PYTHON="${NOON_BUILD_PYTHON:-python3.12}"
if ! command -v "$TASK_BOOTSTRAP_PYTHON" >/dev/null; then
  echo '请先安装Python 3.12及Xcode命令行工具，再运行此脚本。' >&2
  exit 1
fi
"$TASK_BOOTSTRAP_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3,12), "需要Python 3.12"'
xcrun --find swiftc >/dev/null
if [[ ! -x desktop/.venv/bin/python ]]; then
  "$TASK_BOOTSTRAP_PYTHON" -m venv desktop/.venv
fi
desktop/.venv/bin/python -m pip install -r desktop/requirements-build.txt
TASK_BUILD_ARCH="$(desktop/.venv/bin/python -c 'import platform; print(platform.machine())')"
TASK_BUILD_VERSION="$(desktop/.venv/bin/python -c 'import json; print(json.load(open("desktop/release.json"))["version"])')"
TASK_BUILD_OUTPUT="$TASK_DESKTOP_DIR/dist/$TASK_BUILD_VERSION/$TASK_BUILD_ARCH"
desktop/.venv/bin/python desktop/build.py --arch "$TASK_BUILD_ARCH" --dist "$TASK_BUILD_OUTPUT" "$@"
desktop/.venv/bin/python desktop/package_dmg.py --app "$TASK_BUILD_OUTPUT/Noon Studio.app" --output-dir "$TASK_BUILD_OUTPUT" --version "$TASK_BUILD_VERSION" --arch "$TASK_BUILD_ARCH" --zip "$@"
open "$TASK_BUILD_OUTPUT"
