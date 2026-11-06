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
"$PY" -m energy_analyzer selftest

printf '\ncheck.sh: ALL STAGES PASSED\n'
