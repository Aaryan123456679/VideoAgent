"""The nine node bodies and their routers. `graph.md` §3.

Every router calls `guard()` first (`graph.md` §3.1) — a reflective CI test in
`test_graph_build.py` asserts this over every function in `_ROUTERS`. Nodes never call `guard`
themselves; a node's job is to do one unit of work and report what changed, and the harness
veto is applied once, uniformly, by the router that follows it.

**`generate_shot` and `extract_final_frame` are real as of T2.3** — the provider
submit/poll/settle sequence and ffmpeg frame extraction described in their own docstrings below.
**`assemble` and `deliver` remain T2.4 stubs**; wiring them up is what makes a job produce a
finished, delivered video rather than four accepted, chained clips.

**`qc_shot` is a stub of a different kind.** `graph.md`'s v1 status header is explicit that
QC scoring and repair are deferred to E3/`S3.2.2`; this node unconditionally accepts every shot
rather than scoring it. The marker below is what `S3.2.2`'s test asserts has been removed.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from video_agent.assembly.media_toolchain import build_thumbnail, concat_clips, normalize_clip
from video_agent.assembly.models import DeliveryManifest, ManifestEntry
from video_agent.gateway.models import ArtifactRef
from video_agent.graph.deps import GraphDeps
from video_agent.graph.frame_extraction import STEP_BACK_ATTEMPTS, find_last_usable_frame
from video_agent.graph.guard import guard
from video_agent.graph.state import GraphInvariantError, JobState, ShotState
from video_agent.harness.budget import Charge, ChargeState
from video_agent.harness.context import NodeContext
from video_agent.persistence.enums import ArtifactKind, ShotStatus
from video_agent.persistence.enums import AttemptState as PersistenceAttemptState
from video_agent.persistence.enums import BeatKind as PersistenceBeatKind
from video_agent.persistence.enums import JobOutcome as PersistenceJobOutcome
from video_agent.persistence.objects import sha256_of
from video_agent.persistence.repositories import (
    ArtifactRecord,
    ArtifactRepository,
    AttemptClaim,
    AttemptRequest,
    CheckpointRepository,
    ContinuityBibleRepository,
    CostSettlement,
    JobRepository,
    NewArtifact,
    NewCheckpoint,
    NewContinuityBible,
    NewStoryPlan,
    ProviderSubmission,
    ShotAttemptRepository,
    ShotRepository,
    StoryPlanRepository,
)
from video_agent.persistence.session import tenant_session
from video_agent.planning.service import lock_bible as lock_bible_domain
from video_agent.planning.service import plan_story as plan_story_domain
from video_agent.providers.compose import ComposedPrompt, compose_prompt
from video_agent.providers.models import (
    Capability,
    ShotRequest,
    ShotResult,
    compute_request_fingerprint,
)

__all__ = [
    "assemble_node",
    "deliver_node",
    "extract_final_frame_node",
    "finalize_node",
    "generate_shot_node",
    "lock_bible_node",
    "plan_story_node",
    "qc_shot_node",
    "route_after_assemble",
    "route_after_bible",
    "route_after_frame",
    "route_after_generate",
    "route_after_plan",
    "route_after_qc",
    "route_select",
    "select_next_shot_node",
]

QC_ACCEPT_THRESHOLD = 0.75
"""`[PRD §How it works 5]`. Unreachable as a *decision* while `qc_shot` stubs a fixed accept,
kept as the named constant the real scorer (`S3.2.2`) will compare against."""


# ---------------------------------------------------------------------------
# plan_story
# ---------------------------------------------------------------------------


async def plan_story_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """`planning.md` §3.1: one LLM pass, persisted, then seeded into four pending shots."""
    ctx = NodeContext.for_node(
        job_id=state.job_id,
        node="plan_story",
        trace_id=state.trace_id,
        budget_remaining=state.budget.view(deps.now()),
    )
    plan = await plan_story_domain(state.prompt, ctx=ctx, gateway=deps.gateway)
    async with tenant_session(deps.engine, state.tenant_id) as session:
        await StoryPlanRepository(session).create(
            NewStoryPlan(
                job_id=plan.job_id,
                logline=plan.logline,
                total_duration_s=Decimal(str(plan.total_duration_s)),
                model_alias=plan.model_alias,
                prompt_version=plan.prompt_version,
                beats=[
                    {
                        "kind": beat.kind.value,
                        "action": beat.action,
                        "camera_move": beat.camera_move.value,
                        "duration_s": beat.duration_s,
                        "continuity_note": beat.continuity_note,
                    }
                    for beat in plan.beats
                ],
            )
        )
    shots = tuple(
        ShotState(index=beat.index, beat_kind=PersistenceBeatKind(beat.kind.value))
        for beat in plan.beats
    )
    return {"story_plan": plan, "shots": shots}


async def route_after_plan(state: JobState, deps: GraphDeps) -> str:
    diverted = await guard(state, "plan_story", harness=deps.harness, now=deps.now())
    return diverted if diverted is not None else "lock_bible"


# ---------------------------------------------------------------------------
# lock_bible
# ---------------------------------------------------------------------------


async def lock_bible_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """`planning.md` §3.2: one LLM pass against the accepted plan, persisted once, never updated."""
    if state.story_plan is None:
        message = "lock_bible reached without a story plan; route_after_plan should have caught it"
        raise GraphInvariantError(message)
    ctx = NodeContext.for_node(
        job_id=state.job_id,
        node="lock_bible",
        trace_id=state.trace_id,
        budget_remaining=state.budget.view(deps.now()),
    )
    bible = await lock_bible_domain(state.story_plan, state.prompt, ctx=ctx, gateway=deps.gateway)
    async with tenant_session(deps.engine, state.tenant_id) as session:
        await ContinuityBibleRepository(session).create(
            NewContinuityBible(
                job_id=state.job_id,
                dimensions={
                    "character": bible.character.model_dump(mode="json"),
                    "wardrobe": bible.wardrobe.model_dump(mode="json"),
                    "location": bible.location.model_dump(mode="json"),
                    "lighting": bible.lighting.model_dump(mode="json"),
                    "palette": bible.palette.model_dump(mode="json"),
                    "lens_language": bible.lens_language.model_dump(mode="json"),
                },
                negative_constraints=list(bible.negative_constraints),
                content_hash=bible.content_hash,
                model_alias=bible.model_alias,
                prompt_version=bible.prompt_version,
            )
        )
    return {"bible": bible, "bible_hash": bible.content_hash}


async def route_after_bible(state: JobState, deps: GraphDeps) -> str:
    diverted = await guard(state, "lock_bible", harness=deps.harness, now=deps.now())
    return diverted if diverted is not None else "select_next_shot"


# ---------------------------------------------------------------------------
# select_next_shot
# ---------------------------------------------------------------------------


async def select_next_shot_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """Advances `shot_index` to the lowest-index pending shot.

    v1 gap (`graph.md` §5's "second guard"): this reads only the checkpointed `state.shots`,
    never Postgres directly — `ShotRepository` has no list-by-job query yet. Safe today because
    `state.shots` changes only under the one-writer Redis lock and the `[D-23]` atomic
    checkpoint write; a stale checkpoint causing a wrongly-repeated shot is exactly the
    resume-time failure mode `graph.md` §5 defers to E3 alongside `resume()`/`regenerate_shot()`.
    """
    del deps
    next_index = next(
        (shot.index for shot in state.shots if shot.status is ShotStatus.PENDING), None
    )
    if next_index is None:
        return {}
    return {"shot_index": next_index}


async def route_select(state: JobState, deps: GraphDeps) -> str:
    diverted = await guard(state, "select_next_shot", harness=deps.harness, now=deps.now())
    if diverted is not None:
        return diverted
    has_pending = any(shot.status is ShotStatus.PENDING for shot in state.shots)
    return "generate_shot" if has_pending else "assemble"


# ---------------------------------------------------------------------------
# generate_shot / extract_final_frame — T2.3
# ---------------------------------------------------------------------------

_DEFAULT_MAX_PROMPT_CHARS = 4000
"""Fallback `compose_prompt` limit when no provider currently satisfies the shot's required
capabilities. Composition still runs so the later `deps.providers.generate()` call raises
`NoProviderSatisfiesCapabilitiesError` with a specific message, rather than the composer
failing first with a less informative one."""

_PROVIDER_TIMEOUT_S = 180.0
"""One shot's submit-plus-poll ceiling. `GraphDeps` carries no settings object and
`ShotRequest.timeout_s` has no default, so this is a deliberate constant rather than a
per-provider value; `MagicHourProvider`'s own `typical_latency_s` is 60s, so 180s leaves
headroom for a slow render without a node that can hang indefinitely."""

_SHOT_CLIP_CONTENT_TYPE = "video/mp4"
_CONTINUITY_FRAME_CONTENT_TYPE = "image/png"
"""`[D-44]`: every continuity anchor is lossless PNG."""


def _required_capabilities(*, conditioning_frame_present: bool) -> frozenset[Capability]:
    """`providers.md` §3's negotiation table, inlined rather than called through
    `providers.negotiate.required_for` because that helper takes a fully-built `ShotRequest`
    and this needs the answer *before* the prompt (and therefore the request) exists — the
    provider's `max_prompt_chars` is itself an input to composing the prompt.
    """
    required = {Capability.DURATION_10S, Capability.ASPECT_16_9, Capability.RES_720P}
    if conditioning_frame_present:
        required.add(Capability.IMAGE_CONDITIONING)
    return frozenset(required)


def _artifact_ref(record: ArtifactRecord) -> ArtifactRef:
    """A catalogued artifact, as the bytes store and the provider layer address it."""
    return ArtifactRef(artifact_id=str(record.id), storage_key=record.storage_key)


async def _resolve_conditioning(
    state: JobState, deps: GraphDeps
) -> tuple[ArtifactRef | None, bool, str | None]:
    """Chaining (`providers.md` §6, `[D-05]`): shot 0 is text-only by definition and that is
    never degraded. A later shot chains the most recent accepted frame; if none exists (every
    predecessor was abandoned), it also generates text-only, but that *is* flagged degraded.

    Returns `(conditioning_ref, degraded, degraded_reason)`.
    """
    if state.shot_index == 0:
        return None, False, None
    if state.last_good_frame_artifact_id is None:
        degraded_reason = (
            f"shot {state.shot_index} has no accepted predecessor frame; generating "
            "text-only from the bible and beat `[D-05]`"
        )
        return None, True, degraded_reason
    async with tenant_session(deps.engine, state.tenant_id) as session:
        frame_record = await ArtifactRepository(session).get(state.last_good_frame_artifact_id)
    if frame_record is None:
        message = (
            f"artifact {state.last_good_frame_artifact_id} named by "
            f"last_good_frame_artifact_id was not found for job {state.job_id}"
        )
        raise GraphInvariantError(message)
    return _artifact_ref(frame_record), False, None


async def _claim_shot_attempt(
    state: JobState,
    deps: GraphDeps,
    *,
    attempt_no: int,
    fingerprint: str,
    composed: ComposedPrompt,
    bible_hash: str,
) -> AttemptClaim:
    """Phase 1 (`graph.md` §4): insert the `ShotAttempt` as `in_flight`, committed before the
    provider is ever called `[D-24]`. `ShotRepository.ensure` is what makes this redeliverable
    `[D-67]`: nothing upstream gives a shot a Postgres row until its first attempt needs one.
    """
    async with tenant_session(deps.engine, state.tenant_id) as session:
        beat_id = await StoryPlanRepository(session).get_beat_id(state.job_id, state.shot_index)
        if beat_id is None:
            message = f"no locked beat found for job {state.job_id} shot {state.shot_index}"
            raise GraphInvariantError(message)
        shot_row = await ShotRepository(session).ensure(
            job_id=state.job_id, beat_id=beat_id, idx=state.shot_index
        )
        return await ShotAttemptRepository(session).claim(
            AttemptRequest(
                shot_id=shot_row.id,
                job_id=state.job_id,
                attempt_no=attempt_no,
                request_fingerprint=fingerprint,
                prompt_text=composed.text,
                prompt_hash=composed.prompt_hash,
                bible_hash=bible_hash,
                conditioning_frame_id=state.last_good_frame_artifact_id,
            )
        )
    # `claim.adopted` means a redelivered call already owns this fingerprint. Full
    # crash-reconciliation via `lookup()` needs a *pinned* `VideoProvider`, not the
    # registry-level `ProviderRegistry` protocol this node is handed (only `select`/`generate`;
    # see `providers/models.py`) — a documented v1 gap. The fingerprint's unique constraint
    # still makes a second attempt row impossible either way; only the provider-side
    # double-submit guard is unavailable at this layer today.


@dataclass(frozen=True, slots=True)
class _GenerateOutcome:
    """Everything phase 3 needs about what phase 2 produced, as one value rather than seven
    positional facts `_settle_shot_and_checkpoint` would otherwise have to keep in order.
    """

    claim: AttemptClaim
    result: ShotResult
    clip_bytes: bytes
    checksum: str
    attempt_no: int
    degraded: bool
    degraded_reason: str | None


async def _settle_shot_and_checkpoint(
    state: JobState, deps: GraphDeps, *, shot: ShotState, outcome: _GenerateOutcome
) -> dict[str, Any]:
    """Phase 3 (`graph.md` §4): settle the cost, catalogue the clip, and checkpoint — one
    transaction, so a crash here is a repeated node on resume, never an unrecorded one.
    """
    async with tenant_session(deps.engine, state.tenant_id) as session:
        await ShotAttemptRepository(session).settle_cost(
            outcome.claim.attempt.id,
            CostSettlement(
                state=PersistenceAttemptState.SUCCEEDED,
                cost_usd=outcome.result.cost_usd,
                credits_charged=outcome.result.credits_charged,
            ),
        )
        artifact_record = await ArtifactRepository(session).record(
            NewArtifact(
                job_id=state.job_id,
                kind=ArtifactKind.SHOT_CLIP,
                storage_key=outcome.result.clip.storage_key,
                content_type=_SHOT_CLIP_CONTENT_TYPE,
                size_bytes=len(outcome.clip_bytes),
                checksum_sha256=outcome.checksum,
                shot_index=state.shot_index,
            )
        )

        updated_shot = shot.model_copy(
            update={"attempts_used": outcome.attempt_no, "clip_artifact_id": artifact_record.id}
        )
        shots = tuple(updated_shot if s.index == state.shot_index else s for s in state.shots)
        partial: dict[str, Any] = {"shots": shots, "budget": state.budget}
        if outcome.degraded:
            partial["degraded"] = True
            partial["degraded_reason"] = outcome.degraded_reason
        snapshot = state.model_copy(update=partial)

        latest_checkpoint = await CheckpointRepository(session).latest(state.job_id)
        next_seq = 0 if latest_checkpoint is None else latest_checkpoint.seq + 1
        await CheckpointRepository(session).write(
            NewCheckpoint(
                thread_id=state.job_id,
                node="generate_shot",
                seq=next_seq,
                state=snapshot.model_dump(mode="json"),
                budget_used={
                    "usd_spent": str(state.budget.usd_spent),
                    "tokens_used": state.budget.tokens_used,
                    "iterations_used": state.budget.iterations_used,
                },
            )
        )
    return partial


async def generate_shot_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """T2.3: submit the current shot to a provider via the three-phase write sequence
    (`graph.md` §4, `[D-23]`/`[D-24]`/`[D-58]`) and chain from `last_good_frame_artifact_id`.

    Three phases, three transactions, exactly as `graph.md` §4 spells out: (1) `_claim_shot_
    attempt` inserts the attempt as `in_flight`, committed before the provider is ever called;
    (2) call the provider, then immediately persist `provider_project_id`; (3) `_settle_shot_
    and_checkpoint` settles the cost, catalogues the clip, and writes a checkpoint, all in one
    transaction.

    **Known v1 gap** (documented in the T2.3 task report, not silent): `MagicHourProvider.
    generate()` (T2.2, frozen) submits and polls to completion in one call, so phase (2)'s
    `record_submission` happens right after the whole call returns rather than the instant the
    provider accepts the submission — the provider layer exposes no submit-only hook a caller
    could use to persist the id before the poll loop runs. A crash during that poll therefore
    leaves an `in_flight` attempt with no `provider_project_id`, which is a narrower window than
    `[D-24]` designs for but not a wider one: nothing is billed and nothing is lost, resume
    simply cannot skip straight to reconciliation for a crash in that specific window.
    """
    if state.story_plan is None or state.bible is None or state.bible_hash is None:
        message = (
            f"generate_shot reached for job {state.job_id} without a locked plan and bible; "
            "route_after_bible should have caught it"
        )
        raise GraphInvariantError(message)
    bible_hash = state.bible_hash

    shot = state.shots[state.shot_index]
    beat = state.story_plan.beats[state.shot_index]
    conditioning_ref, degraded, degraded_reason = await _resolve_conditioning(state, deps)

    ctx = NodeContext.for_node(
        job_id=state.job_id,
        node="generate_shot",
        trace_id=state.trace_id,
        budget_remaining=state.budget.view(deps.now()),
        bible=state.bible,
        beat=beat,
        chained_frame_ref=conditioning_ref,
    )
    ctx.require_tool("video.generate")

    candidates = deps.providers.select(
        _required_capabilities(conditioning_frame_present=conditioning_ref is not None)
    )
    max_chars = min(
        (candidate.profile.max_prompt_chars for candidate in candidates),
        default=_DEFAULT_MAX_PROMPT_CHARS,
    )
    composed = compose_prompt(state.bible, beat, max_chars=max_chars)

    attempt_no = shot.repairs_used + 1
    frame_id = str(state.last_good_frame_artifact_id) if conditioning_ref is not None else None
    fingerprint = compute_request_fingerprint(
        job_id=state.job_id,
        shot_index=state.shot_index,
        attempt_no=attempt_no,
        prompt_hash=composed.prompt_hash,
        frame_id=frame_id,
        seed=None,
    )

    claim = await _claim_shot_attempt(
        state,
        deps,
        attempt_no=attempt_no,
        fingerprint=fingerprint,
        composed=composed,
        bible_hash=bible_hash,
    )

    shot_request = ShotRequest(
        job_id=state.job_id,
        shot_index=state.shot_index,
        attempt_no=attempt_no,
        prompt=composed.text,
        conditioning_frame=conditioning_ref,
        duration_s=beat.duration_s,
        request_fingerprint=fingerprint,
        timeout_s=_PROVIDER_TIMEOUT_S,
    )

    # Phase 2: call the provider. Deliberately outside any DB transaction `[D-23]`.
    result = await deps.providers.generate(shot_request, ctx=ctx)
    if result.degraded and result.degrade_reason:
        degraded = True
        degraded_reason = (
            result.degrade_reason
            if degraded_reason is None
            else f"{degraded_reason}; {result.degrade_reason}"
        )

    async with tenant_session(deps.engine, state.tenant_id) as session:
        await ShotAttemptRepository(session).record_submission(
            claim.attempt.id,
            ProviderSubmission(
                provider_project_id=result.provider_project_id,
                provider_key=result.provider_key,
                provider_model=result.provider_model,
                seed=result.seed_used,
                seed_supported=result.seed_used is not None,
            ),
        )

    ctx.require_tool("artifact.write")
    clip_bytes = await deps.artifacts.read(result.clip)
    checksum = sha256_of(clip_bytes)
    state.budget.apply(
        Charge(
            charge_id=fingerprint,
            usd=result.cost_usd,
            tokens=0,
            state=ChargeState.FINAL if result.cost_is_final else ChargeState.PROVISIONAL,
        )
    )

    outcome = _GenerateOutcome(
        claim=claim,
        result=result,
        clip_bytes=clip_bytes,
        checksum=checksum,
        attempt_no=attempt_no,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )
    return await _settle_shot_and_checkpoint(state, deps, shot=shot, outcome=outcome)


async def route_after_generate(state: JobState, deps: GraphDeps) -> str:
    diverted = await guard(state, "generate_shot", harness=deps.harness, now=deps.now())
    return diverted if diverted is not None else "extract_final_frame"


async def extract_final_frame_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """T2.3: last decodable frame, lossless PNG, uniform-frame rejection (`assembly.md`)."""
    shot = state.shots[state.shot_index]
    if shot.clip_artifact_id is None:
        message = (
            f"extract_final_frame reached for job {state.job_id} shot {state.shot_index} "
            "with no clip_artifact_id; route_after_generate should have caught it"
        )
        raise GraphInvariantError(message)

    ctx = NodeContext.for_node(
        job_id=state.job_id,
        node="extract_final_frame",
        trace_id=state.trace_id,
        budget_remaining=state.budget.view(deps.now()),
        bible=state.bible,
    )
    ctx.require_tool("ffmpeg.extract_frame")

    async with tenant_session(deps.engine, state.tenant_id) as session:
        clip_record = await ArtifactRepository(session).get(shot.clip_artifact_id)
    if clip_record is None:
        message = (
            f"artifact {shot.clip_artifact_id} named by shot {state.shot_index}'s "
            f"clip_artifact_id was not found for job {state.job_id}"
        )
        raise GraphInvariantError(message)
    clip_bytes = await deps.artifacts.read(_artifact_ref(clip_record))

    with tempfile.TemporaryDirectory(prefix=f"frame-{state.job_id}-") as scratch:
        clip_path = Path(scratch) / "clip.mp4"
        await asyncio.to_thread(clip_path.write_bytes, clip_bytes)
        frame_path = Path(scratch) / "frame.png"
        # `assembly.md` §6: ffmpeg invocations are awaited via an executor, never inline in the
        # event loop — `find_last_usable_frame` shells out synchronously underneath.
        found = await asyncio.to_thread(find_last_usable_frame, clip_path, frame_path)
        if not found:
            # `assembly.md` §8: total extraction failure and uniform-frame rejection are the
            # same outcome here — no anchor, flag degraded, never block the pipeline on a
            # chaining aid. `final_frame_artifact_id` is left unset.
            return {
                "degraded": True,
                "degraded_reason": (
                    f"shot {state.shot_index}: no non-uniform frame found within "
                    f"{STEP_BACK_ATTEMPTS}s of end-of-stream; no continuity anchor extracted "
                    "`[D-45]`"
                ),
            }
        png_bytes = await asyncio.to_thread(frame_path.read_bytes)

    ctx.require_tool("artifact.write")
    frame_ref = await deps.artifacts.write(
        content_type=_CONTINUITY_FRAME_CONTENT_TYPE, data=png_bytes
    )
    checksum = sha256_of(png_bytes)

    async with tenant_session(deps.engine, state.tenant_id) as session:
        artifact_record = await ArtifactRepository(session).record(
            NewArtifact(
                job_id=state.job_id,
                kind=ArtifactKind.CONTINUITY_FRAME,
                storage_key=frame_ref.storage_key,
                content_type=_CONTINUITY_FRAME_CONTENT_TYPE,
                size_bytes=len(png_bytes),
                checksum_sha256=checksum,
                shot_index=state.shot_index,
            )
        )

    updated_shot = shot.model_copy(update={"final_frame_artifact_id": artifact_record.id})
    shots = tuple(updated_shot if s.index == state.shot_index else s for s in state.shots)
    return {"shots": shots}


async def route_after_frame(state: JobState, deps: GraphDeps) -> str:
    diverted = await guard(state, "extract_final_frame", harness=deps.harness, now=deps.now())
    return diverted if diverted is not None else "qc_shot"


# ---------------------------------------------------------------------------
# qc_shot — unconditional-accept stub, v1
# ---------------------------------------------------------------------------


async def qc_shot_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """# QC-STUB [S3.2.2 removes this]: v1 accepts every shot unconditionally.

    Real scoring and the repair back-edge are deferred to E3 (`qc.md`, `S3.2.2`); this node
    only performs the bookkeeping a scored decision would trigger on an unconditional accept —
    marking the shot accepted and advancing the chaining frame. Never scores, never repairs.
    """
    del deps
    shot = state.shots[state.shot_index]
    accepted = shot.model_copy(
        update={
            "status": ShotStatus.ACCEPTED,
            "best_score": 1.0,
        }
    )
    shots = tuple(
        accepted if s.index == state.shot_index else s for s in state.shots
    )
    return {
        "shots": shots,
        "last_good_frame_artifact_id": accepted.final_frame_artifact_id,
    }


async def route_after_qc(state: JobState, deps: GraphDeps) -> str:
    diverted = await guard(state, "qc_shot", harness=deps.harness, now=deps.now())
    if diverted is not None:
        return diverted
    shot = state.shots[state.shot_index]
    if shot.status in (ShotStatus.ACCEPTED, ShotStatus.ABANDONED):
        return "select_next_shot"
    return "generate_shot"  # the repair back-edge — unreachable while qc_shot always accepts


# ---------------------------------------------------------------------------
# assemble / deliver — T2.4
# ---------------------------------------------------------------------------

_FINAL_VIDEO_CONTENT_TYPE = "video/mp4"
_THUMBNAIL_CONTENT_TYPE = "image/jpeg"


def _accepted_shots_in_order(state: JobState) -> list[ShotState]:
    return sorted(
        (shot for shot in state.shots if shot.status is ShotStatus.ACCEPTED),
        key=lambda shot: shot.index,
    )


def _best_thumbnail_source(accepted: list[ShotState]) -> ShotState:
    """`[D-49]`: the highest-`best_score` accepted shot's frame, falling back through the next
    best when the top scorer has no usable continuity frame (`extract_final_frame_node` leaves
    `final_frame_artifact_id` unset on total extraction failure — `assembly.md` §8's "no anchor,
    degraded=true" path). Raises only when *no* accepted shot has one at all.
    """
    candidates = sorted(
        (shot for shot in accepted if shot.final_frame_artifact_id is not None),
        key=lambda shot: (-(shot.best_score if shot.best_score is not None else 0.0), shot.index),
    )
    if not candidates:
        message = (
            "no accepted shot has a final_frame_artifact_id; assemble cannot select a "
            "thumbnail source"
        )
        raise GraphInvariantError(message)
    return candidates[0]


async def _fetch_clip_and_frame_records(
    state: JobState, deps: GraphDeps, *, accepted: list[ShotState], thumbnail_shot: ShotState
) -> tuple[list[ArtifactRecord], ArtifactRecord]:
    """Phase 1: resolve every accepted shot's clip, plus the one continuity frame the
    thumbnail is built from — all reads, in one transaction, before any ffmpeg runs.
    """
    frame_artifact_id = thumbnail_shot.final_frame_artifact_id
    if frame_artifact_id is None:  # pragma: no cover - guarded by _best_thumbnail_source
        message = "thumbnail_shot has no final_frame_artifact_id"
        raise GraphInvariantError(message)

    async with tenant_session(deps.engine, state.tenant_id) as session:
        artifact_repo = ArtifactRepository(session)
        clip_records: list[ArtifactRecord] = []
        for shot in accepted:
            if shot.clip_artifact_id is None:
                message = (
                    f"assemble reached for job {state.job_id} shot {shot.index} accepted "
                    "with no clip_artifact_id"
                )
                raise GraphInvariantError(message)
            record = await artifact_repo.get(shot.clip_artifact_id)
            if record is None:
                message = (
                    f"artifact {shot.clip_artifact_id} named by shot {shot.index}'s "
                    f"clip_artifact_id was not found for job {state.job_id}"
                )
                raise GraphInvariantError(message)
            clip_records.append(record)

        frame_record = await artifact_repo.get(frame_artifact_id)
        if frame_record is None:
            message = (
                f"artifact {frame_artifact_id} named by shot {thumbnail_shot.index}'s "
                f"final_frame_artifact_id was not found for job {state.job_id}"
            )
            raise GraphInvariantError(message)
    return clip_records, frame_record


async def _render_final_video_and_thumbnail(
    state: JobState, ctx: NodeContext, *, clip_bytes_list: list[bytes], frame_bytes: bytes
) -> tuple[bytes, bytes]:
    """Phase 2: normalize + concat (`[D-46]`/`[D-47]`) and re-encode the thumbnail, entirely in
    a scratch directory. `assembly.md` §6: every ffmpeg call is awaited via an executor, never
    inline in the event loop — `normalize_clip`/`concat_clips`/`build_thumbnail` shell out
    synchronously and are wrapped in `asyncio.to_thread` at this, their one call site.
    """
    with tempfile.TemporaryDirectory(prefix=f"assemble-{state.job_id}-") as scratch:
        scratch_path = Path(scratch)

        ctx.require_tool("ffmpeg.concat")
        normalized_paths: list[Path] = []
        for index, clip_bytes in enumerate(clip_bytes_list):
            raw_path = scratch_path / f"raw-{index}.mp4"
            norm_path = scratch_path / f"norm-{index}.mp4"
            await asyncio.to_thread(raw_path.write_bytes, clip_bytes)
            await asyncio.to_thread(normalize_clip, raw_path, norm_path)
            normalized_paths.append(norm_path)

        final_path = scratch_path / "final.mp4"
        await asyncio.to_thread(concat_clips, normalized_paths, final_path)
        final_bytes = await asyncio.to_thread(final_path.read_bytes)

        ctx.require_tool("ffmpeg.thumbnail")
        source_png = scratch_path / "thumbnail-source.png"
        thumb_path = scratch_path / "thumbnail.jpg"
        await asyncio.to_thread(source_png.write_bytes, frame_bytes)
        await asyncio.to_thread(build_thumbnail, source_png, thumb_path)
        thumbnail_bytes = await asyncio.to_thread(thumb_path.read_bytes)
    return final_bytes, thumbnail_bytes


async def _catalogue_final_video_and_thumbnail(
    state: JobState,
    deps: GraphDeps,
    *,
    final_bytes: bytes,
    thumbnail_bytes: bytes,
) -> tuple[ArtifactRecord, ArtifactRecord]:
    """Phase 3: upload both renders, then catalogue them in one transaction."""
    final_ref = await deps.artifacts.write(content_type=_FINAL_VIDEO_CONTENT_TYPE, data=final_bytes)
    thumbnail_ref = await deps.artifacts.write(
        content_type=_THUMBNAIL_CONTENT_TYPE, data=thumbnail_bytes
    )

    async with tenant_session(deps.engine, state.tenant_id) as session:
        artifact_repo = ArtifactRepository(session)
        final_video_record = await artifact_repo.record(
            NewArtifact(
                job_id=state.job_id,
                kind=ArtifactKind.FINAL_VIDEO,
                storage_key=final_ref.storage_key,
                content_type=_FINAL_VIDEO_CONTENT_TYPE,
                size_bytes=len(final_bytes),
                checksum_sha256=sha256_of(final_bytes),
            )
        )
        thumbnail_record = await artifact_repo.record(
            NewArtifact(
                job_id=state.job_id,
                kind=ArtifactKind.THUMBNAIL,
                storage_key=thumbnail_ref.storage_key,
                content_type=_THUMBNAIL_CONTENT_TYPE,
                size_bytes=len(thumbnail_bytes),
                checksum_sha256=sha256_of(thumbnail_bytes),
            )
        )
    return final_video_record, thumbnail_record


def _music_bed_partial(state: JobState) -> dict[str, Any]:
    """`[D-48]`/`[D-69]`: a requested music bed is a documented, non-fatal no-op — v1 ships no
    bundled library and no caller-supplied-audio fetch is wired anywhere in this repo, so the
    job is delivered silent and flagged `degraded` rather than inventing a fake library or
    blocking delivery over optional audio.
    """
    if not state.music_bed:
        return {}
    reason = (
        "music_bed was requested but v1 has no bundled or caller-supplied audio library "
        "wired up; delivered silent `[D-48]`/`[D-69]`"
    )
    return {
        "degraded": True,
        "degraded_reason": (
            reason if not state.degraded_reason else f"{state.degraded_reason}; {reason}"
        ),
    }


async def assemble_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """T2.4: normalize every accepted shot's clip to the canonical delivery profile
    (`assembly.md` §4.1 `[D-46]`; see `assembly.media_toolchain`'s `CANONICAL_*` constants —
    1280x720, 24fps CFR, H.264 High `yuv420p`, BT.709 limited, `faststart`), concatenate by
    stream copy only (`[D-47]`: hard cuts, never a crossfade — no such logic exists here), and
    pick the thumbnail from the highest-`best_score` accepted shot's already-extracted
    continuity frame (`[D-49]`) rather than re-extracting one from the clip.

    `[D-73]`/`assembly.md` §5: zero accepted shots is not an empty video, it is the
    "no playable video artifact" case — a `GraphInvariantError`, the same "should never happen,
    stop rather than paper over it" signal `graph.md` §8 uses elsewhere, not a silently empty
    manifest.

    **Known v1 gaps**, documented rather than silent: the concatenated output is not re-probed
    with `ffprobe` for duration/stream-count before being catalogued (`assembly.md` §6's
    "unprobed output is not a deliverable"), and partial assembly (`assembly.md` §5 — including
    abandoned-but-clipped shots) is out of scope, matching `route_after_qc`'s v1 note that the
    repair back-edge and therefore `abandoned` shots with a usable clip are unreachable while
    `qc_shot` unconditionally accepts.
    """
    accepted = _accepted_shots_in_order(state)
    if not accepted:
        message = (
            f"assemble reached for job {state.job_id} with zero accepted shots; a "
            "zero-deliverable job is a real error, not an empty video `[D-73]`"
        )
        raise GraphInvariantError(message)
    thumbnail_shot = _best_thumbnail_source(accepted)

    ctx = NodeContext.for_node(
        job_id=state.job_id,
        node="assemble",
        trace_id=state.trace_id,
        budget_remaining=state.budget.view(deps.now()),
        bible=state.bible,
    )

    clip_records, frame_record = await _fetch_clip_and_frame_records(
        state, deps, accepted=accepted, thumbnail_shot=thumbnail_shot
    )
    clip_bytes_list = [await deps.artifacts.read(_artifact_ref(record)) for record in clip_records]
    frame_bytes = await deps.artifacts.read(_artifact_ref(frame_record))

    final_bytes, thumbnail_bytes = await _render_final_video_and_thumbnail(
        state, ctx, clip_bytes_list=clip_bytes_list, frame_bytes=frame_bytes
    )

    ctx.require_tool("artifact.write")
    final_video_record, thumbnail_record = await _catalogue_final_video_and_thumbnail(
        state, deps, final_bytes=final_bytes, thumbnail_bytes=thumbnail_bytes
    )

    return {
        "final_video_artifact_id": final_video_record.id,
        "thumbnail_artifact_id": thumbnail_record.id,
        **_music_bed_partial(state),
    }


async def route_after_assemble(state: JobState, deps: GraphDeps) -> str:
    diverted = await guard(state, "assemble", harness=deps.harness, now=deps.now())
    return diverted if diverted is not None else "deliver"


async def deliver_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """T2.4: build the delivery manifest (`assembly.models.DeliveryManifest`). No router follows
    this node — `graph.md` §3 wires `deliver -> finalize` as a direct edge.

    `[D-52]`: the manifest names artifacts by id only, never a presigned URL — presigning
    happens once, at API-response time, via `persistence.presign` (see `api/artifacts.py`), not
    here and not into checkpointed state.

    `deliver`'s tool grant (`harness.grants.GRANTS["deliver"]`) includes `artifact.presign`,
    but this node never calls it: that grant is for the future per-job resume/regeneration
    surface `graph.md` defers to E3, not for v1's deliver, which only ever assembles ids it
    already has.
    """
    del deps
    if state.final_video_artifact_id is None or state.thumbnail_artifact_id is None:
        message = (
            f"deliver reached for job {state.job_id} without both a final video and a "
            "thumbnail artifact id; route_after_assemble should have caught it"
        )
        raise GraphInvariantError(message)
    manifest = DeliveryManifest(
        entries=[
            ManifestEntry(kind="video", artifact_id=state.final_video_artifact_id),
            ManifestEntry(kind="thumbnail", artifact_id=state.thumbnail_artifact_id),
        ]
    )
    return {"manifest": manifest}


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


async def finalize_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """The only node allowed to write a job's terminal outcome. `graph.md` §2.

    Reached two ways: via a router's `guard()` divert (`state.outcome` already set), or via the
    direct `deliver -> finalize` edge on the success path, where nothing has called `decide()`
    yet — this node makes that call itself rather than leaving the success path unclassified.
    """
    outcome = state.outcome
    degraded = state.degraded
    terminal_reason_code = state.terminal_reason_code
    if outcome is None:
        decision = await deps.harness.decide(state, "finalize", now=deps.now())
        if decision.outcome is None:
            message = "finalize reached with no outcome decided and no divert set one"
            raise GraphInvariantError(message)
        outcome = decision.outcome
        degraded = decision.degraded
        terminal_reason_code = decision.reason_code.value if decision.reason_code else None
    async with tenant_session(deps.engine, state.tenant_id) as session:
        await JobRepository(session).finalize(
            state.job_id,
            outcome=PersistenceJobOutcome(outcome.value),
            degraded=degraded,
            degraded_reason=state.degraded_reason,
            terminal_reason_code=terminal_reason_code,
            budget_used={
                "usd_spent": str(state.budget.usd_spent),
                "tokens_used": state.budget.tokens_used,
                "iterations_used": state.budget.iterations_used,
            },
        )
    return {
        "outcome": outcome,
        "degraded": degraded,
        "terminal_reason_code": terminal_reason_code,
    }
