#!/usr/bin/env bash
# The single local gate: lint, format, types, tests, secret scan, selftest.
# `make check` calls this. Exit code is non-zero if any stage fails.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
# shellcheck source=scripts/pyenv.sh
. scripts/pyenv.sh

stage() { printf '\n== %s\n' "$1"; }

stage "ruff check"
"$PY" -m ruff check src tests
stage "ruff format --check"
"$PY" -m ruff format --check src tests
stage "mypy"
"$PY" -m mypy
stage "pytest"
"$PY" -m pytest
stage "secret scan (tracked files)"
bash scripts/secret_scan.sh --tree
stage "selftest"
# TEMPORARY until plan step 6 adds the CLI: remove this guard in step 6.
if [ -f src/energy_analyzer/cli.py ]; then
  "$PY" -m energy_analyzer selftest
else
  echo "SKIP selftest: CLI not implemented yet (plan step 6)"
fi

printf '\ncheck.sh: ALL STAGES PASSED\n'
