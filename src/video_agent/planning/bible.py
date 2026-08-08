"""Bible hashing, rendering and verification. `planning.md` §2.3, §3.4.

`render_bible_block` is the **one renderer, two consumers** function: `providers.md`'s prompt
composition and `qc.md`'s scoring reference must read byte-identical text, because a QC pass
scoring against a differently-worded bible would be scoring against a target the generator
was never given.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from video_agent.harness.errors import BibleHashMismatchError
from video_agent.planning.models import ContinuityBible

__all__ = ["compute_content_hash", "render_bible_block", "verify_bible"]


def _canonical_json(payload: dict[str, Any]) -> str:
    """Stable-ordered, whitespace-free JSON — the same bytes on every process and every run."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_content_hash(bible: ContinuityBible) -> str:
    """sha256 over the bible's canonical JSON, excluding `content_hash` itself."""
    payload = bible.model_dump(mode="json", exclude={"content_hash"})
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def verify_bible(bible: ContinuityBible) -> None:
    """Raise `VA-BIBLE-002` if the bible's stored hash no longer matches its content.

    `planning.md` §3.2: every later read calls this. A mismatch means every remaining shot
    would render against a different bible than the one the caller approved, so it terminates
    the job rather than degrading.
    """
    expected = compute_content_hash(bible)
    if expected != bible.content_hash:
        message = (
            f"continuity bible for job {bible.job_id} does not match its recorded content "
            f"hash: expected {expected}, stored {bible.content_hash}"
        )
        raise BibleHashMismatchError(message)


def render_bible_block(bible: ContinuityBible) -> str:
    """The canonical bible fragment. Deterministic and stable-ordered across runs/processes.

    Its output is hashed into `ShotAttempt.prompt_hash` for reproducibility, so a field
    reordering here would be a silent break of every recorded reproducibility hash.
    """
    character = bible.character
    wardrobe = bible.wardrobe
    location = bible.location
    lighting = bible.lighting
    palette = bible.palette
    lens = bible.lens_language
    lines = [
        "CHARACTER:",
        f"  name: {character.name}",
        f"  age_appearance: {character.age_appearance}",
        f"  build: {character.build}",
        f"  skin_tone: {character.skin_tone}",
        f"  hair: {character.hair}",
        f"  facial_features: {character.facial_features}",
        f"  distinguishing_marks: {character.distinguishing_marks or 'none'}",
        "WARDROBE:",
        f"  garments: {', '.join(wardrobe.garments)}",
        f"  colours: {', '.join(wardrobe.colours)}",
        f"  materials: {', '.join(wardrobe.materials)}",
        f"  condition: {wardrobe.condition}",
        "LOCATION:",
        f"  setting: {location.setting}",
        f"  time_of_day: {location.time_of_day}",
        f"  architecture_or_terrain: {location.architecture_or_terrain}",
        f"  key_props: {', '.join(location.key_props)}",
        f"  weather: {location.weather or 'unspecified'}",
        "LIGHTING:",
        f"  key_light: {lighting.key_light}",
        f"  direction: {lighting.direction}",
        f"  quality: {lighting.quality}",
        f"  colour_temperature: {lighting.colour_temperature}",
        f"  contrast_ratio: {lighting.contrast_ratio}",
        "PALETTE:",
        f"  dominant: {', '.join(palette.dominant)}",
        f"  accent: {', '.join(palette.accent)}",
        f"  saturation: {palette.saturation}",
        f"  grade: {palette.grade}",
        "LENS_LANGUAGE:",
        f"  focal_length: {lens.focal_length}",
        f"  aperture_feel: {lens.aperture_feel}",
        f"  framing: {lens.framing}",
        f"  movement_style: {lens.movement_style}",
        f"  aspect_ratio: {lens.aspect_ratio}",
        "NEGATIVE_CONSTRAINTS:",
        *(f"  - {item}" for item in bible.negative_constraints),
    ]
    return "\n".join(lines)
