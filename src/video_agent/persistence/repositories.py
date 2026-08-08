"""One repository per aggregate. All of them take a `TenantSession` and none opens one.

Two rules shape every method here.

**`tenant_id` comes from the session, never from an argument.** There is no method on this
module that accepts a tenant id. `[D-68]` resolves it from the API key into
`Principal.tenant_id`, `tenant_session` sets the Postgres session variable from that, and the
repository reads it back off the session object. A caller cannot pass a foreign tenant id
because there is no parameter to pass it in — which is a stronger guarantee than validating
one, since validation is a check somebody can decide to skip for an admin path.

**Records are projections, not mirrors.** A frozen dataclass per aggregate carrying the
columns the application actually decides on. Deliberately *not* a field-for-field copy of the
table: a second full description of the schema is a second thing to keep in step, and the
schema's shape is already asserted against the live database by the drift test. What is
asserted here is that every column a record reads exists, which is what
`test_persistence_repositories` checks against `schema.py`.

The two methods worth reading closely are `JobRepository.create` and
`ShotAttemptRepository.claim`. Both are `INSERT ... ON CONFLICT DO NOTHING` followed by a read
of the row that won, and both exist because the constraint — not the application — is what
makes the operation happen once. `[D-16]` for the job, `[D-24]` and `[D-67]` for the attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError
from video_agent.persistence.enums import (
    ArtifactKind,
    AttemptState,
    BeatKind,
    JobOutcome,
    JobStatus,
)
from video_agent.persistence.schema import (
    artifact,
    beat,
    checkpoint,
    continuity_bible,
    job,
    shot,
    shot_attempt,
    story_plan,
)
from video_agent.persistence.session import TenantSession


class IdempotencyKeyReusedError(VideoAgentError):
    """The same idempotency key arrived with a different request body. `[D-16]`"""

    code = ErrorCode.VA_REQ_003


@dataclass(frozen=True, slots=True)
class JobRecord:
    """A job, as far as anything outside `persistence` needs to decide anything about it."""

    id: UUID
    tenant_id: UUID
    idempotency_key: str
    request_fingerprint: str
    status: JobStatus
    trace_id: str
    prompt: str
    music_bed: bool
    budget_caps: dict[str, Any]
    budget_epoch: int
    created: bool
    """False when this call adopted a job an earlier identical request had already created."""


@dataclass(frozen=True, slots=True)
class ShotAttemptRecord:
    """One provider render attempt.

    `provider_project_id` and `state` together are the crash-reconciliation record: a row in
    `in_flight` with a project id means a render was submitted and may have been billed, and
    the recovery path re-reads it rather than submitting another `[D-24]`, `[D-67]`.
    """

    id: UUID
    shot_id: UUID
    job_id: UUID
    attempt_no: int
    state: AttemptState
    request_fingerprint: str
    provider_project_id: str | None
    seed: int | None
    seed_supported: bool
    cost_usd: Decimal
    credits_charged: Decimal | None
    cost_is_final: bool


@dataclass(frozen=True, slots=True)
class AttemptClaim:
    """The outcome of trying to claim a provider request.

    `adopted` is the whole point of the type. A boolean returned alongside the row forces the
    caller to notice that it may not have created this attempt, and therefore that submitting
    a provider request now would be the second one for a fingerprint that has already been
    charged once.
    """

    attempt: ShotAttemptRecord
    adopted: bool


@dataclass(frozen=True, slots=True)
class ShotRecord:
    """One shot of a job, with the two counters the repair cap is enforced against."""

    id: UUID
    job_id: UUID
    idx: int
    status: str
    attempts_used: int
    repairs_used: int
    best_attempt_id: UUID | None
    best_score: Decimal | None


@dataclass(frozen=True, slots=True)
class StoryPlanRecord:
    """The plan for a job. One per job, enforced by a unique constraint on `job_id`."""

    id: UUID
    job_id: UUID
    logline: str
    total_duration_s: Decimal
    model_alias: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class ContinuityBibleRecord:
    """The bible for a job. Immutable for the life of the job, enforced by a trigger."""

    id: UUID
    job_id: UUID
    content_hash: str
    model_alias: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Metadata for one object in the store. Never the bytes."""

    id: UUID
    job_id: UUID
    kind: ArtifactKind
    shot_index: int | None
    storage_key: str
    content_type: str
    bytes: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """One node's checkpoint. `thread_id` is the job id."""

    id: int
    thread_id: UUID
    node: str
    seq: int
    state: dict[str, Any]
    budget_used: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NewJob:
    """Everything needed to create a job — except the tenant, which is never a parameter."""

    idempotency_key: str
    request_fingerprint: str
    prompt: str
    trace_id: str
    budget_caps: dict[str, Any]
    music_bed: bool = False


