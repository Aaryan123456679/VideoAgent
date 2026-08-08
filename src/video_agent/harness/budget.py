"""Hard caps on iterations, wall-clock, tokens and dollars, and the ledger that enforces them.

`[CPS §Non-negotiables]` lists hard budget caps and sets no numbers, so every number is
configuration `[D-08]` and this module contains none. `BudgetCaps.from_settings` is the only
constructor a running job uses; the literals live in `.env.example` and `Settings`, which is
what makes a cap changeable without a deploy and un-hard-codeable by accident.

**Pre-flight is the enforcement point; post-charge is only bookkeeping.** `harness.md` §4:
before an expensive act the harness asks `would_exceed(estimate)`, and a call the cap would
refuse is never made. Noticing afterwards records an overspend, it does not prevent one — and
for `video.generate` the money is gone by the time the response arrives. This is why
`would_exceed` returns a `BudgetBreach` naming the axis rather than a bool: the caller has to
report *which* cap stopped it (`VA-BUDGET-001`..`004` are four codes, not one), and a bool
forces it to re-derive that by re-checking each axis, which is where the two answers drift.

**The ledger reconciles; it does not merely accumulate.** `[D-60]`: the v1 provider bills in
credits and reports a **provisional** amount that settles — and is refunded on a failed render
— only at terminal status. So a charge has two states and exactly one transition between
them. Corrections are allowed once, because the settlement is the truth; a second correction
is refused, because with two of them the ledger cannot say which is real. Charges are keyed by
id and idempotent, which is what makes "crash between the provider charge and the checkpoint"
resolve to *charged once* rather than *charged twice* or *charged never*.

**Estimates over-state, never under-state.** `[D-65]`: the credits-to-USD rate is the
undiscounted list rate, so a volume discount can only make the actual cost lower than the
estimate. That direction matters here and nowhere else: pre-flight admits a call only if the
*estimate* fits under the cap, so an estimate that is a ceiling makes terminal spend a
guaranteed under-estimate of the cap. Were estimates allowed to under-state, a single call
could overshoot by its own estimation error — bounded, recorded and immediately fatal to the
next pre-flight, but an overshoot. A discount applied pre-flight would let a job run *further*
before tripping the cap, which is why it is a reconciliation-time credit and never an
allowance.

**Wall-clock comes from the persisted `started_at`.** `AGENT.md` §1.1: never reset the budget
ledger on resume. A monotonic counter restarts at zero when the process does, so a job that
crash-looped would have an unbounded wall-clock budget while appearing to respect one.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from video_agent.harness.errors import ChargeConflictError, SettlementError
from video_agent.observability.codes import ErrorCode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from video_agent.config.settings import Settings

__all__ = [
    "BudgetAxis",
    "BudgetBreach",
    "BudgetCaps",
    "BudgetLedger",
    "BudgetView",
    "Charge",
    "ChargeState",
    "CostEstimate",
]

ZERO: Final = Decimal(0)


class BudgetAxis(StrEnum):
    """The four axes `[CPS §Non-negotiables]` names. Each maps to its own taxonomy code."""

    USD = "usd"
    WALL_CLOCK = "wall_clock"
    TOKENS = "tokens"
    ITERATIONS = "iterations"


AXIS_CODES: Final[Mapping[BudgetAxis, ErrorCode]] = {
    BudgetAxis.USD: ErrorCode.VA_BUDGET_001,
    BudgetAxis.WALL_CLOCK: ErrorCode.VA_BUDGET_002,
    BudgetAxis.TOKENS: ErrorCode.VA_BUDGET_003,
    BudgetAxis.ITERATIONS: ErrorCode.VA_BUDGET_004,
}
"""Axis → code. One code per axis, so an alert can say *which* budget ran out.

Collapsing the four into one `VA-BUDGET-001` would make "we ran out of time" and "we ran out
of money" indistinguishable in the one place an operator looks, and they need opposite fixes.
"""

AXIS_ORDER: Final[tuple[BudgetAxis, ...]] = (
    BudgetAxis.USD,
    BudgetAxis.WALL_CLOCK,
    BudgetAxis.TOKENS,
    BudgetAxis.ITERATIONS,
)
"""The order axes are checked in when more than one is breached.

Fixed rather than dictionary order so the reported code is deterministic across runs: a job
that exhausts money and time in the same superstep must not report `VA-BUDGET-001` on one
machine and `VA-BUDGET-002` on another. Money first because it is the cap
`[PRD §Key risks]` names, and the one an operator most needs to see attributed correctly.
"""


class BudgetBreach(BaseModel):
    """Which cap broke, by how much, and under which code.

    Carries the numbers as well as the axis because the operator question after a `PARTIAL` is
    always *how close was it* — a job stopped at 101% of its cap needs a different response
    from one stopped at 400%.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    axis: BudgetAxis
    limit: Decimal
    projected: Decimal

    @property
    def code(self) -> ErrorCode:
        """The `VA-BUDGET-*` code for this axis."""
        return AXIS_CODES[self.axis]

    @property
    def human_reason(self) -> str:
        """A sentence for the error envelope. Numbers only — nothing here is user content."""
        return (
            f"the {self.axis.value} budget cap of {self.limit} would be exceeded "
            f"at {self.projected}"
        )


