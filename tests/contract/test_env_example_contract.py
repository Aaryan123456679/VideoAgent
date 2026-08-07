"""`.env.example` is the configuration contract; it must stay a template, never a wallet.

Two halves, both load-bearing. The file's **integrity**: no credential value is ever
committed, and `.env` stays untracked. And the file's **binding**: `Settings` declares exactly
one field per variable, spelled identically, so a variable added to one side and not the other
fails here rather than as a `KeyError` in production.

The key-set diff is asserted in both directions on purpose. A missing field is the obvious
failure; an extra field is the quieter one — configuration that no template documents, which
every operator therefore deploys without.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from video_agent.config.settings import Settings

BANNED_SECRET_PREFIXES = ("mhk_live_", "sk-", "pk-", "AKIA")
SECRET_SUFFIXES = ("_KEY", "_KEY_ID", "_SECRET", "_TOKEN", "_PASSWORD")
# Public by name and by design; the paired `LANGFUSE_SECRET_KEY` is the one that must not be
# renderable. Typing an identifier as a secret would make it unusable as a log correlator.
PUBLIC_DESPITE_SECRET_SUFFIX = frozenset({"LANGFUSE_PUBLIC_KEY"})
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


def test_settings_fields_match_env_example_exactly(env_example: dict[str, str]) -> None:
    """The contract test. Both directions, so neither side can drift ahead of the other."""
    declared = set(Settings.model_fields)
    contracted = set(env_example)

    assert declared - contracted == set(), "Settings fields with no .env.example variable"
    assert contracted - declared == set(), ".env.example variables with no Settings field"


def test_every_credential_variable_is_typed_as_a_secret(env_example: dict[str, str]) -> None:
    """A credential typed as `str` is one f-string away from a log line. `[CPS §Observability]`"""
    plain = sorted(
        name
        for name in env_example
        if name.endswith(SECRET_SUFFIXES)
        and name not in PUBLIC_DESPITE_SECRET_SUFFIX
        and Settings.model_fields[name].annotation is not SecretStr
    )
    assert plain == [], f"credential variables not typed SecretStr: {plain}"
