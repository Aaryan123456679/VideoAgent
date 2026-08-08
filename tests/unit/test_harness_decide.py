"""`decide()`'s six-rule priority order, `harness.md` §5 — not exhaustive, just the ordering."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from video_agent.harness.budget import BudgetCaps, BudgetLedger, Charge
from video_agent.harness.cancel import CancelActor, CancelRequest
from video_agent.harness.decide import EvaluatorState, FatalError, LoopState, NoProgress, decide
from video_agent.harness.outcomes import Decision, Outcome, Verdict
from video_agent.observability.codes import ErrorCode

NOW = datetime(2026, 8, 8, tzinfo=UTC)


def caps() -> BudgetCaps:
    return BudgetCaps(
        max_iterations=10, max_wall_clock_s=3600, max_tokens=10_000, max_usd=Decimal(10)
    )


def ledger(*, exceeded: bool = False) -> BudgetLedger:
    ledger_ = BudgetLedger(caps=caps(), started_at=NOW)
    if exceeded:
        ledger_.apply(Charge(charge_id="c1", usd=Decimal(11)))
    return ledger_


def evaluator(*, satisfied: bool = False) -> EvaluatorState:
    if satisfied:
        return EvaluatorState(
            shots_required=4,
            shots_accepted=4,
            assemble_complete=True,
            deliver_complete=True,
            manifest_entries=1,
        )
    return EvaluatorState(shots_required=4)


def base_state(**overrides: object) -> LoopState:
    fields: dict[str, object] = {
        "job_id": uuid4(),
        "ledger": ledger(),
        "evaluator": evaluator(),
        "cancel": None,
        "error": None,
        "no_progress": None,
        "preserved": (),
    }
    fields.update(overrides)
    return LoopState(**fields)  # type: ignore[arg-type]


def test_rule_1_cancel_by_client_beats_everything() -> None:
    state = base_state(
        cancel=CancelRequest(actor=CancelActor.CLIENT, requested_at=NOW),
        error=FatalError(code=ErrorCode.VA_INT_001, message="boom"),
        no_progress=NoProgress(digest="d", count=2),
        ledger=ledger(exceeded=True),
    )
    decision = decide(state, now=NOW)
    assert decision.verdict is Verdict.TERMINATE
    assert decision.outcome is Outcome.FAILED


def test_rule_1_cancel_by_operator_escalates() -> None:
    state = base_state(cancel=CancelRequest(actor=CancelActor.OPERATOR, requested_at=NOW))
    decision = decide(state, now=NOW)
    assert decision.verdict is Verdict.ESCALATE
    assert decision.outcome is Outcome.ESCALATED


def test_rule_2_non_retryable_error_beats_no_progress_and_budget() -> None:
    state = base_state(
        error=FatalError(code=ErrorCode.VA_INT_001, message="boom"),
        no_progress=NoProgress(digest="d", count=2),
        ledger=ledger(exceeded=True),
    )
    decision = decide(state, now=NOW)
    assert decision.verdict is Verdict.TERMINATE
    assert decision.outcome is Outcome.FAILED
    assert decision.reason_code is ErrorCode.VA_INT_001


def test_rule_2_retryable_error_falls_through_to_continue() -> None:
    state = base_state(error=FatalError(code=ErrorCode.VA_PROV_001, message="transient"))
    decision = decide(state, now=NOW)
    assert decision.verdict is Verdict.CONTINUE


def test_rule_3_no_progress_beats_budget_exhaustion() -> None:
    """Counter-intuitive ordering: stuck-and-broke is FAILED_NO_PROGRESS, not PARTIAL."""
    state = base_state(no_progress=NoProgress(digest="d", count=2), ledger=ledger(exceeded=True))
    decision = decide(state, now=NOW)
    assert decision.verdict is Verdict.TERMINATE
    assert decision.outcome is Outcome.FAILED_NO_PROGRESS
    assert decision.reason_code is ErrorCode.VA_INT_002


def test_rule_4_budget_exhaustion_beats_satisfied_evaluator() -> None:
    """Counter-intuitive ordering: a job that met every criterion but blew its budget is
    PARTIAL+degraded, never SUCCESS."""
    state = base_state(ledger=ledger(exceeded=True), evaluator=evaluator(satisfied=True))
    decision = decide(state, now=NOW)
    assert decision.verdict is Verdict.TERMINATE
    assert decision.outcome is Outcome.PARTIAL
    assert decision.degraded is True


def test_rule_5_satisfied_evaluator_terminates_success() -> None:
    state = base_state(evaluator=evaluator(satisfied=True))
    decision = decide(state, now=NOW)
    assert decision.verdict is Verdict.TERMINATE
    assert decision.outcome is Outcome.SUCCESS
    assert decision.reason_code is None


def test_rule_6_otherwise_continue() -> None:
    decision = decide(base_state(), now=NOW)
    assert decision.verdict is Verdict.CONTINUE
    assert decision.outcome is None


def test_decision_rejects_continue_with_outcome() -> None:
    with pytest.raises(ValueError, match="continues the job"):
        Decision(verdict=Verdict.CONTINUE, outcome=Outcome.SUCCESS, human_reason="x")


def test_decision_rejects_terminal_failure_without_reason_code() -> None:
    with pytest.raises(ValueError, match="requires a reason_code"):
        Decision(verdict=Verdict.TERMINATE, outcome=Outcome.FAILED, human_reason="x")


def test_cancel_request_outcome_and_code_are_total_over_actor() -> None:
    for actor in CancelActor:
        request = CancelRequest(actor=actor, requested_at=NOW)
        assert request.outcome in (Outcome.FAILED, Outcome.ESCALATED)
        assert request.reason_code is not None
