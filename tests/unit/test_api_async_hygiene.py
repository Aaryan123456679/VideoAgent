"""`S0.4.3` — no blocking call is reachable from anything on the request path.

`api.md` §9 and `[CPS §Canonical stack]`: every handler is `async def` and performs no blocking
I/O. The failure this guards against is not a handler that calls `subprocess.run` — nobody
writes that. It is the third link in a chain: an `async` handler awaits an `async` helper which
calls a small synchronous utility which, two releases later, grows an `ffmpeg` call. Every step
looks fine on its own, and the event loop stalls for the length of a render.

So the gate is a **reachability walk over the call graph**, not a scan of handler bodies. It
starts from every registered endpoint *and every dependency FastAPI resolves for it* — a
blocking call in `require_tenant` is on the request path exactly as much as one in the handler,
and a walk that started at endpoints alone would miss it.

**What it can and cannot see.** It is static and it resolves names, not values: a call through
a variable, a `getattr`, or an injected callable is invisible to it. That is stated rather than
papered over — the gate raises the cost of the common mistake, it does not prove the absence of
blocking I/O. What it does prove is that no *named* path from a route reaches the banned set,
and it reports the whole path when one does, because "some handler is blocking" is not
actionable.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Route

from tests.unit.test_api_support import build_app

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterator

    from fastapi.dependencies.models import Dependant

SOURCE_ROOT: Final = Path(__file__).resolve().parents[2] / "src"
PACKAGE: Final = "video_agent"

BANNED_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("subprocess", "*"),
        ("requests", "*"),
        ("psycopg2", "*"),
        ("psycopg2.extras", "*"),
        ("time", "sleep"),
        ("os", "system"),
        ("socket", "create_connection"),
        ("urllib.request", "*"),
        # ffmpeg runs in a worker, never in a request path `[assembly.md §6]`. The wrapper is
        # banned as a whole rather than by function name: every entry point it grows is a
        # subprocess call.
        ("video_agent.assembly.media_toolchain", "*"),
    }
)
"""`*` means every attribute of the module. Named individually where the module is otherwise
fine — `time` is harmless until someone sleeps on the event loop with it."""

MAX_WALK_DEPTH: Final = 40
"""A ceiling on the walk. Recursion in the graph is handled by the visited set; this is a
guard against a pathological graph turning a test into a hang."""

EXPECTED_FRAMES: Final = 3


@dataclass(frozen=True, slots=True)
class Target:
    """One function, identified the way a static walk can identify it: module and name."""

    module: str
    name: str

    def __str__(self) -> str:
        return f"{self.module}.{self.name}"


@dataclass(frozen=True, slots=True)
class BlockingPath:
    """A reachable blocking call and the chain of frames that reaches it."""

    frames: tuple[Target, ...]
    call: str

    def __str__(self) -> str:
        chain = " -> ".join(str(frame) for frame in self.frames)
        return f"{chain} -> {self.call}"


def _dotted(node: ast.expr) -> str | None:
    """`a.b.c` as a string, or `None` when the expression is not a plain dotted name."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


