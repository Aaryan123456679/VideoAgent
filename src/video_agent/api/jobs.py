"""`POST /v1/jobs` and its siblings — `api.md` §2.1, the part of `T1.3` this route module ships.

Job creation, status, listing, cancellation and a progress stream. Everything here does **no**
provider or model work inline `[api.md §1]`: `create_job` writes one row and publishes one
queue message, and the graph itself is run later by `graph.worker`, in a different process.

**What is deliberately not here.** `GET /v1/jobs/{id}/artifacts`, `POST .../resume` and
`POST .../shots/{i}/regenerate` are designed in `api.md` but shipped in a later milestone
(`E3`); this module does not stub them. The SSE stream below is a simple poll over
`CheckpointRepository.latest`, not the richer Redis `progress:{job_id}` event channel `api.md`
§5 describes — a working simple version now, not an elaborate one half-built.

**The cross-process cancel contract.** `POST /v1/jobs/{id}/cancel` cannot reach the
`JobHarness` that is actually running the job — that object lives inside a worker process this
route never talks to. The handoff is the Redis key `persistence.keys.cancel_signal_key`, and
its exact contract (who writes it, who reads it, the JSON shape, the TTL) is documented once,
on that constructor, rather than repeated here.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from video_agent.api.database import get_database, tenant_session
from video_agent.api.errors import ApiError
from video_agent.api.idempotency import (
    REPLAYED_HEADER,
    IdempotencyStore,
    Replay,
    begin_idempotent,
    finish_idempotent,
    require_idempotency_key,
)
from video_agent.api.principal import Principal, assert_tenant_owns, require_tenant
from video_agent.harness.budget import BudgetCaps
from video_agent.harness.cancel import CancelActor, CancelRequest
from video_agent.observability.codes import ErrorCode
from video_agent.observability.context import current_trace_id, new_trace_id
from video_agent.persistence.enums import JobOutcome, JobStatus
from video_agent.persistence.keys import cancel_signal_key
from video_agent.persistence.queue import JobMessage, JobQueue
from video_agent.persistence.repositories import CheckpointRepository, JobRepository, NewJob
from video_agent.persistence.session import TenantSession

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

    from video_agent.api.resources import DatabaseResource, Resources
    from video_agent.config.settings import Settings
    from video_agent.persistence.redis_client import RedisStore
    from video_agent.persistence.repositories import CheckpointRecord, JobRecord

__all__ = ["router"]

router = APIRouter(tags=["jobs"])

ENTRY_NODE = "plan_story"
"""The graph's entry point `graph/build.py` sets with `set_entry_point`. Named here as a plain
constant, not imported from `graph.build`: the API depends on nothing that would pull in
langgraph or a node body just to report "no checkpoint yet, so still on the first node"."""

CREATE_STATUS = 202
CANCEL_ACCEPTED_STATUS = 202
CANCEL_NOOP_STATUS = 200
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
STREAM_POLL_INTERVAL_S = 1.5

_JSON_MEDIA_TYPE = "application/json"

RESUMABLE_OUTCOMES: frozenset[JobOutcome] = frozenset(
    {JobOutcome.PARTIAL, JobOutcome.FAILED_NO_PROGRESS, JobOutcome.FAILED}
)
"""`api.md` §2.2: resumable iff the outcome is one of these. This route does not additionally
check per-shot state (`resume` itself is deferred to `E3`), so the flag is a job-level signal
only — documented here rather than silently narrowed."""


# --- request/response models -----------------------------------------------------------------


class CreateJobRequest(BaseModel):
    """`api.md` §2.2. `extra="forbid"` is what makes a removed field like `webhook_url`
    `[D-74]` a loud `422` instead of a silently ignored one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(min_length=8, max_length=2_000)
    music_bed_artifact_id: UUID | None = None
    metadata: dict[str, str] = Field(default_factory=dict, max_length=20)


class JobAccepted(BaseModel):
    """`api.md` §2.2: the body of every `202` this module returns for a create or a cancel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    status: Literal["queued"] = "queued"
    trace_id: str
    created_at: datetime


class BudgetView(BaseModel):
    """`api.md` §2.2: used and cap, on all four axes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    iterations_used: int
    iterations_cap: int
    wall_clock_s: float
    wall_clock_cap_s: float
    tokens_used: int
    tokens_cap: int
    usd_spent: Decimal
    usd_cap: Decimal


