#!/bin/bash
# Gets a Claude Code cloud session ready to run the tests: Qt's system libraries, RDT with its dev
# extras, and Qt's offscreen platform. Does nothing on a local PC.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# The same libraries the Linux CI job installs (.github/workflows/tests.yml).
QT_LIBS="libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3"
if ! ldconfig -p | grep -q 'libEGL\.so\.1'; then
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get install -y -qq $QT_LIBS >/dev/null 2>&1 \
    || { $SUDO apt-get update -qq >/dev/null 2>&1 && $SUDO apt-get install -y -qq $QT_LIBS >/dev/null; }
fi

python -m pip install -q -e ".[dev]" 2>&1 | grep -v "Running pip as the 'root' user" || true

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export QT_QPA_PLATFORM=offscreen" >> "$CLAUDE_ENV_FILE"
fi
