"""Cost per call, from the price table, keyed on the concrete model.

`gateway.md` §6 and `[D-21]`: cost comes from a per-model price table, and an unknown model
prices at *a configured pessimistic ceiling rather than zero, so an unpriced model can never
look free to a budget cap*. The failure that guards against is a rename: a provider ships
`<something>-v2`, the alias table still resolves, the price lookup misses, and every call
thereafter costs nothing as far as the cap is concerned. The job then runs until it hits the
wall-clock cap instead — having spent real money the ledger says was free.

**Keyed on the concrete model, never on the alias.** An alias group's members differ in price
by an order of magnitude in the landed table; pricing at the alias would make the cost of a job
depend on nothing observable, and a fallback to a pricier sibling would be invisible to the
cap.

**`Decimal` throughout, and nothing is rounded.** `S0.7.6` acceptance 4 asks that a job's
`Usage.cost_usd` total equal the sum of its generation costs *exactly*. Rounding per call makes
that false by construction — a thousandth of a cent per call, times a thousand calls — and
"approximately equal" is not a property anyone can test. The arithmetic here is exact:
`Decimal` division by 1000 of an exact decimal rate terminates.

**A cached response costs zero tokens and is still recorded.** `S0.7.6` acceptance 5. A cache
hit that emitted no `Usage` at all would make the ledger's call count disagree with the trace's,
and "how many calls did this job make" is the first question asked when a cost looks wrong.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

from video_agent.gateway.models import Usage
from video_agent.observability.alarms import AlarmCounter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.config.aliases import AliasTable

__all__ = ["UNPRICED_MODEL_ALARM", "CostCalculator", "cached_usage"]

TOKENS_PER_PRICE_UNIT: Final = Decimal(1000)
"""Prices in `config/aliases.yaml` are per 1,000 tokens, in both directions."""

UNPRICED_MODEL_ALARM: Final[AlarmCounter] = AlarmCounter("gateway_unpriced_model")
"""Counts calls charged at the ceiling because the price table had no entry `[D-21]`.

The loader already refuses to start if a model an alias *references* is unpriced, so a non-zero
count here means a model appeared at runtime that the configuration has never seen. That is a
config alarm, not a cost alarm: the charge is deliberately punitive so the cap still holds while
somebody fixes the table.
"""


def cached_usage() -> Usage:
    """The `Usage` for a cache hit: no tokens, no cost, still a recorded call."""
    return Usage(input_tokens=0, output_tokens=0, cost_usd=Decimal(0), cost_is_ceiling=False)


class CostCalculator:
    """Turns token counts into `Usage`, alarming when it has to guess.

    Holds the `AliasTable` rather than a copy of the prices, so a process that reloaded its
    table would price against the table it is actually routing on.
    """

    def __init__(self, table: AliasTable) -> None:
        self._table = table

    def usage_for(self, *, model: str, input_tokens: int, output_tokens: int) -> Usage:
        """Cost this call at the table's rate for `model`, or at the ceiling if it has none."""
        price = self._table.price_for(model)
        priced = self._table.is_priced(model)
        if not priced:
            UNPRICED_MODEL_ALARM.increment()
        cost = (
            Decimal(input_tokens) / TOKENS_PER_PRICE_UNIT * price.input_usd_per_1k_tokens
            + Decimal(output_tokens) / TOKENS_PER_PRICE_UNIT * price.output_usd_per_1k_tokens
        )
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            cost_is_ceiling=not priced,
        )
