"""`graph.md` §3.1's harness veto: `guard()` diverts to `finalize` and writes the decision."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from video_agent.graph.guard import JobHarness, guard
from video_agent.graph.state import JobState, ShotState
from video_agent.harness.budget import BudgetCaps, BudgetLedger, Charge
from video_agent.harness.cancel import CancelActor, CancelRequest
from video_agent.harness.outcomes import Outcome, Verdict
from video_agent.observability.codes import ErrorCode
from video_agent.persistence.enums import BeatKind, ShotStatus

NOW = datetime(2026, 8, 8, tzinfo=UTC)


def ledger(*, exceeded: bool = False) -> BudgetLedger:
    caps = BudgetCaps(
        max_iterations=10, max_wall_clock_s=3600, max_tokens=10_000, max_usd=Decimal(10)
    )
    result = BudgetLedger(caps=caps, started_at=NOW)
    if exceeded:
        result.apply(Charge(charge_id="c1", usd=Decimal(11)))
    return result


def a_job(**overrides: object) -> JobState:
    fields: dict[str, object] = {
        "job_id": uuid4(),
        "tenant_id": uuid4(),
        "trace_id": "trace-1",
        "prompt": "a lighthouse keeper's last watch",
        "budget": ledger(),
    }
    fields.update(overrides)
    return JobState(**fields)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_guard_returns_none_and_leaves_state_untouched_on_continue() -> None:
    job = a_job()
    harness = JobHarness(job_id=job.job_id, shots_required=4)
    result = await guard(job, "select_next_shot", harness=harness, now=NOW)
    assert result is None
    assert job.outcome is None


@pytest.mark.asyncio
async def test_guard_diverts_to_finalize_and_writes_outcome_on_cancel() -> None:
    job = a_job()
    harness = JobHarness(
        job_id=job.job_id,
        shots_required=4,
        cancel=CancelRequest(actor=CancelActor.CLIENT, requested_at=NOW),
    )
    result = await guard(job, "generate_shot", harness=harness, now=NOW)
    assert result == "finalize"
    assert job.outcome is Outcome.FAILED
    assert job.terminal_reason_code == ErrorCode.VA_REQ_006.value


@pytest.mark.asyncio
async def test_guard_diverts_on_budget_exhaustion_and_sets_degraded() -> None:
    job = a_job(budget=ledger(exceeded=True))
    harness = JobHarness(job_id=job.job_id, shots_required=4)
    result = await guard(job, "generate_shot", harness=harness, now=NOW)
    assert result == "finalize"
    assert job.outcome is Outcome.PARTIAL
    assert job.degraded is True


@pytest.mark.asyncio
async def test_job_harness_evaluator_reads_accepted_shots_off_state() -> None:
    shots = tuple(
        ShotState(index=i, beat_kind=BeatKind.SETUP, status=ShotStatus.ACCEPTED) for i in range(4)
    )
    job = a_job(shots=shots)
    harness = JobHarness(job_id=job.job_id, shots_required=4)
    decision = await harness.decide(job, "select_next_shot", now=NOW)
    assert decision.verdict is Verdict.CONTINUE  # assemble/deliver not yet complete
