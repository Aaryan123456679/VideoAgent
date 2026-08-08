"""`decide()` — the only function in this system that may end a job.

`harness.md` §5 fixes six rules in one order, first match wins, and the order *is* the
specification rather than an implementation detail of it:

```
1. cancelled?               → ESCALATE / FAILED   (operator or client cancel)   [D-12]
2. non-retryable error?     → TERMINATE FAILED
3. job-scope signature x2?  → TERMINATE FAILED_NO_PROGRESS         VA-INT-002   [D-02]
4. budget exceeded?         → TERMINATE PARTIAL (degraded=true)    VA-BUDGET-*
5. evaluator satisfied?     → TERMINATE SUCCESS
6. otherwise                → CONTINUE
```

**Two orderings carry the weight, and both are counter-intuitive until stated.**

Rule 3 above rule 4: a job that has both run out of money and proved it is making no progress
is `FAILED_NO_PROGRESS`, not `PARTIAL`. `[CPS §Agent harness]` says a repeated signature stops
*immediately*, and reporting the budget instead would file a stuck job under "we ran out of
money", which sends an operator to raise a cap that was never the problem.

Rule 4 above rule 5: a job that has met every acceptance criterion *and* exhausted its budget
is `PARTIAL`, not `SUCCESS`. This looks harsh — the work is finished — but "satisfied" is
evaluated from state that the exhausted budget may have truncated, and a run that spent past
its cap is not a run anyone should be told went fine. `PARTIAL` with `degraded=true` says both
true things at once; `SUCCESS` says one of them and hides the other.

**This module imports no domain module.** `harness.md` §7: not `planning`, not `qc`, not
`assembly`, not `providers`. Not because layering is tidy, but because the moment the harness
can ask a domain module what "good" means, the domain module has an opinion about termination
— and `[CPS §Agent harness]` puts the model, and everything downstream of it, *inside* the
loop rather than in control of it. Everything rule 5 needs arrives as counters in `LoopState`.
`tests/unit/test_harness_boundary.py` asserts it statically, because the day someone imports
`providers` here to "just check the shot status" is the day that stops being true.

**Every input is data, and none of it is a model's output.** A score reaches rule 5 as an
integer count of accepted shots; nothing in `LoopState` is a route, a next node or a stop
flag. `AGENT.md` §1.4: a model's output may change content, never control flow.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from video_agent.harness.budget import BudgetLedger
from video_agent.harness.cancel import CancelRequest
from video_agent.harness.outcomes import Decision, Outcome, Verdict
from video_agent.observability.codes import ErrorCode

__all__ = ["EvaluatorState", "FatalError", "LoopState", "NoProgress", "decide"]


class FatalError(BaseModel):
    """An error the loop is carrying, with the taxonomy code that classifies it.

    Retryability is read from the code and never set here `[D-62]`. A call site deciding that
    its own failure is worth one more attempt is how a `402` becomes a retry storm, so rule 2
    asks the taxonomy, not the raiser.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode
    message: str = Field(min_length=1)

    @property
    def is_terminal(self) -> bool:
        """Whether this error ends the job — that is, whether the taxonomy says do not retry."""
        return not self.code.retryable


class NoProgress(BaseModel):
    """A repeated failure signature that has reached job scope. `[D-02]`.

    Only job scope reaches `decide()`. A repeated shot-scope signature abandons its shot inside
    the graph and the job continues, so surfacing it here would give rule 3 a second meaning
    and make the ladder untestable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    digest: str = Field(min_length=1)
    count: int = Field(ge=2)
    promoted: bool = False

    @property
    def human_reason(self) -> str:
        detail = " across more than one shot" if self.promoted else ""
        return (
            f"the same failure signature was seen {self.count} times{detail}; "
            "further attempts would repeat a known-dead path"
        )


class EvaluatorState(BaseModel):
    """The counters rule 5 reads. All three conditions, never any of them.

    `harness.md` §5: all shots accepted **and** assemble and deliver completed **and** the
    manifest non-empty. Written as `any` this would call a job successful the moment assemble
    finished, before anything was delivered or the manifest existed — a "success" with no
    playable artifact, which `[D-73]` defines as a zero-deliverable job.

    `shots_required` is a number in state rather than a constant here because the shot count is
    a `[PRD]` product fact, and the harness holds no product facts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    shots_required: int = Field(ge=1)
    shots_accepted: int = Field(default=0, ge=0)
    assemble_complete: bool = False
    deliver_complete: bool = False
    manifest_entries: int = Field(default=0, ge=0)

    @property
    def satisfied(self) -> bool:
        """Whether every acceptance condition holds. Three conjuncts, no shortcuts."""
        return (
            self.shots_accepted >= self.shots_required
            and self.assemble_complete
            and self.deliver_complete
            and self.manifest_entries > 0
        )