class CallGraph:
    """A name-resolved call graph over one source tree."""

    def __init__(self, root: Path, package: str) -> None:
        """Parse every module under `root`; `package` marks which modules count as internal."""
        self.package = package
        self._functions: dict[tuple[str, str], list[ast.AST]] = {}
        self._imports: dict[str, dict[str, tuple[str, str | None]]] = {}
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            module = self._module_name(path, root)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            self._index(module, tree)

    @staticmethod
    def _module_name(path: Path, root: Path) -> str:
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def _index(self, module: str, tree: ast.Module) -> None:
        imports = self._imports.setdefault(module, {})
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = (alias.name, None)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    imports[alias.asname or alias.name] = (node.module, alias.name)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self._functions.setdefault((module, node.name), []).append(node)

    def _resolve(self, module: str, call: ast.Call) -> tuple[Target | None, str | None]:
        """What `call` refers to: an internal target, a banned call, or neither."""
        dotted = _dotted(call.func)
        if dotted is None:
            return None, None
        head, _, attribute = dotted.partition(".")
        if not attribute:
            return self._resolve_bare(module, head)
        if "." in attribute:
            return None, None
        return self._resolve_dotted(module, head, attribute)

    def _resolve_bare(self, module: str, name: str) -> tuple[Target | None, str | None]:
        """`name(...)` — an imported function, or one defined in this module."""
        imported = self._imports.get(module, {}).get(name)
        if imported is not None:
            target_module, imported_name = imported
            if imported_name is None:
                return None, None
            return self._classify(target_module, imported_name)
        if (module, name) in self._functions:
            return Target(module, name), None
        return None, None

    def _resolve_dotted(
        self, module: str, head: str, attribute: str
    ) -> tuple[Target | None, str | None]:
        """`head.attribute(...)` — a module alias, an imported submodule, or `self`."""
        imported = self._imports.get(module, {}).get(head)
        if imported is not None:
            target_module, imported_name = imported
            resolved = (
                target_module if imported_name is None else f"{target_module}.{imported_name}"
            )
            return self._classify(resolved, attribute)
        if head == "self" and (module, attribute) in self._functions:
            return Target(module, attribute), None
        return None, None

    def _classify(self, module: str, attribute: str) -> tuple[Target | None, str | None]:
        if (module, attribute) in BANNED_CALLS or (module, "*") in BANNED_CALLS:
            return None, f"{module}.{attribute}"
        if module == self.package or module.startswith(f"{self.package}."):
            return Target(module, attribute), None
        return None, None

    def _calls(self, module: str, node: ast.AST) -> Iterator[tuple[Target | None, str | None]]:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                yield self._resolve(module, child)

    def blocking_paths(self, roots: list[Target]) -> list[BlockingPath]:
        """Every banned call reachable from `roots`, with the frame chain that reaches it."""
        found: list[BlockingPath] = []
        visited: set[Target] = set()
        stack: list[tuple[Target, tuple[Target, ...]]] = [(root, (root,)) for root in roots]
        while stack:
            target, chain = stack.pop()
            if target in visited or len(chain) > MAX_WALK_DEPTH:
                continue
            visited.add(target)
            for node in self._functions.get((target.module, target.name), []):
                for callee, banned in self._calls(target.module, node):
                    if banned is not None:
                        found.append(BlockingPath(frames=chain, call=banned))
                    elif callee is not None and callee not in visited:
                        stack.append((callee, (*chain, callee)))
        return found


def iter_routes(target: object, seen: set[int] | None = None) -> Iterator[APIRoute | Route]:
    """Every registered route, however deeply nested.

    `app.routes` is not the list it used to be: FastAPI 0.141 keeps an included router as a
    single `_IncludedRouter` entry rather than flattening its routes into the application.
    A gate that iterated `app.routes` and filtered on `APIRoute` would therefore inspect
    **nothing** and pass — which is exactly what the first version of this file did, and why
    `test_the_route_walker_finds_the_registered_routes` exists below.
    """
    visited = set() if seen is None else seen
    if id(target) in visited:
        return
    visited.add(id(target))
    nested = getattr(target, "original_router", None)
    if nested is not None:
        yield from iter_routes(nested, visited)
        return
    for route in getattr(target, "routes", ()):
        if isinstance(route, APIRoute | Route):
            yield route
        else:
            yield from iter_routes(route, visited)


def _route_callables(route: APIRoute | Route) -> Iterator[Callable[..., object]]:
    """The endpoint and, for an `APIRoute`, every dependency FastAPI resolves for it."""
    if isinstance(route, APIRoute):
        yield from _dependant_callables(route.dependant)
    else:
        yield route.endpoint


