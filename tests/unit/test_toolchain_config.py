"""S0.1.2 — lint, format and strict type-check are configured with no baseline suppressions."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REQUIRES_PYTHON = ">=3.12,<3.13"
SUBPROCESS_TIMEOUT = 120


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )


def test_requires_python_is_312_only(pyproject: dict[str, Any]) -> None:
    """`pip install -e .` must refuse 3.11 and 3.13 as well as accept 3.12."""
    assert pyproject["project"]["requires-python"] == REQUIRES_PYTHON


def test_lint_config_has_no_blanket_ignores(pyproject: dict[str, Any]) -> None:
    """`tool.ruff.lint.ignore` and `tool.mypy.overrides` are empty (S0.1.2 acceptance 5)."""
    ruff_lint: dict[str, Any] = pyproject["tool"]["ruff"]["lint"]
    assert ruff_lint.get("ignore", []) == []
    assert ruff_lint.get("per-file-ignores", {}) == {}
    assert ruff_lint.get("extend-per-file-ignores", {}) == {}

    mypy: dict[str, Any] = pyproject["tool"]["mypy"]
    assert mypy.get("overrides", []) == []


def test_mypy_strict_enabled(pyproject: dict[str, Any]) -> None:
    mypy: dict[str, Any] = pyproject["tool"]["mypy"]
    assert mypy["strict"] is True
    assert mypy["disallow_any_generics"] is True
    assert mypy["warn_unused_ignores"] is True


def test_the_pydantic_plugin_is_configured_to_type_its_synthesised_init(
    pyproject: dict[str, Any],
) -> None:
    """`pydantic.mypy` without this table is a relaxation of the whole tree, not a tightening.

    `init_typed` defaults to `false`, so the `__init__` the plugin synthesises annotates every
    field as `Any` and constructor argument checking silently disappears from every model —
    including the six that exist today and every one added since. The plugin itself is needed
    (removing it makes `Settings()` three `call-arg` errors), so the table is how both hold.
    """
    plugin: dict[str, Any] = pyproject["tool"]["pydantic-mypy"]
    assert plugin["init_typed"] is True
    assert plugin["init_forbid_extra"] is True


def test_a_wrongly_typed_model_argument_is_a_type_error(repo_root: Path, tmp_path: Path) -> None:
    """The behavioural half: run the configured type checker over a planted violation.

    Asserted through mypy rather than through the config alone because the setting's *effect*
    is the claim, and a table entry that stopped working after a plugin upgrade would leave the
    config test green. Both spellings the table restores are planted: a wrong argument type and
    an argument that is not a field at all.
    """
    mypy = shutil.which("mypy")
    if mypy is None:
        pytest.skip("mypy is not on PATH; run inside the project venv")

    offender = tmp_path / "wrong_model_argument.py"
    offender.write_text(
        "from video_agent.config.aliases import ModelRef\n"
        "\n"
        "\n"
        "def build() -> ModelRef:\n"
        '    return ModelRef(model=123, weight="not an int", nonsense=True)\n',
        encoding="utf-8",
    )

    result = _run(
        [mypy, "--strict", "--no-incremental", str(offender)],
        cwd=repo_root,
    )
    report = result.stdout + result.stderr

    assert result.returncode != 0, report
    assert "arg-type" in report, report
    assert 'Argument "model"' in report, report
    assert 'Argument "weight"' in report, report
    assert "call-arg" in report, report


def test_ruff_selects_the_bare_type_ignore_rule(pyproject: dict[str, Any]) -> None:
    """PGH003 is what makes an uncoded `# type: ignore` a lint error."""
    select: list[str] = pyproject["tool"]["ruff"]["lint"]["select"]
    assert "PGH" in select or "PGH003" in select


def test_bare_type_ignore_is_a_lint_error(repo_root: Path, tmp_path: Path) -> None:
    """Run the configured linter over a planted bare `# type: ignore` and assert it fails."""
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff is not on PATH; run inside the project venv")

    offender = tmp_path / "bare_ignore.py"
    offender.write_text("x: int = 'not an int'  # type: ignore\n", encoding="utf-8")
    result = _run(
        [ruff, "check", "--config", str(repo_root / "pyproject.toml"), str(offender)],
        cwd=repo_root,
    )
    assert result.returncode != 0
    assert "PGH003" in result.stdout + result.stderr

    coded = tmp_path / "coded_ignore.py"
    coded.write_text("x: int = 'not an int'  # type: ignore[assignment]\n", encoding="utf-8")
    coded_result = _run(
        [ruff, "check", "--config", str(repo_root / "pyproject.toml"), str(coded)],
        cwd=repo_root,
    )
    assert "PGH003" not in coded_result.stdout


def test_make_check_runs_all_three(repo_root: Path) -> None:
    """`make check --dry-run` reaches the lint, type and test targets."""
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not on PATH")

    result = _run([make, "check", "--dry-run"], cwd=repo_root)
    assert result.returncode == 0, result.stderr
    recipe = result.stdout
    assert "ruff format --check" in recipe
    assert "ruff check" in recipe
    assert "mypy" in recipe
    assert "pytest" in recipe


def test_pre_commit_config_covers_the_same_gates(repo_root: Path) -> None:
    text = (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "ruff-check" in text
    assert "ruff-format" in text
    assert "mypy" in text


def test_pre_commit_pins_the_same_ruff_the_venv_runs(repo_root: Path) -> None:
    """Two ruff versions gating one tree is how a lint failure reaches CI green.

    The hook pinned 0.14.6 while `make lint` and CI ran 0.16.2, so `ruff format` and
    `ruff check` could legitimately disagree with themselves depending on which gate fired.
    """
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff is not on PATH; run inside the project venv")

    installed = _run([ruff, "--version"], cwd=repo_root).stdout.split()[1]

    config = yaml.safe_load((repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooked = next(
        repo["rev"] for repo in config["repos"] if "ruff-pre-commit" in str(repo.get("repo", ""))
    )
    assert hooked.lstrip("v") == installed, (
        f"pre-commit pins ruff {hooked} but the venv runs {installed}"
    )
