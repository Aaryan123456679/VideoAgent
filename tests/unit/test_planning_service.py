"""Tests for `plan_story`/`lock_bible` against a scripted fake gateway. `planning.md` §3.1, §3.2."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from video_agent.config.aliases import Alias
from video_agent.gateway.models import AliasHealth, CallContext, LLMRequest, LLMResponse, Usage
from video_agent.harness.budget import BudgetView
from video_agent.harness.context import NodeContext
from video_agent.planning.errors import BibleTooVagueError, PlanInvalidError
from video_agent.planning.models import (
    TOTAL_DURATION_S,
    Beat,
    BeatKind,
    CameraMove,
    CharacterSpec,
    LensLanguageSpec,
    LightingSpec,
    LocationSpec,
    PaletteSpec,
    StoryPlan,
    WardrobeSpec,
)
from video_agent.planning.service import _BibleDraft, _PlanDraft, lock_bible, plan_story


def _beat(index: int, kind: BeatKind) -> Beat:
    return Beat(
        index=index, kind=kind, action="a" * 25, camera_move=CameraMove.STATIC, duration_s=10.0
    )


def _valid_beats() -> list[Beat]:
    return [
        _beat(0, BeatKind.SETUP),
        _beat(1, BeatKind.DEVELOPMENT),
        _beat(2, BeatKind.TURN),
        _beat(3, BeatKind.RESOLUTION),
    ]


def _budget_view() -> BudgetView:
    return BudgetView(
        usd_remaining=Decimal("5.00"),
        tokens_remaining=250_000,
        wall_clock_s_remaining=1200.0,
        iterations_remaining=40,
    )


def _ctx(node: str) -> NodeContext:
    return NodeContext.for_node(
        job_id=uuid4(), node=node, trace_id="t-1", budget_remaining=_budget_view()
    )


def _plan(logline: str = "x") -> StoryPlan:
    return StoryPlan(
        job_id=uuid4(),
        logline=logline,
        beats=_valid_beats(),
        model_alias="reasoning-high",
        prompt_version="v1",
        created_at=datetime.now(UTC),
    )


def _response(parsed: _PlanDraft | _BibleDraft) -> LLMResponse:
    return LLMResponse(
        parsed=parsed,
        text="{}",
        model_used="fake-model",
        alias=Alias.REASONING_HIGH,
        prompt_version="v1",
        usage=Usage(input_tokens=10, output_tokens=10, cost_usd=Decimal("0.01")),
        latency_ms=5,
        generation_id="gen-1",
    )


class _FakeGateway:
    """Returns each scripted response in order; records every call it served."""

    def __init__(self, responses: list[_PlanDraft | _BibleDraft]) -> None:
        self._responses: Iterator[_PlanDraft | _BibleDraft] = iter(responses)
        self.calls: list[tuple[LLMRequest, CallContext]] = []

    async def call(self, req: LLMRequest, *, ctx: CallContext) -> LLMResponse:
        self.calls.append((req, ctx))
        return _response(next(self._responses))

    async def health(self, alias: Alias) -> AliasHealth:  # pragma: no cover - unused
        raise NotImplementedError


def _character(**overrides: str) -> CharacterSpec:
    defaults: dict[str, str] = {
        "name": "Mira",
        "age_appearance": "mid-30s",
        "build": "athletic",
        "skin_tone": "olive",
        "hair": "short black bob",
        "facial_features": "sharp jawline",
        "distinguishing_marks": "anchor tattoo",
    }
    defaults.update(overrides)
    return CharacterSpec(**defaults)


def _bible_draft(**character_overrides: str) -> _BibleDraft:
    return _BibleDraft(
        character=_character(**character_overrides),
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
        negative_constraints=["no additional characters enter frame"],
    )


@pytest.mark.asyncio
async def test_plan_story_happy_path() -> None:
    draft = _PlanDraft(logline="A quiet heist unravels at dusk.", beats=_valid_beats())
    plan = await plan_story(
        "a heist story", ctx=_ctx("plan_story"), gateway=_FakeGateway([draft])
    )
    assert isinstance(plan, StoryPlan)
    assert plan.total_duration_s == TOTAL_DURATION_S


@pytest.mark.asyncio
async def test_plan_story_reasks_once_then_succeeds() -> None:
    bad_beats = _valid_beats()
    bad_beats[0], bad_beats[1] = bad_beats[1], bad_beats[0]
    bad = _PlanDraft(logline="bad", beats=bad_beats)
    good = _PlanDraft(logline="good logline", beats=_valid_beats())
    plan = await plan_story("a story", ctx=_ctx("plan_story"), gateway=_FakeGateway([bad, good]))
    assert plan.logline == "good logline"


@pytest.mark.asyncio
async def test_plan_story_fails_after_two_bad_drafts() -> None:
    bad_beats = _valid_beats()
    bad_beats[0], bad_beats[1] = bad_beats[1], bad_beats[0]
    bad = _PlanDraft(logline="bad", beats=bad_beats)
    with pytest.raises(PlanInvalidError):
        await plan_story("a story", ctx=_ctx("plan_story"), gateway=_FakeGateway([bad, bad]))


@pytest.mark.asyncio
async def test_lock_bible_happy_path() -> None:
    draft = _bible_draft()
    bible = await lock_bible(
        _plan(), "subject matter", ctx=_ctx("lock_bible"), gateway=_FakeGateway([draft])
    )
    assert bible.content_hash != "pending"


@pytest.mark.asyncio
async def test_lock_bible_fails_after_two_vague_drafts() -> None:
    vague = _bible_draft(facial_features="maybe some scar or other mark")
    with pytest.raises(BibleTooVagueError):
        await lock_bible(
            _plan(), "subject matter", ctx=_ctx("lock_bible"), gateway=_FakeGateway([vague, vague])
        )


@pytest.mark.asyncio
async def test_a_bare_or_in_an_enumeration_is_not_treated_as_vague() -> None:
    """A real model's negative-constraints list reads "no text, logos, or captions" — an
    ordinary enumeration, not hedging. `or` alone must never fail the specificity gate."""
    draft = _bible_draft(facial_features="a scar over the left eyebrow or cheekbone, faded")
    bible = await lock_bible(
        _plan(), "subject matter", ctx=_ctx("lock_bible"), gateway=_FakeGateway([draft])
    )
    assert bible.content_hash != "pending"
