# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [0.2.0] - 2026-11-06

### Added
- Web dashboard (`energy-analyzer serve`): overview with KPIs and live values, day detail with
  5-minute curves, load heatmap, analysis (baseline, anomalies, months) and an events page.
  Stdlib HTTP server, no external resources; login required when reachable from the network.
- `energy-analyzer serve --demo`: the dashboard on 120 days of synthetic data, no account needed.
- Events: tell the analyzer about holidays, guests, home office, EV charging, heating/cooling
  seasons, new appliances, outages and notes. They explain anomalies, keep unusual periods out
  of the rolling references, and start new comparison periods.
- `energy-analyzer refresh`: fetch today only (2 API calls) for a 15-minute refresh timer.
- deploy.sh installs the dashboard and the refresh timer (`DEPLOY_WEB_HOST`, `DEPLOY_WEB_PORT`,
  `DEPLOY_REFRESH_ONCALENDAR`) and generates the dashboard password on the target.

### Changed
- Night spikes are only reported when they are rare: if at least `spike_routine_share` (25 %)
  of the recent nights had a similar peak, the spike is routine. A heat pump that starts on
  many nights is no longer flagged once a week of history exists.
- The report marks anomalies explained by events and counts only unexpected ones.
- SQLite runs in WAL mode (schema v2 adds the events table; migrated automatically).

## [0.1.0] - 2026-11-06

### Added
- Signed AlphaESS Open API client (`getEssList`, `getOneDayPowerBySn`, `getOneDateEnergyBySn`,
  `getLastPowerData`) with >= 10 s spacing, retries with backoff and clear auth/serial errors.
- SQLite storage with versioned migrations and idempotent upserts.
- Analysis: night standby baseline and trend, self-consumption, autarky, robust z-score anomaly
  detection with an effect-size floor, night spike detection, monthly summary.
- Self-contained offline HTML report with inline SVG charts.
- CLI: `init`, `backfill`, `daily`, `report`, `live`, `selftest` (synthetic data, no network).
- Dockerfile and docker-compose.yml, systemd user service/timer templates, cron example.
- `deploy.sh` / `rollback.sh` for SSH deployment with versioned releases.
- CI: ruff, mypy, pytest on Python 3.10–3.13, selftest, gitleaks over the full history, Docker.
