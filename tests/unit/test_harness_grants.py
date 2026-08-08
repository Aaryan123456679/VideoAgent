"""The `GRANTS` table matches `harness.md` §3.2's node list and capability names. Not exhaustive."""

from __future__ import annotations

from video_agent.harness.grants import GRANTS

_EXPECTED_NODES = {
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


def test_grants_table_covers_every_graph_node() -> None:
    assert set(GRANTS) == _EXPECTED_NODES


def test_grantless_nodes_are_exactly_select_and_finalize() -> None:
    grantless = {node for node, tools in GRANTS.items() if not tools}
    assert grantless == {"select_next_shot", "finalize"}


def test_tool_names_are_capabilities_not_providers() -> None:
    all_tools = {tool for tools in GRANTS.values() for tool in tools}
    for tool in all_tools:
        assert "magichour" not in tool
        assert "higgsfield" not in tool
