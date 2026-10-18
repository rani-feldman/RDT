#!/bin/bash
# Gets a Claude Code cloud session ready to run the tests: Qt's system libraries, RDT with its dev
# extras, and Qt's offscreen platform. Does nothing on a local PC.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# The same libraries the Linux CI job installs (.github/workflows/tests.yml). Optional: without
# them only the Qt tests fail, so a failed apt-get warns and carries on.
QT_LIBS="libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3"
if ! ldconfig -p | grep -q 'libEGL\.so\.1'; then
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get install -y -qq $QT_LIBS >/dev/null 2>&1 \
    || { $SUDO apt-get update -qq >/dev/null 2>&1 && $SUDO apt-get install -y -qq $QT_LIBS >/dev/null; } \
    || echo "session-start: could not install the Qt libraries ($QT_LIBS); the GUI tests will fail" >&2
fi

# Without RDT installed nothing runs, so a failed pip install shows its output and fails the hook
# (it used to be hidden behind "|| true").
if ! pip_out=$(python -m pip install -q -e ".[dev]" 2>&1); then
  echo "$pip_out" | grep -v "Running pip as the 'root' user" >&2 || true
  echo "session-start: pip install -e .[dev] failed" >&2
  exit 1
fi
if [ -n "$pip_out" ]; then
  echo "$pip_out" | grep -v "Running pip as the 'root' user" || true
fi

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export QT_QPA_PLATFORM=offscreen" >> "$CLAUDE_ENV_FILE"
fi
