"""`S0.7.1` — the gateway interface, alias resolution, and the fail-closed rules.

The acceptance criteria this file covers are about what the *type* permits, not only about what
the implementation does. `LLMRequest.alias` being an `Alias` enum is the mechanism that makes
"code never names a provider" structural rather than aspirational, so the test asserts that a
model-name string is rejected by validation — not that no call site currently passes one.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from tests.gateway_doubles import (
    MODEL_A,
    MODEL_B,
    MODEL_C,
    VISION_MODEL,
    HarnessOverrides,
    ScriptedTransport,
    a_request,
    alias_table,
    build_harness,
    ok,
)
from video_agent.config.aliases import Alias, AliasEntry, ModelRef
from video_agent.gateway import CallContext, LLMRequest
from video_agent.gateway.capabilities import Capability
from video_agent.gateway.errors import AliasResolutionError
from video_agent.observability.codes import ErrorCode


def test_alias_field_rejects_a_model_name_string() -> None:
    """Acceptance 1: the API accepts no model-name string anywhere.

    The point is the *type*, not a convention. A request cannot carry a model name because
    there is no field it would validate into.
    """
    with pytest.raises(ValidationError):
        a_request(alias="vendor-a/model-one")


def test_request_has_no_model_field() -> None:
    """Acceptance 1, the other half: `extra="forbid"`, so a model cannot be smuggled in."""
    assert "model" not in LLMRequest.model_fields
    with pytest.raises(ValidationError):
        a_request(model=MODEL_A)


@pytest.mark.asyncio
async def test_absent_alias_raises_gw_002_with_no_http_call() -> None:
    """Acceptance 2: an alias absent from the table fails closed before anything is sent."""
    transport = ScriptedTransport()
    table = alias_table(
        aliases={Alias.VISION_DEFAULT: AliasEntry(primary=ModelRef(model=VISION_MODEL))}
    )
    gateway = build_harness(transport, HarnessOverrides(table=table)).gateway
    with pytest.raises(AliasResolutionError) as caught:
        await gateway.call(a_request(), ctx=CallContext(job_id="job-1", node="plan"))
    assert caught.value.code is ErrorCode.VA_GW_002
    assert transport.calls == []


@pytest.mark.asyncio
async def test_capability_deficient_model_raises_gw_002_and_is_never_called() -> None:
    """Acceptance 3: a model lacking a declared capability is refused, never substituted."""
    transport = ScriptedTransport()
    gateway = build_harness(
        transport,
        HarnessOverrides(capabilities={VISION_MODEL: frozenset({Capability.STRUCTURED_OUTPUT})}),
    ).gateway
    with pytest.raises(AliasResolutionError) as caught:
        await gateway.call(
            a_request(alias=Alias.VISION_DEFAULT),
            ctx=CallContext(job_id="job-1", node="qc"),
        )
    assert caught.value.code is ErrorCode.VA_GW_002
    assert "image_input" in caught.value.what_happened
    assert transport.calls == []


@pytest.mark.asyncio
async def test_unknown_capability_counts_as_missing_rather_than_satisfied() -> None:
    """Fail closed on the unknown: a model the registry cannot describe does not pass."""
    transport = ScriptedTransport()
    gateway = build_harness(transport, HarnessOverrides(capabilities={})).gateway
    with pytest.raises(AliasResolutionError):
        await gateway.call(a_request(), ctx=CallContext(job_id="job-1", node="plan"))
    assert transport.calls == []


@pytest.mark.asyncio
async def test_model_used_is_populated_with_the_concrete_model() -> None:
    """Acceptance 4: the concrete model surfaces, for observability only."""
    transport = ScriptedTransport({MODEL_A: [ok()]})
    gateway = build_harness(transport).gateway
    response = await gateway.call(a_request(), ctx=CallContext(job_id="job-1", node="plan"))
    assert response.model_used == MODEL_A
    assert response.alias is Alias.REASONING_HIGH


@pytest.mark.asyncio
async def test_a_clean_call_is_not_flagged_degraded() -> None:
    """The flag means something only if it is false when nothing degraded."""
    transport = ScriptedTransport({MODEL_A: [ok()]})
    ctx = CallContext(job_id="job-1", node="plan")
    response = await build_harness(transport).gateway.call(a_request(), ctx=ctx)
    assert response.degraded is False
    assert response.degrade_reason is None
    assert ctx.degraded is False


class _Shape(BaseModel):
    """A tiny structured-output schema for the parse path."""

    verdict: str


@pytest.mark.asyncio
async def test_structured_output_is_returned_parsed() -> None:
    """A `response_model` yields a validated object, not a string the caller must parse."""
    transport = ScriptedTransport({MODEL_A: [ok('{"verdict": "accept"}')]})
    gateway = build_harness(transport).gateway
    response = await gateway.call(
        a_request(response_model=_Shape), ctx=CallContext(job_id="job-1", node="qc")
    )
    assert isinstance(response.parsed, _Shape)
    assert response.parsed.verdict == "accept"


@pytest.mark.asyncio
async def test_health_reports_every_member_of_the_group() -> None:
    """`health()` answers "can this group serve", per member, without consuming a probe."""
    gateway = build_harness().gateway
    health = await gateway.health(Alias.REASONING_HIGH)
    assert [member.model for member in health.models] == [MODEL_A, MODEL_B, MODEL_C]
    assert health.healthy is True
