"""`POST /v1/webhooks/{provider_key}` — the inbound receiver. Compact, non-exhaustive: the
dispatch/verification logic itself is covered in `test_providers_magichour.py`; this file is
only about the route's own contract (status codes, no auth ceremony, provider-agnostic path).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import pytest

from tests.unit.test_api_support import api_client, build_app

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.harness.context import NodeContext
    from video_agent.providers.models import Capability, ShotRequest, ShotResult, VideoProvider

_UNCONFIGURED_STATUS = 503
_UNVERIFIED_STATUS = 401
_ACCEPTED_STATUS = 200


class _FakeRegistry:
    """Only `handle_webhook` is exercised here — `select`/`generate` exist so this satisfies
    the full `ProviderRegistry` protocol under `mypy --strict`, never so a test calls them."""

    def __init__(self, *, verified: bool) -> None:
        self._verified = verified
        self.calls: list[tuple[bytes, dict[str, str]]] = []

    def select(self, required: frozenset[Capability]) -> list[VideoProvider]:
        del required
        raise NotImplementedError

    async def generate(self, req: ShotRequest, *, ctx: NodeContext) -> ShotResult:
        del req, ctx
        raise NotImplementedError

    async def handle_webhook(self, *, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        self.calls.append((raw_body, dict(headers)))
        return self._verified


@pytest.mark.asyncio
async def test_no_registry_configured_answers_503() -> None:
    app = build_app(probes=False)
    async with api_client(app) as client:
        response = await client.post("/v1/webhooks/some-provider", content=b"{}")
    assert response.status_code == _UNCONFIGURED_STATUS


@pytest.mark.asyncio
async def test_an_unverified_delivery_answers_401() -> None:
    registry = _FakeRegistry(verified=False)
    app = build_app(provider_registry=registry, probes=False)
    async with api_client(app) as client:
        response = await client.post("/v1/webhooks/some-provider", content=b"{}")
    assert response.status_code == _UNVERIFIED_STATUS


@pytest.mark.asyncio
async def test_a_verified_delivery_answers_200_and_reaches_the_registry_unmodified() -> None:
    registry = _FakeRegistry(verified=True)
    app = build_app(provider_registry=registry, probes=False)
    body = b'{"id": "proj-1"}'
    async with api_client(app) as client:
        response = await client.post(
            "/v1/webhooks/some-provider", content=body, headers={"X-Signature": "sig-1"}
        )
    assert response.status_code == _ACCEPTED_STATUS
    assert len(registry.calls) == 1
    seen_body, seen_headers = registry.calls[0]
    assert seen_body == body
    assert seen_headers.get("x-signature") == "sig-1"


@pytest.mark.asyncio
async def test_the_route_requires_no_bearer_token() -> None:
    """A provider's signature is the only credential this route ever checks — no
    `Authorization` header is read, unlike every tenant-facing route."""
    registry = _FakeRegistry(verified=True)
    app = build_app(provider_registry=registry, probes=False)
    async with api_client(app) as client:
        response = await client.post("/v1/webhooks/some-provider", content=b"{}")
    assert response.status_code == _ACCEPTED_STATUS
