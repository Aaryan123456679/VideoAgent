"""`S0.7.2` — retryable-only, max 3 attempts, exponential backoff with bounded jitter.

Every timing assertion here runs against an injected clock and an injected jitter source, so the
whole file completes in milliseconds. That is not a convenience: `gateway.md` §9 asks for a
*deterministic-jitter* test, and a suite that took real seconds to prove backoff would be the
first thing disabled when CI got slow, at which point the rule would be unproven and nobody
would know.

Two shapes of assertion are deliberately avoided. The backoff test asserts the *observable*
schedule — the delays actually requested of the clock — rather than that the implementation
called its own helper, which would pass just as happily if the helper returned a constant. And
the non-retryable table is parametrised over every class in `gateway.md` §4.1 rather than
spot-checked, because the failure mode is one row quietly behaving like the other column.
"""

from __future__ import annotations

import pytest

from tests.gateway_doubles import (
    MODEL_A,
    MODEL_B,
    MODEL_C,
    FixedJitter,
    HarnessOverrides,
    ScriptedTransport,
    a_request,
    build_harness,
    ok,
)
from video_agent.gateway import CallContext
from video_agent.gateway.clock import SystemJitter
from video_agent.gateway.errors import (
    AliasGroupExhaustedError,
    ContentPolicyError,
    ContextLengthExceededError,
    GatewayError,
    PaymentRequiredError,
    UpstreamRequestError,
)
from video_agent.gateway.retry import JITTER_HIGH, JITTER_LOW, MAX_ATTEMPTS, RetryPolicy
from video_agent.gateway.transport import UpstreamNetworkError, UpstreamStatusError

RETRYABLE_STATUSES = (408, 429, 500, 502, 503, 504)
CALLS_AFTER_ONE_TRANSIENT_FAILURE = 2

SPEC_MAX_ATTEMPTS = 3
"""`[CPS §Failure behaviour]`: *retry, max 3*. A literal, spelled out here on purpose.

Written as `MAX_ATTEMPTS` — the constant from the module under test — every assertion below
would move with the implementation, and raising the budget to four would leave the suite green.
That is the shape of a test that cannot fail, and it is the shape this constant exists to
prevent. `test_the_policy_matches_the_specification` is what ties the two together, so a
deliberate change to the policy fails in exactly one place instead of nowhere.
"""

SPEC_BASE_DELAY_SECONDS = 0.5
SPEC_CAP_DELAY_SECONDS = 8.0
SPEC_JITTER_LOW = 0.5
SPEC_JITTER_HIGH = 1.5
GROUP_SIZE = 3


def status(code: int, body: str = "{}") -> UpstreamStatusError:
    """One upstream status failure with a body the classifier will read."""
    return UpstreamStatusError(code, body)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", RETRYABLE_STATUSES)
