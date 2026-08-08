"""Failure signatures: what counts as *the same failure twice*, and how far it stops the job.

`[CPS §Agent harness]` is unambiguous — *same failure signature twice → `FAILED_NO_PROGRESS`,
stop immediately* — and the `[PRD §How it works]` repair loop is equally unambiguous that a
failed shot is retried, by construction, with the same node and the same code. Read literally
together, the first repair attempt would end every job that ever needed one. `[D-02]` and
`[D-18]` resolve it, and the resolution is entirely in this module.

**Scope is what makes both rules true at once.** A signature is `shot`-scoped or `job`-scoped.
Shot scope seen twice abandons *that shot* and the job continues — which is what keeps
"never returns nothing" `[PRD §Resilience]` true, and stops the second repair being spent on a
provably stuck shot. Job scope seen twice ends the job. The distinction is not a heuristic: a
planner that produces beats summing to the wrong duration will do it again, whereas a shot that
scored 0.58 may well score 0.71 on the next roll.

**Promotion is what stops scope being a loophole.** The same defect on shot 1 and then on
shot 2 is a systemic fault wearing a per-shot disguise — a bible that cannot be rendered, a
provider degrading, a prompt template that lost a field. So a shot-scope digest recurring on a
*different* shot index is promoted to job scope and ends the job. This is why the digest
deliberately excludes the shot index: including it, as `harness.md` §6.1's example
discriminator does, would give every shot its own signature space and make promotion
unreachable. The index is carried beside the digest instead, so both questions — *again on
this shot?* and *again on another shot?* — are answerable from the same counter.

**The score band is what makes a repair loop able to make progress.** `[D-18]`: the QC
discriminator buckets the score into a 0.05-wide band, so a repair that improves the score by
at least 0.05 always lands in a higher bucket and therefore produces a different signature —
progress, continue. An improvement smaller than the band width usually lands in the same bucket
and counts as no progress. *Usually*, not always: fixed buckets mean a +0.04 that straddles a
boundary reads as progress. That asymmetry is the safe one. Reading real progress as stagnation
abandons a shot that was about to succeed; reading a tiny wobble as progress costs one more
attempt, which the repair budget of two `[PRD]` already bounds.

**Counts are mirrored, not merely cached.** Redis holds `sig:{job_id}` for the live job, and
the same counts go into the checkpoint. Redis is a cache with an eviction policy; a flush would
otherwise reset a job's memory of what has already failed and let it retry a dead path from
zero, which is precisely the spend `[CPS]` wants stopped.
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from video_agent.observability.codes import ErrorCode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Mapping

__all__ = [
    "FailureSignature",
    "RepeatInfo",
    "SignatureLedger",
    "SignatureScope",
    "qc_discriminator",
    "score_band",
]

SCORE_BAND_WIDTH: Final = Decimal("0.05")
"""The bucket width from `[D-18]`. An improvement of at least this much is always progress."""

DIGEST_SEPARATOR: Final = "|"
"""Field separator inside the digest pre-image. Not a character any field may contain."""

NO_SHOT: Final = "-"
"""Stands in for the shot index of a job-scope signature, which belongs to no shot."""

REPEAT_THRESHOLD: Final = 2
"""*Twice* is the `[CPS §Agent harness]` threshold, verbatim. Not tunable — a third attempt at
a signature already seen twice is the runaway this rule exists to stop."""

DISTINCT_SHOT_PROMOTION_THRESHOLD: Final = 2
"""How many distinct shot indices a shot-scope digest must touch before it is systemic."""


class SignatureScope(StrEnum):
    """How far a repeated failure reaches. `[D-02]`."""

    SHOT = "shot"
    JOB = "job"


class FailureSignature(BaseModel):
    """One failure, reduced to the facts that decide whether the next one is *the same*.

    `shot_index` sits outside the digest on purpose — see the module docstring. It is typed as
    optional because a job-scope failure genuinely has no shot, and defaulting it to `0` would
    make every planner failure look like a failure of the first shot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: SignatureScope
    node: str = Field(min_length=1)
    code: ErrorCode
    discriminator: str = ""
    shot_index: int | None = Field(default=None, ge=0)

    def digest(self) -> str:
        """A sha256 over scope, node, code and discriminator — stable across processes.

        `hashlib` rather than `hash()`: the built-in is salted per process by default, so a
        count keyed on it would be meaningless the moment a second worker touched the job, and
        the bug would only appear under the concurrency the production deployment has and the
        test run does not.
        """
        pre_image = DIGEST_SEPARATOR.join(
            (self.scope.value, self.node, self.code.value, self.discriminator)
        )
        return hashlib.sha256(pre_image.encode("utf-8")).hexdigest()

    @property
    def shot_field(self) -> str:
        """The per-shot counter field for this signature."""
        index = NO_SHOT if self.shot_index is None else str(self.shot_index)
        return f"{self.digest()}#{index}"


