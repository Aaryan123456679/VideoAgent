"""A module holding exactly one integration-marked test.

Not collected by a default run — `norecursedirs` excludes `_fixtures`, and the filename does
not match `python_files`. `tests/unit/test_pytest_and_ci_config.py` points pytest at it
explicitly to prove the default marker expression deselects it.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
def test_needs_the_dev_stack() -> None:
    """Must be deselected by a default `pytest` invocation."""
    assert True
