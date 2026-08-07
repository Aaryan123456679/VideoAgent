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

**One live exemption, and it is asserted.** `PENDING_CONSOLIDATION` names two `T0.4` modules
that build their own engine and their own tenant scope — a duplicate of
`video_agent.persistence.session`, written concurrently. The list is checked in both
directions: it may not grow, and an entry that has *stopped* violating must be removed. So the
exemption expires by itself the moment the duplication is consolidated, rather than becoming a
permanent hole that a third module can be added to.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

PERSISTENCE_PACKAGE = "src/video_agent/persistence"

PENDING_CONSOLIDATION: dict[str, str] = {
    "src/video_agent/api/clients.py": (
        "T0.4 builds its own AsyncEngine instead of calling "
        "video_agent.persistence.create_database_engine."
    ),
    "src/video_agent/api/database.py": (
        "T0.4 reimplements the tenant-scoped transaction that "
        "video_agent.persistence.session.tenant_session already provides, including its own "
        "copy of the `app.tenant_id` constant and its own set_config call."
    ),
}
"""Modules that violate the boundary today, each with the reason and the owning task.

Not a permissive pattern and not a directory: two exact paths, so nothing else can inherit the
exemption by living next to them.
"""

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


def test_no_unexempted_module_outside_persistence_opens_a_session(repo_root: Path) -> None:
    """A new module that builds its own engine cannot merge."""
    offenders = _module_violations(repo_root)
    unexpected = [
        line
        for path, lines in offenders.items()
        if path not in PENDING_CONSOLIDATION
        for line in lines
    ]
    assert unexpected == [], "\n".join(unexpected)


def test_the_exemption_list_is_exactly_the_two_known_modules() -> None:
    """It may not grow silently. `[persistence.md §10]` applies the same rule to RLS."""
    assert set(PENDING_CONSOLIDATION) == {
        "src/video_agent/api/clients.py",
        "src/video_agent/api/database.py",
    }


@pytest.mark.parametrize("path", sorted(PENDING_CONSOLIDATION))
def test_each_exempted_module_still_actually_violates(repo_root: Path, path: str) -> None:
    """The exemption expires by itself.

    If `T0.4` consolidates onto `video_agent.persistence`, this fails and the entry has to be
    deleted — which is the opposite of the usual exemption, where the entry outlives the reason
    for it and nobody notices.
    """
    source = repo_root / path
    assert source.is_file(), f"{path} is exempted but does not exist; delete the entry"
    assert violations_in(ast.parse(source.read_text(encoding="utf-8")), path), (
        f"{path} no longer opens its own session; remove it from PENDING_CONSOLIDATION"
    )


@pytest.mark.parametrize("path", sorted(PENDING_CONSOLIDATION))
def test_each_exemption_carries_a_reason(path: str) -> None:
    assert PENDING_CONSOLIDATION[path].strip()


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