class RepeatInfo(BaseModel):
    """What `record_failure` tells the loop: whether this failure has been seen before, and how far.

    `effective_scope` is separate from the signature's own scope because promotion changes the
    answer without changing the signature. A caller reading `sig.scope` would abandon a shot on
    a defect that has already proved itself systemic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    digest: str
    repeat: bool
    effective_scope: SignatureScope
    count: int = Field(ge=1)
    shot_count: int = Field(ge=1)
    promoted: bool = False

    @property
    def terminates_job(self) -> bool:
        """Whether this repeat ends the job — job scope, seen twice. `harness.md` §6.2."""
        return self.repeat and self.effective_scope is SignatureScope.JOB

    @property
    def abandons_shot(self) -> bool:
        """Whether this repeat abandons the shot and lets the job continue."""
        return self.repeat and self.effective_scope is SignatureScope.SHOT


def score_band(score: float | Decimal, width: Decimal = SCORE_BAND_WIDTH) -> str:
    """The 0.05-wide bucket a QC score falls in, rendered as `low-high`. `[D-18]`.

    `Decimal` throughout, and `Decimal(str(...))` on a float input, because the whole point is
    that 0.55 and 0.59 share a bucket while 0.55 and 0.61 do not — and binary floats put
    0.55/0.05 at 10.999999999999998, which floors to the bucket below and inverts the answer at
    exactly the boundaries the rule is about.
    """
    value = score if isinstance(score, Decimal) else Decimal(str(score))
    index = (value / width).to_integral_value(rounding=ROUND_FLOOR)
    low = index * width
    return f"{low:.2f}-{low + width:.2f}"


def qc_discriminator(*, failing_dimensions: Iterable[str], score: float | Decimal) -> str:
    """The QC discriminator from `harness.md` §6.1, minus the shot index.

    Dimensions are sorted so that two failures differing only in the order QC happened to
    report them are the same signature — otherwise a repeat would be missed roughly half the
    time, which is worse than not counting at all because it looks like it works.
    """
    dims = ",".join(sorted(failing_dimensions))
    return f"dims={dims};band={score_band(score)}"


class SignatureLedger(BaseModel):
    """Per-job signature counts, mirrored between Redis and the checkpoint.

    Two counters per signature, not one. The digest counter answers *has this defect happened
    before anywhere in this job*, which is what promotion needs; the per-shot counter answers
    *has it happened before on this shot*, which is what abandonment needs. One counter cannot
    answer both, and deriving either from a scan of the other is a per-failure `HGETALL` for a
    number an increment already returned.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    counts: dict[str, int] = Field(default_factory=dict)

    def record(self, signature: FailureSignature) -> RepeatInfo:
        """Count one failure and report what it means.

        The three outcomes in order of severity: seen twice on this shot (repeat at the
        signature's own scope), seen for the first time on this shot but not for the first time
        in this job (promotion — systemic), or genuinely new.
        """
        digest = signature.digest()
        total = self._increment(digest)
        shot_total = self._increment(signature.shot_field)
        if shot_total >= REPEAT_THRESHOLD:
            return RepeatInfo(
                digest=digest,
                repeat=True,
                effective_scope=signature.scope,
                count=total,
                shot_count=shot_total,
            )
        promoted = (
            signature.scope is SignatureScope.SHOT
            and total >= DISTINCT_SHOT_PROMOTION_THRESHOLD
        )
        return RepeatInfo(
            digest=digest,
            repeat=promoted,
            effective_scope=SignatureScope.JOB if promoted else signature.scope,
            count=total,
            shot_count=shot_total,
            promoted=promoted,
        )

    def count_of(self, signature: FailureSignature) -> int:
        """How many times this exact signature has been recorded, on any shot."""
        return self.counts.get(signature.digest(), 0)

    def snapshot(self) -> dict[str, int]:
        """The counts, as they go into the checkpoint. A copy: the checkpoint is not live state."""
        return dict(self.counts)

    @classmethod
    def restore(cls, snapshot: Mapping[str, int]) -> SignatureLedger:
        """Rebuild from a checkpoint after a Redis flush. `harness.md` §6.2."""
        return cls(counts=dict(snapshot))

    def _increment(self, field: str) -> int:
        updated = self.counts.get(field, 0) + 1
        self.counts = {**self.counts, field: updated}
        return updated
