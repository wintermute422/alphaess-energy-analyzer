#!/usr/bin/env bash
# Run gitleaks: native binary if on PATH, otherwise the pinned Docker image.
# Usage: scripts/gitleaks.sh history | staged | range A..B | dir PATH
set -euo pipefail
GITLEAKS_IMAGE="${GITLEAKS_IMAGE:-ghcr.io/gitleaks/gitleaks:v8.30.1}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

mode="${1:-history}"; shift || true
case "$mode" in
  history) args=(git --redact --no-banner -c /repo/.gitleaks.toml /repo) ;;
  staged)  args=(git --pre-commit --staged --redact --no-banner -c /repo/.gitleaks.toml /repo) ;;
  range)   args=(git --redact --no-banner -c /repo/.gitleaks.toml "--log-opts=$1" /repo) ;;
  dir)     args=(dir --redact --no-banner -c /repo/.gitleaks.toml "/repo/$1") ;;
  *) echo "unknown mode: $mode" >&2; exit 64 ;;
esac

if command -v gitleaks >/dev/null 2>&1; then
  # Native: replace the /repo prefix with the real root.
  native=()
  for a in "${args[@]}"; do native+=("${a//\/repo/$ROOT}"); done
  exec gitleaks "${native[@]}"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "gitleaks.sh: neither gitleaks nor docker found. Install one (see CONTRIBUTING.md)." >&2
  exit 2
fi

host_root="$ROOT"
if pwd -W >/dev/null 2>&1; then host_root="$(pwd -W)"; fi   # Git Bash on Windows
# safe.directory: the container user differs from the mounted repo's owner.
MSYS_NO_PATHCONV=1 exec docker run --rm \
  -v "$host_root:/repo" \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  "$GITLEAKS_IMAGE" "${args[@]}"
