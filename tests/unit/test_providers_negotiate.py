"""Tests for capability negotiation. `providers.md` §3, `[D-31]`."""

from __future__ import annotations

import pytest

from tests.providers_doubles import (
    ALL_CAPABILITIES,
    a_shot_request,
    full_capability_provider,
    no_image_conditioning_provider,
)
from video_agent.gateway.models import ArtifactRef
from video_agent.providers.errors import NoProviderSatisfiesCapabilitiesError
from video_agent.providers.models import Capability
from video_agent.providers.negotiate import negotiate, required_for, select_providers


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [
        ("480p", Capability.RES_480P),
        ("720p", Capability.RES_720P),
        ("1080p", Capability.RES_1080P),
    ],
)
def test_required_for_maps_each_resolution_to_its_own_capability(
    resolution: str, expected: Capability
) -> None:
    """A request for one resolution must never be satisfiable by a provider declaring another —
    the bug this guards against sent every non-1080p request through a hardcoded 720p check."""
    shot = a_shot_request(resolution=resolution)
    assert expected in required_for(shot)


def test_negotiate_picks_providers_that_satisfy_all_required_capabilities() -> None:
    frame = ArtifactRef(artifact_id="frame-1", storage_key="frames/frame-1.png")
    shot = a_shot_request(conditioning_frame=frame)
    capable = full_capability_provider("capable")
    incapable = no_image_conditioning_provider("incapable")

    negotiation = negotiate(shot, [incapable, capable])

    assert [p.profile.provider_key for p in negotiation.candidates] == ["capable"]
    assert not negotiation.degraded


def test_negotiate_raises_when_no_provider_offers_image_conditioning() -> None:
    frame = ArtifactRef(artifact_id="frame-1", storage_key="frames/frame-1.png")
    shot = a_shot_request(conditioning_frame=frame)
    incapable = no_image_conditioning_provider("incapable")

    with pytest.raises(NoProviderSatisfiesCapabilitiesError):
        negotiate(shot, [incapable])


def test_negotiate_waives_negative_prompt_when_unsupported_and_flags_degraded() -> None:
    shot = a_shot_request(negative_prompt="no crowds")
    no_negative = full_capability_provider(
        "no-negative", capabilities=ALL_CAPABILITIES - {Capability.NEGATIVE_PROMPT}
    )

    negotiation = negotiate(shot, [no_negative])

    assert negotiation.degraded
    assert negotiation.waived == {Capability.NEGATIVE_PROMPT}
    assert [p.profile.provider_key for p in negotiation.candidates] == ["no-negative"]


def test_negotiate_prefers_a_provider_offering_negative_prompt_over_waiving_it() -> None:
    shot = a_shot_request(negative_prompt="no crowds")
    no_negative = full_capability_provider(
        "no-negative", capabilities=ALL_CAPABILITIES - {Capability.NEGATIVE_PROMPT}
    )
    with_negative = full_capability_provider("with-negative")

    negotiation = negotiate(shot, [no_negative, with_negative])

    assert not negotiation.degraded
    assert [p.profile.provider_key for p in negotiation.candidates] == ["with-negative"]


def test_select_providers_ranks_capability_superset_ahead_of_narrower_matches() -> None:
    pricier = full_capability_provider("pricier", price_per_second="0.20")
    cheap = full_capability_provider("cheap", price_per_second="0.05")
    narrower = no_image_conditioning_provider("narrower")

    ranked = select_providers(frozenset({Capability.DURATION_10S}), [narrower, pricier, cheap])

    assert [p.profile.provider_key for p in ranked] == ["pricier", "cheap", "narrower"]


def test_select_providers_excludes_providers_missing_a_required_capability() -> None:
    narrower = no_image_conditioning_provider("narrower")
    full = full_capability_provider("full")

    ranked = select_providers(frozenset({Capability.IMAGE_CONDITIONING}), [narrower, full])

    assert [p.profile.provider_key for p in ranked] == ["full"]
