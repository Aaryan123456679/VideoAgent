"""`plan_story` and `lock_bible`. `planning.md` §2.3, §3.1, §3.2.

Both functions take an explicit `gateway` alongside `ctx: NodeContext`. `planning.md`'s
signature shows only `ctx`; the LLD does not say how the gateway instance reaches a node body,
and rather than guess at a hidden global, it is threaded explicitly here — the graph layer that
builds `NodeContext` via `harness.observe()` is the same layer that holds the `Gateway`, so
passing both is one extra parameter, not a new dependency.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from video_agent.config.aliases import Alias
from video_agent.gateway.errors import StructuredOutputError
from video_agent.gateway.models import CallContext, LLMRequest, PromptRef
from video_agent.observability.codes import ErrorCode
from video_agent.planning.bible import compute_content_hash
from video_agent.planning.errors import BibleTooVagueError, PlanInvalidError, PlanUnparseableError
from video_agent.planning.models import (
    Beat,
    CharacterSpec,
    ContinuityBible,
    LensLanguageSpec,
    LightingSpec,
    LocationSpec,
    PaletteSpec,
    StoryPlan,
    WardrobeSpec,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.gateway.gateway import Gateway
    from video_agent.harness.context import NodeContext

__all__ = ["lock_bible", "plan_story"]

_MAX_ATTEMPTS = 2
"""One structured re-ask, never a second. `[D-28]`."""

_HEDGE_WORDS = frozenset({"some", "perhaps", "various", "or", "maybe", "possibly", "several"})
_HEDGE_RE = re.compile(
    r"\b(" + "|".join(re.escape(word) for word in _HEDGE_WORDS) + r")\b", re.IGNORECASE
)
_MIN_DISTINGUISHING_DETAILS = 3
"""How many of the five distinguishing-detail fields must be non-empty. `planning.md` §3.2."""

type _JSONLeaf = str | list["_JSONLeaf"] | dict[str, "_JSONLeaf"]


class _PlanDraft(BaseModel):
    """What the model produces for `plan_story`. Cross-beat checks live on `StoryPlan` alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logline: str = Field(max_length=200)
    beats: list[Beat] = Field(min_length=4, max_length=4)


