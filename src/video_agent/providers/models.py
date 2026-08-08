"""The provider registry's public data types. `providers.md` §2.

`VideoProvider.profile` is what capability negotiation reads and `generate()` is the only
thing a concrete adapter contributes beyond it — a shot request in, a shot result out, with
no branch anywhere in this module or `negotiate.py`/`registry.py` on which provider answered.
`ShotResult.provider_key` exists for observability only, per the protocol's own docstring in
`providers.md` §2: callers must not branch on it.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from video_agent.gateway.models import ArtifactRef

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.harness.context import NodeContext

__all__ = [
    "Capability",
    "ProviderHealth",
    "ProviderProfile",
    "ProviderRegistry",
    "ShotRequest",
    "ShotResult",
    "VideoProvider",
    "compute_request_fingerprint",
]


class Capability(StrEnum):
    """The closed vocabulary of things a provider may or may not offer. `providers.md` §2."""

    IMAGE_CONDITIONING = "image_conditioning"
    END_FRAME_CONDITIONING = "end_frame_conditioning"
    SEED_CONTROL = "seed_control"
    NEGATIVE_PROMPT = "negative_prompt"
    CAMERA_DIRECTIVE = "camera_directive"
    ASPECT_16_9 = "aspect_16_9"
    RES_720P = "res_720p"
    RES_1080P = "res_1080p"
    DURATION_10S = "duration_10s"
    ASYNC_POLL = "async_poll"
    WEBHOOK_CALLBACK = "webhook_callback"


class ProviderProfile(BaseModel):
    """One provider's static capabilities and pricing, checked at startup, never per job.

    `allowed_durations_s` distinguishes a genuinely continuous provider (`None`) from one that
    only accepts a discrete set `[D-61]` — validating that set against the fixed 10s beat
    length at deploy time, rather than per job, is what turns a bad model choice into a failed
    deploy instead of a failed shot three days later.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_key: str = Field(min_length=1)
    capabilities: frozenset[Capability]
    min_duration_s: float = Field(gt=0)
    max_duration_s: float = Field(gt=0)
    allowed_durations_s: frozenset[float] | None = None
    max_resolution: Literal["480p", "720p", "1080p"]
    cost_unit: Literal["usd", "credits"]
    price_per_second: Decimal = Field(ge=0)
    credits_per_usd: Decimal | None = None
    typical_latency_s: float = Field(gt=0)
    max_prompt_chars: int = Field(gt=0)


class ShotRequest(BaseModel):
    """One shot to generate. `providers.md` §2.

    `request_fingerprint` is computed by the caller via `compute_request_fingerprint()` before
    construction — it is reused verbatim across an attempt's retries so a deduplicating
    upstream does not double-bill a retried call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    shot_index: int = Field(ge=0)
    attempt_no: int = Field(ge=1)
    prompt: str = Field(min_length=1)
    negative_prompt: str | None = None
    conditioning_frame: ArtifactRef | None = None
    duration_s: float = 10.0
    aspect_ratio: Literal["16:9"] = "16:9"
    resolution: Literal["720p", "1080p"] = "720p"
    seed: int | None = None
    request_fingerprint: str = Field(min_length=1)
    timeout_s: float = Field(gt=0)


class ShotResult(BaseModel):
    """What a provider produced for one shot. `providers.md` §2.

    `seed_used=None` means the provider offers no seed control at all, never "we forgot to
    record it" `[D-59]` — the distinction only holds if nothing downstream normalises a missing
    value to `0` or drops the field. `credits_charged` is provisional until `cost_is_final`
    `[D-60]`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    clip: ArtifactRef
    provider_key: str = Field(min_length=1)
    provider_model: str = Field(min_length=1)
    provider_project_id: str = Field(min_length=1)
    seed_used: int | None = None
    duration_s: float = Field(gt=0)
    resolution: str = Field(min_length=1)
    fps: int | None = None
    width: int | None = None
    height: int | None = None
    cost_usd: Decimal = Field(ge=0)
    credits_charged: Decimal | None = None
    cost_is_final: bool
    latency_ms: int = Field(ge=0)
    degraded: bool = False
    degrade_reason: str | None = None


class ProviderHealth(BaseModel):
    """Whether one provider is currently serving traffic. Mirrors `gateway.models.AliasHealth`,
    the LLM gateway's equivalent for an alias group."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_key: str = Field(min_length=1)
    healthy: bool


class VideoProvider(Protocol):
    """One concrete video-generation backend. `providers.md` §2.

    `lookup()` is mandatory, not optional, per `[D-24]`: resume needs to ask a provider "did
    you already make this clip" before paying for it again, and a protocol member that some
    adapters skip is a member resume cannot rely on.
    """

    profile: ProviderProfile

    async def generate(self, req: ShotRequest, *, ctx: NodeContext) -> ShotResult: ...

    async def lookup(self, request_fingerprint: str) -> ShotResult | None: ...

    async def health(self) -> ProviderHealth: ...


class ProviderRegistry(Protocol):
    """The single entry point graph nodes call. `providers.md` §2."""

    def select(self, required: frozenset[Capability]) -> list[VideoProvider]: ...

    async def generate(self, req: ShotRequest, *, ctx: NodeContext) -> ShotResult: ...


def compute_request_fingerprint(
    *,
    job_id: UUID,
    shot_index: int,
    attempt_no: int,
    prompt_hash: str,
    frame_id: str | None = None,
    seed: int | None = None,
) -> str:
    """A deterministic sha256 over a shot attempt's identity. `providers.md` §2.

    Every input is a primitive rendered to its own string, never `repr()` or `hash()` of an
    object — the property under test (`S2.1.1`) is that two processes given the same six values
    produce the same fingerprint, and neither `repr` nor Python's per-process `hash()` promises
    that.
    """
    parts = (
        str(job_id),
        str(shot_index),
        str(attempt_no),
        prompt_hash,
        frame_id or "",
        str(seed) if seed is not None else "",
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