@dataclass(frozen=True, slots=True)
class NewStoryPlan:
    """A plan and its beats, written together or not at all."""

    job_id: UUID
    logline: str
    total_duration_s: Decimal
    model_alias: str
    prompt_version: str
    beats: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class NewContinuityBible:
    """The bible for a job. There is no matching update type, and that is deliberate."""

    job_id: UUID
    dimensions: dict[str, Any]
    negative_constraints: list[Any]
    content_hash: str
    model_alias: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class AttemptRequest:
    """The identity of one provider request, including the fingerprint that de-duplicates it.

    A value object rather than a parameter list because the same request is reconstructed
    identically on a redelivery `[D-67]` — if the fields drift between the first call and the
    replay, the fingerprint drifts with them and the anti-double-bill constraint never fires.
    """

    shot_id: UUID
    job_id: UUID
    attempt_no: int
    request_fingerprint: str
    prompt_text: str
    prompt_hash: str
    bible_hash: str
    conditioning_frame_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ProviderSubmission:
    """What the provider returned when the render was accepted.

    `seed` and `seed_supported` travel together so a NULL seed is never ambiguous `[D-59]`.
    """

    provider_project_id: str
    provider_key: str
    provider_model: str
    seed: int | None
    seed_supported: bool


@dataclass(frozen=True, slots=True)
class CostSettlement:
    """The final cost of an attempt. Applied once, at terminal state. `[D-60]`"""

    state: AttemptState
    cost_usd: Decimal
    credits_charged: Decimal | None


@dataclass(frozen=True, slots=True)
class NewArtifact:
    """Metadata for one uploaded object. Never the bytes."""

    job_id: UUID
    kind: ArtifactKind
    storage_key: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    shot_index: int | None = None


@dataclass(frozen=True, slots=True)
class NewCheckpoint:
    """One node's checkpoint, written in that node's transaction. `[D-23]`"""

    thread_id: UUID
    node: str
    seq: int
    state: dict[str, Any]
    budget_used: dict[str, Any]
    failure_signatures: dict[str, Any] | None = None


class _Repository:
    """Shared plumbing: the session, and the tenant every write is stamped with."""

    def __init__(self, session: TenantSession) -> None:
        self._session = session

    @property
    def tenant_id(self) -> UUID:
        """The tenant the session is scoped to. The only source of `tenant_id` for a write."""
        self._session.require_open()
        return self._session.tenant_id

    async def _fetch_one(self, statement: Select[Any]) -> RowMapping | None:
        result = await self._session.execute(statement)
        return result.mappings().first()


class JobRepository(_Repository):
    """Jobs, created exactly once per (tenant, idempotency key)."""

    async def create(self, new_job: NewJob) -> JobRecord:
        """Create the job, or adopt the one an identical earlier request already created.

        The unique constraint decides, not a prior `SELECT`. A read-then-write would leave a
        window in which two concurrent requests both see nothing and both insert, and
        `[CPS §Non-negotiables]` promises one job per key with no window in it.

        A reused key carrying a *different* body is `VA-REQ-003` and never a second job:
        returning the first job would answer a question the caller did not ask, and creating a
        second would bill them twice for a key they believed was protecting them.
        """
        statement = (
            insert(job)
            .values(
                id=uuid4(),
                tenant_id=self.tenant_id,
                idempotency_key=new_job.idempotency_key,
                request_fingerprint=new_job.request_fingerprint,
                prompt=new_job.prompt,
                music_bed=new_job.music_bed,
                trace_id=new_job.trace_id,
                budget_caps=new_job.budget_caps,
            )
            .on_conflict_do_nothing(constraint="job_idem_uq")
            .returning(*_JOB_COLUMNS)
        )
        result = await self._session.execute(statement)
        row = result.mappings().first()
        if row is not None:
            return _job_record(row, created=True)

        existing = await self._fetch_one(
            select(*_JOB_COLUMNS).where(job.c.idempotency_key == new_job.idempotency_key)
        )
        if existing is None:
            message = "the idempotency constraint fired but the winning row is not visible"
            raise VideoAgentError(message)
        if existing["request_fingerprint"] != new_job.request_fingerprint:
            message = (
                f"idempotency key {new_job.idempotency_key!r} was already used for a "
                f"different request body"
            )
            raise IdempotencyKeyReusedError(message)
        return _job_record(existing, created=False)

    async def get(self, job_id: UUID) -> JobRecord | None:
        """Read one job. Returns None for another tenant's job, because RLS filtered it."""
        row = await self._fetch_one(select(*_JOB_COLUMNS).where(job.c.id == job_id))
        return None if row is None else _job_record(row, created=False)

    async def finalize(
        self,
        job_id: UUID,
        *,
        outcome: JobOutcome,
        degraded: bool,
        degraded_reason: str | None,
        terminal_reason_code: str | None,
        budget_used: dict[str, Any],
    ) -> None:
        """Write the one terminal decision a job gets. Only the `finalize` node calls this.

        No `RETURNING`: the caller already holds the `Decision` it is recording here, and
        projecting the same values back out would be read by nothing.
        """
        statement = (
            update(job)
            .where(job.c.id == job_id, job.c.tenant_id == self.tenant_id)
            .values(
                status=JobStatus.TERMINAL.value,
                outcome=outcome.value,
                degraded=degraded,
                degraded_reason=degraded_reason,
                terminal_reason_code=terminal_reason_code,
                budget_used=budget_used,
            )
        )
        await self._session.execute(statement)


