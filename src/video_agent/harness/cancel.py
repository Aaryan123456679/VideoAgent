"""Cooperative cancellation: a flag the loop reads, never a signal that kills a worker.

`harness.md` §8 and `[D-12]`. A cancel sets a flag; the node that is running finishes and
checkpoints; the next `decide()` sees the flag and ends the job. Nothing is interrupted
mid-write.

**Why cooperative and not a hard kill.** `AGENT.md` §1.1 requires the checkpoint and the node's
domain writes to land in one transaction `[D-23]`. A cancel that cancels the task mid-write
either rolls that transaction back — losing a shot that was already paid for — or, worse,
lands the domain rows without the checkpoint, leaving a job whose state says it has not done
work it has already been billed for. Waiting for the node costs seconds. The alternative costs
correctness, and the money is already spent either way.

**Why the actor is part of the flag.** `[D-12]`: a client cancelling their own job is a
`FAILED` job — expected, no one to page. An operator cancelling someone's job is `ESCALATED` —
a human has intervened and the record must say so. Same mechanism, two outcomes, and the
difference cannot be recovered later from the flag alone, so it is stored with it.

**Why cancelling a terminal job is a no-op rather than an error.** Cancel races completion by
nature: the caller pressed stop while the last shot was uploading. Returning the outcome the
job already reached is the honest answer; raising would make a client's perfectly reasonable
request look like their bug.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from video_agent.harness.outcomes import Outcome
from video_agent.observability.codes import ErrorCode

__all__ = ["CancelActor", "CancelRequest", "CancelResult"]


class CancelActor(StrEnum):
    """Who asked. Decides the outcome, so it is required rather than defaulted. `[D-12]`."""

    CLIENT = "client"
    OPERATOR = "operator"


ACTOR_OUTCOMES: dict[CancelActor, Outcome] = {
    CancelActor.CLIENT: Outcome.FAILED,
    CancelActor.OPERATOR: Outcome.ESCALATED,
}
"""Actor → outcome `[D-12]`. A table rather than a conditional so the mapping is one fact in
one place and a test can assert it is total over the enum."""

ACTOR_CODES: dict[CancelActor, ErrorCode] = {
    CancelActor.CLIENT: ErrorCode.VA_REQ_006,
    CancelActor.OPERATOR: ErrorCode.VA_INT_001,
}
"""Actor → taxonomy code. `VA-REQ-006` (*job not resumable*) is the honest code for a
client-cancelled job: it is a request-domain fact about a job that will not continue, and no
`VA-*` code means "cancelled" — inventing one is forbidden `[D-55]`. An operator cancel is an
internal intervention, which is what `VA-INT-001` denotes."""


class CancelRequest(BaseModel):
    """A pending cancellation. Written once; the loop reads it at every `decide()`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: CancelActor
    requested_at: datetime
    reason: str = Field(default="", max_length=200)

    @property
    def outcome(self) -> Outcome:
        """`FAILED` for a client, `ESCALATED` for an operator `[D-12]`."""
        return ACTOR_OUTCOMES[self.actor]

    @property
    def reason_code(self) -> ErrorCode:
        return ACTOR_CODES[self.actor]

    @property
    def human_reason(self) -> str:
        """A sentence for the error envelope, naming the actor and never quoting free text.

        `reason` is caller-supplied and therefore untrusted `AGENT.md` §1.4; it is recorded on
        the request but is not spliced into the message that reaches a prompt or a log.
        """
        return f"the job was cancelled by the {self.actor.value}"


class CancelResult(BaseModel):
    """What `cancel()` returns: whether the flag was set, and the outcome now in force.

    `accepted=False` with an `outcome` is the already-terminal case — not an error, and
    distinguishable from a fresh cancel by any caller that needs to be.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    outcome: Outcome | None = None
