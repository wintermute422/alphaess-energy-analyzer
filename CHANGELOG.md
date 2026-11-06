# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

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