class StoryPlanRepository(_Repository):
    """The plan and its four beats, written together."""

    async def create(self, new_plan: NewStoryPlan) -> StoryPlanRecord:
        """Insert the plan and its beats in the caller's transaction.

        One transaction, so a plan with three beats never exists. The caller owns the
        transaction — `[D-23]` requires the node's checkpoint to commit with its domain
        writes, and a repository that committed on its own would break that.
        """
        plan_id = uuid4()
        statement = (
            insert(story_plan)
            .values(
                id=plan_id,
                job_id=new_plan.job_id,
                tenant_id=self.tenant_id,
                logline=new_plan.logline,
                total_duration_s=new_plan.total_duration_s,
                model_alias=new_plan.model_alias,
                prompt_version=new_plan.prompt_version,
            )
            .returning(*_PLAN_COLUMNS)
        )
        result = await self._session.execute(statement)
        row = result.mappings().one()
        await self._session.execute(
            insert(beat).values(
                [
                    {
                        "id": uuid4(),
                        "story_plan_id": plan_id,
                        "tenant_id": self.tenant_id,
                        "idx": index,
                        "kind": BeatKind(entry["kind"]),
                        "action": entry["action"],
                        "camera_move": entry["camera_move"],
                        "duration_s": entry["duration_s"],
                        "continuity_note": entry.get("continuity_note"),
                    }
                    for index, entry in enumerate(new_plan.beats)
                ]
            )
        )
        return _plan_record(row)

    async def get_for_job(self, job_id: UUID) -> StoryPlanRecord | None:
        """The plan for one job, or None."""
        row = await self._fetch_one(select(*_PLAN_COLUMNS).where(story_plan.c.job_id == job_id))
        return None if row is None else _plan_record(row)


class ContinuityBibleRepository(_Repository):
    """The bible. Written once; there is deliberately no update method."""

    async def create(self, new_bible: NewContinuityBible) -> ContinuityBibleRecord:
        """Insert the bible.

        There is no `update`. `[PRD §How it works 2]` makes it immutable for the life of the
        job and a database trigger enforces that, so a method here would exist only to raise —
        and a method that exists is a method somebody eventually makes work.
        """
        statement = (
            insert(continuity_bible)
            .values(
                id=uuid4(),
                job_id=new_bible.job_id,
                tenant_id=self.tenant_id,
                negative_constraints=new_bible.negative_constraints,
                content_hash=new_bible.content_hash,
                model_alias=new_bible.model_alias,
                prompt_version=new_bible.prompt_version,
                **new_bible.dimensions,
            )
            .returning(*_BIBLE_COLUMNS)
        )
        result = await self._session.execute(statement)
        return _bible_record(result.mappings().one())

    async def get_for_job(self, job_id: UUID) -> ContinuityBibleRecord | None:
        """The bible for one job, or None."""
        row = await self._fetch_one(
            select(*_BIBLE_COLUMNS).where(continuity_bible.c.job_id == job_id)
        )
        return None if row is None else _bible_record(row)


