"""`.env.example` is the configuration contract; it must stay a template, never a wallet.

The typed `Settings` object that binds to these names is a later task. What is in scope here
is the file's integrity: no credential value is ever committed, and `.env` stays untracked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

BANNED_SECRET_PREFIXES = ("mhk_live_", "sk-", "pk-", "AKIA")
SECRET_SUFFIXES = ("_KEY", "_KEY_ID", "_SECRET", "_TOKEN", "_PASSWORD")
GIT_TIMEOUT = 30

pytestmark = pytest.mark.contract


def test_env_example_declares_no_secret_values(env_example: dict[str, str]) -> None:
    filled = {
        name: value
        for name, value in env_example.items()
        if name.endswith(SECRET_SUFFIXES) and value
    }
    assert filled == {}, f"credential values committed in .env.example: {sorted(filled)}"


def test_no_declared_value_is_credential_shaped(env_example: dict[str, str]) -> None:
    """Comments may mention a key format; a *value* may never look like one."""
    offenders = [
        name
        for name, value in env_example.items()
        if any(value.startswith(prefix) for prefix in BANNED_SECRET_PREFIXES)
    ]
    assert offenders == []


def test_dotenv_is_ignored_and_the_template_is_not(repo_root: Path) -> None:
    entries = {
        line.strip() for line in (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert ".env" in entries
    assert "!.env.example" in entries


def test_dotenv_is_not_tracked_by_git(repo_root: Path) -> None:
    git = shutil.which("git")
    if git is None or not (repo_root / ".git").exists():
        pytest.skip("not a git checkout")

    result = subprocess.run(
        [git, "ls-files", "--", ".env", ".env.*"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )
    tracked = {line for line in result.stdout.split() if line and line != ".env.example"}
    assert tracked == set(), f"credential files are tracked: {sorted(tracked)}"