async def test_exactly_three_attempts_on_a_persistently_retryable_error(code: int) -> None:
    """Acceptance 1: three attempts *total*, not three retries after the first.

    Asserted per retryable status rather than only for `429`, because "max 3" is a property of
    the policy and a status that took a different code path would take a different budget.
    """
    transport = ScriptedTransport({MODEL_A: [status(code)]}, fallback=[status(code)])
    harness = build_harness(transport)
    with pytest.raises(AliasGroupExhaustedError):
        await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert len(transport.calls_for(MODEL_A)) == SPEC_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_the_whole_group_is_walked_with_three_attempts_each() -> None:
    """Three attempts is per model, and the group is three deep: nine calls, then `VA-GW-001`."""
    transport = ScriptedTransport(fallback=[status(503)])
    harness = build_harness(transport)
    with pytest.raises(AliasGroupExhaustedError):
        await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert len(transport.calls) == SPEC_MAX_ATTEMPTS * GROUP_SIZE
    assert transport.models_called.count(MODEL_A) == SPEC_MAX_ATTEMPTS
    assert transport.models_called.count(MODEL_B) == SPEC_MAX_ATTEMPTS
    assert transport.models_called.count(MODEL_C) == SPEC_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_a_network_failure_is_retried() -> None:
    """Connection reset, DNS and read timeout are the left column of `gateway.md` §4.1."""
    failure = UpstreamNetworkError("ConnectError")
    transport = ScriptedTransport({MODEL_A: [failure]}, fallback=[failure])
    harness = build_harness(transport)
    with pytest.raises(AliasGroupExhaustedError):
        await harness.gateway.call(
            a_request(),
            ctx=CallContext(job_id="j", node="plan"),
        )
    assert len(transport.calls_for(MODEL_A)) == SPEC_MAX_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        pytest.param(status(400), UpstreamRequestError, id="400-bad-request"),
        pytest.param(status(401), UpstreamRequestError, id="401-unauthorised"),
        pytest.param(status(403), UpstreamRequestError, id="403-forbidden"),
        pytest.param(status(422), UpstreamRequestError, id="422-unprocessable"),
        pytest.param(status(402), PaymentRequiredError, id="402-payment-required"),
        pytest.param(
            status(400, '{"error": {"type": "content_policy_violation"}}'),
            ContentPolicyError,
            id="content-policy",
        ),
        pytest.param(
            status(400, '{"error": {"message": "context length exceeded"}}'),
            ContextLengthExceededError,
            id="context-length",
        ),
    ],
)
async def test_zero_retries_for_each_non_retryable_class(
    failure: UpstreamStatusError, expected: type[GatewayError]
) -> None:
    """Acceptance 2: exactly one call for each non-retryable class, and no fallback either.

    Every class here would be rejected identically by every other model in the group, so
    retrying burns the budget and failing over burns the group, both to arrive at the same
    answer. The eighth class — schema failure after one reformat — is a different mechanism and
    is asserted in `test_gateway_structured_output.py`.
    """
    transport = ScriptedTransport(fallback=[failure])
    harness = build_harness(transport)
    with pytest.raises(expected):
        await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert len(transport.calls) == 1
    assert harness.clock.sleeps == []


@pytest.mark.asyncio
async def test_a_404_falls_over_without_retrying_the_same_model() -> None:
    """A model the proxy does not serve is non-retryable *and* an availability problem.

    Retrying it is pointless — the proxy will not learn the model — but the group's other
    members are exactly what a failover unit is for. One call to the missing model, then the
    sibling serves.
    """
    transport = ScriptedTransport({MODEL_A: [status(404)], MODEL_B: [ok()]})
    harness = build_harness(transport)
    response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert len(transport.calls_for(MODEL_A)) == 1
    assert response.model_used == MODEL_B


@pytest.mark.asyncio
async def test_402_is_not_retried_and_not_failed_over() -> None:
    """`[D-62]`: payment required escalates. Retrying delays the only fix; failing over hides it."""
    transport = ScriptedTransport(fallback=[status(402)])
    harness = build_harness(transport)
    with pytest.raises(PaymentRequiredError) as caught:
        await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert caught.value.retryable is False
    assert transport.models_called == [MODEL_A]


@pytest.mark.asyncio
async def test_an_overloaded_4xx_is_still_retried() -> None:
    """`gateway.md` §4.1 lists provider "overloaded"/"capacity" as retryable whatever the status.

    Some upstreams deliver overload as a client error. Reading only the status would classify a
    transient condition as permanent and fail a job that a one-second wait would have served.
    """
    transport = ScriptedTransport(
        {MODEL_A: [status(400, '{"error": {"message": "Model is overloaded"}}'), ok()]}
    )
    harness = build_harness(transport)
    response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert len(transport.calls_for(MODEL_A)) == CALLS_AFTER_ONE_TRANSIENT_FAILURE
    assert response.model_used == MODEL_A


def test_the_policy_matches_the_specification() -> None:
    """The policy's own constants against the specification's numbers, in one place.

    Every other assertion in this file uses the `SPEC_` literals rather than the module's
    constants, so this is the single test that fails when the policy is deliberately changed —
    and the only one that can be updated when the specification is.
    """
    assert MAX_ATTEMPTS == SPEC_MAX_ATTEMPTS
    assert JITTER_LOW == SPEC_JITTER_LOW
    assert JITTER_HIGH == SPEC_JITTER_HIGH
    policy = RetryPolicy()
    assert policy.base_delay_s == SPEC_BASE_DELAY_SECONDS
    assert policy.cap_delay_s == SPEC_CAP_DELAY_SECONDS
    assert policy.max_attempts == SPEC_MAX_ATTEMPTS


