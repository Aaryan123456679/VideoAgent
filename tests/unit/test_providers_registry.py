"""Tests for `PinnedProviderRegistry`. `providers.md` §3-4, `[D-32]`, `[D-62]`."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from tests.gateway_doubles import FixedJitter, ManualClock
from tests.providers_doubles import a_shot_request, flaky_provider, full_capability_provider
from video_agent.gateway.breaker import CircuitBreaker, InMemoryCircuitStateStore
from video_agent.gateway.retry import RetryPolicy
from video_agent.harness.budget import BudgetView
from video_agent.harness.context import NodeContext
from video_agent.providers.errors import (
    ProviderGroupExhaustedError,
    ProviderPaymentRequiredError,
    ProviderUnavailableError,
)
from video_agent.providers.registry import PinnedProviderRegistry

_RETRY_ATTEMPTS = RetryPolicy().max_attempts


def _ctx() -> NodeContext:
    return NodeContext.for_node(
        job_id=uuid4(),
        node="generate_shot",
        trace_id="trace-1",
        budget_remaining=BudgetView(
            usd_remaining=Decimal("5.00"),
            tokens_remaining=1000,
            wall_clock_s_remaining=600.0,
            iterations_remaining=10,
        ),
    )


def _registry(*providers: object, retry: RetryPolicy | None = None) -> PinnedProviderRegistry:
    clock = ManualClock()
    breaker = CircuitBreaker(store=InMemoryCircuitStateStore(), clock=clock)
    return PinnedProviderRegistry(
        providers=tuple(providers),  # type: ignore[arg-type]
        breaker=breaker,
        retry=retry or RetryPolicy(),
        clock=clock,
        jitter=FixedJitter(1.0),
    )


@pytest.mark.asyncio
async def test_pins_the_provider_that_served_shot_zero_and_skips_the_failed_one_next_shot() -> (
    None
):
    unreachable = flaky_provider("unreachable", failures=_RETRY_ATTEMPTS)
    reliable = full_capability_provider("reliable")
    registry = _registry(unreachable, reliable)
    ctx = _ctx()
    job_id = uuid4()

    shot0 = a_shot_request(job_id=job_id, shot_index=0, request_fingerprint="fp-0")
    result0 = await registry.generate(shot0, ctx=ctx)
    assert result0.provider_key == "reliable"
    assert len(unreachable.calls) == _RETRY_ATTEMPTS
    assert len(reliable.calls) == 1

    shot1 = a_shot_request(job_id=job_id, shot_index=1, request_fingerprint="fp-1")
    result1 = await registry.generate(shot1, ctx=ctx)
    assert result1.provider_key == "reliable"
    assert len(unreachable.calls) == _RETRY_ATTEMPTS
    assert len(reliable.calls) == _RETRY_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_mid_job_switch_away_from_the_pinned_provider_flags_degraded() -> None:
    unstable = full_capability_provider("unstable")
    unstable.outcomes = [None, ProviderUnavailableError("down"), ProviderUnavailableError("down"),
                          ProviderUnavailableError("down")]
    backup = full_capability_provider("backup")
    registry = _registry(unstable, backup)
    ctx = _ctx()
    job_id = uuid4()

    shot0 = a_shot_request(job_id=job_id, shot_index=0, request_fingerprint="fp-0")
    result0 = await registry.generate(shot0, ctx=ctx)
    assert result0.provider_key == "unstable"
    assert not result0.degraded

    shot1 = a_shot_request(job_id=job_id, shot_index=1, request_fingerprint="fp-1")
    result1 = await registry.generate(shot1, ctx=ctx)
    assert result1.provider_key == "backup"
    assert result1.degraded
    assert result1.degrade_reason == "provider_switch_mid_job"


@pytest.mark.asyncio
async def test_group_exhausted_raises_when_every_candidate_fails() -> None:
    always_down_a = flaky_provider("a", failures=_RETRY_ATTEMPTS)
    always_down_b = flaky_provider("b", failures=_RETRY_ATTEMPTS)
    registry = _registry(always_down_a, always_down_b)
    shot = a_shot_request(job_id=uuid4(), shot_index=0, request_fingerprint="fp-0")

    with pytest.raises(ProviderGroupExhaustedError):
        await registry.generate(shot, ctx=_ctx())


@pytest.mark.asyncio
async def test_payment_required_short_circuits_without_trying_the_next_candidate() -> None:
    exhausted = full_capability_provider("exhausted")
    exhausted.outcomes = [
        ProviderPaymentRequiredError(what_happened="credits exhausted", what_was_preserved="n/a")
    ]
    alive = full_capability_provider("alive")
    registry = _registry(exhausted, alive)
    shot = a_shot_request(job_id=uuid4(), shot_index=0, request_fingerprint="fp-0")

    with pytest.raises(ProviderPaymentRequiredError):
        await registry.generate(shot, ctx=_ctx())

    assert alive.calls == []