class ShotRepository(_Repository):
    """Shots and the two counters the repair cap is enforced against."""

    async def create(self, *, job_id: UUID, beat_id: UUID, idx: int) -> ShotRecord:
        """Insert one shot in `pending`."""
        statement = (
            insert(shot)
            .values(
                id=uuid4(),
                job_id=job_id,
                tenant_id=self.tenant_id,
                beat_id=beat_id,
                idx=idx,
            )
            .returning(*_SHOT_COLUMNS)
        )
        result = await self._session.execute(statement)
        return _shot_record(result.mappings().one())

    async def get(self, shot_id: UUID) -> ShotRecord | None:
        """Read one shot."""
        row = await self._fetch_one(select(*_SHOT_COLUMNS).where(shot.c.id == shot_id))
        return None if row is None else _shot_record(row)


class ShotAttemptRepository(_Repository):
    """Provider render attempts — the aggregate the double-billing guard lives on."""

    async def claim(self, request: AttemptRequest) -> AttemptClaim:
        """Record the intent to call the provider, or adopt the record of an earlier one.

        Called **before** the provider request, not after it. That ordering is the entire
        mechanism: `[D-67]` makes queue delivery at-least-once, so this method runs twice for
        one unit of work whenever a worker dies before its `XACK`. The second run conflicts on
        `request_fingerprint`, returns `adopted=True`, and the caller reconciles by re-reading
        the render named in `provider_project_id` instead of submitting a second paid one
        `[D-24]`.

        Writing the row after the provider call instead would leave the crash window exactly
        where the money is: request sent, nothing recorded, no way to find the render again.
        """
        statement = (
            insert(shot_attempt)
            .values(
                id=uuid4(),
                shot_id=request.shot_id,
                job_id=request.job_id,
                tenant_id=self.tenant_id,
                attempt_no=request.attempt_no,
                state=AttemptState.IN_FLIGHT,
                request_fingerprint=request.request_fingerprint,
                prompt_text=request.prompt_text,
                prompt_hash=request.prompt_hash,
                bible_hash=request.bible_hash,
                conditioning_frame_id=request.conditioning_frame_id,
            )
            .on_conflict_do_nothing(constraint="shot_attempt_fingerprint_uq")
            .returning(*_ATTEMPT_COLUMNS)
        )
        result = await self._session.execute(statement)
        row = result.mappings().first()
        if row is not None:
            return AttemptClaim(attempt=_attempt_record(row), adopted=False)

        existing = await self._fetch_one(
            select(*_ATTEMPT_COLUMNS).where(
                shot_attempt.c.request_fingerprint == request.request_fingerprint
            )
        )
        if existing is None:
            message = (
                "the request_fingerprint constraint fired but the winning attempt is not "
                "visible from this tenant's session"
            )
            raise VideoAgentError(message)
        return AttemptClaim(attempt=_attempt_record(existing), adopted=True)

    async def record_submission(
        self, attempt_id: UUID, submission: ProviderSubmission
    ) -> ShotAttemptRecord:
        """Record the provider's render id, as soon as it is known.

        Separate from `claim` because the id does not exist yet at claim time and separate
        from the completion write because a crash between submission and completion is the
        case reconciliation has to survive. `seed` may be NULL and `seed_supported` says so
        `[D-59]` — a fabricated seed would make the reproducibility record a lie.
        """
        statement = (
            shot_attempt.update()
            .where(shot_attempt.c.id == attempt_id)
            .values(
                provider_project_id=submission.provider_project_id,
                provider_key=submission.provider_key,
                provider_model=submission.provider_model,
                seed=submission.seed,
                seed_supported=submission.seed_supported,
            )
            .returning(*_ATTEMPT_COLUMNS)
        )
        result = await self._session.execute(statement)
        return _attempt_record(result.mappings().one())

    async def settle_cost(self, attempt_id: UUID, settlement: CostSettlement) -> ShotAttemptRecord:
        """Settle the provisional cost exactly once, and mark it final. `[D-60]`

        `cost_is_final` is only ever set here, and the sweeper's query for unsettled spend is
        `cost_is_final = false` on a terminal attempt. Setting the flag anywhere else would
        make an unreconciled charge invisible to the thing that exists to find it.
        """
        statement = (
            shot_attempt.update()
            .where(shot_attempt.c.id == attempt_id)
            .values(
                state=settlement.state,
                cost_usd=settlement.cost_usd,
                credits_charged=settlement.credits_charged,
                cost_is_final=True,
            )
            .returning(*_ATTEMPT_COLUMNS)
        )
        result = await self._session.execute(statement)
        return _attempt_record(result.mappings().one())

    async def get_by_fingerprint(self, request_fingerprint: str) -> ShotAttemptRecord | None:
        """Find the attempt that already owns a fingerprint, for the reconciliation path."""
        row = await self._fetch_one(
            select(*_ATTEMPT_COLUMNS).where(
                shot_attempt.c.request_fingerprint == request_fingerprint
            )
        )
        return None if row is None else _attempt_record(row)


