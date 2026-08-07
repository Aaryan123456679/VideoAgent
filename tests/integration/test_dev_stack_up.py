"""Integration checks against the running dev stack.

Collected always, deselected by default (`-m "not integration"`), selected by
`make test-integration`. Bring the stack up first: `make compose-up`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

COMPOSE_TIMEOUT = 120
EXPECTED_HEALTHY_SERVICES = 4

pytestmark = pytest.mark.integration


def _docker(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is not installed")
    return subprocess.run(
        [docker, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=COMPOSE_TIMEOUT,
        check=False,
    )


def test_compose_file_is_valid(repo_root: Path) -> None:
    result = _docker(["compose", "-f", "docker-compose.dev.yml", "config", "-q"], cwd=repo_root)
    assert result.returncode == 0, result.stderr


def test_four_services_report_healthy(repo_root: Path) -> None:
    result = _docker(
        ["compose", "-f", "docker-compose.dev.yml", "ps", "--format", "{{.Service}} {{.Health}}"],
        cwd=repo_root,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        pytest.skip("dev stack is not running; `make compose-up` first")

    healthy = [line for line in lines if line.endswith("healthy")]
    assert len(healthy) == EXPECTED_HEALTHY_SERVICES, result.stdout