class LoopState(BaseModel):
    """Everything `decide()` is allowed to know. No domain object appears in it.

    A deliberately flat record of facts: a flag, an error, a repeat, a ledger and four
    counters. Anything richer would be a place for a domain type to arrive, and the import ban
    would then hold only in the import graph and not in the data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    ledger: BudgetLedger
    evaluator: EvaluatorState
    cancel: CancelRequest | None = None
    error: FatalError | None = None
    no_progress: NoProgress | None = None
    preserved: tuple[str, ...] = ()


def decide(state: LoopState, *, now: datetime) -> Decision:
    """Apply the six rules in order and return at the first match. `harness.md` §5.

    `now` is a parameter and never `datetime.now()`: the wall-clock cap is one of the four, and
    a rule that reads the clock itself can only be tested by waiting — which means it gets
    tested with a tolerance, and a tolerance on a cap is not a cap.
    """
    # Rule 1 — cancelled. Checked first and on every call, so a cancel is honoured at the next
    # conditional edge rather than at the end of whatever the graph had planned.
    if state.cancel is not None:
        outcome = state.cancel.outcome
        return Decision(
            verdict=Verdict.ESCALATE if outcome is Outcome.ESCALATED else Verdict.TERMINATE,
            outcome=outcome,
            reason_code=state.cancel.reason_code,
            human_reason=state.cancel.human_reason,
            preserved=state.preserved,
        )

    # Rule 2 — a non-retryable error. Retryable ones are the retry policy's business and must
    # not reach a termination: ending a job on a transient 503 is the failure mode retries
    # exist to prevent.
    if state.error is not None and state.error.is_terminal:
        return Decision(
            verdict=Verdict.TERMINATE,
            outcome=Outcome.FAILED,
            reason_code=state.error.code,
            human_reason=state.error.message,
            preserved=state.preserved,
        )

    # Rule 3 — no progress. Above rule 4 deliberately; see the module docstring.
    if state.no_progress is not None:
        return Decision(
            verdict=Verdict.TERMINATE,
            outcome=Outcome.FAILED_NO_PROGRESS,
            reason_code=ErrorCode.VA_INT_002,
            human_reason=state.no_progress.human_reason,
            preserved=state.preserved,
        )

    # Rule 4 — budget exhausted. Always PARTIAL and always degraded `[CPS §Agent harness]`:
    # best-so-far, flagged. Never a bare failure and never a silent truncation.
    breach = state.ledger.exceeded(now)
    if breach is not None:
        return Decision(
            verdict=Verdict.TERMINATE,
            outcome=Outcome.PARTIAL,
            reason_code=breach.code,
            human_reason=breach.human_reason,
            degraded=True,
            preserved=state.preserved,
        )

    # Rule 5 — the evaluator is satisfied.
    if state.evaluator.satisfied:
        return Decision(
            verdict=Verdict.TERMINATE,
            outcome=Outcome.SUCCESS,
            human_reason="every shot was accepted and the delivered manifest is complete",
            preserved=state.preserved,
        )

    # Rule 6 — nothing has ended the job, so the graph may take its own next edge.
    return Decision(verdict=Verdict.CONTINUE, human_reason="no termination condition is met")