class ArtifactRepository(_Repository):
    """Object-store metadata. No method here ever handles bytes."""

    async def record(self, new_artifact: NewArtifact) -> ArtifactRecord:
        """Record one uploaded object.

        `storage_key` is written by the caller and is required to be tenant-prefixed
        `[persistence.md §6]`; the uniqueness constraint on it means the same key can never
        describe two different objects, which is what makes the checksum assertion meaningful.
        """
        statement = (
            insert(artifact)
            .values(
                id=uuid4(),
                job_id=new_artifact.job_id,
                tenant_id=self.tenant_id,
                kind=new_artifact.kind,
                shot_index=new_artifact.shot_index,
                storage_key=new_artifact.storage_key,
                content_type=new_artifact.content_type,
                bytes=new_artifact.size_bytes,
                checksum_sha256=new_artifact.checksum_sha256,
            )
            .returning(*_ARTIFACT_COLUMNS)
        )
        result = await self._session.execute(statement)
        return _artifact_record(result.mappings().one())

    async def list_for_job(self, job_id: UUID) -> list[ArtifactRecord]:
        """Every artifact recorded for one job."""
        result = await self._session.execute(
            select(*_ARTIFACT_COLUMNS)
            .where(artifact.c.job_id == job_id)
            .order_by(artifact.c.kind, artifact.c.shot_index)
        )
        return [_artifact_record(row) for row in result.mappings()]


class CheckpointRepository(_Repository):
    """LangGraph checkpoints. Written after every node, in that node's transaction `[D-23]`."""

    async def write(self, new_checkpoint: NewCheckpoint) -> CheckpointRecord:
        """Append one checkpoint. `(thread_id, seq)` is unique, so a replay cannot fork it."""
        statement = (
            insert(checkpoint)
            .values(
                thread_id=new_checkpoint.thread_id,
                tenant_id=self.tenant_id,
                node=new_checkpoint.node,
                seq=new_checkpoint.seq,
                state=new_checkpoint.state,
                budget_used=new_checkpoint.budget_used,
                failure_signatures=new_checkpoint.failure_signatures or {},
            )
            .returning(*_CHECKPOINT_COLUMNS)
        )
        result = await self._session.execute(statement)
        return _checkpoint_record(result.mappings().one())

    async def latest(self, thread_id: UUID) -> CheckpointRecord | None:
        """The highest-`seq` checkpoint for a thread, which is where a resume starts."""
        row = await self._fetch_one(
            select(*_CHECKPOINT_COLUMNS)
            .where(checkpoint.c.thread_id == thread_id)
            .order_by(checkpoint.c.seq.desc())
            .limit(1)
        )
        return None if row is None else _checkpoint_record(row)


# --- Column projections and row adapters ---------------------------------------------------

_JOB_COLUMNS = (
    job.c.id,
    job.c.tenant_id,
    job.c.idempotency_key,
    job.c.request_fingerprint,
    job.c.status,
    job.c.trace_id,
    job.c.prompt,
    job.c.music_bed,
    job.c.budget_caps,
    job.c.budget_epoch,
)
_PLAN_COLUMNS = (
    story_plan.c.id,
    story_plan.c.job_id,
    story_plan.c.logline,
    story_plan.c.total_duration_s,
    story_plan.c.model_alias,
    story_plan.c.prompt_version,
)
_BIBLE_COLUMNS = (
    continuity_bible.c.id,
    continuity_bible.c.job_id,
    continuity_bible.c.content_hash,
    continuity_bible.c.model_alias,
    continuity_bible.c.prompt_version,
)
_SHOT_COLUMNS = (
    shot.c.id,
    shot.c.job_id,
    shot.c.idx,
    shot.c.status,
    shot.c.attempts_used,
    shot.c.repairs_used,
    shot.c.best_attempt_id,
    shot.c.best_score,
)
_ATTEMPT_COLUMNS = (
    shot_attempt.c.id,
    shot_attempt.c.shot_id,
    shot_attempt.c.job_id,
    shot_attempt.c.attempt_no,
    shot_attempt.c.state,
    shot_attempt.c.request_fingerprint,
    shot_attempt.c.provider_project_id,
    shot_attempt.c.seed,
    shot_attempt.c.seed_supported,
    shot_attempt.c.cost_usd,
    shot_attempt.c.credits_charged,
    shot_attempt.c.cost_is_final,
)
_ARTIFACT_COLUMNS = (
    artifact.c.id,
    artifact.c.job_id,
    artifact.c.kind,
    artifact.c.shot_index,
    artifact.c.storage_key,
    artifact.c.content_type,
    artifact.c.bytes,
    artifact.c.checksum_sha256,
)
_CHECKPOINT_COLUMNS = (
    checkpoint.c.id,
    checkpoint.c.thread_id,
    checkpoint.c.node,
    checkpoint.c.seq,
    checkpoint.c.state,
    checkpoint.c.budget_used,
)


