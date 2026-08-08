"""`qc_shot_node`'s manual repair override. Real scoring is deferred (`qc.md`, E3); this
covers only the mechanism a `POST .../shots/{i}/force-repair` signal exercises — accept by
default, repair once flagged (respecting the cap), and `route_after_qc` sending a repaired
shot back to `generate_shot`. Not a test of QC, because there is no QC here to test.

DB access (`ShotRepository.get_by_job_and_idx`/`.record_qc_decision`, the sync onto the `shot`
table's own row) is faked by monkeypatching `graph.nodes.tenant_session`/`.ShotRepository`,
the same technique `test_graph_nodes_generate_shot.py` uses — real node logic, no live Postgres.
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

from video_agent.graph import nodes
from video_agent.graph.deps import GraphDeps
from video_agent.graph.guard import JobHarness
from video_agent.graph.nodes import qc_shot_node, route_after_qc
from video_agent.graph.state import MAX_REPAIRS, JobState, ShotState
from video_agent.harness.budget import BudgetCaps, BudgetLedger
from video_agent.persistence.enums import BeatKind, ShotStatus

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SHOT_0 = 0
SHOT_1 = 1
SHOT_2 = 2
SHOT_3 = 3


@dataclass(frozen=True, slots=True)
class _FakeShotRow:
    id: UUID


@dataclass
class _RecordedDecisions:
    calls: list[dict[str, object]] = field(default_factory=list)


def _install_fake_shot_repository(
    monkeypatch: pytest.MonkeyPatch, *, shot_row_id: UUID
) -> _RecordedDecisions:
    recorded = _RecordedDecisions()

    @asynccontextmanager
    async def fake_tenant_session(engine: object, tenant_id: object) -> AsyncIterator[object]:
        del engine, tenant_id
        yield object()

    class FakeShotRepository:
        def __init__(self, session: object) -> None:
            del session

        async def get_by_job_and_idx(self, job_id: UUID, idx: int) -> _FakeShotRow:
            del job_id, idx
            return _FakeShotRow(id=shot_row_id)

        async def record_qc_decision(
            self,
            shot_id: UUID,
            *,
            status: ShotStatus,
            repairs_used: int,
            best_score: Decimal | None,
        ) -> None:
            recorded.calls.append(
                {
                    "shot_id": shot_id,
                    "status": status,
                    "repairs_used": repairs_used,
                    "best_score": best_score,
                }
            )

    monkeypatch.setattr(nodes, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(nodes, "ShotRepository", FakeShotRepository)
    return recorded


def _caps() -> BudgetCaps:
    return BudgetCaps(
        max_iterations=40, max_wall_clock_s=1200.0, max_tokens=1000, max_usd=Decimal(5)
    )


def _state(*, shot_index: int, repairs_used: int) -> JobState:
    shot = ShotState(
        index=shot_index,
        beat_kind=BeatKind.SETUP,
        status=ShotStatus.QC,
        repairs_used=repairs_used,
        attempts_used=repairs_used + 1,
        final_frame_artifact_id=uuid4(),
    )
    other_shots = tuple(
        ShotState(index=i, beat_kind=BeatKind.SETUP) for i in range(4) if i != shot_index
    )
    shots = tuple(sorted((shot, *other_shots), key=lambda s: s.index))
    return JobState(
        job_id=uuid4(),
        tenant_id=uuid4(),
        trace_id="trace-1",
        prompt="a lighthouse at dawn, waves crashing",
        shot_index=shot_index,
        shots=shots,
        budget=BudgetLedger(caps=_caps(), started_at=NOW),
    )


def _deps(*, harness: JobHarness) -> GraphDeps:
    unused = cast(Any, None)
    return GraphDeps(
        engine=unused,
        gateway=unused,
        checkpointer=unused,
        harness=harness,
        now=lambda: NOW,
        providers=unused,
        artifacts=unused,
    )


async def test_with_no_signal_the_shot_is_accepted_unconditionally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot_row_id = uuid4()
    recorded = _install_fake_shot_repository(monkeypatch, shot_row_id=shot_row_id)
    state = _state(shot_index=0, repairs_used=0)
    harness = JobHarness(job_id=state.job_id, shots_required=4)

    result = await qc_shot_node(state, _deps(harness=harness))

    shot = next(s for s in result["shots"] if s.index == 0)
    assert shot.status is ShotStatus.ACCEPTED
    assert shot.best_score == 1.0
    assert shot.repairs_used == 0
    assert result["last_good_frame_artifact_id"] == shot.final_frame_artifact_id
    assert recorded.calls == [
        {
            "shot_id": shot_row_id,
            "status": ShotStatus.ACCEPTED,
            "repairs_used": 0,
            "best_score": Decimal("1.0"),
        }
    ]


async def test_a_forced_repair_signal_sends_the_shot_back_to_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot_row_id = uuid4()
    recorded = _install_fake_shot_repository(monkeypatch, shot_row_id=shot_row_id)
    state = _state(shot_index=1, repairs_used=0)
    harness = JobHarness(job_id=state.job_id, shots_required=4)
    harness.force_repair_shots.add(1)

    result = await qc_shot_node(state, _deps(harness=harness))

    shot = next(s for s in result["shots"] if s.index == 1)
    assert shot.status is ShotStatus.PENDING
    assert shot.repairs_used == 1
    assert "last_good_frame_artifact_id" not in result
    assert harness.force_repair_shots == set()
    assert recorded.calls == [
        {
            "shot_id": shot_row_id,
            "status": ShotStatus.PENDING,
            "repairs_used": 1,
            "best_score": None,
        }
    ]


async def test_the_signal_is_consumed_once_and_does_not_affect_other_shots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_shot_repository(monkeypatch, shot_row_id=uuid4())
    state = _state(shot_index=2, repairs_used=0)
    harness = JobHarness(job_id=state.job_id, shots_required=4)
    harness.force_repair_shots.update({0, 2})

    result = await qc_shot_node(state, _deps(harness=harness))

    repaired = next(s for s in result["shots"] if s.index == SHOT_2)
    untouched = next(s for s in result["shots"] if s.index == SHOT_0)
    assert repaired.status is ShotStatus.PENDING
    assert untouched.status is ShotStatus.PENDING  # unchanged by this call
    assert harness.force_repair_shots == {0}  # only shot 2's flag was consumed


async def test_the_repair_cap_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_shot_repository(monkeypatch, shot_row_id=uuid4())
    state = _state(shot_index=3, repairs_used=MAX_REPAIRS)
    harness = JobHarness(job_id=state.job_id, shots_required=4)
    harness.force_repair_shots.add(3)

    result = await qc_shot_node(state, _deps(harness=harness))

    shot = next(s for s in result["shots"] if s.index == SHOT_3)
    assert shot.status is ShotStatus.ACCEPTED
    assert shot.repairs_used == MAX_REPAIRS


async def test_route_after_qc_sends_a_repaired_shot_back_to_generate_shot() -> None:
    state = _state(shot_index=0, repairs_used=1)
    shots = tuple(
        s.model_copy(update={"status": ShotStatus.PENDING}) if s.index == 0 else s
        for s in state.shots
    )
    state = state.model_copy(update={"shots": shots})
    harness = JobHarness(job_id=state.job_id, shots_required=4)

    route = await route_after_qc(state, _deps(harness=harness))

    assert route == "generate_shot"


async def test_route_after_qc_sends_an_accepted_shot_to_select_next_shot() -> None:
    state = _state(shot_index=0, repairs_used=0)
    shots = tuple(
        s.model_copy(update={"status": ShotStatus.ACCEPTED}) if s.index == 0 else s
        for s in state.shots
    )
    state = state.model_copy(update={"shots": shots})
    harness = JobHarness(job_id=state.job_id, shots_required=4)

    route = await route_after_qc(state, _deps(harness=harness))

    assert route == "select_next_shot"
