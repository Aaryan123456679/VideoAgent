"""S0.1.3 — pytest harness settings and the CI workflow that gates on them."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

GATED_JOBS = ("lint", "type", "test", "pre-commit")
SUBPROCESS_TIMEOUT = 180
FIXTURE_MODULE = Path("tests") / "_fixtures" / "integration_marker_case.py"


def _pytest(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )


def test_asyncio_strict_mode(pyproject: dict[str, Any]) -> None:
    """An `async def` test with no marker must fail collection, never be skipped silently."""
    assert pyproject["tool"]["pytest"]["ini_options"]["asyncio_mode"] == "strict"


def test_strict_markers_and_strict_config(pyproject: dict[str, Any]) -> None:
    addopts: list[str] = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert "--strict-markers" in addopts
    assert "--strict-config" in addopts


def test_integration_is_deselected_in_default_addopts(pyproject: dict[str, Any]) -> None:
    addopts: list[str] = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert addopts[addopts.index("-m") + 1] == "not integration"


def test_test_layout_exists(repo_root: Path) -> None:
    for leaf in ("unit", "integration", "contract"):
        assert (repo_root / "tests" / leaf).is_dir()


def test_coverage_is_reported_but_not_gated(pyproject: dict[str, Any]) -> None:
    """S0.1.3 acceptance 5 — a coverage threshold is deliberately absent at this commit."""
    addopts: list[str] = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert any(opt.startswith("--cov=") for opt in addopts)
    assert not any(opt.startswith("--cov-fail-under") for opt in addopts)
    assert "fail_under" not in pyproject["tool"]["coverage"]["report"]


def test_integration_marker_deselected_by_default(repo_root: Path) -> None:
    """Collect a module holding one integration test; the default run must deselect it."""
    default_run = _pytest(
        ["--collect-only", "-q", "--no-cov", "-p", "no:cacheprovider", str(FIXTURE_MODULE)],
        cwd=repo_root,
    )
    assert "1 deselected" in default_run.stdout, default_run.stdout + default_run.stderr
    assert "test_needs_the_dev_stack" not in default_run.stdout

    selected_run = _pytest(
        [
            "--collect-only",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-m",
            "integration",
            str(FIXTURE_MODULE),
        ],
        cwd=repo_root,
    )
    assert "test_needs_the_dev_stack" in selected_run.stdout, selected_run.stdout


def test_unmarked_async_test_fails_collection(repo_root: Path, tmp_path: Path) -> None:
    """Strict asyncio mode: an unmarked coroutine test must error, not pass vacuously."""
    offender = tmp_path / "test_unmarked_async.py"
    offender.write_text("async def test_unmarked() -> None:\n    assert False\n", encoding="utf-8")
    result = _pytest(
        [
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-c",
            str(repo_root / "pyproject.toml"),
            "--rootdir",
            str(repo_root),
            str(offender),
        ],
        cwd=repo_root,
    )
    assert result.returncode != 0, result.stdout


def test_make_test_integration_target_exists(repo_root: Path) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not on PATH")
    result = subprocess.run(
        [make, "test-integration", "--dry-run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "-m integration" in result.stdout


def test_ci_workflow_gates_three_jobs(repo_root: Path) -> None:
    """lint, type and test all exist as jobs and none is `continue-on-error`."""
    workflow_path = repo_root / ".github" / "workflows" / "ci.yml"
    assert workflow_path.is_file()

    workflow: dict[str, Any] = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs: dict[str, Any] = workflow["jobs"]
    for job_name in GATED_JOBS:
        assert job_name in jobs, f"CI is missing the {job_name} job"

    raw = workflow_path.read_text(encoding="utf-8")
    assert "continue-on-error" not in raw

    assert "make lint" in raw
    assert "make type" in raw
    assert "make test" in raw


def test_ci_runs_pre_commit_over_the_whole_tree(repo_root: Path) -> None:
    """S0.1.2 AC4 is a gate, so something has to gate it.

    `make lint` runs ruff and mypy; the hook set also runs end-of-file-fixer,
    mixed-line-ending and detect-private-key. With pre-commit absent from CI those hooks
    were failing on committed files with nothing to catch it.
    """
    raw = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pre-commit run --all-files" in raw