def _job_record(row: RowMapping, *, created: bool) -> JobRecord:
    return JobRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        idempotency_key=row["idempotency_key"],
        request_fingerprint=row["request_fingerprint"],
        status=JobStatus(row["status"]),
        trace_id=row["trace_id"],
        prompt=row["prompt"],
        music_bed=row["music_bed"],
        budget_caps=row["budget_caps"],
        budget_epoch=row["budget_epoch"],
        created=created,
    )


def _plan_record(row: RowMapping) -> StoryPlanRecord:
    return StoryPlanRecord(
        id=row["id"],
        job_id=row["job_id"],
        logline=row["logline"],
        total_duration_s=row["total_duration_s"],
        model_alias=row["model_alias"],
        prompt_version=row["prompt_version"],
    )


def _bible_record(row: RowMapping) -> ContinuityBibleRecord:
    return ContinuityBibleRecord(
        id=row["id"],
        job_id=row["job_id"],
        content_hash=row["content_hash"],
        model_alias=row["model_alias"],
        prompt_version=row["prompt_version"],
    )


def _shot_record(row: RowMapping) -> ShotRecord:
    return ShotRecord(
        id=row["id"],
        job_id=row["job_id"],
        idx=row["idx"],
        status=row["status"],
        attempts_used=row["attempts_used"],
        repairs_used=row["repairs_used"],
        best_attempt_id=row["best_attempt_id"],
        best_score=row["best_score"],
    )


def _attempt_record(row: RowMapping) -> ShotAttemptRecord:
    return ShotAttemptRecord(
        id=row["id"],
        shot_id=row["shot_id"],
        job_id=row["job_id"],
        attempt_no=row["attempt_no"],
        state=AttemptState(row["state"]),
        request_fingerprint=row["request_fingerprint"],
        provider_project_id=row["provider_project_id"],
        seed=row["seed"],
        seed_supported=row["seed_supported"],
        cost_usd=row["cost_usd"],
        credits_charged=row["credits_charged"],
        cost_is_final=row["cost_is_final"],
    )


def _artifact_record(row: RowMapping) -> ArtifactRecord:
    return ArtifactRecord(
        id=row["id"],
        job_id=row["job_id"],
        kind=ArtifactKind(row["kind"]),
        shot_index=row["shot_index"],
        storage_key=row["storage_key"],
        content_type=row["content_type"],
        bytes=row["bytes"],
        checksum_sha256=row["checksum_sha256"],
    )


def _checkpoint_record(row: RowMapping) -> CheckpointRecord:
    return CheckpointRecord(
        id=row["id"],
        thread_id=row["thread_id"],
        node=row["node"],
        seq=row["seq"],
        state=row["state"],
        budget_used=row["budget_used"],
    )


RECORD_COLUMN_PROJECTIONS: dict[str, tuple[str, ...]] = {
    "job": tuple(column.name for column in _JOB_COLUMNS),
    "story_plan": tuple(column.name for column in _PLAN_COLUMNS),
    "continuity_bible": tuple(column.name for column in _BIBLE_COLUMNS),
    "shot": tuple(column.name for column in _SHOT_COLUMNS),
    "shot_attempt": tuple(column.name for column in _ATTEMPT_COLUMNS),
    "artifact": tuple(column.name for column in _ARTIFACT_COLUMNS),
    "checkpoint": tuple(column.name for column in _CHECKPOINT_COLUMNS),
}
"""Table name to the columns its record projects, so a test can check they all still exist."""
