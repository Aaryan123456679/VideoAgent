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
    """sha256 over the bible's canonical JSON, excluding `content_hash` and `locked_at`.

    `locked_at` is write-time metadata, not content: `persistence.schema`'s `continuity_bible`
    table gives it a server-side `now()` default, so the row's stored value is always a few
    microseconds later than whatever the in-memory object held the instant this function first
    hashed it. Including it here would make a bible loaded fresh from Postgres fail its own
    hash check every time — not because anything about the bible changed, but because the two
    reads of "now" never agree. Only what the bible *says* determines its identity.
    """
    payload = bible.model_dump(mode="json", exclude={"content_hash", "locked_at"})
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

    Flowing prose, not a labelled spec sheet: a real render provider's prompt endpoint rejected
    the original `KEY: value` block format outright (`VA-PROV-007`, discovered live) — every
    field below is still present and in the same stable order, just phrased as sentences instead
    of headers, which is the shape these APIs actually expect.
    """
    character = bible.character
    wardrobe = bible.wardrobe
    location = bible.location
    lighting = bible.lighting
    palette = bible.palette
    lens = bible.lens_language
    marks = f", with {character.distinguishing_marks}" if character.distinguishing_marks else ""
    sentences = [
        (
            f"The subject is {character.name}, appearing {character.age_appearance}, with a "
            f"{character.build} build, {character.skin_tone} skin, {character.hair}, and "
            f"{character.facial_features}{marks}."
        ),
        (
            f"They wear {', '.join(wardrobe.garments)} in {', '.join(wardrobe.colours)}, made "
            f"of {', '.join(wardrobe.materials)}, {wardrobe.condition}."
        ),
        (
            f"The scene is set at {location.setting} during {location.time_of_day}, amid "
            f"{location.architecture_or_terrain}"
            + (f", with {', '.join(location.key_props)} visible" if location.key_props else "")
            + (f", weather {location.weather}" if location.weather else "")
            + "."
        ),
        (
            f"Lighting is {lighting.key_light}, coming from {lighting.direction}, "
            f"{lighting.quality} in quality, {lighting.colour_temperature} in colour "
            f"temperature, with a {lighting.contrast_ratio} contrast ratio."
        ),
        (
            f"The colour palette is dominated by {', '.join(palette.dominant)}"
            + (f" with {', '.join(palette.accent)} as accents" if palette.accent else "")
            + f", {palette.saturation} saturation, graded {palette.grade}."
        ),
        (
            f"Shot on a {lens.focal_length} lens, {lens.aperture_feel}, framed as "
            f"{lens.framing}, with {lens.movement_style} camera movement, {lens.aspect_ratio} "
            "aspect ratio."
        ),
    ]
    if bible.negative_constraints:
        sentences.append("Avoid: " + "; ".join(bible.negative_constraints) + ".")
    return " ".join(sentences)