class JobView(BaseModel):
    """`api.md` §2.2, minus the per-shot `shots` list: this route reads only `JobRepository`
    and `CheckpointRepository`, never `ShotRepository`, so no per-shot state is available to
    report yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    status: Literal["queued", "running", "terminal"]
    outcome: Literal["SUCCESS", "PARTIAL", "FAILED_NO_PROGRESS", "FAILED", "ESCALATED"] | None
    degraded: bool
    degraded_reason: str | None
    current_node: str
    budget: BudgetView
    trace_id: str
    resumable: bool
    created_at: datetime
    updated_at: datetime


class JobSummaryView(BaseModel):
    """One row of `GET /v1/jobs` — enough to decide whether to fetch the full `JobView`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    status: Literal["queued", "running", "terminal"]
    outcome: Literal["SUCCESS", "PARTIAL", "FAILED_NO_PROGRESS", "FAILED", "ESCALATED"] | None
    created_at: datetime
    updated_at: datetime


class JobListView(BaseModel):
    """`api.md` §2.1: cursor-paginated, tenant-scoped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: list[JobSummaryView]
    next_cursor: str | None


class CancelResponseView(BaseModel):
    """`harness.cancel.CancelResult`, rendered for the wire. `accepted=False` with an
    `outcome` is the already-terminal no-op case, not an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    outcome: Literal["SUCCESS", "PARTIAL", "FAILED_NO_PROGRESS", "FAILED", "ESCALATED"] | None = (
        None
    )


# --- reaching the cache from a route ------------------------------------------------------


class _JobsCache(Protocol):
    """The slice of `api.clients.Cache` this router needs.

    `Resources.cache` is typed as the narrow `ProbedResource` (`ping`/`aclose` only) so
    `/readyz` stays decoupled from what a cache client can do beyond answering a probe.
    Production always wires the concrete `api.clients.Cache`, which satisfies this protocol
    structurally; the one cast in `_cache_resource` documents that assumption in one place
    instead of scattering it through every handler.
    """

    def idempotency_store(self) -> IdempotencyStore:
        """The store `begin_idempotent`/`finish_idempotent` claim keys against."""
        ...  # pragma: no cover - protocol declaration

    @property
    def queue(self) -> JobQueue:
        """The job queue a created job's first step is published to."""
        ...  # pragma: no cover - protocol declaration

    @property
    def store(self) -> RedisStore:
        """The typed key/value store the cancel signal is written through."""
        ...  # pragma: no cover - protocol declaration


def _cache_resource(request: Request) -> _JobsCache:
    resources: Resources = request.app.state.resources
    return cast("_JobsCache", resources.cache)


def _database_resource(request: Request) -> DatabaseResource:
    return get_database(request)


# --- idempotent replay -----------------------------------------------------------------------


def _replay_response(outcome: Replay) -> Response:
    """Return the stored body byte-for-byte — never re-serialised, never re-parsed."""
    return Response(
        content=outcome.body,
        status_code=outcome.status_code,
        media_type=_JSON_MEDIA_TYPE,
        headers={REPLAYED_HEADER: "true"},
    )


# --- POST /v1/jobs -----------------------------------------------------------------------


