# Scheduling the nightly run

The job to schedule is:

```bash
energy-analyzer --config /path/to/config.toml daily --report /path/to/report.html
```

It fetches yesterday and today (safe to repeat) and regenerates the report. Pick a time after
midnight, e.g. 03:30, so yesterday is complete. Replace `/path/to/...` and `<install-dir>`
below with your own folders.

## systemd user timer (Linux, no root needed)

Templates are in [`../systemd/`](../systemd/). With a virtual environment in
`<install-dir>/current/.venv` and config, `.env` and database in `<install-dir>/shared`
(the layout `deploy.sh` creates):

```bash
mkdir -p ~/.config/systemd/user
sed -e "s|@APP_DIR@|<install-dir>|g" systemd/energy-analyzer.service > ~/.config/systemd/user/energy-analyzer.service
sed -e "s|@ONCALENDAR@|*-*-* 03:30:00|g" systemd/energy-analyzer.timer > ~/.config/systemd/user/energy-analyzer.timer
systemctl --user daemon-reload
systemctl --user enable --now energy-analyzer.timer
systemctl --user list-timers energy-analyzer.timer     # next run
systemctl --user start energy-analyzer.service         # run once now
journalctl --user -u energy-analyzer.service -n 50     # logs
```

User timers only run while you are logged in unless lingering is enabled for your user
(one-time, needs an administrator): `sudo loginctl enable-linger "$USER"`.

## cron

```cron
30 3 * * * cd <install-dir>/shared && <install-dir>/current/.venv/bin/energy-analyzer --config config.toml daily --report report.html >> energy-analyzer.log 2>&1
```

## Docker

From the folder with `docker-compose.yml` (data in `./data`):

```cron
30 3 * * * cd /path/to/alphaess-energy-analyzer && docker compose run --rm energy-analyzer daily --report report.html >> data/energy-analyzer.log 2>&1
```
