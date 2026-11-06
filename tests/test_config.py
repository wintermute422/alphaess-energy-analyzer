from __future__ import annotations

from pathlib import Path

import pytest

from energy_analyzer.config import CONFIG_TEMPLATE, ConfigError, load_settings, parse_dotenv


def test_parse_dotenv() -> None:
    env = parse_dotenv("# c\nexport A=1\nB='two words'\nC=\"x\"\nD=val # comment\nnot a line\nE=\n")
    assert env == {"A": "1", "B": "two words", "C": "x", "D": "val", "E": ""}


def test_defaults_without_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    s = load_settings(environ={})
    assert s.min_interval_s == 10.0 and s.night_start == "02:00"
    assert s.db_path == tmp_path / "energy.db"
    with pytest.raises(ConfigError, match="ALPHA_APP_ID"):
        s.require_credentials()


def test_precedence_config_env_file_environ_cli(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG_TEMPLATE.replace('sn = ""', 'sn = "FROMCONFIG"'), "utf-8")
    (tmp_path / ".env").write_text("ALPHA_APP_ID=id-file\nALPHA_APP_SECRET=s\n", "utf-8")
    s = load_settings(cfg, environ={})
    assert (s.app_id, s.sys_sn) == ("id-file", "FROMCONFIG")
    assert s.db_path == tmp_path / "energy.db"  # relative to the config file
    s = load_settings(cfg, environ={"ALPHA_APP_ID": "id-env", "ALPHA_SYSTEM_SN": "FROMENV"})
    assert (s.app_id, s.sys_sn) == ("id-env", "FROMENV")
    s = load_settings(cfg, environ={}, overrides={"sys_sn": "FROMCLI", "db_path": "x.db"})
    assert s.sys_sn == "FROMCLI" and s.db_path == tmp_path / "x.db"
    assert "id-file" not in repr(s)  # credentials hidden from repr


def test_rejects_too_short_interval_and_bad_values(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[api]\nmin_interval_s = 2\n", "utf-8")
    with pytest.raises(ConfigError, match=">= 10"):
        load_settings(cfg, environ={})
    cfg.write_text("[analysis]\nrolling_days = 'many'\n", "utf-8")
    with pytest.raises(ConfigError, match="rolling_days"):
        load_settings(cfg, environ={})
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "missing.toml", environ={})
