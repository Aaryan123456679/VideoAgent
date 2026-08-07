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

from tests.support import BANNED_SECRET_PREFIXES, SECRET_SUFFIXES
from video_agent.assembly.media_toolchain import (
    BINARY_PATH_ENV_VARS,
    DEFAULT_FFMPEG_VERSION,
    VERSION_PIN_ENV_VAR,
)
from video_agent.config.settings import Settings

# Public by name and by design; the paired `LANGFUSE_SECRET_KEY` is the one that must not be
# renderable. Typing an identifier as a secret would make it unusable as a log correlator.
PUBLIC_DESPITE_SECRET_SUFFIX = frozenset({"LANGFUSE_PUBLIC_KEY"})
GIT_TIMEOUT = 30

pytestmark = pytest.mark.contract


def test_the_credential_suffix_list_cannot_be_narrowed_silently(
    env_example: dict[str, str],
) -> None:
    """Pin the deny-list itself, because narrowing it is invisible.

    `docker-compose.dev.yml` carried a literal `POSTGRES_PASSWORD` while the compose test
    checked only `("_KEY", "_KEY_ID", "_TOKEN", "_SECRET")` — the list was exactly one
    suffix short of catching the one literal present, and nothing failed. Dropping a suffix
    makes every check that uses it quieter without making any of them red, so the list needs
    an assertion of its own.
    """
    assert set(SECRET_SUFFIXES) == {"_KEY", "_KEY_ID", "_SECRET", "_TOKEN", "_PASSWORD"}
    assert set(BANNED_SECRET_PREFIXES) == {"mhk_live_", "sk-", "pk-", "AKIA"}

    # And prove the list is not merely correct but load-bearing: it must match real
    # variables in the contract, or it is a deny-list guarding an empty set.
    matched = [name for name in env_example if name.endswith(SECRET_SUFFIXES)]
    assert len(matched) >= len(SECRET_SUFFIXES)


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


def test_default_ffmpeg_pin_matches_the_env_example_default(env_example: dict[str, str]) -> None:
    """The code default and the template default must be the same version.

    Deliberately compares the *constant*, not `pinned_version()`. Every other ffmpeg test
    derives its expectation from `pinned_version()`, which reads the environment, so the
    whole suite stays self-consistent no matter what the constant says — setting it to
    "99.99" left 58 tests passing. This is the one assertion with an external referent, and
    it is what stops the shipped default from drifting away from the documented one.
    """
    assert env_example[VERSION_PIN_ENV_VAR] == DEFAULT_FFMPEG_VERSION


def test_media_toolchain_env_vars_are_declared_in_the_contract(
    env_example: dict[str, str],
) -> None:
    """Preflight reads `os.environ` directly, so its variables bypass `Settings`.

    They still have to be documented, or an operator configures a toolchain override that
    no template mentions.
    """
    read_by_preflight = {VERSION_PIN_ENV_VAR, *BINARY_PATH_ENV_VARS.values()}
    undocumented = sorted(read_by_preflight - set(env_example))
    assert undocumented == [], f"preflight reads variables .env.example does not declare: {
        undocumented
    }"


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
