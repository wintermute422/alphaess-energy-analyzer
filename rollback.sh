#!/usr/bin/env bash
# Switch the target's `current` symlink back to the previous release and run its selftest.
#
#   bash rollback.sh [--to <release timestamp>] [--dry-run]
#
# Reads the same deploy.env (or $DEPLOY_ENV_FILE) as deploy.sh. The systemd units point at
# <DEPLOY_PATH>/current, so they need no change. shared/ (.env, database) is untouched.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

die() { echo "rollback: ERROR - $*" >&2; exit 1; }

TARGET=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --to) TARGET="${2:-}"; [ -n "$TARGET" ] || die "--to needs a release name"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
case "$TARGET" in *[!0-9A-Za-z]*) die "invalid release name" ;; esac

CONF="${DEPLOY_ENV_FILE:-deploy.env}"
[ -f "$CONF" ] || die "missing $CONF - copy deploy.env.example to deploy.env"
conf() {
  local v
  v="$(grep -E "^[[:space:]]*$1=" "$CONF" | tail -n1 | cut -d= -f2- | tr -d '\r' || true)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "${v:-${2:-}}"
}
HOST="$(conf DEPLOY_HOST)"
USER_="$(conf DEPLOY_USER)"
KEY="$(conf DEPLOY_SSH_KEY)"
APP_PATH="$(conf DEPLOY_PATH '~/alphaess-energy-analyzer')"
[ -n "$HOST" ] && [ -n "$USER_" ] || die "DEPLOY_HOST and DEPLOY_USER must be set in $CONF"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10)
if [ -n "$KEY" ]; then KEY="${KEY/#\~/$HOME}"; SSH+=(-i "$KEY"); fi

read -r -d '' SCRIPT <<'EOF' || true
set -euo pipefail
APP="$1"; TARGET="$2"; DRY_RUN="$3"
case "$APP" in "~") APP="$HOME" ;; "~/"*) APP="$HOME/${APP#\~/}" ;; esac
[ -L "$APP/current" ] || { echo "no current release in $APP" >&2; exit 1; }
cur="$(basename "$(readlink "$APP/current")")"
if [ -z "$TARGET" ]; then
  TARGET="$(ls -1 "$APP/releases" | sort | awk -v c="$cur" '$0 < c' | tail -n1)"
  [ -n "$TARGET" ] || { echo "no release older than $cur to roll back to" >&2; exit 1; }
fi
[ -x "$APP/releases/$TARGET/.venv/bin/energy-analyzer" ] \
  || { echo "release $TARGET is missing or incomplete" >&2; exit 1; }
echo "current: $cur"
echo "target:  $TARGET"
[ "$DRY_RUN" = 1 ] && { echo "dry run - nothing changed"; exit 0; }
ln -sfn "releases/$TARGET" "$APP/current.new"
mv -Tf "$APP/current.new" "$APP/current"
echo "current -> $(readlink "$APP/current")"
"$APP/current/.venv/bin/energy-analyzer" selftest | tail -n1
EOF

"${SSH[@]}" "$USER_@$HOST" \
  "bash -c $(printf '%q' "$SCRIPT") _ $(printf '%q' "$APP_PATH") $(printf '%q' "$TARGET") $DRY_RUN"
