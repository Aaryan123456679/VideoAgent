"""`build_graph(deps)` — the compiled topology. `graph.md` §3.

Sequential only: the repair back-edge `qc_shot -> generate_shot` is the **only** cycle, there is
no `Send`/map-reduce fan-out anywhere, and `test_graph_build.py` asserts both over the compiled
graph rather than trusting this module to keep them true by convention.

Every node/router below is a named closure over `deps`, spelled out individually rather than
built through a generic binder helper: langgraph's `add_node`/`add_conditional_edges` overloads
match a plain closure defined this way against their `_Node` protocol, but not one returned from
a helper generic over `Callable[[JobState, GraphDeps], ...]` — a real `mypy --strict` limitation
against this version's generic-protocol overloads, confirmed by minimal repro, not a style choice.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from video_agent.graph import nodes
from video_agent.graph.deps import GraphDeps
from video_agent.graph.state import JobState

__all__ = ["build_graph"]


def build_graph(deps: GraphDeps) -> CompiledStateGraph[JobState, None, JobState, JobState]:
    """Wire all nine nodes and their routers, entry at `plan_story`. `graph.md` §3."""

    async def plan_story_node(state: JobState) -> dict[str, Any]:
        return await nodes.plan_story_node(state, deps)

    async def lock_bible_node(state: JobState) -> dict[str, Any]:
        return await nodes.lock_bible_node(state, deps)

    async def select_next_shot_node(state: JobState) -> dict[str, Any]:
        return await nodes.select_next_shot_node(state, deps)

    async def generate_shot_node(state: JobState) -> dict[str, Any]:
        return await nodes.generate_shot_node(state, deps)

    async def extract_final_frame_node(state: JobState) -> dict[str, Any]:
        return await nodes.extract_final_frame_node(state, deps)

    async def qc_shot_node(state: JobState) -> dict[str, Any]:
        return await nodes.qc_shot_node(state, deps)

    async def assemble_node(state: JobState) -> dict[str, Any]:
        return await nodes.assemble_node(state, deps)

    async def deliver_node(state: JobState) -> dict[str, Any]:
        return await nodes.deliver_node(state, deps)

    async def finalize_node(state: JobState) -> dict[str, Any]:
        return await nodes.finalize_node(state, deps)

    async def route_after_plan(state: JobState) -> str:
        return await nodes.route_after_plan(state, deps)

    async def route_after_bible(state: JobState) -> str:
        return await nodes.route_after_bible(state, deps)

    async def route_select(state: JobState) -> str:
        return await nodes.route_select(state, deps)

    async def route_after_generate(state: JobState) -> str:
        return await nodes.route_after_generate(state, deps)

    async def route_after_frame(state: JobState) -> str:
        return await nodes.route_after_frame(state, deps)

    async def route_after_qc(state: JobState) -> str:
        return await nodes.route_after_qc(state, deps)

    async def route_after_assemble(state: JobState) -> str:
        return await nodes.route_after_assemble(state, deps)

    g: StateGraph[JobState, None, JobState, JobState] = StateGraph(JobState)

    g.add_node("plan_story", plan_story_node)
    g.add_node("lock_bible", lock_bible_node)
    g.add_node("select_next_shot", select_next_shot_node)
    g.add_node("generate_shot", generate_shot_node)
    g.add_node("extract_final_frame", extract_final_frame_node)
    g.add_node("qc_shot", qc_shot_node)
    g.add_node("assemble", assemble_node)
    g.add_node("deliver", deliver_node)
    g.add_node("finalize", finalize_node)

    g.set_entry_point("plan_story")

    # Explicit path maps, not just router functions: without one, langgraph cannot know a
    # conditional edge's possible targets ahead of running it, and `.get_graph()` — the
    # topology snapshot `graph.md` §9 tests against — renders nothing for that edge at all.
    g.add_conditional_edges(
        "plan_story", route_after_plan, {"lock_bible": "lock_bible", "finalize": "finalize"}
    )
    g.add_conditional_edges(
        "lock_bible",
        route_after_bible,
        {"select_next_shot": "select_next_shot", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "select_next_shot",
        route_select,
        {"generate_shot": "generate_shot", "assemble": "assemble", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "generate_shot",
        route_after_generate,
        {"extract_final_frame": "extract_final_frame", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "extract_final_frame", route_after_frame, {"qc_shot": "qc_shot", "finalize": "finalize"}
    )
    g.add_conditional_edges(
        "qc_shot",
        route_after_qc,
        {
            "select_next_shot": "select_next_shot",
            "generate_shot": "generate_shot",
            "finalize": "finalize",
        },
    )
    g.add_conditional_edges(
        "assemble", route_after_assemble, {"deliver": "deliver", "finalize": "finalize"}
    )
    g.add_edge("deliver", "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=deps.checkpointer)
