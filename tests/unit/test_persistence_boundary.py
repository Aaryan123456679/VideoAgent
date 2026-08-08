"""S0.5.8 acceptance 5 — nothing outside `video_agent.persistence` opens a database session.

`persistence.md` §3 rule 3 requires `SET LOCAL app.tenant_id` at the start of *every*
transaction. That is only enforceable if there is one place transactions begin. A module that
builds its own engine gets a connection with no tenant context on it, and under the
missing-safe policy that connection does not fail — it quietly sees nothing, or, on the two
RLS-exempt tables, sees everything.

So the rule is checked structurally: an AST scan for the SQLAlchemy constructors that mint an
engine or a session, anywhere under `src/` other than the persistence package. The scanner is
exercised against source that violates the rule as well as against the tree, so "no
violations" is a result rather than a default.

**Imports under `if TYPE_CHECKING:` are not violations.** A name used only in an annotation
constructs nothing at runtime. Counting it would make the check fire on modules that are
obeying the rule, and a check with false positives is a check that gets an exemption added to
it.

**There is no longer an exemption, and that is the point.** `T0.5` shipped this gate with a
`PENDING_CONSOLIDATION` list naming `api/clients.py` and `api/database.py`, which built their
own engine and their own tenant scope — a duplicate of `video_agent.persistence.session` down
to a second copy of the `app.tenant_id` constant. The list was asserted in both directions, so
an entry that stopped violating had to be deleted. `T0.6` consolidated both onto
`persistence.session` and deleted the list, which is the exemption working as designed: it
expired because the reason for it went away, not because someone stopped looking.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

PERSISTENCE_PACKAGE = "src/video_agent/persistence"

SESSION_CONSTRUCTORS: frozenset[str] = frozenset(
    {
        "create_engine",
        "create_async_engine",
        "sessionmaker",
        "async_sessionmaker",
        "Session",
        "AsyncSession",
        "async_engine_from_config",
        "engine_from_config",
    }
)
"""Every SQLAlchemy entry point that produces a connection or a unit of work.

Named rather than pattern-matched: the list is short, it is in one place, and adding to it is
a deliberate act. A regex over `sqlalchemy.*` would also catch the `select()` and `Table` uses
that any module is entitled to.
"""


def _is_type_checking_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _runtime_nodes(tree: ast.Module) -> Iterator[ast.AST]:
    """Every node that runs, skipping `if TYPE_CHECKING:` bodies entirely."""
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt) and _is_type_checking_guard(child):
                continue
            stack.append(child)


def violations_in(tree: ast.Module, relative: str) -> list[str]:
    """Every place in one module that mints an engine or a session at runtime."""
    found: list[str] = []
    for node in _runtime_nodes(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
            found.extend(
                f"{relative}:{node.lineno}: imports {alias.name} from sqlalchemy"
                for alias in node.names
                if alias.name in SESSION_CONSTRUCTORS
            )
        if isinstance(node, ast.Call):
            name = _called_name(node.func)
            if name in SESSION_CONSTRUCTORS:
                found.append(f"{relative}:{node.lineno}: calls {name}()")
    return sorted(found)


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_violations(root: Path) -> dict[str, list[str]]:
    """Every module under `src/` outside the persistence package, to its violations."""
    found: dict[str, list[str]] = {}
    for path in sorted((root / "src").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(PERSISTENCE_PACKAGE):
            continue
        offences = violations_in(ast.parse(path.read_text(encoding="utf-8")), relative)
        if offences:
            found[relative] = offences
    return found


# --- The gate --------------------------------------------------------------------------------


def test_no_module_outside_persistence_opens_a_session(repo_root: Path) -> None:
    """Nothing under `src/` outside the persistence package mints an engine or a session.

    No exemption list any more. A module that needs a transaction calls
    `video_agent.persistence.tenant_session`, which is the only place `SET LOCAL app.tenant_id`
    is issued.
    """
    offenders = _module_violations(repo_root)

    assert offenders == {}, "\n".join(line for lines in offenders.values() for line in lines)


@pytest.mark.parametrize(
    "path",
    ["src/video_agent/api/clients.py", "src/video_agent/api/database.py"],
)
def test_the_two_consolidated_modules_are_clean(repo_root: Path, path: str) -> None:
    """The `T0.4` duplication is gone, named module by module.

    Pinned by name rather than left to the sweep above, because the sweep would also pass if
    both files were deleted or renamed — and "the violation is gone because the file is gone"
    is not the same result as "the file now uses the shared session".
    """
    source = repo_root / path
    assert source.is_file()

    assert violations_in(ast.parse(source.read_text(encoding="utf-8")), path) == []


def test_the_api_reuses_the_persistence_session_rather_than_reimplementing_it(
    repo_root: Path,
) -> None:
    """Consolidated *onto* `persistence.session`, not merely stripped of its constructors.

    A `Database.tenant_scope` that had simply stopped opening a transaction would satisfy the
    scan above while quietly running every query unscoped. This asserts the positive: the
    module imports the shared scope and the shared setting name.
    """
    source = (repo_root / "src/video_agent/api/database.py").read_text(encoding="utf-8")
    imported = {
        f"{node.module}.{alias.name}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }

    assert "video_agent.persistence.session.tenant_session" in imported
    assert "video_agent.persistence.rls.TENANT_SETTING" in imported
    assert "SET_LOCAL_TENANT_SQL" not in source


# --- The scanner ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy.ext.asyncio import create_async_engine\n",
        "from sqlalchemy.orm import sessionmaker\n",
        "import sqlalchemy\nengine = sqlalchemy.create_engine('postgresql://')\n",
        "def go(f):\n    return async_sessionmaker(f)\n",
    ],
)
def test_the_scanner_catches_each_way_of_opening_one(source: str) -> None:
    """Import, attribute call and bare call are three spellings of the same violation."""
    assert violations_in(ast.parse(source), "src/video_agent/api/routes.py")


def test_the_scanner_ignores_ordinary_sqlalchemy_use() -> None:
    """`select()` and `Table` are not the boundary; the constructors are."""
    source = "from sqlalchemy import select, Table\nq = select(Table)\n"
    assert violations_in(ast.parse(source), "src/video_agent/api/routes.py") == []


def test_the_scanner_ignores_type_only_imports() -> None:
    """A name used only in an annotation constructs nothing."""
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.ext.asyncio import AsyncSession\n"
    )
    assert violations_in(ast.parse(source), "src/video_agent/api/routes.py") == []


def test_the_scanner_still_catches_a_runtime_import_beside_a_type_only_one() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "from sqlalchemy.ext.asyncio import async_sessionmaker\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.ext.asyncio import AsyncEngine\n"
    )
    assert len(violations_in(ast.parse(source), "src/video_agent/api/routes.py")) == 1


def test_the_persistence_package_does_open_one(repo_root: Path) -> None:
    """The exclusion is meaningful only because the excluded package really does this.

    Without this assertion the gate above would keep passing if `create_async_engine`
    disappeared from the tree entirely, which would mean the boundary was being enforced
    against nothing.
    """
    session_module = repo_root / PERSISTENCE_PACKAGE / "session.py"
    tree = ast.parse(session_module.read_text(encoding="utf-8"))
    assert violations_in(tree, "session.py")
