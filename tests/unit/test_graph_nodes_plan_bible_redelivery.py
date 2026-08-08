"""`plan_story_node`/`lock_bible_node` must be safe to run twice. `graph.md` §6.1: queue
delivery is at-least-once, so a redelivered message that already wrote a plan/bible must not
call the model again or crash on the `job_id`-unique constraint.

Fakes the repositories the same way `test_graph_nodes_generate_shot.py` does — monkeypatching
the classes `graph.nodes` imports rather than a recording SQL connection, since the point here
is the node's *branching* (skip vs. do the work), not the SQL either repository builds.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from tests.unit.test_graph_nodes_generate_shot import a_bible, a_story_plan
from video_agent.graph import nodes
from video_agent.graph.deps import GraphDeps
from video_agent.graph.guard import JobHarness
from video_agent.graph.state import JobState
from video_agent.harness.budget import BudgetCaps, BudgetLedger
from video_agent.persistence.repositories import ContinuityBibleRecord, StoryPlanRecord
from video_agent.planning.models import BeatKind as PlanBeatKind
from video_agent.planning.models import CameraMove

NOW = datetime(2026, 8, 8, tzinfo=UTC)
SHOT_COUNT = 4


def a_ledger() -> BudgetLedger:
    caps = BudgetCaps(
        max_iterations=10, max_wall_clock_s=3600, max_tokens=10_000, max_usd=Decimal(10)
    )
    return BudgetLedger(caps=caps, started_at=NOW)


def a_job(**overrides: object) -> JobState:
    fields: dict[str, object] = {
        "job_id": uuid4(),
        "tenant_id": uuid4(),
        "trace_id": "trace-1",
        "prompt": "a lighthouse keeper's last watch",
        "budget": a_ledger(),
    }
    fields.update(overrides)
    return JobState(**fields)  # type: ignore[arg-type]


class _ExplodingGateway:
    """A `deps.gateway` that fails the test if the redelivery-skip branch calls it anyway."""

    async def call(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        message = "plan/bible domain call reached — the redelivery-skip branch did not skip"
        raise AssertionError(message)


@dataclass
class FakeDB:
    plan: StoryPlanRecord | None = None
    beats: list[dict[str, Any]] = field(default_factory=list)
    bible: ContinuityBibleRecord | None = None
    plan_creates: int = 0
    bible_creates: int = 0


def install_fakes(monkeypatch: pytest.MonkeyPatch, db: FakeDB) -> None:
    @asynccontextmanager
    async def fake_tenant_session(engine: object, tenant_id: UUID) -> AsyncIterator[object]:
        del engine, tenant_id
        yield object()

    class FakeStoryPlanRepository:
        def __init__(self, session: object) -> None:
            del session

        async def get_for_job(self, job_id: UUID) -> StoryPlanRecord | None:
            del job_id
            return db.plan

        async def list_beats(self, job_id: UUID) -> list[dict[str, Any]]:
            del job_id
            return db.beats

        async def create(self, new_plan: object) -> None:
            del new_plan
            db.plan_creates += 1

    class FakeContinuityBibleRepository:
        def __init__(self, session: object) -> None:
            del session

        async def get_for_job(self, job_id: UUID) -> ContinuityBibleRecord | None:
            del job_id
            return db.bible

        async def create(self, new_bible: object) -> None:
            del new_bible
            db.bible_creates += 1

    monkeypatch.setattr(nodes, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(nodes, "StoryPlanRepository", FakeStoryPlanRepository)
    monkeypatch.setattr(nodes, "ContinuityBibleRepository", FakeContinuityBibleRepository)


def a_deps() -> GraphDeps:
    return GraphDeps(
        engine=cast(Any, None),
        gateway=cast(Any, _ExplodingGateway()),
        checkpointer=cast(Any, None),
        harness=JobHarness(job_id=uuid4(), shots_required=4),
        now=lambda: NOW,
        providers=cast(Any, None),
        artifacts=cast(Any, None),
    )


@pytest.mark.asyncio
async def test_plan_story_skips_the_model_and_the_insert_when_a_plan_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = a_job()
    db = FakeDB(
        plan=StoryPlanRecord(
            id=uuid4(),
            job_id=job.job_id,
            logline="a lighthouse keeper's last watch",
            total_duration_s=Decimal("40.00"),
            model_alias="reasoning-high",
            prompt_version="v1",
            created_at=NOW,
        ),
        beats=[
            {
                "idx": i,
                "kind": kind.value,
                "action": "a beat action long enough to pass validation",
                "camera_move": CameraMove.STATIC.value,
                "duration_s": Decimal("10.00"),
                "continuity_note": None,
            }
            for i, kind in enumerate(
                (
                    PlanBeatKind.SETUP,
                    PlanBeatKind.DEVELOPMENT,
                    PlanBeatKind.TURN,
                    PlanBeatKind.RESOLUTION,
                )
            )
        ],
    )
    install_fakes(monkeypatch, db)

    result = await nodes.plan_story_node(job, a_deps())

    assert db.plan_creates == 0  # never re-inserted
    assert len(result["shots"]) == SHOT_COUNT
    assert result["story_plan"].logline == "a lighthouse keeper's last watch"


@pytest.mark.asyncio
async def test_lock_bible_skips_the_model_and_the_insert_when_a_bible_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    bible = a_bible(job_id)
    job = a_job(job_id=job_id, story_plan=a_story_plan(job_id))
    db = FakeDB(
        bible=ContinuityBibleRecord(
            id=uuid4(),
            job_id=job_id,
            character=bible.character.model_dump(mode="json"),
            wardrobe=bible.wardrobe.model_dump(mode="json"),
            location=bible.location.model_dump(mode="json"),
            lighting=bible.lighting.model_dump(mode="json"),
            palette=bible.palette.model_dump(mode="json"),
            lens_language=bible.lens_language.model_dump(mode="json"),
            negative_constraints=list(bible.negative_constraints),
            content_hash=bible.content_hash,
            locked_at=bible.locked_at,
            model_alias=bible.model_alias,
            prompt_version=bible.prompt_version,
        )
    )
    install_fakes(monkeypatch, db)

    result = await nodes.lock_bible_node(job, a_deps())

    assert db.bible_creates == 0  # never re-inserted
    assert result["bible_hash"] == bible.content_hash
    assert result["bible"].character.name == bible.character.name
