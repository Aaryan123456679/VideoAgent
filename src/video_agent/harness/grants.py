"""The tool registry: what each node may call, and nothing else. `harness.md` §3.2.

Tool names are **capabilities, never providers** `[D-06]`, `[D-58]`: a grant names
`video.generate`, never a concrete provider's method. That is what lets the video provider
behind that capability change without touching a single grant.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from video_agent.harness.budget import CostEstimate

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

__all__ = ["GRANTS", "ToolSpec"]


class ToolSpec(BaseModel):
    """One callable tool: its I/O contract and how to pre-flight-estimate its cost."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    cost_estimator: Callable[[BaseModel], CostEstimate]
    retryable_errors: frozenset[type[Exception]] = frozenset()


GRANTS: dict[str, frozenset[str]] = {
    "plan_story": frozenset({"llm.reasoning_high"}),
    "lock_bible": frozenset({"llm.reasoning_high"}),
    "select_next_shot": frozenset(),
    "generate_shot": frozenset({"llm.reasoning_fast", "video.generate", "artifact.write"}),
    "extract_final_frame": frozenset({"ffmpeg.extract_frame", "artifact.write"}),
    "qc_shot": frozenset({"llm.vision_default", "artifact.read"}),
    "assemble": frozenset({"ffmpeg.concat", "ffmpeg.thumbnail", "artifact.write"}),
    "deliver": frozenset({"artifact.presign"}),
    "finalize": frozenset(),
}
"""Node → the exact set of tools it may call. `harness.md` §3.2, transcribed verbatim.

A static table rather than a decorator or a per-node declaration, so the whole grant surface
is readable — and diffable — in one place.
"""
