"""Minimal QC data types needed by `NodeContext` while scoring itself stays deferred to E3.

`qc.md` is the full spec for scoring, aggregation and repair, and none of that ships in this
build. `NodeContext.prior_findings: list[QCFinding]` still needs a real type to type-check
against, so this module carries only the shape of a finding — never a scorer.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Dimension", "QCFinding"]


class Dimension(StrEnum):
    """The QC scoring dimensions named in `qc.md`. Declared here only so a finding can name one."""

    CHARACTER_CONSISTENCY = "character_consistency"
    WARDROBE_CONSISTENCY = "wardrobe_consistency"
    LOCATION_CONSISTENCY = "location_consistency"
    LIGHTING_CONSISTENCY = "lighting_consistency"
    PALETTE_CONSISTENCY = "palette_consistency"
    NEGATIVE_CONSTRAINTS = "negative_constraints"
    MOTION_QUALITY = "motion_quality"


class QCFinding(BaseModel):
    """One dimension's score for one shot attempt. Sanitised: a rationale, never raw model text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: Dimension
    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=500)