class _BibleDraft(BaseModel):
    """What the model produces for `lock_bible`. Locking metadata is computed, never asked for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    character: CharacterSpec
    wardrobe: WardrobeSpec
    location: LocationSpec
    lighting: LightingSpec
    palette: PaletteSpec
    lens_language: LensLanguageSpec
    negative_constraints: list[str] = Field(default_factory=list)


async def plan_story(prompt: str, *, ctx: NodeContext, gateway: Gateway) -> StoryPlan:
    """One `reasoning-high` pass, structured against `StoryPlan`. `planning.md` §3.1."""
    ctx.require_tool("llm.reasoning_high")
    call_ctx = CallContext(job_id=str(ctx.job_id), node=ctx.node)
    violations = ""
    last_error: ValidationError | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        draft = await _draft_plan(gateway, call_ctx, prompt, violations)
        try:
            return StoryPlan(
                job_id=ctx.job_id,
                logline=draft.logline,
                beats=draft.beats,
                model_alias=Alias.REASONING_HIGH.value,
                prompt_version="v1",
                created_at=datetime.now(UTC),
            )
        except ValidationError as exc:
            last_error = exc
            violations = (
                f"Your previous plan was invalid: {exc}. Correct exactly this and resubmit."
            )
    assert last_error is not None  # loop always runs at least once
    code_hint = (
        ErrorCode.VA_PLAN_002 if "sum to exactly" in str(last_error) else ErrorCode.VA_PLAN_003
    )
    raise PlanInvalidError(str(last_error), code=code_hint)


async def _draft_plan(
    gateway: Gateway, call_ctx: CallContext, prompt: str, violations: str
) -> _PlanDraft:
    request = LLMRequest(
        alias=Alias.REASONING_HIGH,
        prompt_ref=PromptRef(name="story_plan", version="v1"),
        variables={"retry_violations": violations},
        untrusted={"user_prompt": prompt},
        response_model=_PlanDraft,
        max_output_tokens=2000,
        timeout_s=60.0,
        idempotency_hint=f"plan_story:{call_ctx.job_id}",
    )
    try:
        response = await gateway.call(request, ctx=call_ctx)
    except StructuredOutputError as exc:
        raise PlanUnparseableError(str(exc)) from exc
    assert isinstance(response.parsed, _PlanDraft)
    return response.parsed


async def lock_bible(
    plan: StoryPlan, prompt: str, *, ctx: NodeContext, gateway: Gateway
) -> ContinuityBible:
    """One `reasoning-high` pass against the accepted plan. `planning.md` §3.2."""
    ctx.require_tool("llm.reasoning_high")
    call_ctx = CallContext(job_id=str(ctx.job_id), node=ctx.node)
    violations = ""
    last_violations: list[str] = []
    for _attempt in range(_MAX_ATTEMPTS):
        draft = await _draft_bible(gateway, call_ctx, plan, prompt, violations)
        found = _specificity_violations(draft)
        if not found:
            return _finalize_bible(draft, ctx.job_id)
        last_violations = found
        violations = (
            "Your previous bible was too vague: "
            + "; ".join(found)
            + ". Correct exactly this and resubmit."
        )
    message = f"continuity bible failed the specificity gate twice: {'; '.join(last_violations)}"
    raise BibleTooVagueError(message)


async def _draft_bible(
    gateway: Gateway,
    call_ctx: CallContext,
    plan: StoryPlan,
    prompt: str,
    violations: str,
) -> _BibleDraft:
    request = LLMRequest(
        alias=Alias.REASONING_HIGH,
        prompt_ref=PromptRef(name="continuity_bible", version="v1"),
        variables={
            "story_plan_json": plan.model_dump_json(),
            "retry_violations": violations,
        },
        untrusted={"user_prompt": prompt},
        response_model=_BibleDraft,
        max_output_tokens=3000,
        timeout_s=60.0,
        idempotency_hint=f"lock_bible:{call_ctx.job_id}",
    )
    try:
        response = await gateway.call(request, ctx=call_ctx)
    except StructuredOutputError as exc:
        raise PlanUnparseableError(str(exc)) from exc
    assert isinstance(response.parsed, _BibleDraft)
    return response.parsed


def _finalize_bible(draft: _BibleDraft, job_id: UUID) -> ContinuityBible:
    provisional = ContinuityBible(
        job_id=job_id,
        character=draft.character,
        wardrobe=draft.wardrobe,
        location=draft.location,
        lighting=draft.lighting,
        palette=draft.palette,
        lens_language=draft.lens_language,
        negative_constraints=draft.negative_constraints,
        content_hash="pending",
        locked_at=datetime.now(UTC),
        model_alias=Alias.REASONING_HIGH.value,
        prompt_version="v1",
    )
    content_hash = compute_content_hash(provisional)
    return provisional.model_copy(update={"content_hash": content_hash})


def _specificity_violations(draft: _BibleDraft) -> list[str]:
    """Concreteness checks. `planning.md` §3.2. Directional, not exhaustive."""
    violations: list[str] = []
    for path, value in _iter_strings(draft.model_dump()):
        if not value.strip():
            violations.append(f"{path} is empty")
            continue
        if _HEDGE_RE.search(value):
            violations.append(f"{path} uses hedging language: {value!r}")
    character = draft.character
    distinguishing = [
        character.facial_features,
        character.distinguishing_marks or "",
        character.hair,
        character.build,
        character.skin_tone,
    ]
    concrete_count = sum(1 for detail in distinguishing if detail.strip())
    if concrete_count < _MIN_DISTINGUISHING_DETAILS:
        violations.append("character needs at least three concrete distinguishing details")
    return violations


def _iter_strings(value: _JSONLeaf, path: str = "") -> list[tuple[str, str]]:
    """Every string leaf in a nested dict/list, labelled by its dotted path."""
    results: list[tuple[str, str]] = []
    if isinstance(value, str):
        results.append((path or "value", value))
    elif isinstance(value, dict):
        for key, item in value.items():
            results.extend(_iter_strings(item, f"{path}.{key}" if path else key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            results.extend(_iter_strings(item, f"{path}[{index}]"))
    return results