class CostEstimate(BaseModel):
    """What an act is expected to consume, on every axis it can consume.

    All four fields default to nothing so a caller states only what its act spends, and an axis
    it forgot reads as zero rather than as an unstated assumption. `supersteps` is here because
    the iteration cap must be checkable pre-flight too: a loop that only notices it has run out
    of supersteps *after* the fortieth is a loop with forty-one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    usd: Decimal = Field(default=ZERO, ge=0)
    tokens: int = Field(default=0, ge=0)
    wall_clock_s: float = Field(default=0.0, ge=0.0)
    supersteps: int = Field(default=0, ge=0)


class BudgetCaps(BaseModel):
    """The four hard caps for one job. Every value comes from configuration `[D-08]`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_iterations: int = Field(gt=0)
    max_wall_clock_s: float = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_usd: Decimal = Field(gt=0)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        tenant_max_usd_per_job: Decimal | None = None,
    ) -> BudgetCaps:
        """Build the caps for a job from settings and the tenant's optional USD override.

        `max_usd_for_tenant` is consumed rather than reimplemented: `[D-70]` makes
        `tenant.max_usd_per_job` NULL mean *inherit the global cap*, and a second reading of
        that column somewhere else is how NULL comes to mean *unlimited* in one code path.
        """
        return cls(
            max_iterations=settings.BUDGET_MAX_SUPERSTEPS,
            max_wall_clock_s=float(settings.BUDGET_MAX_WALL_CLOCK_SECONDS),
            max_tokens=settings.BUDGET_MAX_TOKENS,
            max_usd=settings.max_usd_for_tenant(tenant_max_usd_per_job),
        )


class BudgetView(BaseModel):
    """What a node is told about the budget: how much is left, never how to spend it.

    Read-only and derived. A node receiving the ledger itself could charge against it, and
    `harness.md` §1 puts budgets in the harness precisely so that nothing else can.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    usd_remaining: Decimal
    tokens_remaining: int
    wall_clock_s_remaining: float
    iterations_remaining: int


class ChargeState(StrEnum):
    """A charge is provisional until the provider says the render is terminal `[D-60]`."""

    PROVISIONAL = "provisional"
    FINAL = "final"


class Charge(BaseModel):
    """One recorded cost, identified so that recording it twice is recording it once.

    `charge_id` is the caller's idempotency handle — for a render it is the attempt's request
    fingerprint `[D-24]`, which is already unique per attempt and already survives a crash in
    the database. Minting an id here instead would make the resume path generate a *new* id for
    the same money.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    charge_id: str = Field(min_length=1)
    usd: Decimal = Field(default=ZERO, ge=0)
    tokens: int = Field(default=0, ge=0)
    state: ChargeState = ChargeState.PROVISIONAL