def test_base_schedule_is_the_documented_sequence() -> None:
    """Acceptance 3, the arithmetic: `min(0.5 * 2**(n-1), 8)` gives `0.5, 1.0, 2.0`."""
    assert RetryPolicy().base_schedule() == (0.5, 1.0, 2.0)


def test_base_schedule_is_monotonically_non_decreasing_and_capped() -> None:
    """The sequence never dips, and the cap truncates it rather than resetting it."""
    schedule = RetryPolicy(max_attempts=8).base_schedule()
    assert list(schedule) == sorted(schedule)
    assert schedule[-1] == SPEC_CAP_DELAY_SECONDS
    assert schedule == (0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0)


@pytest.mark.asyncio
async def test_backoff_schedule_with_pinned_jitter_is_exactly_predictable() -> None:
    """Acceptance 3 and 5, observably: the delays actually asked of the clock, scaled by jitter.

    Asserting the clock's recorded sleeps rather than an internal helper call. A test that
    asserted "the policy consulted its jitter source" would pass against an implementation that
    ignored the returned value.
    """
    transport = ScriptedTransport(fallback=[status(503)])
    harness = build_harness(transport, HarnessOverrides(jitter=FixedJitter(1.5)))
    with pytest.raises(AliasGroupExhaustedError):
        await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    per_model = [0.5 * 1.5, 1.0 * 1.5]
    assert harness.clock.sleeps == per_model * 3


@pytest.mark.asyncio
async def test_nothing_sleeps_after_the_final_attempt() -> None:
    """Two sleeps for three attempts. A third would add pure latency waiting for nothing."""
    transport = ScriptedTransport({MODEL_A: [status(503)], MODEL_B: [ok()]})
    harness = build_harness(transport)
    await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert len(harness.clock.sleeps) == SPEC_MAX_ATTEMPTS - 1


def test_jitter_stays_within_the_documented_bounds_over_many_samples() -> None:
    """Acceptance 3: `uniform(0.5, 1.5)`, asserted against the real source, not a double.

    A thousand draws from the production jitter source. The double used elsewhere clamps, so
    testing the bound against it would prove only that the double clamps.
    """
    jitter = SystemJitter()
    policy = RetryPolicy()
    base = policy.base_delay(2)
    samples = [policy.delay(2, jitter) for _ in range(1000)]
    assert all(base * SPEC_JITTER_LOW <= sample <= base * SPEC_JITTER_HIGH for sample in samples)
    assert len(set(samples)) > 1


def test_attempt_numbers_start_at_one_and_stop_at_the_maximum() -> None:
    """The loop bound is the policy's, so no call site can quietly add a fourth attempt."""
    assert list(RetryPolicy().attempt_numbers()) == list(range(1, SPEC_MAX_ATTEMPTS + 1))


def test_base_delay_rejects_a_zeroth_attempt() -> None:
    """Attempt numbers are one-based; a zeroth would halve the first delay silently."""
    with pytest.raises(ValueError, match="attempt numbers start at 1"):
        RetryPolicy().base_delay(0)


@pytest.mark.asyncio
async def test_the_idempotency_hint_is_identical_on_every_attempt() -> None:
    """Acceptance 4: one logical call means one hint, so a deduplicating upstream bills once."""
    transport = ScriptedTransport(fallback=[status(503)])
    harness = build_harness(transport)
    with pytest.raises(AliasGroupExhaustedError):
        await harness.gateway.call(
            a_request(idempotency_hint="job-1:shot-3:attempt-1"),
            ctx=CallContext(job_id="job-1", node="render"),
        )
    hints = {call.idempotency_key for call in transport.calls_for(MODEL_A)}
    assert hints == {"job-1:shot-3:attempt-1"}
    assert len(transport.calls_for(MODEL_A)) == SPEC_MAX_ATTEMPTS