def route_targets(app: FastAPI, package: str) -> list[Target]:
    """Every endpoint and dependency on the request path, restricted to `package`.

    Dependencies are included because they run on the request path. A gate that walked only
    endpoint bodies would pass an application whose authentication dependency shelled out.
    """
    targets: list[Target] = []
    for route in iter_routes(app):
        for function in _route_callables(route):
            module = getattr(function, "__module__", "") or ""
            if module == package or module.startswith(f"{package}."):
                targets.append(Target(module, function.__name__))
    return targets


def _dependant_callables(dependant: Dependant) -> Iterator[Callable[..., object]]:
    if dependant.call is not None:
        yield dependant.call
    for sub in dependant.dependencies:
        yield from _dependant_callables(sub)


def synchronous_handlers(app: FastAPI) -> list[str]:
    """Every registered endpoint or dependency that is not `async`.

    `async def` and `async def ... yield` both qualify; a plain `def` does not. FastAPI would
    run it in a threadpool, which is not wrong for a pure function but is exactly how a
    blocking database driver ends up on the request path without anyone deciding to put it
    there.
    """
    offenders: list[str] = []
    for route in iter_routes(app):
        for function in _route_callables(route):
            if inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function):
                continue
            offenders.append(f"{route.path} -> {getattr(function, '__qualname__', function)}")
    return offenders


@pytest.fixture(scope="module")
def source_graph() -> CallGraph:
    """The call graph of the shipped package, parsed once."""
    return CallGraph(SOURCE_ROOT, PACKAGE)


# --- The gate itself -------------------------------------------------------------------------


def test_the_route_walker_finds_the_registered_routes() -> None:
    """The guard against a vacuous gate.

    Every assertion below is of the form "no violations found". If the walker returned nothing,
    all of them would pass while inspecting an empty application — which is the failure mode
    this whole file exists to avoid elsewhere.
    """
    app = build_app(probes=False)

    paths = {route.path for route in iter_routes(app)}
    targets = {str(target) for target in route_targets(app, PACKAGE)}

    assert {"/healthz", "/readyz"} <= paths
    assert "video_agent.api.health.healthz" in targets
    assert "video_agent.api.health.readyz" in targets


def test_clean_app_passes(source_graph: CallGraph) -> None:
    """The real application reaches nothing in the banned set."""
    app = build_app(probes=False)

    paths = source_graph.blocking_paths(route_targets(app, PACKAGE))

    assert paths == [], "blocking calls reachable from a route:\n" + "\n".join(map(str, paths))


def test_every_registered_handler_is_async() -> None:
    """Acceptance 1, on the application as it actually ships."""
    app = build_app(probes=False)

    assert synchronous_handlers(app) == []


def test_ffmpeg_unreachable_from_routes(source_graph: CallGraph) -> None:
    """`assembly.md` §6: ffmpeg runs in a worker, never in a request path.

    Asserted specifically rather than trusting the clean-app test, because the interesting
    regression is a route importing the toolchain wrapper for "just a probe".
    """
    app = build_app(probes=False)

    reached = [
        path
        for path in source_graph.blocking_paths(route_targets(app, PACKAGE))
        if "media_toolchain" in path.call
    ]

    assert reached == []


def test_the_gate_covers_dependencies_and_not_just_endpoints(source_graph: CallGraph) -> None:
    """The walk's roots include what FastAPI injects, or the gate has a hole the size of auth."""
    app = build_app(probes=True)

    roots = {str(target) for target in route_targets(app, PACKAGE)}

    assert "video_agent.api.principal.require_tenant" in roots
    assert "video_agent.api.database.tenant_session" in roots
    assert source_graph.package == PACKAGE


# --- The gate detects what it claims to detect ------------------------------------------------


def test_sync_handler_rejected() -> None:
    """A `def` endpoint fails the gate. Without this the async check could be a no-op."""
    app = FastAPI()

    @app.get("/sync")
    def handler() -> dict[str, str]:
        return {}

    offenders = synchronous_handlers(app)

    assert len(offenders) == 1
    assert "handler" in offenders[0]


