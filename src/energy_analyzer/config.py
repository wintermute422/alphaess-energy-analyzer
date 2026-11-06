"""Settings from defaults < config.toml < .env < environment < CLI flags."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 in CI
    import tomli as tomllib

DEFAULT_BASE_URL = "https://openapi.alphaess.com/api"

CONFIG_TEMPLATE = """\
# energy-analyzer configuration. Secrets do NOT belong here - put them in .env.
# Relative paths are resolved against the directory of this file.

[storage]
db = "energy.db"

[system]
# Serial of the system to use when your developer account has several (or set ALPHA_SYSTEM_SN).
sn = ""

[api]
min_interval_s = 10      # minimum seconds between API calls (keep >= 10)
timeout_s = 30
max_retries = 4

[analysis]
night_start = "02:00"    # night window for the standby baseline (local system time)
night_end = "05:00"
rolling_days = 28        # window of the rolling median used for anomaly detection
z_threshold = 3.5        # robust z-score above which a day is flagged ...
min_rel_deviation = 0.2  # ... if it also differs >= 20 % from the rolling median
spike_factor = 3.0       # night sample > spike_factor x baseline ...
spike_min_w = 500        # ... and at least this many watts above it = night spike
min_history_days = 7     # days of history needed before a day can be flagged

[report]
title = "Energy report"

[web]
# `energy-analyzer serve`: the dashboard. Listening on anything but 127.0.0.1 requires
# WEB_PASSWORD in .env (login user below).
host = "127.0.0.1"
port = 8088
user = "energy"
"""

# config.toml key -> Settings field
_TOML_KEYS: dict[tuple[str, str], str] = {
    ("storage", "db"): "db_path",
    ("system", "sn"): "sys_sn",
    ("api", "base_url"): "base_url",
    ("api", "min_interval_s"): "min_interval_s",
    ("api", "timeout_s"): "timeout_s",
    ("api", "max_retries"): "max_retries",
    ("api", "backoff_s"): "backoff_s",
    ("analysis", "night_start"): "night_start",
    ("analysis", "night_end"): "night_end",
    ("analysis", "rolling_days"): "rolling_days",
    ("analysis", "z_threshold"): "z_threshold",
    ("analysis", "min_rel_deviation"): "min_rel_deviation",
    ("analysis", "spike_factor"): "spike_factor",
    ("analysis", "spike_min_w"): "spike_min_w",
    ("analysis", "min_history_days"): "min_history_days",
    ("report", "title"): "report_title",
    ("web", "host"): "web_host",
    ("web", "port"): "web_port",
    ("web", "user"): "web_user",
}

# environment / .env name -> Settings field
_ENV_KEYS: dict[str, str] = {
    "ALPHA_APP_ID": "app_id",
    "ALPHA_APP_SECRET": "app_secret",
    "ALPHA_SYSTEM_SN": "sys_sn",
    "ENERGY_DB": "db_path",
    "ALPHA_BASE_URL": "base_url",
    "WEB_USER": "web_user",
    "WEB_PASSWORD": "web_password",
    "WEB_HOST": "web_host",
    "WEB_PORT": "web_port",
}


class ConfigError(Exception):
    """Invalid or missing configuration (message is safe to show the user)."""


@dataclass(frozen=True)
class Settings:
    app_id: str | None = field(default=None, repr=False)
    app_secret: str | None = field(default=None, repr=False)
    sys_sn: str | None = field(default=None, repr=False)
    db_path: Path = Path("energy.db")
    base_url: str = DEFAULT_BASE_URL
    min_interval_s: float = 10.0
    timeout_s: float = 30.0
    max_retries: int = 4
    backoff_s: float = 5.0
    night_start: str = "02:00"
    night_end: str = "05:00"
    rolling_days: int = 28
    z_threshold: float = 3.5
    min_rel_deviation: float = 0.2
    spike_factor: float = 3.0
    spike_min_w: float = 500.0
    min_history_days: int = 7
    report_title: str = "Energy report"
    web_host: str = "127.0.0.1"
    web_port: int = 8088
    web_user: str = "energy"
    web_password: str | None = field(default=None, repr=False)
    config_path: Path | None = None
    env_path: Path | None = None

    def require_credentials(self) -> tuple[str, str]:
        if not self.app_id or not self.app_secret:
            where = self.env_path or Path(".env")
            raise ConfigError(
                "AlphaESS credentials missing: set ALPHA_APP_ID and ALPHA_APP_SECRET in "
                f"{where} (see .env.example) or in the environment."
            )
        return self.app_id, self.app_secret


def parse_dotenv(text: str) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, # comments, optional quotes, optional `export`."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key.strip()] = value
    return out


def _coerce(name: str, value: Any) -> Any:
    target = {f.name: f.type for f in fields(Settings)}[name]
    try:
        if target in ("float", float):
            return float(value)
        if target in ("int", int):
            return int(value)
        if name == "db_path":
            return Path(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid value for {name}: {value!r}") from exc
    text = str(value).strip()
    return text or None if name in ("app_id", "app_secret", "sys_sn", "web_password") else text


def load_settings(
    config_path: Path | None = None,
    env_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> Settings:
    environ = dict(os.environ) if environ is None else environ
    values: dict[str, Any] = {}

    if config_path is None and environ.get("ENERGY_CONFIG"):
        config_path = Path(environ["ENERGY_CONFIG"])
    if config_path is None and Path("config.toml").is_file():
        config_path = Path("config.toml")
    base_dir = Path.cwd()
    if config_path is not None:
        if not config_path.is_file():
            raise ConfigError(f"config file not found: {config_path}")
        base_dir = config_path.resolve().parent
        try:
            data = tomllib.loads(config_path.read_text("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"cannot parse {config_path}: {exc}") from exc
        for (section, key), name in _TOML_KEYS.items():
            if key in data.get(section, {}):
                values[name] = _coerce(name, data[section][key])

    if env_path is None:
        for cand in (base_dir / ".env", Path.cwd() / ".env"):
            if cand.is_file():
                env_path = cand
                break
    layers = []
    if env_path is not None and env_path.is_file():
        layers.append(parse_dotenv(env_path.read_text("utf-8")))
    layers.append(environ)
    for layer in layers:
        for env_key, name in _ENV_KEYS.items():
            if layer.get(env_key):
                values[name] = _coerce(name, layer[env_key])

    for name, value in (overrides or {}).items():
        if value is not None:
            values[name] = _coerce(name, value)

    settings = replace(Settings(), **values, config_path=config_path, env_path=env_path)
    if not settings.db_path.is_absolute():
        settings = replace(settings, db_path=base_dir / settings.db_path)
    if settings.min_interval_s < 10.0 and settings.base_url == DEFAULT_BASE_URL:
        raise ConfigError("api.min_interval_s must be >= 10 for the AlphaESS Open API")
    return settings
