"""Tests for `compose_prompt()`. `providers.md` §5, `[D-33]`."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from video_agent.planning.models import (
    Beat,
    BeatKind,
    CameraMove,
    CharacterSpec,
    ContinuityBible,
    LensLanguageSpec,
    LightingSpec,
    LocationSpec,
    PaletteSpec,
    WardrobeSpec,
)
from video_agent.providers.compose import compose_prompt
from video_agent.providers.errors import PromptExceedsLimitError


def _bible(*, negative_constraints: list[str] | None = None) -> ContinuityBible:
    return ContinuityBible(
        job_id=uuid4(),
        character=CharacterSpec(
            name="Mira",
            age_appearance="mid-30s",
            build="athletic",
            skin_tone="olive",
            hair="short black bob",
            facial_features="sharp jawline, thin scar above left eyebrow",
        ),
        wardrobe=WardrobeSpec(
            garments=["navy field jacket"], colours=["navy"], materials=["canvas"],
            condition="worn",
        ),
        location=LocationSpec(
            setting="rooftop", time_of_day="dusk", architecture_or_terrain="concrete",
            key_props=["water tank"],
        ),
        lighting=LightingSpec(
            key_light="setting sun", direction="camera left", quality="hard",
            colour_temperature="warm 3200K", contrast_ratio="4:1",
        ),
        palette=PaletteSpec(
            dominant=["#1c2b3a", "#e08030"], accent=["#ffffff"], saturation="muted",
            grade="teal-orange",
        ),
        lens_language=LensLanguageSpec(
            focal_length="35mm", aperture_feel="shallow", framing="medium",
            movement_style="handheld",
        ),
        negative_constraints=negative_constraints or ["no additional characters enter frame"],
        content_hash="placeholder",
        locked_at=datetime.now(UTC),
        model_alias="reasoning-high",
        prompt_version="v1",
    )


def _beat(*, continuity_note: str | None = None, action: str | None = None) -> Beat:
    return Beat(
        index=0,
        kind=BeatKind.SETUP,
        action=action or "she steps onto the rooftop and scans the skyline for the signal",
        camera_move=CameraMove.PUSH_IN,
        duration_s=10.0,
        continuity_note=continuity_note,
    )


def test_sections_appear_in_fixed_order() -> None:
    bible = _bible()
    beat = _beat(
        action="she steps onto the rooftop and scans the skyline for the signal",
        continuity_note="jacket still has the tear from beat 0",
    )
    composed = compose_prompt(bible, beat, repair_delta="tighten the framing", max_chars=4000)

    order = [
        "The subject is Mira",  # bible block
        beat.action,
        "Camera movement:",
        "Continuity:",
        "Revision:",
    ]
    positions = [composed.text.index(marker) for marker in order]
    assert positions == sorted(positions)
    # The bible block ends with its own "Avoid:" sentence, so this section's "Avoid:" (the
    # last occurrence) must come after everything else, not the bible's own (the first).
    assert composed.text.rindex("Avoid:") > positions[-1]
    assert not composed.truncated_sections


def test_empty_optional_sections_are_dropped() -> None:
    bible = _bible()
    beat = _beat()
    composed = compose_prompt(bible, beat, max_chars=4000)

    assert "Continuity:" not in composed.text
    assert "Revision:" not in composed.text


def test_prompt_hash_is_deterministic_for_the_same_inputs() -> None:
    bible = _bible()
    beat = _beat(continuity_note="same note")
    first = compose_prompt(bible, beat, max_chars=4000)
    second = compose_prompt(bible, beat, max_chars=4000)

    assert first.text == second.text
    assert first.prompt_hash == second.prompt_hash


def test_truncation_drops_continuity_note_before_camera() -> None:
    bible = _bible()
    beat = _beat(continuity_note="x" * 50)
    full = compose_prompt(bible, beat, max_chars=4000)
    tight_limit = len(full.text) - 10

    composed = compose_prompt(bible, beat, max_chars=tight_limit)

    assert "CONTINUITY NOTE" in composed.truncated_sections
    assert len(composed.text) <= tight_limit


def test_bible_and_negative_are_never_truncated_and_raise_instead() -> None:
    bible = _bible()
    beat = _beat()

    with pytest.raises(PromptExceedsLimitError):
        compose_prompt(bible, beat, max_chars=1)