@router.post("/v1/jobs", status_code=CREATE_STATUS, response_model=JobAccepted)
async def create_job(
    request: Request,
    body: CreateJobRequest,
    principal: Annotated[Principal, Depends(require_tenant)],
    session: Annotated[TenantSession, Depends(tenant_session)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> Response:
    """`api.md` §3, in full: claim the key, create-or-adopt the job, enqueue once, finish.

    O(1) work on the happy path: one idempotency claim, one `JobRepository.create` (which is
    itself one `INSERT ... ON CONFLICT DO NOTHING`), and — only when this call actually created
    the row — one queue publish. No provider or model call is reachable from here.
    """
    raw_body = await request.body()
    cache = _cache_resource(request)
    store = cache.idempotency_store()
    outcome = await begin_idempotent(
        store,
        tenant_id=principal.tenant_id,
        route=request.url.path,
        key=idempotency_key,
        body=raw_body,
    )
    if isinstance(outcome, Replay):
        return _replay_response(outcome)

    settings: Settings = request.app.state.settings
    # No per-tenant override lookup exists yet (no `TenantRepository`); inheriting the global
    # cap is the honest v1 behaviour rather than an invented lookup.
    caps = BudgetCaps.from_settings(settings, tenant_max_usd_per_job=None)
    trace_id = current_trace_id() or new_trace_id()

    record = await JobRepository(session).create(
        NewJob(
            idempotency_key=idempotency_key,
            request_fingerprint=outcome.fingerprint,
            prompt=body.prompt,
            trace_id=trace_id,
            budget_caps=caps.model_dump(mode="json"),
            # v1 has no artifact-backed music library; the boolean records only that the
            # caller supplied one, not which. [D-69]
            music_bed=body.music_bed_artifact_id is not None,
        )
    )
    if record.created:
        # Only when this call actually created the row: an adopted row was already published
        # by whichever request created it, and a second publish would double-run the graph.
        await cache.queue.publish(
            JobMessage(tenant_id=principal.tenant_id, job_id=record.id, node=ENTRY_NODE)
        )

    accepted = JobAccepted(
        job_id=record.id, status="queued", trace_id=record.trace_id, created_at=record.created_at
    )
    response_body = accepted.model_dump_json()
    await finish_idempotent(
        store,
        outcome,
        status_code=CREATE_STATUS,
        body=response_body,
        job_id=record.id,
    )
    return Response(content=response_body, status_code=CREATE_STATUS, media_type=_JSON_MEDIA_TYPE)


# --- GET /v1/jobs/{job_id} -----------------------------------------------------------------


def _budget_view(record: JobRecord, *, now: datetime) -> BudgetView:
    """Cap/used for all four axes.

    `wall_clock_s` is approximated as elapsed time since the row was created (or, once
    terminal, since it was last updated) rather than read from a persisted ledger: v1's
    `finalize_node` writes `usd_spent`/`tokens_used`/`iterations_used` into `job.budget_used`
    but not a wall-clock figure, and node bodies do not all checkpoint yet. The approximation
    is honest about being one — it is documented here, not silently presented as exact.
    """
    caps = record.budget_caps
    used = record.budget_used or {}
    reference = record.updated_at if record.status is JobStatus.TERMINAL else now
    elapsed = max((reference - record.created_at).total_seconds(), 0.0)
    return BudgetView(
        iterations_used=int(used.get("iterations_used", 0)),
        iterations_cap=int(caps.get("max_iterations", 0)),
        wall_clock_s=elapsed,
        wall_clock_cap_s=float(caps.get("max_wall_clock_s", 0.0)),
        tokens_used=int(used.get("tokens_used", 0)),
        tokens_cap=int(caps.get("max_tokens", 0)),
        usd_spent=Decimal(str(used.get("usd_spent", "0"))),
        usd_cap=Decimal(str(caps.get("max_usd", "0"))),
    )


def _current_node(checkpoint: CheckpointRecord | None) -> str:
    """The node named by the latest checkpoint, or the graph's entry node when there is none
    yet — `AGENT.md`'s instruction to treat a missing checkpoint as "still on its first node"."""
    return checkpoint.node if checkpoint is not None else ENTRY_NODE


def _to_job_view(
    record: JobRecord, checkpoint: CheckpointRecord | None, *, now: datetime
) -> JobView:
    return JobView(
        job_id=record.id,
        status=record.status.value,
        outcome=record.outcome.value if record.outcome is not None else None,
        degraded=record.degraded,
        degraded_reason=record.degraded_reason,
        current_node=_current_node(checkpoint),
        budget=_budget_view(record, now=now),
        trace_id=record.trace_id,
        resumable=record.outcome in RESUMABLE_OUTCOMES,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.get("/v1/jobs/{job_id}", response_model=JobView)
async def get_job(
    job_id: UUID,
    principal: Annotated[Principal, Depends(require_tenant)],
    session: Annotated[TenantSession, Depends(tenant_session)],
) -> JobView:
    """`api.md` §2.1/§2.2: status, outcome, budget and resumability. A terminal job is a `200`,
    never an HTTP error `[api.md §4]` — the outcome describes the job, not the API call."""
    job = await JobRepository(session).get(job_id)
    if job is None:
        raise ApiError(ErrorCode.VA_REQ_005, job_id=job_id)
    assert_tenant_owns(principal, job.tenant_id, job_id=job_id)
    checkpoint = await CheckpointRepository(session).latest(job_id)
    return _to_job_view(job, checkpoint, now=datetime.now(UTC))


# --- GET /v1/jobs -------------------------------------------------------------------------


def _encode_cursor(created_at: datetime, job_id: UUID) -> str:
    payload = f"{created_at.isoformat()}|{job_id}"
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at_raw, _, job_id_raw = raw.partition("|")
        return datetime.fromisoformat(created_at_raw), UUID(job_id_raw)
    except (ValueError, UnicodeDecodeError, InvalidOperation) as exc:
        raise ApiError(
            ErrorCode.VA_REQ_007, log_detail=f"malformed pagination cursor: {exc}"
        ) from exc


@router.get("/v1/jobs", response_model=JobListView)
async def list_jobs(
    session: Annotated[TenantSession, Depends(tenant_session)],
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> JobListView:
    """`api.md` §2.1: cursor-paginated, tenant-scoped by RLS rather than a `WHERE` clause.

    `tenant_session` already depends on `require_tenant`, so the tenant boundary is enforced
    even though no handler parameter names the `Principal` directly.
    """
    page_size = max(1, min(limit, MAX_PAGE_SIZE))
    before = _decode_cursor(cursor) if cursor else None
    records = await JobRepository(session).list_page(limit=page_size, before=before)
    next_cursor = (
        _encode_cursor(records[-1].created_at, records[-1].id)
        if len(records) == page_size
        else None
    )
    return JobListView(
        jobs=[
            JobSummaryView(
                job_id=record.id,
                status=record.status.value,
                outcome=record.outcome.value if record.outcome is not None else None,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            for record in records
        ],
        next_cursor=next_cursor,
    )


# --- POST /v1/jobs/{job_id}/cancel ---------------------------------------------------------


@router.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(
    request: Request,
    job_id: UUID,
    principal: Annotated[Principal, Depends(require_tenant)],
    session: Annotated[TenantSession, Depends(tenant_session)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> Response:
    """`api.md` §2.1/§8: cooperative cancel. Cancelling a terminal job is a no-op that returns
    the existing outcome, never an error — see `harness.cancel`'s module docstring for why."""
    raw_body = await request.body()
    cache = _cache_resource(request)
    store = cache.idempotency_store()
    outcome = await begin_idempotent(
        store,
        tenant_id=principal.tenant_id,
        route=request.url.path,
        key=idempotency_key,
        body=raw_body,
    )
    if isinstance(outcome, Replay):
        return _replay_response(outcome)
    first = outcome

    job = await JobRepository(session).get(job_id)
    if job is None:
        raise ApiError(ErrorCode.VA_REQ_005, job_id=job_id)
    assert_tenant_owns(principal, job.tenant_id, job_id=job_id)

    if job.status is JobStatus.TERMINAL:
        result = CancelResponseView(
            accepted=False,
            outcome=job.outcome.value if job.outcome is not None else None,
        )
        response_body = result.model_dump_json()
        await finish_idempotent(
            store, first, status_code=CANCEL_NOOP_STATUS, body=response_body, job_id=job.id
        )
        return Response(
            content=response_body, status_code=CANCEL_NOOP_STATUS, media_type=_JSON_MEDIA_TYPE
        )

    signal = CancelRequest(actor=CancelActor.CLIENT, requested_at=datetime.now(UTC))
    await cache.store.set(cancel_signal_key(job_id), signal.model_dump_json())

    result = CancelResponseView(accepted=True, outcome=None)
    response_body = result.model_dump_json()
    await finish_idempotent(
        store, first, status_code=CANCEL_ACCEPTED_STATUS, body=response_body, job_id=job.id
    )
    return Response(
        content=response_body, status_code=CANCEL_ACCEPTED_STATUS, media_type=_JSON_MEDIA_TYPE
    )


# --- GET /v1/jobs/{job_id}/stream ----------------------------------------------------------


def _stream_event(job_id: UUID, current: JobRecord, checkpoint: CheckpointRecord | None) -> bytes:
    event_name = "terminal" if current.status is JobStatus.TERMINAL else "progress"
    payload = {
        "job_id": str(job_id),
        "status": current.status.value,
        "node": _current_node(checkpoint),
        "seq": checkpoint.seq if checkpoint is not None else None,
        "outcome": current.outcome.value if current.outcome is not None else None,
    }
    return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n".encode()


@router.get("/v1/jobs/{job_id}/stream")
async def stream_job(
    request: Request,
    job_id: UUID,
    principal: Annotated[Principal, Depends(require_tenant)],
) -> StreamingResponse:
    """A simple poll over `CheckpointRepository.latest`, not the richer `progress:{job_id}`
    channel `api.md` §5 designs — a working simple version, per this task's own instruction not
    to over-build the stream.

    Uses its own short-lived tenant scope per poll rather than one `TenantSession` held open
    for the stream's whole lifetime, which for a multi-minute video would otherwise pin a
    Postgres connection and a transaction for as long as the client stays connected.
    """
    database = _database_resource(request)
    async with database.tenant_scope(principal.tenant_id) as session:
        job = await JobRepository(session).get(job_id)
        if job is None:
            raise ApiError(ErrorCode.VA_REQ_005, job_id=job_id)
        assert_tenant_owns(principal, job.tenant_id, job_id=job_id)

    async def events() -> AsyncIterator[bytes]:
        last_seq: int | None = None
        while True:
            if await request.is_disconnected():
                return
            async with database.tenant_scope(principal.tenant_id) as poll_session:
                current = await JobRepository(poll_session).get(job_id)
                if current is None:
                    return
                checkpoint = await CheckpointRepository(poll_session).latest(job_id)
            seq = checkpoint.seq if checkpoint is not None else None
            terminal = current.status is JobStatus.TERMINAL
            if seq != last_seq or terminal:
                last_seq = seq
                yield _stream_event(job_id, current, checkpoint)
            if terminal:
                return
            await asyncio.sleep(STREAM_POLL_INTERVAL_S)

    return StreamingResponse(events(), media_type="text/event-stream")
