"""Capability negotiation and deterministic provider ranking. `providers.md` §3.

`IMAGE_CONDITIONING` is deliberately absent from `_WAIVABLE`: `[D-31]` forbids generating an
unchained clip whenever a conditioning frame exists, so a provider set that cannot offer it
must raise rather than silently drop the requirement. `NEGATIVE_PROMPT` is the one capability
the spec allows negotiation to fold into the positive prompt instead — always flagged
degraded, never silent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from video_agent.providers.errors import NoProviderSatisfiesCapabilitiesError
from video_agent.providers.models import Capability, ShotRequest, VideoProvider

__all__ = [
    "REQUIRED_ALWAYS",
    "Negotiation",
    "negotiate",
    "required_for",
    "select_providers",
]

REQUIRED_ALWAYS: frozenset[Capability] = frozenset(
    {Capability.DURATION_10S, Capability.ASPECT_16_9}
)

_WAIVABLE: frozenset[Capability] = frozenset({Capability.NEGATIVE_PROMPT})


def required_for(shot: ShotRequest) -> frozenset[Capability]:
    """The capabilities `shot` needs, per `providers.md` §3's negotiation table."""
    required = set(REQUIRED_ALWAYS)
    required.add(Capability.RES_1080P if shot.resolution == "1080p" else Capability.RES_720P)
    if shot.conditioning_frame is not None:
        required.add(Capability.IMAGE_CONDITIONING)
    if shot.negative_prompt:
        required.add(Capability.NEGATIVE_PROMPT)
    return frozenset(required)


def select_providers(
    required: frozenset[Capability], providers: Sequence[VideoProvider]
) -> list[VideoProvider]:
    """Every provider whose capabilities are a superset of `required`, ranked deterministically.

    Ranking key, in the priority order `providers.md` §3 rule 3 lists: capability superset
    (more capabilities first — a more flexible provider ranks ahead of a narrower one that
    happens to satisfy the same minimum), then the caller's own configured order (the input
    list's index, the closest thing to a preference this module is given), then price, then
    latency.
    """
    indexed = [(index, provider) for index, provider in enumerate(providers)]
    satisfying = [
        (index, provider)
        for index, provider in indexed
        if required <= provider.profile.capabilities
    ]
    ranked = sorted(
        satisfying,
        key=lambda item: (
            -len(item[1].profile.capabilities),
            item[0],
            item[1].profile.price_per_second,
            item[1].profile.typical_latency_s,
        ),
    )
    return [provider for _, provider in ranked]


@dataclass(frozen=True, slots=True)
class Negotiation:
    """What negotiation decided for one shot: the ranked candidates and what was waived."""

    candidates: tuple[VideoProvider, ...]
    waived: frozenset[Capability]

    @property
    def degraded(self) -> bool:
        """Whether serving from `candidates` requires flagging the shot degraded."""
        return bool(self.waived)


def negotiate(shot: ShotRequest, providers: Sequence[VideoProvider]) -> Negotiation:
    """Rank candidates for `shot`, waiving `NEGATIVE_PROMPT` if no provider offers it.

    Never waives anything else: a required resolution or `IMAGE_CONDITIONING` that no provider
    satisfies raises `VA-PROV-002` rather than serving a lesser shot silently `[D-31]`.
    """
    required = required_for(shot)
    hard = required - _WAIVABLE
    soft = required & _WAIVABLE

    hard_satisfying = select_providers(hard, providers)
    if not hard_satisfying:
        message = (
            f"no provider satisfies the required capabilities {sorted(hard)} for shot "
            f"{shot.shot_index} of job {shot.job_id}"
        )
        raise NoProviderSatisfiesCapabilitiesError(message)

    if not soft:
        return Negotiation(candidates=tuple(hard_satisfying), waived=frozenset())

    fully_satisfying = [
        provider for provider in hard_satisfying if soft <= provider.profile.capabilities
    ]
    if fully_satisfying:
        return Negotiation(candidates=tuple(fully_satisfying), waived=frozenset())
    return Negotiation(candidates=tuple(hard_satisfying), waived=soft)
