"""`compose_prompt()`: fixed six-section prompt assembly. `providers.md` §5.

Section [1] (the bible block) is produced by `planning.bible.render_bible_block`, the single
renderer every shot shares — that is what makes it byte-identical across all four shots and
`prompt_hash` reproducible. Truncation, when `max_chars` binds, drops section [4] then [3],
then compresses [2]; sections [1] and [6] are never touched, and a bible that alone exceeds
`max_chars` raises rather than generating against a partial one `[D-33]`.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from video_agent.planning.bible import render_bible_block
from video_agent.planning.models import Beat, ContinuityBible
from video_agent.providers.errors import PromptExceedsLimitError

__all__ = ["ComposedPrompt", "compose_prompt"]

_BEAT_ACTION = "BEAT ACTION"
_DROPPABLE_IN_ORDER = ("CONTINUITY NOTE", "CAMERA")


class ComposedPrompt(BaseModel):
    """The rendered prompt, its hash, and which sections truncation dropped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    prompt_hash: str = Field(min_length=1)
    truncated_sections: tuple[str, ...] = ()


def compose_prompt(
    bible: ContinuityBible,
    beat: Beat,
    *,
    repair_delta: str | None = None,
    max_chars: int,
) -> ComposedPrompt:
    """Assemble the fixed six-section prompt for one shot, truncating to fit `max_chars`."""
    bible_block = render_bible_block(bible)
    camera = beat.camera_move.value
    if bible.lens_language.movement_style:
        camera = f"{camera} ({bible.lens_language.movement_style})"
    negative_text = "; ".join(bible.negative_constraints)

    sections: list[tuple[str, str]] = [
        ("CONTINUITY BIBLE", bible_block),
        (_BEAT_ACTION, beat.action),
        ("CAMERA", camera),
        ("CONTINUITY NOTE", beat.continuity_note or ""),
        ("REPAIR DELTA", repair_delta or ""),
        ("NEGATIVE", negative_text),
    ]
    sections = [(name, body) for name, body in sections if body]

    dropped: list[str] = []
    text = _render(sections)
    for droppable in _DROPPABLE_IN_ORDER:
        if len(text) <= max_chars:
            break
        before = len(sections)
        sections = [(name, body) for name, body in sections if name != droppable]
        if len(sections) != before:
            dropped.append(droppable)
        text = _render(sections)

    if len(text) > max_chars:
        overflow = len(text) - max_chars
        for index, (name, body) in enumerate(sections):
            if name == _BEAT_ACTION:
                sections[index] = (name, body[: max(0, len(body) - overflow)])
                break
        text = _render(sections)

    if len(text) > max_chars:
        message = (
            f"composed prompt is {len(text)} chars after full truncation, limit is "
            f"{max_chars}; the continuity bible and negative constraints are never "
            f"truncated `[D-33]`"
        )
        raise PromptExceedsLimitError(message)

    prompt_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ComposedPrompt(text=text, prompt_hash=prompt_hash, truncated_sections=tuple(dropped))


def _render(sections: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{name}:\n{body}" for name, body in sections)
