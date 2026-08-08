"""`S0.7.1` acceptance 5 — the wire: one base URL, one credential, and nothing vendor-specific.

Driven through `httpx.MockTransport`, so the request that would go out is inspected without a
socket, a proxy or a Docker daemon. That is not a compromise forced by the environment: a test
that needed a live proxy could not assert "no upstream vendor key is ever read", because the
absence of a header is only observable on the request object.

The credential is deliberately supplied by a callable rather than a captured string. The
application has to start with `LITELLM_MASTER_KEY` unset — only a code path that actually calls
a model may demand one — and a key read at construction time would move that failure to import.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from video_agent.config.errors import MissingCredentialError
from video_agent.config.settings import Settings
from video_agent.gateway.capabilities import Capability, ProxyCapabilityRegistry
from video_agent.gateway.transport import (
    CHAT_COMPLETIONS_PATH,
    IDEMPOTENCY_HEADER,
    HttpxLiteLLMTransport,
    TransportCall,
    UpstreamNetworkError,
    UpstreamStatusError,
    build_payload,
)

BASE_URL = "http://proxy.invalid:4000"
PROXY_CREDENTIAL = "proxy-credential-for-tests"
"""A fake, and named so it is not mistaken for one. The prefix deliberately matches no issuer's
format, so `tests/support.py`'s committed-credential check has nothing to catch and neither does
anyone reading it in a diff."""

COMPLETION_BODY: dict[str, Any] = {
    "model": "vendor-a/model-one",
    "choices": [{"message": {"content": "an answer"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
}

EXPECTED_INPUT_TOKENS = 11
EXPECTED_OUTPUT_TOKENS = 7
INSTRUCTION_AND_QUARANTINE_MESSAGES = 2


def a_call(**overrides: object) -> TransportCall:
    """One transport call, with the fields a given test is varying."""
    fields: dict[str, Any] = {
        "model": "vendor-a/model-one",
        "instruction": "Describe the scene.",
        "untrusted_block": None,
        "max_output_tokens": 256,
        "temperature": 0.0,
        "timeout_s": 30.0,
        "idempotency_key": None,
        "response_schema": None,
        "image_keys": (),
    }
    fields.update(overrides)
    return TransportCall(**fields)


def build_transport(
    handler: httpx.MockTransport, *, key: str = PROXY_CREDENTIAL
) -> HttpxLiteLLMTransport:
    """A transport over a mock wire, pointed at a base URL that resolves to nothing."""
    client = httpx.AsyncClient(base_url=BASE_URL, transport=handler)
    return HttpxLiteLLMTransport(client, lambda: key)


def capture(
    seen: list[httpx.Request], *, status: int = 200, body: dict[str, Any] | None = None
) -> httpx.MockTransport:
    """A mock wire that records the request and returns a canned response."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else COMPLETION_BODY)

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_the_request_goes_to_the_configured_base_url_and_nowhere_else() -> None:
    """Acceptance 5: the egress URL is `LITELLM_BASE_URL`, never a vendor endpoint."""
    seen: list[httpx.Request] = []
    await build_transport(capture(seen)).complete(a_call())
    assert len(seen) == 1
    assert str(seen[0].url) == f"{BASE_URL}{CHAT_COMPLETIONS_PATH}"


@pytest.mark.asyncio
async def test_the_only_credential_sent_is_the_proxy_master_key() -> None:
    """Acceptance 5: no upstream vendor key is read by application code.

    Asserted as an absence over the whole header set rather than by checking three specific
    names, so a fourth vendor added later is covered without anyone remembering to add it.
    """
    seen: list[httpx.Request] = []
    await build_transport(capture(seen)).complete(a_call())
    headers = seen[0].headers
    assert headers["Authorization"] == f"Bearer {PROXY_CREDENTIAL}"
    vendor_headers = [name for name in headers if "api-key" in name.lower()]
    assert vendor_headers == []
    assert PROXY_CREDENTIAL in headers["Authorization"]


@pytest.mark.asyncio
async def test_the_key_is_read_per_request_so_an_unset_one_fails_only_at_call_time() -> None:
    """The application starts without the key; only the path that calls a model demands it.

    Constructed with the real `Settings` accessor and an empty key, so this exercises the
    production wiring rather than a stand-in that happens to raise.
    """
    settings = Settings(
        MAGICHOUR_API_KEY=SecretStr(""),
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        REDIS_URL="redis://localhost:6379/0",
        LITELLM_MASTER_KEY=SecretStr(""),
    )
    seen: list[httpx.Request] = []
    transport = HttpxLiteLLMTransport(
        httpx.AsyncClient(base_url=BASE_URL, transport=capture(seen)),
        settings.require_litellm_master_key,
    )
    with pytest.raises(MissingCredentialError, match="LITELLM_MASTER_KEY"):
        await transport.complete(a_call())
    assert seen == []


