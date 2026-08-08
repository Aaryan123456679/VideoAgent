"""Test doubles for the provider registry: scripted providers, not a real Magic Hour call.

Not a test module — the filename does not match `python_files`, so pytest does not collect it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from video_agent.gateway.models import ArtifactRef
from video_agent.providers.errors import ProviderUnavailableError
from video_agent.providers.models import (
    Capability,
    ProviderHealth,
    ProviderProfile,
    ShotRequest,
    ShotResult,
)

ALL_CAPABILITIES: frozenset[Capability] = frozenset(Capability)
"""Everything, for the tests whose subject is not the capability check."""


def profile(
    provider_key: str,
    *,
    capabilities: frozenset[Capability] = ALL_CAPABILITIES,
    price_per_second: str = "0.10",
    typical_latency_s: float = 5.0,
) -> ProviderProfile:
    """A minimal valid profile; the overrides are what a given test is actually varying."""
    return ProviderProfile(
        provider_key=provider_key,
        capabilities=capabilities,
        min_duration_s=5.0,
        max_duration_s=10.0,
        max_resolution="1080p",
        cost_unit="usd",
        price_per_second=Decimal(price_per_second),
        typical_latency_s=typical_latency_s,
        max_prompt_chars=4000,
    )


def a_result(provider_key: str, **overrides: object) -> ShotResult:
    """A minimal valid `ShotResult`; the overrides are what a given test is actually varying."""
    fields: dict[str, object] = {
        "clip": ArtifactRef(artifact_id="clip-1", storage_key="clips/clip-1.mp4"),
        "provider_key": provider_key,
        "provider_model": "fake-model",
        "provider_project_id": "proj-1",
        "duration_s": 10.0,
        "resolution": "720p",
        "cost_usd": Decimal("0.50"),
        "cost_is_final": True,
        "latency_ms": 1200,
    }
    fields.update(overrides)
    return ShotResult.model_validate(fields)


@dataclass
class FakeProvider:
    """A provider whose `generate()` replays a scripted list of outcomes, one call each.

    `outcomes` exhausting mid-test is a test bug, not a case to handle gracefully — it raises
    `IndexError` immediately rather than silently repeating the last entry, since (unlike the
    LLM gateway's transport double) failover exhaustion is exactly one of the behaviours these
    tests assert on.
    """

    profile: ProviderProfile
    outcomes: list[BaseException | ShotResult | None] = field(default_factory=list)
    calls: list[ShotRequest] = field(default_factory=list)
    _index: int = field(default=0, init=False)
    _lookups: dict[str, ShotResult] = field(default_factory=dict)

    async def generate(self, req: ShotRequest, *, ctx: object) -> ShotResult:
        del ctx
        self.calls.append(req)
        outcome = self.outcomes[self._index]
        self._index += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if outcome is not None else a_result(self.profile.provider_key)

    async def lookup(self, request_fingerprint: str) -> ShotResult | None:
        return self._lookups.get(request_fingerprint)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_key=self.profile.provider_key, healthy=True)


def full_capability_provider(
    key: str = "full",
    *,
    capabilities: frozenset[Capability] = ALL_CAPABILITIES,
    price_per_second: str = "0.10",
) -> FakeProvider:
    """A provider that always succeeds; offers every capability unless overridden."""
    made = profile(key, capabilities=capabilities, price_per_second=price_per_second)
    return FakeProvider(profile=made, outcomes=[None] * 8)


def no_image_conditioning_provider(key: str = "no-image") -> FakeProvider:
    """A provider that satisfies everything except `IMAGE_CONDITIONING`."""
    capabilities = ALL_CAPABILITIES - {Capability.IMAGE_CONDITIONING}
    return FakeProvider(profile=profile(key, capabilities=capabilities), outcomes=[None] * 8)


def flaky_provider(key: str = "flaky", *, failures: int = 2) -> FakeProvider:
    """A provider that fails `failures` times with a retryable error, then succeeds."""
    unavailable = ProviderUnavailableError(f"{key} is temporarily unavailable")
    outcomes: list[BaseException | ShotResult | None] = [unavailable] * failures
    outcomes.append(None)
    return FakeProvider(profile=profile(key), outcomes=outcomes)


def a_shot_request(**overrides: object) -> ShotRequest:
    """A minimal valid `ShotRequest`; the overrides are what a given test is actually varying."""
    fields: dict[str, object] = {
        "job_id": UUID("00000000-0000-0000-0000-000000000001"),
        "shot_index": 0,
        "attempt_no": 1,
        "prompt": "a lighthouse at dawn, waves crashing",
        "duration_s": 10.0,
        "resolution": "720p",
        "request_fingerprint": "fingerprint-1",
        "timeout_s": 30.0,
    }
    fields.update(overrides)
    return ShotRequest.model_validate(fields)
