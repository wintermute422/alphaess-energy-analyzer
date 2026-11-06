# Deploying to a Linux machine with deploy.sh

`deploy.sh` installs the committed version of this repo on another Linux machine over SSH and
sets up the nightly systemd **user** timer. No root access is used.

## Target requirements

- SSH access with a key, Python 3.10+ with the `venv` module (Debian/Ubuntu:
  `sudo apt install python3-venv`), systemd user session, internet access for `pip`.
- For the timer to run while you are logged out: `sudo loginctl enable-linger <user>` (once).

## Configure

```bash
cp deploy.env.example deploy.env    # git-ignored; set DEPLOY_HOST, DEPLOY_USER, ...
cp .env.example .env                # your credentials; copied once to the target
bash deploy.sh --dry-run            # shows what would happen
bash deploy.sh
```

## Layout on the target

```
<DEPLOY_PATH>/
  releases/<UTC timestamp>/   one folder per deploy: code + its own .venv
  current -> releases/<...>   symlink used by the systemd service
  shared/                     .env (chmod 600), config.toml, energy.db, report.html
```

- `shared/.env` is copied only if it does not exist yet; it is never part of a release.
- Each deploy runs `energy-analyzer selftest` in the new release before switching `current`.
- The newest `DEPLOY_KEEP_RELEASES` (default 3) releases are kept, older ones are removed.
- With `DEPLOY_REFRESH_ONCALENDAR` (default in the example: every 15 minutes) today's data is
  refreshed by `energy-analyzer-refresh.timer`.
- With `DEPLOY_WEB_HOST` set, the dashboard runs as `energy-analyzer-web.service` on
  `DEPLOY_WEB_PORT`. If `shared/.env` has no `WEB_PASSWORD`, a random one is generated there
  (never printed). Read it on the target with `grep WEB_PASSWORD <DEPLOY_PATH>/shared/.env`;
  the login user is `WEB_USER` (default `energy`). If the machine runs a firewall, allow the
  port for your LAN.

## Roll back

```bash
bash rollback.sh            # switch current to the previous release and run its selftest
```

## Useful commands on the target

```bash
systemctl --user list-timers 'energy-analyzer*'
systemctl --user status energy-analyzer-web.service
systemctl --user start energy-analyzer.service
journalctl --user -u energy-analyzer.service -n 50
xdg-open <DEPLOY_PATH>/shared/report.html    # or copy it: scp <user>@<host>:<DEPLOY_PATH>/shared/report.html .
```
