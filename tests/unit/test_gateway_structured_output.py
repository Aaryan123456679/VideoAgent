"""`S0.7.5` — structured output: exactly one reformat attempt, then `VA-GW-004`, non-retryable.

"Exactly one" is the whole assertion. Zero reformats throws away a response that a single
corrective nudge would have salvaged; two or more is an unbounded loop against a model that has
already demonstrated it will not comply, paid for per attempt.

The context-length case sits here too, because its rule is a negative one — nothing is
truncated to make the prompt fit — and a negative rule is only tested by asserting that the
second, shorter call never happens.
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

from tests.gateway_doubles import MODEL_A, ScriptedTransport, a_request, ok
from tests.gateway_doubles import build_harness as build
from video_agent.gateway import CallContext, LLMRequest
from video_agent.gateway.errors import ContextLengthExceededError, StructuredOutputError
from video_agent.gateway.gateway import REFORMAT_DIRECTIVE, _widen_schema_const_to_enum
from video_agent.gateway.transport import UpstreamStatusError
from video_agent.observability.codes import ErrorCode

EXPECTED_CALLS_AFTER_ONE_REFORMAT = 2
A_LARGE_OUTPUT_BUDGET = 4096


class Verdict(BaseModel):
    """The schema under test."""

    score: float
    reason: str


def a_structured_request(**overrides: object) -> LLMRequest:
    """A request that asks for `Verdict`."""
    return a_request(response_model=Verdict, **overrides)


@pytest.mark.asyncio
async def test_malformed_then_malformed_makes_exactly_two_calls_and_raises_gw_004() -> None:
    """One reformat, then the error. Not zero, not a loop."""
    transport = ScriptedTransport({MODEL_A: [ok("not json"), ok("still not json")]})
    harness = build(transport)
    with pytest.raises(StructuredOutputError) as caught:
        await harness.gateway.call(a_structured_request(), ctx=CallContext(job_id="j", node="qc"))
    assert caught.value.code is ErrorCode.VA_GW_004
    assert len(transport.calls_for(MODEL_A)) == EXPECTED_CALLS_AFTER_ONE_REFORMAT


@pytest.mark.asyncio
async def test_the_schema_failure_is_not_retried_and_does_not_fall_over() -> None:
    """The eighth non-retryable class in `gateway.md` §4.1: schema failure after one reformat.

    Two calls in total across the *whole group* — the fallback models are never tried, because
    a model that returns valid text in the wrong shape is not an availability problem.
    """
    transport = ScriptedTransport(fallback=[ok("not json")])
    harness = build(transport)
    with pytest.raises(StructuredOutputError):
        await harness.gateway.call(a_structured_request(), ctx=CallContext(job_id="j", node="qc"))
    assert len(transport.calls) == EXPECTED_CALLS_AFTER_ONE_REFORMAT
    assert set(transport.models_called) == {MODEL_A}
    assert harness.clock.sleeps == []


@pytest.mark.asyncio
async def test_malformed_then_valid_returns_the_parsed_object_without_error() -> None:
    """The reformat exists to succeed sometimes, or it is only an extra bill."""
    transport = ScriptedTransport(
        {MODEL_A: [ok("```json\nbroken"), ok('{"score": 0.9, "reason": "clean"}')]}
    )
    harness = build(transport)
    response = await harness.gateway.call(
        a_structured_request(), ctx=CallContext(job_id="j", node="qc")
    )
    assert isinstance(response.parsed, Verdict)
    assert response.parsed.score == pytest.approx(0.9)
    assert len(transport.calls_for(MODEL_A)) == EXPECTED_CALLS_AFTER_ONE_REFORMAT


@pytest.mark.asyncio
async def test_valid_first_time_makes_no_reformat_call() -> None:
    """The negative: a compliant reply costs one call, not two."""
    transport = ScriptedTransport({MODEL_A: [ok('{"score": 1.0, "reason": "fine"}')]})
    harness = build(transport)
    await harness.gateway.call(a_structured_request(), ctx=CallContext(job_id="j", node="qc"))
    assert len(transport.calls_for(MODEL_A)) == 1


@pytest.mark.asyncio
async def test_the_reformat_call_carries_the_directive_and_a_distinct_idempotency_key() -> None:
    """A retry reuses the hint; a reformat must not, or a deduplicating upstream replays the bad
    reply.

    Reusing the key here would turn the one corrective attempt into a guaranteed `VA-GW-004`
    against any upstream that deduplicates, and the failure would look like a model that cannot
    follow a schema.
    """
    transport = ScriptedTransport({MODEL_A: [ok("nope"), ok('{"score": 1.0, "reason": "ok"}')]})
    harness = build(transport)
    await harness.gateway.call(
        a_structured_request(idempotency_hint="job-1:qc:shot-2"),
        ctx=CallContext(job_id="j", node="qc"),
    )
    first, second = transport.calls_for(MODEL_A)
    assert REFORMAT_DIRECTIVE not in first.instruction
    assert REFORMAT_DIRECTIVE in second.instruction
    assert first.idempotency_key == "job-1:qc:shot-2"
    assert second.idempotency_key != first.idempotency_key


@pytest.mark.asyncio
async def test_the_response_schema_is_sent_on_the_wire() -> None:
    """`gateway.md` §5: structured output is requested via the proxy's schema mode."""
    transport = ScriptedTransport({MODEL_A: [ok('{"score": 1.0, "reason": "ok"}')]})
    harness = build(transport)
    await harness.gateway.call(a_structured_request(), ctx=CallContext(job_id="j", node="qc"))
    schema = transport.calls[0].response_schema
    assert schema is not None
    assert set(schema["properties"]) == {"score", "reason"}


