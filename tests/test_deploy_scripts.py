"""Static and dry-run checks for deploy.sh / rollback.sh (no SSH, no network)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["deploy.sh", "rollback.sh"]
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


@needs_bash
@pytest.mark.parametrize("script", SCRIPTS)
def test_bash_syntax(script: str) -> None:
    assert BASH is not None
    subprocess.run([BASH, "-n", str(ROOT / script)], check=True)


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_hardcoded_hosts_users_or_paths(script: str) -> None:
    text = (ROOT / script).read_text("utf-8")
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert not re.search(r"/home/[a-z]", text)
    assert "sudo" not in text.replace("no sudo", "")


@pytest.mark.parametrize("script", SCRIPTS)
def test_scripts_read_settings_only_from_deploy_env(script: str) -> None:
    text = (ROOT / script).read_text("utf-8")
    assert 'CONF="${DEPLOY_ENV_FILE:-deploy.env}"' in text
    assert "source " not in text  # deploy.env is parsed, never executed


def _run(script: str, env_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(ROOT / script), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "DEPLOY_ENV_FILE": str(env_file)},
        cwd=ROOT,
    )


@needs_bash
def test_deploy_dry_run_changes_nothing(tmp_path: Path) -> None:
    env = tmp_path / "deploy.env"
    env.write_text(
        "DEPLOY_HOST=server.example\nDEPLOY_USER=someone\nDEPLOY_PATH=~/ea\n"
        "DEPLOY_KEEP_RELEASES=3\n",
        "utf-8",
    )
    proc = _run("deploy.sh", env, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "dry run" in proc.stdout and "someone@server.example:~/ea/releases/" in proc.stdout


@needs_bash
@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("DEPLOY_USER=x\n", "DEPLOY_HOST and DEPLOY_USER"),
        ("DEPLOY_HOST=h\nDEPLOY_USER=u\nDEPLOY_KEEP_RELEASES=1\n", ">= 2"),
        ("DEPLOY_HOST=h\nDEPLOY_USER=u\nDEPLOY_KEEP_RELEASES=abc\n", "must be a number"),
    ],
)
def test_deploy_rejects_bad_config(tmp_path: Path, content: str, message: str) -> None:
    env = tmp_path / "deploy.env"
    env.write_text(content, "utf-8")
    proc = _run("deploy.sh", env, "--dry-run")
    assert proc.returncode != 0 and message in proc.stderr


@needs_bash
def test_missing_deploy_env_is_explained(tmp_path: Path) -> None:
    for script in SCRIPTS:
        proc = _run(script, tmp_path / "nope.env")
        assert proc.returncode != 0 and "deploy.env.example" in proc.stderr


@needs_bash
def test_rollback_rejects_bad_release_name(tmp_path: Path) -> None:
    env = tmp_path / "deploy.env"
    env.write_text("DEPLOY_HOST=h\nDEPLOY_USER=u\n", "utf-8")
    proc = _run("rollback.sh", env, "--to", "../../etc")
    assert proc.returncode != 0 and "invalid release name" in proc.stderr
