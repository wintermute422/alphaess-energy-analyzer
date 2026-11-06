"""Claude Code hooks for this repo (wired in .claude/settings.json).

post-edit: after Edit/Write of a *.py file, run ruff + mypy and feed failures back (exit 2).
pre-bash:  block force pushes, pushes to non-feature branches and broad/forced `git add`.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BLOCKED_BASH = [
    (r"\bgit\b[^;&|]*\bpush\b[^;&|]*(\s--force(-with-lease)?\b|\s-[a-zA-Z]*f\b|\s\+\S)",
     "force pushes are not allowed"),
    (r"\bgit\b[^;&|]*\bpush\b[^;&|]*\s(origin\s+)?(main|master)\b",
     "push to main is not allowed; open a PR from a feature branch"),
    (r"\bgit\b[^;&|]*\badd\b[^;&|]*\s(-A|--all|-f|--force)\b",
     "use explicit paths with git add (no -A/--all/-f)"),
    (r"\bgit\b[^;&|]*\badd\s+\.(\s|$)", "use explicit paths with git add (no `git add .`)"),
    (r"--no-verify\b", "skipping git hooks is not allowed"),
    (r"\bgit\b[^;&|]*\b(rebase|filter-branch|filter-repo)\b",
     "history rewrites are not allowed on pushed branches"),
]


def _python() -> str:
    for cand in (ROOT / ".venv" / "bin" / "python", ROOT / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand)
    return sys.executable


def post_edit(event: dict[str, object]) -> int:
    tool_input = event.get("tool_input")
    path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
    if not isinstance(path, str) or not path.endswith(".py"):
        return 0
    py = _python()
    problems = []
    for cmd in ([py, "-m", "ruff", "check", path], [py, "-m", "mypy", path]):
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            problems.append(f"$ {' '.join(cmd[1:])}\n{proc.stdout}{proc.stderr}".strip())
    if problems:
        print("\n\n".join(problems), file=sys.stderr)
        return 2
    return 0


def pre_bash(event: dict[str, object]) -> int:
    tool_input = event.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str):
        return 0
    for pattern, reason in BLOCKED_BASH:
        if re.search(pattern, command):
            print(f"Blocked by repo policy (scripts/claude_hook.py): {reason}", file=sys.stderr)
            return 2
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if mode == "post-edit":
        return post_edit(event)
    if mode == "pre-bash":
        return pre_bash(event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
