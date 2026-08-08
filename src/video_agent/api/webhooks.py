"""`POST /v1/webhooks/{provider_key}` — the inbound side of `providers.md` §7.3's webhook design.

Provider-agnostic on purpose: this module names no concrete provider, only the `ProviderRegistry`
Protocol every graph node already depends on. `{provider_key}` is a path parameter a provider's
own dashboard is configured with, never a literal this module
writes — the one place allowed to know that literal is the adapter itself, which is also the one
place that verifies the delivery and decides whether it recognises the id inside.

**No `Principal`, no idempotency key.** Both are for our own tenants calling our own API; a
provider's webhook is a different caller entirely; a signature is the only credential it will
ever present. Two consequences: this route reads no bearer token, and its 401 means "the
signature did not verify," not "no API key was presented."

**No registry configured is a real, honest 503**, mirroring `UnconfiguredApiKeyVerifier`'s
pattern in `api.principal`: webhooks are an accelerant, polling is what runs today with no
configuration at all, and a 503 tells the provider to keep sending events it is safe for it to
retry rather than pretending a delivery was accepted when nothing was listening.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.providers.models import ProviderRegistry

__all__ = ["router"]

router = APIRouter(tags=["webhooks"])

_UNVERIFIED_STATUS = 401
_UNCONFIGURED_STATUS = 503
_ACCEPTED_STATUS = 200


def _registry(request: Request) -> ProviderRegistry | None:
    return getattr(request.app.state, "provider_registry", None)


@router.post("/v1/webhooks/{provider_key}")
async def receive_provider_webhook(request: Request, provider_key: str) -> Response:
    """Verify one delivery and, if it verifies, flag the render it names for an early re-read.

    `provider_key` is not used to route to a specific provider — the registry's own
    `handle_webhook` tries each of its providers' own verification in turn, since the
    signature itself is what proves which provider a delivery actually came from. The path
    segment exists so a provider's own webhook configuration has somewhere to point, and so
    the access log can distinguish deliveries without decoding a body that node hasn't verified.
    """
    del provider_key
    registry = _registry(request)
    if registry is None:
        return Response(status_code=_UNCONFIGURED_STATUS)
    raw_body = await request.body()
    verified = await registry.handle_webhook(raw_body=raw_body, headers=dict(request.headers))
    if not verified:
        return Response(status_code=_UNVERIFIED_STATUS)
    return Response(status_code=_ACCEPTED_STATUS)
