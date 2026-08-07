"""Integration checks against the running dev stack.

Collected always, deselected by default (`-m "not integration"`), selected by
`make test-integration`. Bring the stack up first: `make compose-up`.

The guard is daemon *responsiveness*, not binary presence. `shutil.which("docker")` only
proves a client is installed; a Docker Desktop VM that has wedged leaves the client on PATH
and every command hanging until the subprocess timeout, which surfaces as `TimeoutExpired`
— an error, where S0.1.3 asks for a skip. One short probe up front turns that into a skip
with a reason.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

COMPOSE_TIMEOUT = 120
DAEMON_PROBE_TIMEOUT = 5
EXPECTED_HEALTHY_SERVICES = 4

pytestmark = pytest.mark.integration


def _daemon_status() -> str | None:
    """Return None when the daemon answers, otherwise the reason it did not."""
    docker = shutil.which("docker")
    if docker is None:
        return "the docker client is not installed"

    try:
        probe = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=DAEMON_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"the docker daemon did not answer `docker info` within {DAEMON_PROBE_TIMEOUT}s"
    except OSError as exc:
        return f"the docker client could not be executed: {exc.strerror or exc}"

    if probe.returncode != 0:
        return f"`docker info` exited {probe.returncode}: {probe.stderr.strip()[:200]}"
    if not probe.stdout.strip():
        return "`docker info` reported no server version; the daemon is not running"
    return None


@pytest.fixture(scope="module")
def docker_daemon() -> None:
    """Skip the whole module unless the daemon answers. Probed once, not per test."""
    reason = _daemon_status()
    if reason is not None:
        pytest.skip(f"dev stack unavailable: {reason}")


def _docker(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    assert docker is not None, "guarded by the docker_daemon fixture"
    try:
        return subprocess.run(
            [docker, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=COMPOSE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(f"docker {args[0]} exceeded {COMPOSE_TIMEOUT}s; the daemon wedged mid-run")


@pytest.mark.usefixtures("docker_daemon")
def test_compose_file_is_valid(repo_root: Path) -> None:
    result = _docker(["compose", "-f", "docker-compose.dev.yml", "config", "-q"], cwd=repo_root)
    assert result.returncode == 0, result.stderr


@pytest.mark.usefixtures("docker_daemon")
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
