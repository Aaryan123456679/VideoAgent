"""`PinnedProviderRegistry`: negotiation + per-job pinning + failover. `providers.md` §3-4.

Mirrors `gateway.py`'s `_serve`/`_attempt_model` failover shape exactly (`[D-32]`'s pinning is
the one thing the LLM gateway does not need, since an alias group has no notion of "stick with
the model that generated shot 0") — retry/backoff and circuit-breaking are `gateway.retry` and
`gateway.breaker` reused as-is, not reimplemented, and `[D-62]`'s payment-required short-circuit
is the one place this loop's control flow diverges from the gateway's.

Pinning is process-local and per job id: a resumed job simply re-derives its pin from shot 0
again rather than persisting it anywhere, which is sufficient because shot 0 always runs before
any later shot can consult the pin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from video_agent.gateway.breaker import Admission, CircuitBreaker
from video_agent.gateway.clock import Clock, JitterSource, SystemClock, SystemJitter
from video_agent.gateway.retry import RetryPolicy
from video_agent.persistence.keys import circuit_breaker_key
from video_agent.providers.errors import (
    NOTHING_PRESERVED,
    ProviderError,
    ProviderGroupExhaustedError,
    ProviderPaymentRequiredError,
)
from video_agent.providers.models import Capability, ShotRequest, ShotResult, VideoProvider
from video_agent.providers.negotiate import negotiate, select_providers

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.harness.context import NodeContext

__all__ = ["PinnedProviderRegistry"]

_SWITCH_REASON = "provider_switch_mid_job"
_WAIVER_REASON = "negative_prompt_waived"


def _circuit_key(provider_key: str) -> str:
    return circuit_breaker_key(f"video:{provider_key}").value


@dataclass
class PinnedProviderRegistry:
    """The single `ProviderRegistry` implementation: negotiate, pin, fail over."""

    providers: tuple[VideoProvider, ...]
    breaker: CircuitBreaker
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    clock: Clock = field(default_factory=SystemClock)
    jitter: JitterSource = field(default_factory=SystemJitter)
    _pins: dict[UUID, str] = field(default_factory=dict, init=False)

    def select(self, required: frozenset[Capability]) -> list[VideoProvider]:
        return select_providers(required, self.providers)

    async def generate(self, req: ShotRequest, *, ctx: NodeContext) -> ShotResult:
        negotiation = negotiate(req, self.providers)
        candidates = self._pin_first(req.job_id, negotiation.candidates)

        tried: list[str] = []
        for provider in candidates:
            key = _circuit_key(provider.profile.provider_key)
            admission = await self.breaker.allows(key)
            if admission is Admission.REFUSE:
                continue
            tried.append(provider.profile.provider_key)
            result = await self._attempt(provider, req, ctx=ctx, key=key)
            if result is not None:
                return self._finalize(
                    req.job_id, result, provider.profile.provider_key, degraded=negotiation.degraded
                )

        message = (
            f"every candidate provider for shot {req.shot_index} of job {req.job_id} failed or "
            f"was circuit-open: tried {tried or 'none (all circuits open)'}"
        )
        raise ProviderGroupExhaustedError(
            what_happened=message, what_was_preserved=NOTHING_PRESERVED
        )

    async def _attempt(
        self, provider: VideoProvider, req: ShotRequest, *, ctx: NodeContext, key: str
    ) -> ShotResult | None:
        for attempt in self.retry.attempt_numbers():
            try:
                result = await provider.generate(req, ctx=ctx)
            except ProviderPaymentRequiredError:
                raise
            except ProviderError as exc:
                await self.breaker.record_failure(key)
                if not exc.retryable or self.retry.is_last(attempt):
                    return None
                await self.clock.sleep(self.retry.delay(attempt, self.jitter))
                continue
            else:
                await self.breaker.record_success(key)
                return result
        return None

    def _pin_first(
        self, job_id: UUID, candidates: tuple[VideoProvider, ...]
    ) -> tuple[VideoProvider, ...]:
        pinned_key = self._pins.get(job_id)
        if pinned_key is None:
            return candidates
        pinned = [p for p in candidates if p.profile.provider_key == pinned_key]
        rest = [p for p in candidates if p.profile.provider_key != pinned_key]
        return tuple(pinned + rest)

    def _finalize(
        self, job_id: UUID, result: ShotResult, provider_key: str, *, degraded: bool
    ) -> ShotResult:
        pinned_key = self._pins.get(job_id)
        switched = pinned_key is not None and pinned_key != provider_key
        self._pins[job_id] = provider_key

        if not (degraded or switched or result.degraded):
            return result
        if result.degraded:
            return result
        reason = _SWITCH_REASON if switched else _WAIVER_REASON
        return result.model_copy(update={"degraded": True, "degrade_reason": reason})