def test_sync_dependency_rejected() -> None:
    """The same for an injected dependency, which runs on the same request path."""
    app = FastAPI()

    def dependency() -> str:
        return "value"

    @app.get("/mixed")
    async def handler(value: str = Depends(dependency)) -> dict[str, str]:
        return {"value": value}

    offenders = synchronous_handlers(app)

    assert any("dependency" in offender for offender in offenders)


def _write_probe_package(root: Path, *, blocking: bool) -> None:
    """A three-frame call chain, optionally ending in `subprocess.run`."""
    package = root / "probe_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    tail = "subprocess.run(['ffmpeg'])" if blocking else "return None"
    (package / "tools.py").write_text(
        f"import subprocess\n\n\ndef blocking_helper():\n    {tail}\n",
        encoding="utf-8",
    )
    (package / "handlers.py").write_text(
        "from probe_pkg.tools import blocking_helper\n"
        "\n"
        "\n"
        "async def middle():\n"
        "    blocking_helper()\n"
        "\n"
        "\n"
        "async def handler():\n"
        "    await middle()\n",
        encoding="utf-8",
    )


def test_transitive_blocking_call_detected(tmp_path: Path) -> None:
    """An async handler, an async helper, and a sync utility that shells out.

    The reported path must name all three frames: "something under here blocks" is not a
    finding anyone can act on.
    """
    _write_probe_package(tmp_path, blocking=True)
    graph = CallGraph(tmp_path, "probe_pkg")

    paths = graph.blocking_paths([Target("probe_pkg.handlers", "handler")])

    assert len(paths) == 1
    assert paths[0].call == "subprocess.run"
    assert [str(frame) for frame in paths[0].frames] == [
        "probe_pkg.handlers.handler",
        "probe_pkg.handlers.middle",
        "probe_pkg.tools.blocking_helper",
    ]
    assert len(paths[0].frames) == EXPECTED_FRAMES


def test_the_same_chain_without_the_blocking_call_passes(tmp_path: Path) -> None:
    """The control. A walker that flagged every chain would pass the test above too."""
    _write_probe_package(tmp_path, blocking=False)
    graph = CallGraph(tmp_path, "probe_pkg")

    assert graph.blocking_paths([Target("probe_pkg.handlers", "handler")]) == []


@pytest.mark.parametrize(
    ("import_line", "call"),
    [
        ("import subprocess", "subprocess.run(['ls'])"),
        ("import subprocess as sp", "sp.run(['ls'])"),
        ("from subprocess import run", "run(['ls'])"),
        ("from subprocess import run as spawn", "spawn(['ls'])"),
        ("import time", "time.sleep(1)"),
        ("from time import sleep", "sleep(1)"),
    ],
    ids=["module", "aliased-module", "from", "aliased-from", "time", "from-time"],
)
def test_every_spelling_of_a_banned_call_is_caught(
    tmp_path: Path, import_line: str, call: str
) -> None:
    """Six ways to write the same blocking call; a gate that caught one would be theatre."""
    package = tmp_path / "probe_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "handlers.py").write_text(
        f"{import_line}\n\n\nasync def handler():\n    {call}\n", encoding="utf-8"
    )
    graph = CallGraph(tmp_path, "probe_pkg")

    paths = graph.blocking_paths([Target("probe_pkg.handlers", "handler")])

    assert len(paths) == 1


def test_an_unrelated_call_is_not_flagged(tmp_path: Path) -> None:
    """`asyncio.to_thread` is the sanctioned way to run blocking work, and must stay usable."""
    package = tmp_path / "probe_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "handlers.py").write_text(
        "import asyncio\n\n\nasync def handler():\n    await asyncio.to_thread(len, [1])\n",
        encoding="utf-8",
    )
    graph = CallGraph(tmp_path, "probe_pkg")

    assert graph.blocking_paths([Target("probe_pkg.handlers", "handler")]) == []
