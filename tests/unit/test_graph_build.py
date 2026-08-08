"""`graph.md` §9's topology-level test strategy: node/edge snapshot, one cycle, guard-first."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from video_agent.graph import nodes
from video_agent.graph.build import build_graph
from video_agent.graph.deps import GraphDeps
from video_agent.graph.guard import JobHarness

EXPECTED_NODES = {
    "plan_story",
    "lock_bible",
    "select_next_shot",
    "generate_shot",
    "extract_final_frame",
    "qc_shot",
    "assemble",
    "deliver",
    "finalize",
}

_ROUTERS = (
    nodes.route_after_plan,
    nodes.route_after_bible,
    nodes.route_select,
    nodes.route_after_generate,
    nodes.route_after_frame,
    nodes.route_after_qc,
    nodes.route_after_assemble,
)


def a_deps() -> GraphDeps:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    return GraphDeps(
        engine=cast(Any, None),
        gateway=cast(Any, None),
        checkpointer=InMemorySaver(),
        harness=JobHarness(job_id=uuid4(), shots_required=4),
        now=lambda: now,
        providers=cast(Any, None),
        artifacts=cast(Any, None),
    )


def test_compiled_graph_has_exactly_the_nine_documented_nodes() -> None:
    compiled = build_graph(a_deps())
    node_names = set(compiled.get_graph().nodes) - {"__start__", "__end__"}
    assert node_names == EXPECTED_NODES


def test_every_cycle_closes_at_qc_shot_and_nowhere_else() -> None:
    """DFS back-edge detection: an edge to a node still on the recursion stack is a back edge.

    This topology has two, not one: `qc_shot -> select_next_shot` closes the per-shot loop that
    processes all four shots, and `qc_shot -> generate_shot` is `graph.md` §3.4's repair
    back-edge. Both close at `qc_shot` — its routing decision (accept-and-advance vs. repair)
    is the only place this graph loops, which is the property worth asserting: no *other* node
    ever routes back to something already on the call stack.
    """
    compiled = build_graph(a_deps())
    graph = compiled.get_graph()
    edges_by_source: dict[str, set[str]] = {}
    for edge in graph.edges:
        edges_by_source.setdefault(edge.source, set()).add(edge.target)

    visited: set[str] = set()
    on_stack: set[str] = set()
    back_edges: set[tuple[str, str]] = set()

    def visit(node: str) -> None:
        visited.add(node)
        on_stack.add(node)
        for target in edges_by_source.get(node, ()):
            if target in on_stack:
                back_edges.add((node, target))
            elif target not in visited:
                visit(target)
        on_stack.discard(node)

    visit("__start__")
    assert back_edges == {("qc_shot", "select_next_shot"), ("qc_shot", "generate_shot")}


def test_no_send_or_map_reduce_fan_out_is_used() -> None:
    source = inspect.getsource(nodes) + inspect.getsource(build_graph)
    assert "Send(" not in source
    assert ".map(" not in source


def test_every_router_calls_guard_first() -> None:
    """Reflective coverage: `graph.md` §3.1 requires every router to call `guard` as its first
    statement. A router that checks anything else before the veto could act on stale state.
    """
    for router in _ROUTERS:
        source = inspect.getsource(router)
        body_lines = [
            line.strip()
            for line in source.splitlines()[1:]
            if line.strip() and not line.strip().startswith(('"""', "#"))
        ]
        assert body_lines, f"{router.__name__} has no body"
        assert "await guard(" in body_lines[0], (
            f"{router.__name__}'s first statement does not call guard(): {body_lines[0]!r}"
        )
