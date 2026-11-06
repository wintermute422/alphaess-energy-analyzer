#!/usr/bin/env bash
# Sourced by other scripts: sets $PY to the project venv's python (or the PATH python in CI).
ROOT="${ROOT:-$(git rev-parse --show-toplevel)}"
if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
elif [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
  PY="$ROOT/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  PY="python"
fi
export PY
