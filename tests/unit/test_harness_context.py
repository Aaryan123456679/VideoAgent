"""Tests for `NodeContext.for_node` grant resolution and bible verification. `harness.md` §3.1."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from video_agent.harness.budget import BudgetView
from video_agent.harness.context import NodeContext
from video_agent.harness.errors import BibleHashMismatchError, UngrantedToolError, UnknownToolError
from video_agent.planning.bible import compute_content_hash
from video_agent.planning.models import (
    CharacterSpec,
    ContinuityBible,
    LensLanguageSpec,
    LightingSpec,
    LocationSpec,
    PaletteSpec,
    WardrobeSpec,
)


def _budget_view() -> BudgetView:
    return BudgetView(
        usd_remaining=Decimal("5.00"),
        tokens_remaining=250_000,
        wall_clock_s_remaining=1200.0,
        iterations_remaining=40,
    )


def _bible() -> ContinuityBible:
    provisional = ContinuityBible(
        job_id=uuid4(),
        character=CharacterSpec(
            name="Mira", age_appearance="mid-30s", build="athletic", skin_tone="olive",
            hair="short black bob", facial_features="sharp jawline",
            distinguishing_marks="anchor tattoo",
        ),
        wardrobe=WardrobeSpec(
            garments=["jacket"], colours=["navy"], materials=["canvas"], condition="worn"
        ),
        location=LocationSpec(
            setting="rooftop",
            time_of_day="dusk",
            architecture_or_terrain="concrete",
            key_props=["tank"],
        ),
        lighting=LightingSpec(
            key_light="sun",
            direction="left",
            quality="hard",
            colour_temperature="3200K",
            contrast_ratio="4:1",
        ),
        palette=PaletteSpec(
            dominant=["#1c2b3a", "#e08030"], accent=["#ffffff"], saturation="muted",
            grade="teal-orange",
        ),
        lens_language=LensLanguageSpec(
            focal_length="35mm", aperture_feel="shallow", framing="medium",
            movement_style="handheld",
        ),
        negative_constraints=["no additional characters"],
        content_hash="pending",
        locked_at=datetime.now(UTC),
        model_alias="reasoning-high",
        prompt_version="v1",
    )
    return provisional.model_copy(update={"content_hash": compute_content_hash(provisional)})


def test_for_node_resolves_grants_from_table() -> None:
    ctx = NodeContext.for_node(
        job_id=uuid4(), node="generate_shot", trace_id="t-1", budget_remaining=_budget_view()
    )
    assert ctx.tools == {"llm.reasoning_fast", "video.generate", "artifact.write"}


def test_for_node_allows_grant_less_nodes() -> None:
    ctx = NodeContext.for_node(
        job_id=uuid4(), node="select_next_shot", trace_id="t-1", budget_remaining=_budget_view()
    )
    assert ctx.tools == frozenset()


def test_for_node_rejects_unknown_node() -> None:
    with pytest.raises(UnknownToolError):
        NodeContext.for_node(
            job_id=uuid4(), node="not_a_real_node", trace_id="t-1", budget_remaining=_budget_view()
        )


def test_for_node_rejects_corrupted_bible() -> None:
    corrupted = _bible().model_copy(update={"content_hash": "deadbeef"})
    with pytest.raises(BibleHashMismatchError):
        NodeContext.for_node(
            job_id=uuid4(),
            node="generate_shot",
            trace_id="t-1",
            budget_remaining=_budget_view(),
            bible=corrupted,
        )


def test_require_tool_raises_when_ungranted() -> None:
    ctx = NodeContext.for_node(
        job_id=uuid4(), node="finalize", trace_id="t-1", budget_remaining=_budget_view()
    )
    with pytest.raises(UngrantedToolError):
        ctx.require_tool("video.generate")
