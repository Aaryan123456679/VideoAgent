"""S0.1.1 — the package exists, imports, and holds exactly the fixed module set."""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

import video_agent

SUPPORTED_PYTHON = (3, 12)

# The ten LLDs in docs/LLD/ plus config. A twelfth entry here without a documentation
# change is the failure this file exists to catch.
EXPECTED_SUBPACKAGES = frozenset(
    {
        "api",
        "harness",
        "gateway",
        "graph",
        "planning",
        "providers",
        "qc",
        "assembly",
        "persistence",
        "observability",
        "config",
    }
)


@pytest.mark.parametrize("name", sorted(EXPECTED_SUBPACKAGES))
def test_package_imports(name: str) -> None:
    """`import video_agent` and each of the eleven sub-packages import cleanly."""
    assert importlib.import_module(f"video_agent.{name}") is not None


def test_root_package_imports() -> None:
    assert video_agent.__name__ == "video_agent"


def test_module_set_is_exactly_ten_plus_config() -> None:
    """Enumerate the sub-packages on disk and assert set equality with the fixed list."""
    discovered = {
        module.name for module in pkgutil.iter_modules(video_agent.__path__) if module.ispkg
    }
    assert discovered == EXPECTED_SUBPACKAGES
    assert video_agent.MODULES == EXPECTED_SUBPACKAGES


def test_python_version_floor() -> None:
    assert sys.version_info[:2] == SUPPORTED_PYTHON


def test_version_is_a_non_empty_string() -> None:
    assert isinstance(video_agent.__version__, str)
    assert video_agent.__version__


def test_package_ships_a_py_typed_marker() -> None:
    """Downstream mypy runs need the inline-types marker or the package reads as untyped."""
    package_dir = next(iter(video_agent.__path__))
    assert (Path(package_dir) / "py.typed").is_file()
