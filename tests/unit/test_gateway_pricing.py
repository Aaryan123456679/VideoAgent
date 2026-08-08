"""`S0.7.6` — usage and cost: priced on the concrete model, exact, and never zero for an unknown.

The acceptance criterion that dictates the implementation is "the sum over a job equals the sum
of the generation costs, exactly". `test_a_job_total_is_exactly_the_sum_of_its_calls` is
therefore written with values chosen so that a rounding implementation fails: ten calls whose
individual costs do not terminate at two decimal places. A test using round numbers would pass
against an implementation that rounded every call to the cent, and the discrepancy would only
appear on a real invoice.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel

from tests.gateway_doubles import (
    MODEL_A,
    MODEL_B,
    UNPRICED_MODEL,
    HarnessOverrides,
    ScriptedTransport,
    a_request,
    alias_table,
    build_harness,
    ok,
    price,
)
from video_agent.config.aliases import Alias, AliasEntry, ModelRef
from video_agent.gateway import CallContext
from video_agent.gateway.pricing import UNPRICED_MODEL_ALARM, CostCalculator, cached_usage
from video_agent.gateway.transport import UpstreamStatusError

TOTAL_CALLS = 10
TOKENS_PER_CALL = 1000
BILLED_ACROSS_A_REFORMAT = TOKENS_PER_CALL * 2
"""Two calls were made, so two are charged."""


def test_cost_is_computed_from_the_price_table_for_the_concrete_model() -> None:
    """Golden: 1,000 input at $0.001/1k and 500 output at $0.01/1k is exactly $0.006."""
    calculator = CostCalculator(alias_table())
    usage = calculator.usage_for(model=MODEL_A, input_tokens=1000, output_tokens=500)
    assert usage.cost_usd == Decimal("0.006")
    assert usage.cost_is_ceiling is False


def test_two_models_in_one_group_price_differently() -> None:
    """Acceptance 1: keyed on the model, not the alias.

    Priced at the alias, a failover to a pricier sibling would be invisible to the budget cap —
    the job would cost twice as much and the ledger would not notice.
    """
    calculator = CostCalculator(alias_table())
    primary = calculator.usage_for(model=MODEL_A, input_tokens=1000, output_tokens=1000)
    fallback = calculator.usage_for(model=MODEL_B, input_tokens=1000, output_tokens=1000)
    assert primary.cost_usd == Decimal("0.011")
    assert fallback.cost_usd == Decimal("0.022")


def test_an_unpriced_model_charges_the_ceiling_and_raises_the_config_alarm() -> None:
    """`[D-21]`: never zero. A model that looks free is a USD cap that a rename can evade."""
    UNPRICED_MODEL_ALARM.reset()
    calculator = CostCalculator(alias_table())
    usage = calculator.usage_for(model=UNPRICED_MODEL, input_tokens=1000, output_tokens=1000)
    assert usage.cost_usd == Decimal("0.200")
    assert usage.cost_is_ceiling is True
    assert UNPRICED_MODEL_ALARM.count == 1


def test_an_unpriced_model_is_more_expensive_than_every_priced_one() -> None:
    """The ceiling is pessimistic by construction, not merely non-zero.

    A ceiling below the priced models would still be non-zero and would still under-charge, so
    the cap would still fail to hold — just less obviously.
    """
    calculator = CostCalculator(alias_table())
    ceiling = calculator.usage_for(model=UNPRICED_MODEL, input_tokens=1000, output_tokens=1000)
    priced = [
        calculator.usage_for(model=model, input_tokens=1000, output_tokens=1000).cost_usd
        for model in (MODEL_A, MODEL_B)
    ]
    assert all(ceiling.cost_usd > cost for cost in priced)


def test_a_job_total_is_exactly_the_sum_of_its_calls() -> None:
    """Acceptance 4: exactly equal, not approximately.

    The token counts are chosen so no individual cost terminates at two decimal places, which
    is what makes a per-call rounding implementation fail here rather than pass by luck.
    """
    calculator = CostCalculator(alias_table())
    per_call = [
        calculator.usage_for(model=MODEL_A, input_tokens=333, output_tokens=77)
        for _ in range(TOTAL_CALLS)
    ]
    total = sum((usage.cost_usd for usage in per_call), Decimal(0))
    expected = (Decimal(333) / 1000 * Decimal("0.00100")) + (
        Decimal(77) / 1000 * Decimal("0.01000")
    )
    assert total == expected * TOTAL_CALLS
    assert total != Decimal("0")


def test_a_cached_response_costs_zero_tokens_and_is_still_a_recorded_call() -> None:
    """Acceptance 5. A hit that emitted no `Usage` would desynchronise the call count."""
    usage = cached_usage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cost_usd == Decimal(0)
    assert usage.cost_is_ceiling is False


@pytest.mark.asyncio
async def test_usage_is_returned_on_a_degraded_fallback_served_response() -> None:
    """Acceptance 3: every response carries `Usage`, including the ones that degraded."""
    transport = ScriptedTransport(
        {
            MODEL_A: [UpstreamStatusError(503, "{}")],
            MODEL_B: [ok(input_tokens=1000, output_tokens=1000)],
        }
    )
    harness = build_harness(transport)
    response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert response.degraded is True
    assert response.usage.cost_usd == Decimal("0.022")


@pytest.mark.asyncio
async def test_a_runtime_only_model_prices_at_the_ceiling_end_to_end() -> None:
    """A model the config has never seen still costs something. `[D-21]` at the call site."""
    UNPRICED_MODEL_ALARM.reset()
    table = alias_table(
        aliases={Alias.REASONING_HIGH: AliasEntry(primary=ModelRef(model=UNPRICED_MODEL))},
        prices={MODEL_A: price("0.00100", "0.01000")},
    )
    transport = ScriptedTransport({UNPRICED_MODEL: [ok(input_tokens=1000, output_tokens=1000)]})
    harness = build_harness(transport, HarnessOverrides(table=table))
    response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert response.usage.cost_usd == Decimal("0.200")
    assert response.usage.cost_is_ceiling is True
    assert UNPRICED_MODEL_ALARM.count == 1


@pytest.mark.asyncio
async def test_the_reformat_attempt_is_billed_as_well() -> None:
    """Two calls were made and two are charged. Billing only the successful one understates."""

    class Shape(BaseModel):
        value: int

    transport = ScriptedTransport(
        {
            MODEL_A: [
                ok("nope", input_tokens=1000, output_tokens=1000),
                ok('{"value": 1}', input_tokens=1000, output_tokens=1000),
            ]
        }
    )
    harness = build_harness(transport)
    response = await harness.gateway.call(
        a_request(response_model=Shape), ctx=CallContext(job_id="j", node="qc")
    )
    assert response.usage.input_tokens == BILLED_ACROSS_A_REFORMAT
    assert response.usage.output_tokens == BILLED_ACROSS_A_REFORMAT
