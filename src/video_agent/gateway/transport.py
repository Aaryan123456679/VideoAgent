"""The wire: one HTTP call to the LiteLLM proxy, and the two failure shapes it can produce.

`gateway.md` §1 makes this module the single egress for LLM calls, and §2's acceptance is that
the only URL is `LITELLM_BASE_URL` and the only credential is `LITELLM_MASTER_KEY`. Upstream
vendor keys are held by the proxy `[CPS §Canonical stack]`; application code never reads one,
so a leaked application config cannot spend an upstream account.

The transport is a `Protocol` because the policy engine above it — retry, fallback, circuit
break — is the part worth testing, and testing it against a real socket would mean either a
running proxy or a test that sleeps. The policy tests drive a fake transport; the HTTP
implementation is exercised against `httpx.MockTransport` for its wire shape and against a live
proxy only under `@pytest.mark.integration`.

Failures arrive as exactly two types, and the split is the one `classify` needs: a response
that came back with a status, and a call that never got a response at all. Everything else —
which statuses retry, which fall over, which escalate — is `classify`'s decision and not made
here, so there is one table rather than one per call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "HttpxLiteLLMTransport",
    "LLMTransport",
    "TransportCall",
    "TransportResult",
    "UpstreamNetworkError",
    "UpstreamStatusError",
]

CHAT_COMPLETIONS_PATH: Final = "/v1/chat/completions"
"""The proxy's chat-completion route, in the de-facto standard shape every proxy speaks.

A path on `LITELLM_BASE_URL` and never a vendor endpoint: the proxy owns which upstream this
becomes, which is why naming the upstream here would be both wrong and a static-guard failure.
"""

MODEL_INFO_PATH: Final = "/model/info"
"""Where the proxy publishes what each model it serves can actually do."""

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
"""`gateway.md` §4.1: retries reuse the hint so a deduplicating upstream does not double-bill."""


@dataclass(frozen=True, slots=True)
class TransportCall:
    """One attempt against one concrete model. Built by the gateway, never by a caller."""

    model: str
    instruction: str
    untrusted_block: str | None
    max_output_tokens: int
    temperature: float
    timeout_s: float
    idempotency_key: str | None
    response_schema: Mapping[str, Any] | None
    image_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransportResult:
    """What one successful attempt returned. Token counts come from the proxy, not a guess."""

    text: str
    input_tokens: int
    output_tokens: int
    model_reported: str | None = None


class UpstreamStatusError(Exception):
    """The proxy answered with a non-success status.

    Carries the body because `classify` needs it: a `400` whose body says the context window
    was exceeded is a different outcome from a `400` whose body says the request was malformed,
    and the status alone cannot tell them apart. The body is never logged — it is
    upstream-controlled text that may echo the request `[AGENT.md §3]`.
    """

    def __init__(self, status: int, body: str, *, error_type: str | None = None) -> None:
        self.status = status
        self.body = body
        self.error_type = error_type
        super().__init__(f"upstream returned HTTP {status}")


class UpstreamNetworkError(Exception):
    """The call never got a response: connection reset, DNS failure, read timeout.

    A distinct type rather than a status of `0`, because "no answer" is unambiguously
    retryable and unambiguously an availability problem, while a status needs a table to say
    which of the two it is.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"upstream call did not complete: {detail}")


class LLMTransport(Protocol):
    """One attempt against one model, and nothing else. No retry, no fallback, no policy."""

    async def complete(self, call: TransportCall) -> TransportResult: ...

    async def model_info(self) -> Sequence[Mapping[str, Any]]: ...


def _messages(call: TransportCall) -> list[dict[str, str]]:
    """The instruction section and, if there is one, the quarantined block, in that order.

    Two separate messages rather than one concatenated string. The block is already delimited
    and escaped by `rendering`, and keeping it in its own message means the boundary survives
    the serialisation as well as the text: a fence that is a message boundary cannot be closed
    by content inside the message.
    """
    messages = [{"role": "system", "content": call.instruction}]
    if call.untrusted_block is not None:
        messages.append({"role": "user", "content": call.untrusted_block})
    return messages


