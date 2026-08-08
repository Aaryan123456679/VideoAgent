"""`S0.7.3` — fallback within the alias group, always flagged degraded.

The rule that is easiest to break here is the *last* one: a fallback-served response that is
not flagged looks exactly like a clean one, and every consumer downstream — the job row, the
manifest, the eval comparison — then treats a second-choice model's answer as the primary's.
So the degrade assertions check both the response and the calling context, because a caller
that only reads one of the two would not propagate it.

The other rule worth being literal about is that failover never leaves the group. A
`vision-default` failure that fell back to `reasoning-high` would answer a question about a
frame with a model that never saw it, and the answer would be confident.
"""

from __future__ import annotations

import pytest

from tests.gateway_doubles import (
    MODEL_A,
    MODEL_B,
    MODEL_C,
    VISION_MODEL,
    ScriptedTransport,
    a_request,
    build_harness,
    ok,
)
from video_agent.config.aliases import Alias
from video_agent.gateway import CallContext, DegradeReason
from video_agent.gateway.errors import AliasGroupExhaustedError
from video_agent.gateway.transport import UpstreamStatusError
from video_agent.observability.codes import ErrorCode

SPEC_MAX_ATTEMPTS = 3
"""`[CPS §Failure behaviour]`, as a literal rather than as the module's own constant — see
`tests/unit/test_gateway_retry.py`, which pins the two together in one place."""


def status(code: int, body: str = "{}") -> UpstreamStatusError:
    """One upstream status failure."""
    return UpstreamStatusError(code, body)


@pytest.mark.asyncio
async def test_fallback_serves_after_the_primary_exhausts_its_retries() -> None:
    """Each fallback gets its own retry budget: three on the primary, then the sibling serves."""
    transport = ScriptedTransport({MODEL_A: [status(503)], MODEL_B: [ok("from the sibling")]})
    harness = build_harness(transport)
    response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert response.model_used == MODEL_B
    assert response.text == "from the sibling"
    assert len(transport.calls_for(MODEL_A)) == SPEC_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_a_fallback_served_response_is_flagged_degraded_with_a_reason() -> None:
    """`gateway.md` §4.2: served by a fallback means `degraded=true` and the reason, always."""
    transport = ScriptedTransport({MODEL_A: [status(503)], MODEL_B: [ok()]})
    harness = build_harness(transport)
    response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert response.degraded is True
    assert response.degrade_reason is DegradeReason.FALLBACK


@pytest.mark.asyncio
async def test_the_degrade_propagates_to_the_calling_context() -> None:
    """`gateway.md` §4.4: the flag reaches `Job.degraded`, not only whoever read the response.

    The harness writes the job row; the gateway raises the flag on the context it was handed.
    A response-only flag would be silently dropped by any caller that used the text and ignored
    the envelope.
    """
    transport = ScriptedTransport({MODEL_A: [status(503)], MODEL_B: [ok()]})
    ctx = CallContext(job_id="j", node="plan")
    await build_harness(transport).gateway.call(a_request(), ctx=ctx)
    assert ctx.degraded is True
    assert ctx.degrade_reasons == [DegradeReason.FALLBACK]


@pytest.mark.asyncio
async def test_the_group_is_walked_in_declared_order() -> None:
    """Primary, then each fallback in the order the table lists them."""
    transport = ScriptedTransport({MODEL_A: [status(503)], MODEL_B: [status(503)], MODEL_C: [ok()]})
    harness = build_harness(transport)
    response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert response.model_used == MODEL_C
    first_calls = [MODEL_A, MODEL_B, MODEL_C]
    assert [model for model in first_calls if model in transport.models_called] == first_calls
    assert transport.models_called.index(MODEL_A) < transport.models_called.index(MODEL_B)
    assert transport.models_called.index(MODEL_B) < transport.models_called.index(MODEL_C)


@pytest.mark.asyncio
async def test_failover_never_crosses_alias_groups() -> None:
    """`gateway.md` §3 rule 2: a `vision-default` failure never reaches a `reasoning-high` model."""
    transport = ScriptedTransport(fallback=[status(503)])
    harness = build_harness(transport)
    with pytest.raises(AliasGroupExhaustedError):
        await harness.gateway.call(
            a_request(alias=Alias.VISION_DEFAULT),
            ctx=CallContext(job_id="j", node="qc"),
        )
    assert set(transport.models_called) == {VISION_MODEL}
    assert MODEL_A not in transport.models_called


@pytest.mark.asyncio
async def test_group_exhausted_raises_gw_001_rather_than_returning_an_empty_response() -> None:
    """`gateway.md` §4.5: it never returns an empty or fabricated response."""
    transport = ScriptedTransport(fallback=[status(503)])
    harness = build_harness(transport)
    with pytest.raises(AliasGroupExhaustedError) as caught:
        await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert caught.value.code is ErrorCode.VA_GW_001


@pytest.mark.asyncio
async def test_the_exhaustion_error_says_what_happened_preserved_and_next() -> None:
    """`gateway.md` §4.5's three facts, plus the trace the failure happened in."""
    transport = ScriptedTransport(fallback=[status(503)])
    harness = build_harness(transport)
    with pytest.raises(AliasGroupExhaustedError) as caught:
        await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    error = caught.value
    assert Alias.REASONING_HIGH.value in error.what_happened
    assert error.what_was_preserved
    assert "retry" in error.what_to_do_next
    assert error.trace_id is None or isinstance(error.trace_id, str)


@pytest.mark.asyncio
async def test_a_primary_served_response_records_no_degrade() -> None:
    """The negative case, so the flag distinguishes rather than merely being present."""
    transport = ScriptedTransport({MODEL_A: [ok()]})
    ctx = CallContext(job_id="j", node="plan")
    response = await build_harness(transport).gateway.call(a_request(), ctx=ctx)
    assert response.degraded is False
    assert ctx.degrade_reasons == []
