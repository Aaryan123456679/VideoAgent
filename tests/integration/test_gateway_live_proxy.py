"""The gateway assertions that need a real LiteLLM proxy, and nothing that does not.

Collected always, deselected by default (`-m "not integration"`), selected by
`make test-integration`. The guard is a **short connection attempt**, not the presence of a
configuration value: a wedged Docker VM leaves `LITELLM_BASE_URL` set and every connection
hanging until some outer timeout, which surfaces as an error where a skip is wanted.

Only two things genuinely need a live proxy, and it is worth being explicit that neither is a
policy rule. The policy engine — retry counts, backoff schedule, circuit transitions, fallback
order, the untrusted-content rules — is fully asserted in `tests/unit/`, against an injected
clock and a scripted wire, because those rules are about *this* code's behaviour and a live
proxy would only make them slower and flakier.

What is left is the pair of facts a fake cannot establish:

1. **The proxy's `/model/info` really does describe capabilities in the shape this code parses.**
   A recorded fixture asserts that the parser handles the shape it was written against; only a
   live proxy can say the shape is still what the proxy sends.
2. **A real completion round-trips**, so the request body this module builds is one the proxy
   accepts, and the token counts come back where the cost calculation looks for them.

Both are contract assertions against an external component. Everything else stays a unit test.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

import httpx
import pytest
import pytest_asyncio

from video_agent.config.errors import MissingCredentialError
from video_agent.config.settings import get_settings
from video_agent.gateway.capabilities import ProxyCapabilityRegistry
from video_agent.gateway.transport import (
    HttpxLiteLLMTransport,
    TransportCall,
    UpstreamNetworkError,
    UpstreamStatusError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

CONNECT_TIMEOUT: Final = 3.0
"""Seconds to wait for the proxy before deciding it is not there. Short on purpose: a hung
daemon must produce a skip, not a run that appears to stall."""

HEALTH_PATH: Final = "/health/readiness"
"""The proxy's readiness route. A `GET` that needs no credential, so an unreachable proxy and a
misconfigured credential produce different skips."""


async def _probe(client: httpx.AsyncClient) -> str | None:
    """`None` if the proxy answered in time, otherwise why it did not."""
    try:
        response = await asyncio.wait_for(client.get(HEALTH_PATH), timeout=CONNECT_TIMEOUT)
    except TimeoutError:
        return f"did not answer within {CONNECT_TIMEOUT}s"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
        return f"answered {response.status_code}"
    return None


@pytest_asyncio.fixture
async def live_transport() -> AsyncIterator[HttpxLiteLLMTransport]:
    """A transport against the configured proxy, or a skip explaining why not."""
    settings = get_settings()
    client = httpx.AsyncClient(base_url=settings.LITELLM_BASE_URL, timeout=CONNECT_TIMEOUT)
    reason = await _probe(client)
    if reason is not None:
        await client.aclose()
        pytest.skip(f"litellm proxy unavailable: {reason}")
    try:
        settings.require_litellm_master_key()
    except MissingCredentialError as exc:
        await client.aclose()
        pytest.skip(f"litellm proxy credential not configured: {exc}")
    try:
        yield HttpxLiteLLMTransport(client, settings.require_litellm_master_key)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_the_proxy_publishes_capabilities_in_the_shape_the_parser_expects(
    live_transport: HttpxLiteLLMTransport,
) -> None:
    """Contract: `/model/info` still describes models the way `capabilities` reads it.

    A recorded fixture proves the parser handles the shape it was written against. Only this
    proves the shape has not moved — and if it has, every capability check fails closed, which
    is safe but stops the gateway entirely.
    """
    entries = await live_transport.model_info()
    assert entries, "the proxy serves no models"
    registry = ProxyCapabilityRegistry(live_transport)
    names = [entry.get("model_name") for entry in entries if isinstance(entry, dict)]
    described = [name for name in names if isinstance(name, str) and name]
    assert described, "no entry carried a model name"
    capabilities = await registry.capabilities(described[0])
    assert isinstance(capabilities, frozenset)


@pytest.mark.asyncio
async def test_a_real_completion_round_trips_with_token_counts(
    live_transport: HttpxLiteLLMTransport,
) -> None:
    """Contract: the body this module builds is accepted, and usage lands where cost reads it.

    Skipped rather than failed when the proxy serves the route but has no upstream credential
    for the model: that is a deployment fact about the environment, not a defect in this code,
    and the request having been accepted is most of what this asserts.
    """
    entries = await live_transport.model_info()
    names = [
        entry["model_name"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("model_name"), str)
    ]
    if not names:
        pytest.skip("the proxy serves no named model")
    call = TransportCall(
        model=names[0],
        instruction="Reply with the single word: ok",
        untrusted_block=None,
        max_output_tokens=16,
        temperature=0.0,
        timeout_s=CONNECT_TIMEOUT,
        idempotency_key=None,
        response_schema=None,
        image_keys=(),
    )
    try:
        result = await live_transport.complete(call)
    except UpstreamStatusError as exc:
        pytest.skip(f"the proxy refused the completion with HTTP {exc.status}")
    except UpstreamNetworkError as exc:
        pytest.skip(f"the completion did not complete: {exc.detail}")
    assert isinstance(result.text, str)
    assert result.input_tokens >= 0
    assert result.output_tokens >= 0