@pytest.mark.asyncio
async def test_context_length_exceeded_raises_gw_005_and_truncates_nothing() -> None:
    """`gateway.md` §8: never silently truncate. A truncated bible breaks every shot after it.

    The assertion is that no *second*, shorter call is made — which is the only way to test a
    rule whose content is that something does not happen.
    """
    failure = UpstreamStatusError(400, '{"error": {"code": "context_length_exceeded"}}')
    transport = ScriptedTransport(fallback=[failure])
    harness = build(transport)
    with pytest.raises(ContextLengthExceededError) as caught:
        await harness.gateway.call(
            a_request(max_output_tokens=A_LARGE_OUTPUT_BUDGET),
            ctx=CallContext(job_id="j", node="bible"),
        )
    assert caught.value.code is ErrorCode.VA_GW_005
    assert len(transport.calls) == 1
    assert transport.calls[0].max_output_tokens == A_LARGE_OUTPUT_BUDGET


class _AspectRatio(BaseModel):
    """A single-value `Literal` field, exactly the shape Pydantic renders as `const`."""

    aspect_ratio: Literal["16:9"] = "16:9"


def test_widen_schema_const_to_enum_rewrites_a_nested_const() -> None:
    """Vertex/Gemini's structured-output dialect rejects `const`; `enum` with one member is the
    same constraint in a shape every provider accepts."""
    schema = _AspectRatio.model_json_schema()
    assert schema["properties"]["aspect_ratio"]["const"] == "16:9"

    widened = _widen_schema_const_to_enum(schema)

    assert "const" not in widened["properties"]["aspect_ratio"]
    assert widened["properties"]["aspect_ratio"]["enum"] == ["16:9"]


@pytest.mark.asyncio
async def test_a_request_for_a_model_with_a_literal_field_sends_enum_not_const() -> None:
    """The rewrite is actually applied on the request path, not just unit-tested in isolation."""
    transport = ScriptedTransport({MODEL_A: [ok('{"aspect_ratio": "16:9"}')]})
    harness = build(transport)
    await harness.gateway.call(
        a_request(response_model=_AspectRatio), ctx=CallContext(job_id="j", node="bible")
    )
    sent_schema = transport.calls_for(MODEL_A)[0].response_schema
    assert sent_schema is not None
    assert "const" not in sent_schema["properties"]["aspect_ratio"]
    assert sent_schema["properties"]["aspect_ratio"]["enum"] == ["16:9"]
