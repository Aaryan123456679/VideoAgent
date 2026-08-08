"""`graph.md` §2's checkpoint-time invariants — non-exhaustive, one case per row of the table."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from video_agent.graph.state import (
    GraphInvariantError,
    JobState,
    ShotCountInvariantError,
    ShotState,
    assert_invariants,
)
from video_agent.harness.budget import BudgetCaps, BudgetLedger, Charge
from video_agent.harness.errors import BibleHashMismatchError
from video_agent.harness.outcomes import Outcome
from video_agent.persistence.enums import BeatKind, ShotStatus
from video_agent.planning.bible import compute_content_hash
from video_agent.planning.models import (
    Beat,
    CameraMove,
    CharacterSpec,
    ContinuityBible,
    LensLanguageSpec,
    LightingSpec,
    LocationSpec,
    PaletteSpec,
    StoryPlan,
    WardrobeSpec,
)
from video_agent.planning.models import BeatKind as PlanBeatKind

NOW = datetime(2026, 8, 8, tzinfo=UTC)


def ledger() -> BudgetLedger:
    caps = BudgetCaps(
        max_iterations=10, max_wall_clock_s=3600, max_tokens=10_000, max_usd=Decimal(10)
    )
    return BudgetLedger(caps=caps, started_at=NOW)


def base_job(**overrides: object) -> JobState:
    fields: dict[str, object] = {
        "job_id": uuid4(),
        "tenant_id": uuid4(),
        "trace_id": "trace-1",
        "prompt": "a short film about a lighthouse",
        "budget": ledger(),
    }
    fields.update(overrides)
    return JobState(**fields)  # type: ignore[arg-type]


def four_shots() -> tuple[ShotState, ...]:
    kinds = (BeatKind.SETUP, BeatKind.DEVELOPMENT, BeatKind.TURN, BeatKind.RESOLUTION)
    return tuple(ShotState(index=i, beat_kind=kind) for i, kind in enumerate(kinds))


def a_bible(job_id: object) -> ContinuityBible:
    provisional = ContinuityBible(
        job_id=job_id,  # type: ignore[arg-type]
        character=CharacterSpec(
            name="Mara",
            age_appearance="thirties",
            build="lean",
            skin_tone="olive",
            hair="short black hair",
            facial_features="sharp jaw, freckles across the nose",
        ),
        wardrobe=WardrobeSpec(
            garments=["oilskin coat"], colours=["yellow"], materials=["canvas"], condition="worn"
        ),
        location=LocationSpec(
            setting="lighthouse", time_of_day="dusk", architecture_or_terrain="cliff"
        ),
        lighting=LightingSpec(
            key_light="lamp", direction="side", quality="hard", colour_temperature="warm",
            contrast_ratio="high",
        ),
        palette=PaletteSpec(dominant=["grey", "amber"], saturation="low", grade="cool"),
        lens_language=LensLanguageSpec(
            focal_length="35mm",
            aperture_feel="shallow",
            framing="medium",
            movement_style="handheld",
        ),
        content_hash="pending",
        locked_at=NOW,
        model_alias="reasoning-high",
        prompt_version="v1",
    )
    return provisional.model_copy(update={"content_hash": compute_content_hash(provisional)})


def a_story_plan(job_id: object) -> StoryPlan:
    beats = tuple(
        Beat(
            index=i,
            kind=kind,
            action="a" * 20,
            camera_move=CameraMove.STATIC,
        )
        for i, kind in enumerate(
            (
                PlanBeatKind.SETUP,
                PlanBeatKind.DEVELOPMENT,
                PlanBeatKind.TURN,
                PlanBeatKind.RESOLUTION,
            )
        )
    )
    return StoryPlan(
        job_id=job_id,  # type: ignore[arg-type]
        logline="a lighthouse keeper's last night on watch",
        beats=list(beats),
        model_alias="reasoning-high",
        prompt_version="v1",
        created_at=NOW,
    )


def test_shot_count_invariant_fires_once_planned() -> None:
    job_id = uuid4()
    state = base_job(job_id=job_id, story_plan=a_story_plan(job_id), shots=four_shots()[:3])
    with pytest.raises(ShotCountInvariantError):
        assert_invariants(state, node="plan_story")


def test_repair_cap_invariant() -> None:
    shot = ShotState(index=0, beat_kind=BeatKind.SETUP, repairs_used=2, attempts_used=3)
    state = base_job(shots=(shot,))
    assert_invariants(state, node="generate_shot")  # at the cap is fine


def test_attempts_used_must_equal_repairs_plus_one() -> None:
    shot = ShotState(index=0, beat_kind=BeatKind.SETUP, repairs_used=0, attempts_used=2)
    state = base_job(shots=(shot,))
    with pytest.raises(GraphInvariantError):
        assert_invariants(state, node="generate_shot")


def test_bible_hash_mismatch_raises() -> None:
    job_id = uuid4()
    bible = a_bible(job_id)
    mutated = bible.model_copy(update={"negative_constraints": ["no crowds"]})
    state = base_job(job_id=job_id, bible=mutated, bible_hash=bible.content_hash)
    with pytest.raises(BibleHashMismatchError):
        assert_invariants(state, node="lock_bible")


def test_outcome_set_outside_finalize_raises() -> None:
    state = base_job(outcome=Outcome.SUCCESS)
    with pytest.raises(GraphInvariantError):
        assert_invariants(state, node="qc_shot")
    assert_invariants(state, node="finalize")  # the one node allowed to


def test_budget_must_not_decrease_between_checkpoints() -> None:
    previous = base_job()
    later = base_job(job_id=previous.job_id, budget=ledger())
    later.budget.apply(Charge(charge_id="c1", usd=Decimal(1)))
    assert_invariants(later, node="generate_shot", previous=previous)  # increased: fine
    with pytest.raises(GraphInvariantError):
        assert_invariants(previous, node="generate_shot", previous=later)  # decreased: not fine


def test_shot_status_defaults_pending() -> None:
    shot = ShotState(index=0, beat_kind=BeatKind.SETUP)
    assert shot.status is ShotStatus.PENDING
    assert shot.repairs_used == 0
