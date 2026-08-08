"""`JobState`/`ShotState` — the one object every node reads and writes. `graph.md` §2.

Checkpointed verbatim after every node (`[CPS §Non-negotiables]`), so nothing here may be
media bytes, a presigned URL, or a credential — only ids, counters and already-verified domain
objects. `assert_invariants` is the enforcement point: it runs at every checkpoint write, not
just in tests, because an invariant that is only checked by a test is a convention, and this
session already found one convention (`decide.py`'s `UUID` import) that silently broke at
runtime.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from video_agent.assembly.models import DeliveryManifest
from video_agent.harness.budget import BudgetLedger
from video_agent.harness.errors import BibleHashMismatchError, HarnessError
from video_agent.harness.outcomes import Outcome
from video_agent.observability.codes import ErrorCode
from video_agent.persistence.enums import BeatKind, ShotStatus
from video_agent.planning.bible import compute_content_hash
from video_agent.planning.models import ContinuityBible, StoryPlan
from video_agent.qc.models import QCFinding

__all__ = [
    "MAX_REPAIRS",
    "SHOT_COUNT",
    "GraphInvariantError",
    "JobState",
    "ShotCountInvariantError",
    "ShotState",
    "assert_invariants",
]

SHOT_COUNT = 4
"""Four ten-second shots. `[PRD §header]`. Not configurable — it is a product fact."""

MAX_REPAIRS = 2
"""The repair cap `[D-01]`. `attempts_used` may therefore reach `MAX_REPAIRS + 1`."""


class ShotState(BaseModel):
    """One of the job's four shots. `graph.md` §2."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, le=SHOT_COUNT - 1)
    beat_kind: BeatKind
    status: ShotStatus = ShotStatus.PENDING
    attempts_used: int = Field(default=0, ge=0)
    repairs_used: int = Field(default=0, ge=0, le=MAX_REPAIRS)
    best_attempt_id: UUID | None = None
    best_score: float | None = Field(default=None, ge=0.0, le=1.0)
    last_findings: tuple[QCFinding, ...] = ()
    clip_artifact_id: UUID | None = None
    final_frame_artifact_id: UUID | None = None


class JobState(BaseModel):
    """Everything the compiled graph knows about one job. `graph.md` §2.

    `budget` is the one field the graph both reads and writes on every node (via the harness,
    never directly, `harness.md` §1) — everything else under "control" is write-once-per-
    termination and read by routers only.
    """

    model_config = ConfigDict(extra="forbid")

    # identity
    job_id: UUID
    tenant_id: UUID
    trace_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    """Untrusted user content; never treated as an instruction outside the gateway boundary."""
    music_bed: bool = False

    # planning products
    story_plan: StoryPlan | None = None
    bible: ContinuityBible | None = None
    bible_hash: str | None = None
    """Verified against `bible.content_hash` on every checkpoint write, not just on load."""

    # sequential loop
    shot_index: int = Field(default=0, ge=0, le=SHOT_COUNT - 1)
    shots: tuple[ShotState, ...] = ()
    last_good_frame_artifact_id: UUID | None = None
    """Chaining source `[D-05]`. Advances only on acceptance — never on an abandoned shot."""

    # delivery
    final_video_artifact_id: UUID | None = None
    thumbnail_artifact_id: UUID | None = None
    manifest: DeliveryManifest | None = None

    # control — written by the harness via `_guard`, read by routers
    budget: BudgetLedger
    outcome: Outcome | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    terminal_reason_code: str | None = None


class GraphInvariantError(HarnessError):
    """A checkpoint-time invariant (`graph.md` §2) did not hold.

    Raised rather than tolerated: every invariant here guards against spending money on a shot
    that should not exist (wrong count) or regenerating one that already cost twice its cap
    (repair count) — `graph.md` §8 classifies both as programming errors, not runtime
    conditions, and a programming error that is silently repaired is a programming error that
    recurs.
    """

    code: ErrorCode = ErrorCode.VA_INT_001


class ShotCountInvariantError(GraphInvariantError):
    """`len(shots) != SHOT_COUNT` once a story plan exists. `graph.md` §8: `VA-PLAN-003`."""

    code: ErrorCode = ErrorCode.VA_PLAN_003


def assert_invariants(
    state: JobState, *, node: str, previous: JobState | None = None
) -> None:
    """The six checkpoint-time invariants of `graph.md` §2, in the order the table lists them.

    `previous` is only available from the second checkpoint onward; the monotonic-budget check
    is skipped on the first one because there is nothing yet to compare against.
    """
    if state.story_plan is not None and len(state.shots) != SHOT_COUNT:
        message = (
            f"job {state.job_id} has a story plan but {len(state.shots)} shots, "
            f"not exactly {SHOT_COUNT}"
        )
        raise ShotCountInvariantError(message)

    for shot in state.shots:
        if shot.repairs_used > MAX_REPAIRS:
            message = (
                f"shot {shot.index} used {shot.repairs_used} repairs, above cap {MAX_REPAIRS}"
            )
            raise GraphInvariantError(message)
        if shot.attempts_used > 0 and shot.attempts_used != shot.repairs_used + 1:
            message = (
                f"shot {shot.index} has {shot.attempts_used} attempts but "
                f"{shot.repairs_used} repairs; the back-edge is the only way to add an attempt"
            )
            raise GraphInvariantError(message)

    if state.bible is not None and state.bible_hash is not None:
        expected = compute_content_hash(state.bible)
        if expected != state.bible_hash or expected != state.bible.content_hash:
            message = f"job {state.job_id}'s bible no longer hashes to its recorded digest"
            raise BibleHashMismatchError(message)

    if state.outcome is not None and node != "finalize":
        message = f"node {node!r} set an outcome; only 'finalize' may terminate a job"
        raise GraphInvariantError(message)

    if previous is not None and state.budget.usd_spent < previous.budget.usd_spent:
        message = (
            f"job {state.job_id}'s spend decreased between checkpoints "
            f"({previous.budget.usd_spent} -> {state.budget.usd_spent})"
        )
        raise GraphInvariantError(message)
