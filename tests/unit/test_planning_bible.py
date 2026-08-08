"""Tests for bible hashing, rendering and verification. `planning.md` §3.4, §6."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from video_agent.harness.errors import BibleHashMismatchError
from video_agent.planning.bible import compute_content_hash, render_bible_block, verify_bible
from video_agent.planning.models import (
    CharacterSpec,
    ContinuityBible,
    LensLanguageSpec,
    LightingSpec,
    LocationSpec,
    PaletteSpec,
    WardrobeSpec,
)


def _bible(content_hash: str = "placeholder") -> ContinuityBible:
    return ContinuityBible(
        job_id=uuid4(),
        character=CharacterSpec(
            name="Mira",
            age_appearance="mid-30s",
            build="athletic",
            skin_tone="olive",
            hair="short black bob",
            facial_features="sharp jawline, thin scar above left eyebrow",
            distinguishing_marks="small anchor tattoo on right wrist",
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
        negative_constraints=["no additional characters enter frame"],
        content_hash=content_hash,
        locked_at=datetime.now(UTC),
        model_alias="reasoning-high",
        prompt_version="v1",
    )


def test_content_hash_deterministic() -> None:
    bible = _bible()
    assert compute_content_hash(bible) == compute_content_hash(bible)


def test_verify_bible_accepts_matching_hash() -> None:
    provisional = _bible()
    correct = provisional.model_copy(update={"content_hash": compute_content_hash(provisional)})
    verify_bible(correct)  # does not raise


def test_verify_bible_rejects_mutated_content() -> None:
    provisional = _bible()
    correct = provisional.model_copy(update={"content_hash": compute_content_hash(provisional)})
    mutated = correct.model_copy(update={"locked_at": datetime.now(UTC)})
    with pytest.raises(BibleHashMismatchError):
        verify_bible(mutated)


def test_render_bible_block_is_deterministic_and_stable_ordered() -> None:
    bible = _bible()
    first = render_bible_block(bible)
    second = render_bible_block(bible)
    assert first == second
    assert first.index("CHARACTER:") < first.index("WARDROBE:") < first.index("LOCATION:")
