#!/usr/bin/env bash
# Deploy the committed HEAD of this repo to a Linux host over SSH (no sudo). See docs/deploy.md.
#
#   bash deploy.sh [--dry-run]
#
# Settings come from deploy.env (or $DEPLOY_ENV_FILE); copy deploy.env.example to start.
# Layout on the target: <DEPLOY_PATH>/releases/<timestamp>/, current -> releases/<timestamp>,
# shared/ (.env, config.toml, energy.db, report.html). Secrets are never part of a release.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

die() { echo "deploy: ERROR - $*" >&2; exit 1; }
say() { echo "deploy: $*"; }

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

CONF="${DEPLOY_ENV_FILE:-deploy.env}"
[ -f "$CONF" ] || die "missing $CONF - copy deploy.env.example to deploy.env and fill it in"
conf() {  # read KEY=VALUE without executing the file
  local v
  v="$(grep -E "^[[:space:]]*$1=" "$CONF" | tail -n1 | cut -d= -f2- | tr -d '\r' || true)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "${v:-${2:-}}"
}
HOST="$(conf DEPLOY_HOST)"
USER_="$(conf DEPLOY_USER)"
KEY="$(conf DEPLOY_SSH_KEY)"
APP_PATH="$(conf DEPLOY_PATH '~/alphaess-energy-analyzer')"
KEEP="$(conf DEPLOY_KEEP_RELEASES 3)"
ONCALENDAR="$(conf DEPLOY_ONCALENDAR '*-*-* 03:30:00')"
WEB_HOST="$(conf DEPLOY_WEB_HOST)"
WEB_PORT="$(conf DEPLOY_WEB_PORT 8088)"
REFRESH="$(conf DEPLOY_REFRESH_ONCALENDAR)"
case "$WEB_PORT" in ''|*[!0-9]*) die "DEPLOY_WEB_PORT must be a number" ;; esac
case "$WEB_HOST" in *[!0-9A-Za-z.:-]*) die "DEPLOY_WEB_HOST must be an address" ;; esac
[ -n "$HOST" ] && [ -n "$USER_" ] || die "DEPLOY_HOST and DEPLOY_USER must be set in $CONF"
case "$KEEP" in ''|*[!0-9]*) die "DEPLOY_KEEP_RELEASES must be a number" ;; esac
[ "$KEEP" -ge 2 ] || die "DEPLOY_KEEP_RELEASES must be >= 2 (rollback needs a previous release)"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10)
if [ -n "$KEY" ]; then
  KEY="${KEY/#\~/$HOME}"
  [ -f "$KEY" ] || die "DEPLOY_SSH_KEY not found"
  SSH+=(-i "$KEY")
fi
DEST="$USER_@$HOST"

COMMIT="$(git rev-parse --short HEAD)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  say "warning: uncommitted changes are NOT deployed (deploying HEAD $COMMIT)"
fi

# Runs a bash script (stdin) on the target with safely quoted positional arguments.
remote_script() {
  local args=() a
  for a in "$@"; do args+=("$(printf '%q' "$a")"); done
  "${SSH[@]}" "$DEST" "bash -s -- ${args[*]}"
}

# Shared prologue for remote scripts: resolve ~ in the install path on the target.
read -r -d '' RESOLVE <<'EOF' || true
set -euo pipefail
APP="$1"
case "$APP" in "~") APP="$HOME" ;; "~/"*) APP="$HOME/${APP#\~/}" ;; esac
EOF

read -r -d '' PREFLIGHT <<'EOF' || true
python3 -c 'import sys; assert sys.version_info >= (3, 10), sys.version' \
  || { echo "python3 >= 3.10 is required on the target" >&2; exit 1; }
python3 -c 'import venv, ensurepip' 2>/dev/null \
  || { echo "python3 venv/ensurepip missing: ask an admin for 'apt install python3-venv'" >&2; exit 1; }
systemctl --user show-environment >/dev/null \
  || { echo "systemd user session not available" >&2; exit 1; }
mkdir -p "$APP/releases" "$APP/shared"
chmod 700 "$APP/shared"
echo "$APP"
EOF

read -r -d '' INSTALL <<'EOF' || true
TS="$2"; COMMIT="$3"; KEEP="$4"; ONCALENDAR="$5"; WEB_HOST="$6"; WEB_PORT="$7"; REFRESH="$8"
REL="$APP/releases/$TS"
cd "$REL"
echo "$COMMIT" > REVISION
python3 -m venv .venv
.venv/bin/python -m pip install --quiet --disable-pip-version-check .
echo "installed: $(.venv/bin/energy-analyzer --version)"
.venv/bin/energy-analyzer selftest > selftest.log 2>&1 \
  || { cat selftest.log >&2; echo "selftest FAILED - current release left unchanged" >&2; exit 1; }
tail -n1 selftest.log
if [ ! -f "$APP/shared/config.toml" ]; then
  (cd "$APP/shared" && "$REL/.venv/bin/energy-analyzer" init --no-check)
fi
ln -sfn "releases/$TS" "$APP/current.new"
mv -Tf "$APP/current.new" "$APP/current"
echo "current -> $(readlink "$APP/current")"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
sed -e "s|@APP_DIR@|$APP|g" "$REL/systemd/energy-analyzer.service" > "$UNIT_DIR/energy-analyzer.service"
sed -e "s|@ONCALENDAR@|$ONCALENDAR|g" "$REL/systemd/energy-analyzer.timer" > "$UNIT_DIR/energy-analyzer.timer"
render() {  # render a unit template into the user unit folder
  sed -e "s|@APP_DIR@|$APP|g" -e "s|@ONCALENDAR@|$ONCALENDAR|g" \
      -e "s|@WEB_HOST@|$WEB_HOST|g" -e "s|@WEB_PORT@|$WEB_PORT|g" \
      -e "s|@REFRESH_ONCALENDAR@|$REFRESH|g" "$REL/systemd/$1" > "$UNIT_DIR/$1"
}
if [ -n "$REFRESH" ]; then
  render energy-analyzer-refresh.service; render energy-analyzer-refresh.timer
