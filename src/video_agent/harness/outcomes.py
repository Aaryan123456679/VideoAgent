"""The loop's phases and the five outcomes a job may end in.

`[CPS §Agent harness & loop engine]` fixes both halves of this module and neither is
negotiable. The loop is `observe → think → act → evaluate → repeat | terminate | escalate`,
and the termination table has exactly four rows — evaluator satisfied, budget exhausted,
repeated failure signature, non-retryable error or human trigger — which resolve to five
outcome names because the last row carries two.

**Why `Outcome` is a closed enum and `Decision` is validated rather than documented.**
`harness.md` §2 declares `outcome` as *set iff `verdict != CONTINUE`*. Written as a comment
that is a convention, and a convention holds until the first caller builds a `Decision` in a
hurry. A `CONTINUE` carrying an outcome is the dangerous direction: every consumer that asks
"did the job end?" by reading `outcome is not None` would see a job that ended while the graph
kept running it. So the relationship is a model validator, and the constructor refuses.

**`Verdict` and `Outcome` are not the same axis.** `Verdict` says what the loop does next —
keep going, stop, or hand to a human. `Outcome` says how the job is recorded. Three verdicts
map onto five outcomes because `TERMINATE` covers four of them, and the one-to-one pairs
(`ESCALATE` ↔ `ESCALATED`) are enforced here so a decision cannot claim a human is needed
while filing itself as a clean success.

**`reason_code` is `ErrorCode` or nothing, never free text.** `[CPS §Failure behaviour]`
requires a stable code on every error response; `AGENT.md` §3 adds that a code's meaning never
changes. Typing the field as the enum makes an invented code a type error rather than a string
that reaches an operator's runbook and matches nothing. `SUCCESS` is the single terminal
outcome with no code, because the taxonomy has no member meaning "nothing went wrong" and
inventing one would be inventing a code.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from video_agent.observability.codes import ErrorCode

__all__ = ["Decision", "Outcome", "Phase", "Verdict"]


class Phase(StrEnum):
    """The four phases of one superstep. `[CPS §Agent harness]`.

    Named here rather than inferred from the node being run, because the phase is what a span
    is tagged with and two different nodes can be in the same phase.
    """

    OBSERVE = "observe"
    THINK = "think"
    ACT = "act"
    EVALUATE = "evaluate"


class Outcome(StrEnum):
    """Every way a job may end. Exactly the `[CPS §Agent harness]` table, plus `SUCCESS`.

    A sixth member is not an extension, it is a spec change: the error envelope, the job
    status column and the "zero deliverable" metric `[D-73]` all enumerate this set, and a new
    member would be silently unhandled by all three.
    """

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED_NO_PROGRESS = "FAILED_NO_PROGRESS"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


class Verdict(StrEnum):
    """What the loop does next. Distinct from `Outcome`; see the module docstring."""

    CONTINUE = "continue"
    TERMINATE = "terminate"
    ESCALATE = "escalate"


TERMINAL_VERDICTS: frozenset[Verdict] = frozenset({Verdict.TERMINATE, Verdict.ESCALATE})
"""The verdicts that end a job. `CONTINUE` is the only one that does not."""


class Decision(BaseModel):
    """One `decide()` result: what happens next, why, and what survived.

    `preserved` is on the decision rather than assembled later because
    `[CPS §Failure behaviour]` requires an error to say *what was preserved*, and the only
    moment that is reliably known is the moment the job is ended. Reconstructing it afterwards
    means reading state that the termination may itself have made unreadable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Verdict
    outcome: Outcome | None = None
    reason_code: ErrorCode | None = None
    human_reason: str = Field(min_length=1)
    degraded: bool = False
    preserved: tuple[str, ...] = ()

    @property
    def is_terminal(self) -> bool:
        """Whether this decision ends the job."""
        return self.verdict in TERMINAL_VERDICTS

    @model_validator(mode="after")
    def _check_outcome_matches_verdict(self) -> Decision:
        """`outcome` is set if and only if the verdict is terminal, and agrees with it."""
        if self.is_terminal and self.outcome is None:
            message = f"verdict {self.verdict.value!r} is terminal and requires an outcome"
            raise ValueError(message)
        if not self.is_terminal and self.outcome is not None:
            message = (
                f"verdict {self.verdict.value!r} continues the job and must carry no outcome; "
                f"got {self.outcome.value!r}"
            )
            raise ValueError(message)
        escalated = self.outcome is Outcome.ESCALATED
        if (self.verdict is Verdict.ESCALATE) is not escalated:
            message = (
                f"verdict {self.verdict.value!r} and outcome {self.outcome} disagree: "
                "ESCALATE and ESCALATED are the same event and neither occurs alone"
            )
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _check_reason_code(self) -> Decision:
        """Every terminal decision but `SUCCESS` carries a taxonomy code; `CONTINUE` has none."""
        needs_code = self.is_terminal and self.outcome is not Outcome.SUCCESS
        if needs_code and self.reason_code is None:
            message = f"outcome {self.outcome} is a failure and requires a reason_code"
            raise ValueError(message)
        if not self.is_terminal and self.reason_code is not None:
            message = "a CONTINUE decision must carry no reason_code; nothing has gone wrong"
            raise ValueError(message)
        return self