def build_payload(call: TransportCall) -> dict[str, Any]:
    """The JSON body for one attempt.

    `response_format` uses the proxy's schema mode when a `response_model` was given
    `gateway.md` §5 — asking the model for a shape is strictly better than parsing whatever it
    returns, and the reformat attempt exists for the case where it still does not comply.
    """
    payload: dict[str, Any] = {
        "model": call.model,
        "messages": _messages(call),
        "max_tokens": call.max_output_tokens,
        "temperature": call.temperature,
    }
    if call.response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "structured_output", "schema": dict(call.response_schema)},
        }
    return payload


def parse_result(body: Mapping[str, Any]) -> TransportResult:
    """Pull text and token counts out of a completion body, tolerating an absent usage block.

    An absent `usage` yields zero tokens rather than an exception, because a response that
    arrived and parsed is a success, and refusing it over a missing accounting field would turn
    a proxy quirk into a failed job. `pricing` treats an unpriced *model* as expensive; it does
    not need to treat an unreported *token count* as fatal.
    """
    choices = body.get("choices") or [{}]
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message", {}) if isinstance(first, dict) else {}
    text = message.get("content") if isinstance(message, dict) else None
    usage = body.get("usage") or {}
    reported = body.get("model")
    return TransportResult(
        text=text if isinstance(text, str) else "",
        input_tokens=int(usage.get("prompt_tokens", 0)) if isinstance(usage, dict) else 0,
        output_tokens=int(usage.get("completion_tokens", 0)) if isinstance(usage, dict) else 0,
        model_reported=reported if isinstance(reported, str) else None,
    )


def _error_fields(response: httpx.Response) -> tuple[str, str | None]:
    """The body text and, if the proxy sent a typed error, its `type` discriminator."""
    text = response.text
    try:
        parsed = response.json()
    except ValueError:
        return text, None
    if not isinstance(parsed, dict):
        return text, None
    error = parsed.get("error")
    if isinstance(error, dict):
        kind = error.get("type")
        return text, kind if isinstance(kind, str) else None
    return text, None


class HttpxLiteLLMTransport:
    """`LLMTransport` over `httpx`, pointed at `LITELLM_BASE_URL` and nothing else.

    The client is injected rather than constructed here so a test can hand it an
    `httpx.MockTransport` and assert the wire shape without a socket, and so a deployment
    controls connection pooling in one place.

    `key_provider` is a callable invoked **per request**, not a string captured at
    construction. That is what lets the application start with `LITELLM_MASTER_KEY` unset: the
    provider is `Settings.require_litellm_master_key`, which raises only when it is called, so
    an unset key fails the first model call with a sentence naming the variable instead of
    failing the process at import. It also means the header value is never held on an object
    that a `repr` or a traceback could render.
    """

    def __init__(self, client: httpx.AsyncClient, key_provider: Callable[[], str]) -> None:
        self._client = client
        self._key_provider = key_provider

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key_provider()}"}

    async def complete(self, call: TransportCall) -> TransportResult:
        """One attempt. Raises `UpstreamStatusError` or `UpstreamNetworkError`, never both."""
        headers = self._headers()
        if call.idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = call.idempotency_key
        try:
            response = await self._client.post(
                CHAT_COMPLETIONS_PATH,
                json=build_payload(call),
                headers=headers,
                timeout=call.timeout_s,
            )
        except httpx.HTTPError as exc:
            raise UpstreamNetworkError(type(exc).__name__) from exc
        if response.status_code >= httpx.codes.BAD_REQUEST:
            body, kind = _error_fields(response)
            raise UpstreamStatusError(response.status_code, body, error_type=kind)
        return parse_result(response.json())

    async def model_info(self) -> Sequence[Mapping[str, Any]]:
        """What the proxy says each model it serves supports. Feeds the capability check."""
        try:
            response = await self._client.get(MODEL_INFO_PATH, headers=self._headers())
        except httpx.HTTPError as exc:
            raise UpstreamNetworkError(type(exc).__name__) from exc
        if response.status_code >= httpx.codes.BAD_REQUEST:
            body, kind = _error_fields(response)
            raise UpstreamStatusError(response.status_code, body, error_type=kind)
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        return (
            [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []
        )