fi
if [ -n "$WEB_HOST" ]; then
  render energy-analyzer-web.service
  ENVF="$APP/shared/.env"
  touch "$ENVF"; chmod 600 "$ENVF"
  grep -q '^WEB_USER=' "$ENVF" || echo "WEB_USER=energy" >> "$ENVF"
  if ! grep -q '^WEB_PASSWORD=.' "$ENVF"; then
    # Generated on the target and never printed: read it there with `grep WEB_PASSWORD`.
    echo "WEB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')" >> "$ENVF"
    echo "dashboard password generated in shared/.env"
  fi
fi
systemctl --user daemon-reload
systemctl --user enable --now energy-analyzer.timer >/dev/null 2>&1
systemctl --user is-active energy-analyzer.timer
if [ -n "$REFRESH" ]; then
  systemctl --user enable --now energy-analyzer-refresh.timer >/dev/null 2>&1
  echo "refresh timer: $(systemctl --user is-active energy-analyzer-refresh.timer)"
else
  systemctl --user disable --now energy-analyzer-refresh.timer >/dev/null 2>&1 || true
fi
if [ -n "$WEB_HOST" ]; then
  systemctl --user enable energy-analyzer-web.service >/dev/null 2>&1
  systemctl --user restart energy-analyzer-web.service
  code=000
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    code="$(python3 -c "import urllib.request as u, urllib.error as e
try: print(u.urlopen('http://127.0.0.1:$WEB_PORT/api/meta', timeout=3).status)
except e.HTTPError as x: print(x.code)
except Exception: print('000')")"
    [ "$code" != 000 ] && break
    sleep 1
  done
  echo "dashboard on port $WEB_PORT: HTTP $code without login (401 = protected, OK)"
  [ "$code" = 401 ] || [ "$code" = 200 -a "$WEB_HOST" = 127.0.0.1 ] \
    || { journalctl --user -u energy-analyzer-web.service -n 20 --no-pager >&2; exit 1; }
else
  systemctl --user disable --now energy-analyzer-web.service >/dev/null 2>&1 || true
fi
current="$(readlink "$APP/current")"
ls -1 "$APP/releases" | sort | head -n "-$KEEP" | while read -r old; do
  [ "releases/$old" = "$current" ] && continue
  rm -rf -- "${APP:?}/releases/$old" && echo "pruned old release $old"
done
EOF

read -r -d '' COPY_ENV <<'EOF' || true
if [ -f "$APP/shared/.env" ]; then
  cat > /dev/null; echo "shared/.env exists - left unchanged"
else
  umask 077; cat > "$APP/shared/.env.tmp"; mv "$APP/shared/.env.tmp" "$APP/shared/.env"
  echo "shared/.env installed"
fi
chmod 600 "$APP/shared/.env"; stat -c 'shared/.env mode %a' "$APP/shared/.env"
EOF

if [ "$DRY_RUN" = 1 ]; then
  say "dry run - nothing is changed"
  say "would deploy HEAD $COMMIT to $DEST:$APP_PATH/releases/$TS"
  say "would install venv, run selftest, switch current, install energy-analyzer.timer ($ONCALENDAR)"
  say "would keep the newest $KEEP releases; would copy .env to shared/.env only if missing"
  [ -n "$REFRESH" ] && say "would refresh today's data on: $REFRESH"
  [ -n "$WEB_HOST" ] && say "would run the dashboard on $WEB_HOST:$WEB_PORT (login required)"
  exit 0
fi

say "preflight on $DEST"
APP="$(printf '%s\n%s\n' "$RESOLVE" "$PREFLIGHT" | remote_script "$APP_PATH" | tail -n1)"
[ -n "$APP" ] || die "preflight failed"
REL="$APP/releases/$TS"

if [ -f .env ]; then
  say "runtime secrets"
  # The .env content travels over stdin; the script on the command line holds no secrets.
  "${SSH[@]}" "$DEST" \
    "bash -c $(printf '%q' "$RESOLVE"$'\n'"$COPY_ENV") _ $(printf '%q' "$APP_PATH")" < .env
else
  say "no local .env - create $APP_PATH/shared/.env on the target yourself (chmod 600)"
fi

say "transferring HEAD $COMMIT to releases/$TS"
if command -v rsync >/dev/null 2>&1 && "${SSH[@]}" "$DEST" "command -v rsync" >/dev/null; then
  STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
  git archive --format=tar HEAD | tar -x -C "$STAGE"
  rsync -a -e "${SSH[*]}" "$STAGE/" "$DEST:$(printf '%q' "$REL")/"
else
  # No local rsync (e.g. Git Bash on Windows): stream the same committed tree with tar.
  git archive --format=tar HEAD | "${SSH[@]}" "$DEST" "mkdir -p $(printf '%q' "$REL") && tar -x -C $(printf '%q' "$REL")"
fi

say "installing, selftest, switching current, systemd units"
printf '%s\n%s\n' "$RESOLVE" "$INSTALL" \
  | remote_script "$APP_PATH" "$TS" "$COMMIT" "$KEEP" "$ONCALENDAR" "$WEB_HOST" "$WEB_PORT" \
    "$REFRESH" \
  || die "install failed (current release unchanged)"

say "done: $DEST:$APP/current -> releases/$TS ($COMMIT)"
