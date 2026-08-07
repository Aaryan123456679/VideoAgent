"""Shared fixtures for the whole suite.

Everything here is repository-level: paths, parsed configuration files and the async
plumbing. Domain fixtures (database, redis, object store) arrive with the modules that
own them.
"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse a ``KEY=value`` template into a mapping, ignoring comments and blank lines."""
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def pyproject() -> dict[str, Any]:
    """The parsed ``pyproject.toml``."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        data: dict[str, Any] = tomllib.load(handle)
    return data


@pytest.fixture(scope="session")
def env_example_text() -> str:
    """Raw text of the ``.env.example`` configuration contract."""
    return (REPO_ROOT / ".env.example").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def env_example(env_example_text: str) -> dict[str, str]:
    """``.env.example`` parsed into variable name to default value."""
    return parse_dotenv(env_example_text)


@pytest.fixture
def clean_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run a test in an empty working directory; ``monkeypatch`` restores the old one."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def running_loop() -> AsyncIterator[asyncio.AbstractEventLoop]:
    """The event loop the test is running on.

    Exists so async fixture plumbing is exercised under ``asyncio_mode = strict``; later
    tasks hang their pool fixtures off the same loop.
    """
    yield asyncio.get_running_loop()