class BudgetLedger(BaseModel):
    """One job's spend on all four axes, with the pre-flight veto and the settle-once rule.

    `usd_spent` and `tokens_used` are derived from the charge table rather than stored beside
    it. A stored total and a charge list are two representations of one fact, and a refund that
    updates one and not the other is a ledger that disagrees with itself — which is exactly the
    bug `[D-60]`'s reconciliation invites.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    caps: BudgetCaps
    started_at: datetime
    iterations_used: int = Field(default=0, ge=0)
    charges: dict[str, Charge] = Field(default_factory=dict)

    @property
    def usd_spent(self) -> Decimal:
        """Total USD across every charge, provisional charges counted at their current amount.

        An unsettled provisional is counted, never treated as free `harness.md` §8: a render
        whose settlement never arrives has still consumed credits, and reading it as zero would
        hand a stuck job an unlimited budget.
        """
        return sum((charge.usd for charge in self.charges.values()), start=ZERO)

    @property
    def tokens_used(self) -> int:
        return sum(charge.tokens for charge in self.charges.values())

    def wall_clock_s(self, now: datetime) -> float:
        """Elapsed seconds since the persisted `started_at`.

        Never from a process-local monotonic origin. A resumed job continues accruing, which is
        what `AGENT.md` §1.1 means by *never reset the budget ledger on resume* — a crash-loop
        must not buy more wall-clock budget by crashing.
        """
        return max((now - self.started_at).total_seconds(), 0.0)

    def view(self, now: datetime) -> BudgetView:
        """The read-only remainder handed to a node in its `NodeContext`."""
        return BudgetView(
            usd_remaining=max(self.caps.max_usd - self.usd_spent, ZERO),
            tokens_remaining=max(self.caps.max_tokens - self.tokens_used, 0),
            wall_clock_s_remaining=max(self.caps.max_wall_clock_s - self.wall_clock_s(now), 0.0),
            iterations_remaining=max(self.caps.max_iterations - self.iterations_used, 0),
        )

    def exceeded(self, now: datetime) -> BudgetBreach | None:
        """The first cap already breached, or `None`. Checked at `evaluate` every superstep."""
        return self._breach(CostEstimate(), now)

    def would_exceed(self, estimate: CostEstimate, now: datetime) -> BudgetBreach | None:
        """The first cap this act would breach, or `None` — the pre-flight veto.

        Always the estimate, never a measured cost: the measured cost of a call that has not
        been made does not exist, and the only alternative to trusting the estimate is making
        the call to find out, which is the spend the cap exists to prevent.
        """
        return self._breach(estimate, now)

    def _breach(self, estimate: CostEstimate, now: datetime) -> BudgetBreach | None:
        projected: Mapping[BudgetAxis, tuple[Decimal, Decimal]] = {
            BudgetAxis.USD: (
                self.caps.max_usd,
                self.usd_spent + estimate.usd,
            ),
            BudgetAxis.WALL_CLOCK: (
                Decimal(str(self.caps.max_wall_clock_s)),
                Decimal(str(self.wall_clock_s(now) + estimate.wall_clock_s)),
            ),
            BudgetAxis.TOKENS: (
                Decimal(self.caps.max_tokens),
                Decimal(self.tokens_used + estimate.tokens),
            ),
            BudgetAxis.ITERATIONS: (
                Decimal(self.caps.max_iterations),
                Decimal(self.iterations_used + estimate.supersteps),
            ),
        }
        for axis in AXIS_ORDER:
            limit, amount = projected[axis]
            if amount > limit:
                return BudgetBreach(axis=axis, limit=limit, projected=amount)
        return None

    def count_superstep(self) -> None:
        """Record that one superstep was consumed. Called once per node, by the harness."""
        self.iterations_used += 1

    def apply(self, charge: Charge) -> None:
        """Record a charge, idempotently by id.

        Re-applying an identical charge is a no-op because that is the resume path: the process
        died between the provider taking the money and the checkpoint recording it, and the
        redelivered step must observe the charge exactly once. Re-applying a *different* charge
        under the same id is refused, because two costs sharing an identity means one of them
        is unrecorded whichever way it is resolved.
        """
        existing = self.charges.get(charge.charge_id)
        if existing is None:
            self.charges = {**self.charges, charge.charge_id: charge}
            return
        if existing != charge:
            message = (
                f"charge {charge.charge_id!r} was already recorded with different values; "
                "a charge id identifies one cost and may not be reused"
            )
            raise ChargeConflictError(message)

    def settle(self, charge_id: str, *, usd: Decimal, tokens: int | None = None) -> None:
        """Correct a provisional charge to its settled amount. Exactly once `[D-60]`.

        The settled amount may be lower than the provisional one — that is a refund on a failed
        render, and `[D-60]` requires it to reach the ledger. It may also be higher, and the
        ledger records that truthfully rather than clamping: a clamped ledger under-reports
        spend, and the next pre-flight would then authorise a call on money already gone.
        """
        existing = self.charges.get(charge_id)
        if existing is None:
            message = f"charge {charge_id!r} cannot be settled because it was never recorded"
            raise SettlementError(message)
        if existing.state is ChargeState.FINAL:
            message = (
                f"charge {charge_id!r} is already final; a provisional charge settles exactly "
                "once and a second settlement cannot be told from a double count"
            )
            raise SettlementError(message)
        settled = existing.model_copy(
            update={
                "usd": usd,
                "tokens": existing.tokens if tokens is None else tokens,
                "state": ChargeState.FINAL,
            }
        )
        self.charges = {**self.charges, charge_id: settled}

    def refund(self, charge_id: str, *, refunded_usd: Decimal | None = None) -> None:
        """Return credits for a failed render and mark the charge final `[D-60]`.

        A refund is a settlement, not a separate kind of event: the provider refunds at
        terminal status, which is the same moment the charge stops being provisional. Modelling
        it separately would allow a charge to be both refunded and settled.
        """
        existing = self.charges.get(charge_id)
        if existing is None:
            message = f"charge {charge_id!r} cannot be refunded because it was never recorded"
            raise SettlementError(message)
        amount = existing.usd if refunded_usd is None else refunded_usd
        self.settle(charge_id, usd=max(existing.usd - amount, ZERO))

    def unsettled_ids(self) -> tuple[str, ...]:
        """Charge ids still provisional, in insertion order. Read by the settlement sweeper."""
        return tuple(
            charge_id
            for charge_id, charge in self.charges.items()
            if charge.state is ChargeState.PROVISIONAL
        )
