"""`StoryPlan` and `ContinuityBible`: the two public, immutable artifacts of planning.

`planning.md` §2, transcribed exactly — both are delivered to the user as machine-readable
JSON `[PRD §What's delivered]`, so a field here is a public API contract, not scratch state.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "Beat",
    "BeatKind",
    "CameraMove",
    "CharacterSpec",
    "ContinuityBible",
    "LensLanguageSpec",
    "LightingSpec",
    "LocationSpec",
    "PaletteSpec",
    "StoryPlan",
    "WardrobeSpec",
]

BEAT_COUNT = 4
TOTAL_DURATION_S = 40.0
FIXED_BEAT_DURATION_S = 10.0
DURATION_TOLERANCE = 1e-6


class BeatKind(StrEnum):
    """The fixed 4-beat arc order. `[PRD §How it works 1]`."""

    SETUP = "setup"
    DEVELOPMENT = "development"
    TURN = "turn"
    RESOLUTION = "resolution"


BEAT_ORDER: tuple[BeatKind, ...] = (
    BeatKind.SETUP,
    BeatKind.DEVELOPMENT,
    BeatKind.TURN,
    BeatKind.RESOLUTION,
)


class CameraMove(StrEnum):
    """Closed camera vocabulary `[D-26]`: a bounded set is QC-checkable, free text is not."""

    STATIC = "static"
    PAN_LEFT = "pan_left"
    PAN_RIGHT = "pan_right"
    PUSH_IN = "push_in"
    PULL_OUT = "pull_out"
    TILT_UP = "tilt_up"
    TILT_DOWN = "tilt_down"
    TRACKING = "tracking"
    ORBIT = "orbit"


class Beat(BaseModel):
    """One 10-second beat of the arc. `[D-03]` fixes v1's duration at exactly 10s."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0, le=3)
    kind: BeatKind
    action: str = Field(min_length=20, max_length=400)
    camera_move: CameraMove
    duration_s: float = Field(default=FIXED_BEAT_DURATION_S, ge=10.0, le=10.0)
    continuity_note: str | None = None


class StoryPlan(BaseModel):
    """The locked 4-beat arc for a job. `planning.md` §2.1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    logline: str = Field(max_length=200)
    beats: list[Beat] = Field(min_length=BEAT_COUNT, max_length=BEAT_COUNT)
    total_duration_s: float = TOTAL_DURATION_S
    model_alias: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def _validate(self) -> StoryPlan:
        indices = [beat.index for beat in self.beats]
        if indices != list(range(BEAT_COUNT)):
            message = f"beats must be indexed 0..{BEAT_COUNT - 1} in order, got {indices}"
            raise ValueError(message)
        kinds = [beat.kind for beat in self.beats]
        if kinds != list(BEAT_ORDER):
            message = f"beats must be kinded {[k.value for k in BEAT_ORDER]} in order, got {kinds}"
            raise ValueError(message)
        total = sum(beat.duration_s for beat in self.beats)
        if abs(total - TOTAL_DURATION_S) >= DURATION_TOLERANCE:
            message = f"beat durations must sum to exactly {TOTAL_DURATION_S}s, got {total}"
            raise ValueError(message)
        return self


class CharacterSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    age_appearance: str = Field(min_length=1)
    build: str = Field(min_length=1)
    skin_tone: str = Field(min_length=1)
    hair: str = Field(min_length=1)
    facial_features: str = Field(min_length=1)
    distinguishing_marks: str | None = None


class WardrobeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    garments: list[str] = Field(min_length=1)
    colours: list[str] = Field(min_length=1)
    materials: list[str] = Field(min_length=1)
    condition: str = Field(min_length=1)


class LocationSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    setting: str = Field(min_length=1)
    time_of_day: str = Field(min_length=1)
    architecture_or_terrain: str = Field(min_length=1)
    key_props: list[str] = Field(default_factory=list)
    weather: str | None = None


class LightingSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key_light: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    quality: str = Field(min_length=1)
    colour_temperature: str = Field(min_length=1)
    contrast_ratio: str = Field(min_length=1)


class PaletteSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dominant: list[str] = Field(min_length=2, max_length=5)
    accent: list[str] = Field(default_factory=list, max_length=3)
    saturation: str = Field(min_length=1)
    grade: str = Field(min_length=1)


class LensLanguageSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    focal_length: str = Field(min_length=1)
    aperture_feel: str = Field(min_length=1)
    framing: str = Field(min_length=1)
    movement_style: str = Field(min_length=1)
    aspect_ratio: Literal["16:9"] = "16:9"
    resolution_ceiling: Literal["1080p"] = "1080p"


class ContinuityBible(BaseModel):
    """The locked, immutable bible. `planning.md` §2.2. `frozen=True` guards the in-process
    copy; a DB trigger (see `persistence.md`) guards the persisted row against `UPDATE`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    character: CharacterSpec
    wardrobe: WardrobeSpec
    location: LocationSpec
    lighting: LightingSpec
    palette: PaletteSpec
    lens_language: LensLanguageSpec
    negative_constraints: list[str] = Field(default_factory=list)
    content_hash: str = Field(min_length=1)
    locked_at: datetime
    model_alias: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