@pytest.mark.asyncio
async def test_the_idempotency_key_is_sent_as_a_header_when_present() -> None:
    """`gateway.md` §4.1: the hint has to reach the upstream to prevent a double bill."""
    seen: list[httpx.Request] = []
    await build_transport(capture(seen)).complete(a_call(idempotency_key="job-1:shot-2"))
    assert seen[0].headers[IDEMPOTENCY_HEADER] == "job-1:shot-2"


@pytest.mark.asyncio
async def test_no_idempotency_header_is_sent_when_there_is_no_hint() -> None:
    """An empty header would be a key that deduplicates every call against every other."""
    seen: list[httpx.Request] = []
    await build_transport(capture(seen)).complete(a_call())
    assert IDEMPOTENCY_HEADER not in seen[0].headers


@pytest.mark.asyncio
async def test_the_two_prompt_sections_stay_two_messages_on_the_wire() -> None:
    """Fence integrity survives serialisation: a message boundary cannot be closed by content."""
    seen: list[httpx.Request] = []
    await build_transport(capture(seen)).complete(
        a_call(untrusted_block="<<<UNTRUSTED_DATA>>>\nrationale: muted\n<<<END_UNTRUSTED_DATA>>>")
    )
    payload = json.loads(seen[0].content)
    assert len(payload["messages"]) == INSTRUCTION_AND_QUARANTINE_MESSAGES
    assert payload["messages"][0]["content"] == "Describe the scene."
    assert "rationale: muted" in payload["messages"][1]["content"]


@pytest.mark.asyncio
async def test_tokens_and_text_are_read_from_the_response() -> None:
    """Token counts come from the proxy, so cost is measured rather than estimated."""
    result = await build_transport(capture([])).complete(a_call())
    assert result.text == "an answer"
    assert result.input_tokens == EXPECTED_INPUT_TOKENS
    assert result.output_tokens == EXPECTED_OUTPUT_TOKENS


@pytest.mark.asyncio
async def test_an_error_status_becomes_an_upstream_status_error_carrying_the_type() -> None:
    """`classify` needs the body: a `400` saying "context length" is not a malformed request."""
    handler = capture([], status=429, body={"error": {"type": "rate_limit_exceeded"}})
    with pytest.raises(UpstreamStatusError) as caught:
        await build_transport(handler).complete(a_call())
    assert caught.value.status == httpx.codes.TOO_MANY_REQUESTS
    assert caught.value.error_type == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_a_connection_failure_becomes_an_upstream_network_error() -> None:
    """No answer is unambiguously retryable; a status needs a table to say which it is."""

    def refuse(request: httpx.Request) -> httpx.Response:
        message = f"connection refused for {request.url}"
        raise httpx.ConnectError(message)

    with pytest.raises(UpstreamNetworkError):
        await build_transport(httpx.MockTransport(refuse)).complete(a_call())


def test_the_payload_carries_the_schema_when_structured_output_is_asked_for() -> None:
    """`gateway.md` §5: the provider's native schema mode, requested through the proxy."""
    payload = build_payload(a_call(response_schema={"type": "object", "properties": {}}))
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"]["type"] == "object"


def test_the_payload_omits_the_schema_for_a_free_text_call() -> None:
    """Sending an empty schema would ask every free-text call for JSON."""
    assert "response_format" not in build_payload(a_call())


@pytest.mark.asyncio
async def test_capabilities_are_discovered_from_the_proxy_rather_than_hard_coded() -> None:
    """The capability facts come from the component that knows, not from a table in code.

    A table of model names mapped to what they support would be a table of model names in
    application code, which is exactly what the alias rule forbids.
    """
    info = {
        "data": [
            {
                "model_name": "vendor-a/vision-one",
                "model_info": {"supports_vision": True, "supports_response_schema": True},
            }
        ]
    }

    def serve(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=info)

    handler = httpx.MockTransport(serve)
    registry = ProxyCapabilityRegistry(build_transport(handler))
    assert await registry.capabilities("vendor-a/vision-one") == frozenset(
        {Capability.IMAGE_INPUT, Capability.STRUCTURED_OUTPUT}
    )
    assert await registry.capabilities("vendor-b/unknown") == frozenset()


@pytest.mark.asyncio
async def test_an_unreachable_proxy_yields_no_capabilities_rather_than_an_exception() -> None:
    """Unknown means unsatisfied, and the refusal surfaces as `VA-GW-002` at resolution.

    Returning an empty set here rather than raising keeps the fail-closed decision in one place
    — alias resolution — instead of splitting it between two modules.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        message = f"connection refused for {request.url}"
        raise httpx.ConnectError(message)

    registry = ProxyCapabilityRegistry(build_transport(httpx.MockTransport(refuse)))
    assert await registry.capabilities("vendor-a/vision-one") == frozenset()
