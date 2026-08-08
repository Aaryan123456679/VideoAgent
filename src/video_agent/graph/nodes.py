"""The nine node bodies and their routers. `graph.md` §3.

Every router calls `guard()` first (`graph.md` §3.1) — a reflective CI test in
`test_graph_build.py` asserts this over every function in `_ROUTERS`. Nodes never call `guard`
themselves; a node's job is to do one unit of work and report what changed, and the harness
veto is applied once, uniformly, by the router that follows it.

**`generate_shot`, `extract_final_frame`, `assemble`, `deliver` are stubs.** Their real bodies
are T2.3/T2.4, tracked separately — wiring them up is what makes a job produce an actual video.
The topology compiles and the planning/selection/finalization path is real without them.

**`qc_shot` is a stub of a different kind.** `graph.md`'s v1 status header is explicit that
QC scoring and repair are deferred to E3/`S3.2.2`; this node unconditionally accepts every shot
rather than scoring it. The marker below is what `S3.2.2`'s test asserts has been removed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from video_agent.graph.deps import GraphDeps
from video_agent.graph.guard import guard
from video_agent.graph.state import GraphInvariantError, JobState, ShotState
from video_agent.harness.context import NodeContext
from video_agent.persistence.enums import BeatKind as PersistenceBeatKind
from video_agent.persistence.enums import JobOutcome as PersistenceJobOutcome
from video_agent.persistence.enums import ShotStatus
from video_agent.persistence.repositories import (
    ContinuityBibleRepository,
    JobRepository,
    NewContinuityBible,
    NewStoryPlan,
    StoryPlanRepository,
)
from video_agent.persistence.session import tenant_session
from video_agent.planning.service import lock_bible as lock_bible_domain
from video_agent.planning.service import plan_story as plan_story_domain

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
# generate_shot / extract_final_frame — T2.3, stubbed here
# ---------------------------------------------------------------------------


async def generate_shot_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """T2.3: submit the current shot to a provider via the three-phase write sequence
    (`graph.md` §4, `[D-23]`/`[D-24]`/`[D-58]`) and chain from `last_good_frame_artifact_id`.
    """
    del state, deps
    message = "generate_shot_node is a T2.3 stub; the provider adapter is not wired in yet"
    raise NotImplementedError(message)


async def route_after_generate(state: JobState, deps: GraphDeps) -> str:
    diverted = await guard(state, "generate_shot", harness=deps.harness, now=deps.now())
    return diverted if diverted is not None else "extract_final_frame"


async def extract_final_frame_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """T2.3: last decodable frame, lossless PNG, uniform-frame rejection (`assembly.md`)."""
    del state, deps
    message = "extract_final_frame_node is a T2.3 stub; frame extraction is not wired in yet"
    raise NotImplementedError(message)


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
# assemble / deliver — T2.4, stubbed here
# ---------------------------------------------------------------------------


async def assemble_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """T2.4: normalize, concatenate by stream copy, thumbnail from the best accepted shot."""
    del state, deps
    message = "assemble_node is a T2.4 stub; concatenation/thumbnailing is not wired in yet"
    raise NotImplementedError(message)


async def route_after_assemble(state: JobState, deps: GraphDeps) -> str:
    diverted = await guard(state, "assemble", harness=deps.harness, now=deps.now())
    return diverted if diverted is not None else "deliver"


async def deliver_node(state: JobState, deps: GraphDeps) -> dict[str, Any]:
    """T2.4: build the delivery manifest (`assembly.models.DeliveryManifest`). No router follows
    this node — `graph.md` §3 wires `deliver -> finalize` as a direct edge.
    """
    del state, deps
    message = "deliver_node is a T2.4 stub; manifest construction is not wired in yet"
    raise NotImplementedError(message)


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
