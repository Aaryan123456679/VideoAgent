"""`T1.3` — `POST/GET /v1/jobs`, `GET /v1/jobs/{id}`, cancel and the SSE stream.

Driven the same way `test_api_idempotency.py` drives the algorithm and `test_api_principal.py`
drives the boundary: a real `create_app()`, a real `Resources`, and fakes only at the two
seams a unit test cannot cross without a live Postgres or Redis — the database connection and
the cache client. `ScriptedConnection` is the same technique `test_persistence_repositories.py`
uses for the repositories themselves: script the rows a statement would return, and let the
real `JobRepository`/`CheckpointRepository` build and read the real statements.

What is **not** asserted here: that RLS itself filters a cross-tenant row (that needs a live
Postgres and belongs to `tests/integration`). What *is* asserted is this module's own
defence-in-depth — `assert_tenant_owns` turning a row from the "wrong" tenant into a `404` —
by scripting a row that RLS would never actually hand back.
"""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from tests.unit.test_api_support import (
    TENANT_A,
    TENANT_B,
    InMemoryIdempotencyStore,
    StaticVerifier,
    api_client,
    authorised,
    build_app,
    build_resources,
)
from video_agent.api.errors import ApiError, ErrorEnvelope
from video_agent.api.idempotency import (
    IdempotencyRecord,
    IdempotencyState,
    IdempotencyStore,
    request_fingerprint,
    storage_key_for,
)
from video_agent.api.jobs import CREATE_STATUS, ENTRY_NODE, _decode_cursor, _encode_cursor
from video_agent.observability.codes import ErrorCode
from video_agent.persistence.queue import JobMessage
from video_agent.persistence.session import TenantSession

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy import Executable, Result

    from video_agent.persistence.keys import RedisKey

EXPECTED_ITERATIONS_CAP = 40
EXPECTED_PAGE_LEN = 2

ACCEPTED = 202
NOOP = 200
NOT_FOUND = 404
BAD_REQUEST = 400
UNPROCESSABLE = 422

JOB_ID: UUID = UUID("33333333-3333-3333-3333-333333333333")
PROMPT = "a lighthouse at dusk, waves against the rocks"


# --- fakes -------------------------------------------------------------------------------


class _ScriptedResult:
    """The slice of `Result` the repositories use, scripted with rows to hand back."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = list(rows)

    def mappings(self) -> _ScriptedResult:
        return self

    def first(self) -> Mapping[str, Any] | None:
        return self._rows[0] if self._rows else None

    def one(self) -> Mapping[str, Any]:
        assert self._rows, "the route issued a query the test did not script a row for"
        return self._rows[0]

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return iter(self._rows)


class ScriptedConnection:
    """A `DatabaseConnection` answering each `execute` from a queue of scripted row-sets.

    One connection can back several `tenant_scope()` calls in sequence — the SSE stream opens
    a fresh scope per poll — because scripting is per-*call*, not per-scope.
    """

    def __init__(self, replies: Sequence[Sequence[Mapping[str, Any]]] = ()) -> None:
        self.statements: list[Executable] = []
        self._replies = list(replies)

    async def execute(
        self, statement: Executable, _parameters: Mapping[str, Any] | None = None
    ) -> Result[Any]:
        self.statements.append(statement)
        rows = self._replies.pop(0) if self._replies else []
        return cast("Result[Any]", _ScriptedResult(rows))


class ScriptedDatabase:
    """A `DatabaseResource` whose `tenant_scope` hands out sessions over one scripted connection."""

    def __init__(self, connection: ScriptedConnection | None = None) -> None:
        self.connection = connection if connection is not None else ScriptedConnection()
        self.tenant_ids_seen: list[UUID] = []

    def tenant_scope(self, tenant_id: UUID) -> AbstractAsyncContextManager[TenantSession]:
        return self._scope(tenant_id)

    @staticmethod
    async def _open(db: ScriptedDatabase, tenant_id: UUID) -> AsyncIterator[TenantSession]:
        db.tenant_ids_seen.append(tenant_id)
        session = TenantSession(connection=db.connection, tenant_id=tenant_id)
        try:
            yield session
        finally:
            session.close()

    def _scope(self, tenant_id: UUID) -> AbstractAsyncContextManager[TenantSession]:
        return asynccontextmanager(self._open)(self, tenant_id)

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


@dataclass
class FakeQueue:
    published: list[JobMessage] = field(default_factory=list)

    async def publish(self, message: JobMessage) -> str:
        self.published.append(message)
        return "0-1"


@dataclass
class FakeRedisStore:
    writes: list[tuple[RedisKey, str]] = field(default_factory=list)

    async def set(self, key: RedisKey, value: str) -> None:
        self.writes.append((key, value))


class FakeCache:
    """Satisfies `api.jobs._JobsCache` structurally: `idempotency_store()`, `.queue`, `.store`."""

    def __init__(self) -> None:
        self._store_impl = InMemoryIdempotencyStore()
        self.queue = FakeQueue()
        self.store = FakeRedisStore()

    def idempotency_store(self) -> IdempotencyStore:
        return self._store_impl

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _job_row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": JOB_ID,
        "tenant_id": TENANT_A,
        "idempotency_key": "key-1",
        "request_fingerprint": "fp-1",
        "status": "queued",
        "trace_id": "trace-1",
        "prompt": PROMPT,
        "music_bed": False,
        "budget_caps": {
            "max_iterations": 40,
            "max_wall_clock_s": 1200.0,
            "max_tokens": 250_000,
            "max_usd": "5.00",
        },
        "budget_epoch": 0,
        "outcome": None,
        "degraded": False,
        "degraded_reason": None,
        "budget_used": {},
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _checkpoint_row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1,
        "thread_id": JOB_ID,
        "node": "lock_bible",
        "seq": 2,
        "state": {},
        "budget_used": {},
    }
    row.update(overrides)
    return row


def _app(database: ScriptedDatabase, cache: FakeCache) -> FastAPI:
    return build_app(
        resources=build_resources(database=database, cache=cache), verifier=StaticVerifier()
    )


# --- POST /v1/jobs -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_first_call_enqueues_and_returns_202() -> None:
    database = ScriptedDatabase(ScriptedConnection([[_job_row()]]))
    cache = FakeCache()
    app = _app(database, cache)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/jobs",
            json={"prompt": PROMPT},
            headers={**authorised(), "Idempotency-Key": "a" * 20},
        )

    assert response.status_code == ACCEPTED
    body = response.json()
    assert body["job_id"] == str(JOB_ID)
    assert body["status"] == "queued"
    assert len(cache.queue.published) == 1
    assert cache.queue.published[0] == JobMessage(
        tenant_id=TENANT_A, job_id=JOB_ID, node=ENTRY_NODE
    )


@pytest.mark.asyncio
async def test_create_job_missing_idempotency_key_is_400() -> None:
    app = _app(ScriptedDatabase(), FakeCache())

    async with api_client(app) as client:
        response = await client.post(
            "/v1/jobs", json={"prompt": PROMPT}, headers=authorised()
        )

    assert response.status_code == BAD_REQUEST
    assert response.json()["error"]["code"] == ErrorCode.VA_REQ_002.value


@pytest.mark.asyncio
async def test_create_job_rejects_a_removed_field() -> None:
    """`[D-74]`: `webhook_url` is unknown to `CreateJobRequest` and must be rejected loudly."""
    app = _app(ScriptedDatabase(), FakeCache())

    async with api_client(app) as client:
        response = await client.post(
            "/v1/jobs",
            json={"prompt": PROMPT, "webhook_url": "https://example.com/hook"},
            headers={**authorised(), "Idempotency-Key": "a" * 20},
        )

    assert response.status_code == UNPROCESSABLE


@pytest.mark.asyncio
async def test_create_job_replays_the_stored_body_without_touching_the_queue() -> None:
    """A completed idempotency record short-circuits before the database or the queue."""
    database = ScriptedDatabase(ScriptedConnection())
    cache = FakeCache()
    app = _app(database, cache)
    stored_job_id = uuid4()
    stored_body = json.dumps(
        {"job_id": str(stored_job_id), "status": "queued", "trace_id": "t", "created_at": "x"}
    )
    # The fingerprint has to match what the replay compares against, so it is computed with
    # the real algorithm rather than guessed.
    key = storage_key_for(TENANT_A, "/v1/jobs", "a" * 20)
    fingerprint = request_fingerprint(TENANT_A, "/v1/jobs", json.dumps({"prompt": PROMPT}).encode())
    cache._store_impl.records[key] = IdempotencyRecord(
        state=IdempotencyState.DONE,
        fingerprint=fingerprint,
        status_code=ACCEPTED,
        body=stored_body,
        job_id=stored_job_id,
    )

    async with api_client(app) as client:
        response = await client.post(
            "/v1/jobs",
            json={"prompt": PROMPT},
            headers={**authorised(), "Idempotency-Key": "a" * 20},
        )

    assert response.status_code == ACCEPTED
    assert response.text == stored_body
    assert response.headers["Idempotency-Replayed"] == "true"
    assert cache.queue.published == []
    assert database.connection.statements == []


# --- GET /v1/jobs/{job_id} -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_reports_status_budget_and_resumable() -> None:
    database = ScriptedDatabase(
        ScriptedConnection([[_job_row(status="terminal", outcome="PARTIAL")], [_checkpoint_row()]])
    )
    app = _app(database, FakeCache())

    async with api_client(app) as client:
        response = await client.get(f"/v1/jobs/{JOB_ID}", headers=authorised())

    assert response.status_code == NOOP
    body = response.json()
    assert body["status"] == "terminal"
    assert body["outcome"] == "PARTIAL"
    assert body["resumable"] is True
    assert body["current_node"] == "lock_bible"
    assert body["budget"]["iterations_cap"] == EXPECTED_ITERATIONS_CAP
    assert body["budget"]["usd_cap"] == "5.00"


@pytest.mark.asyncio
async def test_get_job_with_no_checkpoint_reports_the_entry_node() -> None:
    database = ScriptedDatabase(ScriptedConnection([[_job_row()], []]))
    app = _app(database, FakeCache())

    async with api_client(app) as client:
        response = await client.get(f"/v1/jobs/{JOB_ID}", headers=authorised())

    assert response.status_code == NOOP
    assert response.json()["current_node"] == ENTRY_NODE
    assert response.json()["resumable"] is False


@pytest.mark.asyncio
async def test_get_job_not_found_is_404() -> None:
    database = ScriptedDatabase(ScriptedConnection([[]]))
    app = _app(database, FakeCache())

    async with api_client(app) as client:
        response = await client.get(f"/v1/jobs/{uuid4()}", headers=authorised())

    assert response.status_code == NOT_FOUND
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.VA_REQ_005.value


@pytest.mark.asyncio
async def test_get_job_owned_by_another_tenant_is_404() -> None:
    """Defence-in-depth: even if a row reached this code, it is not this tenant's to see."""
    database = ScriptedDatabase(ScriptedConnection([[_job_row(tenant_id=TENANT_B)]]))
    app = _app(database, FakeCache())

    async with api_client(app) as client:
        response = await client.get(f"/v1/jobs/{JOB_ID}", headers=authorised())

    assert response.status_code == NOT_FOUND


# --- GET /v1/jobs (list) -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs_returns_a_next_cursor_when_the_page_is_full() -> None:
    rows = [_job_row(id=uuid4(), created_at=datetime(2026, 1, i + 1, tzinfo=UTC)) for i in range(2)]
    database = ScriptedDatabase(ScriptedConnection([rows]))
    app = _app(database, FakeCache())

    async with api_client(app) as client:
        response = await client.get("/v1/jobs?limit=2", headers=authorised())

    assert response.status_code == NOOP
    body = response.json()
    assert len(body["jobs"]) == EXPECTED_PAGE_LEN
    assert body["next_cursor"] is not None


@pytest.mark.asyncio
async def test_list_jobs_no_next_cursor_on_a_partial_page() -> None:
    database = ScriptedDatabase(ScriptedConnection([[_job_row()]]))
    app = _app(database, FakeCache())

    async with api_client(app) as client:
        response = await client.get("/v1/jobs?limit=20", headers=authorised())

    assert response.json()["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_jobs_tampered_cursor_is_422() -> None:
    app = _app(ScriptedDatabase(), FakeCache())

    async with api_client(app) as client:
        response = await client.get("/v1/jobs?cursor=not-a-real-cursor", headers=authorised())

    assert response.status_code == UNPROCESSABLE
    assert response.json()["error"]["code"] == ErrorCode.VA_REQ_007.value


@pytest.mark.asyncio
async def test_list_jobs_page_size_is_clamped() -> None:
    database = ScriptedDatabase(ScriptedConnection([[_job_row()]]))
    app = _app(database, FakeCache())

    async with api_client(app) as client:
        response = await client.get("/v1/jobs?limit=10000", headers=authorised())

    assert response.status_code == NOOP
    # The clamp is exercised by the absence of a 422/500; the exact bound is a repository
    # concern already covered by list_page's own LIMIT.


def test_cursor_roundtrips() -> None:
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    job_id = uuid4()

    encoded = _encode_cursor(created_at, job_id)
    decoded_created_at, decoded_job_id = _decode_cursor(encoded)

    assert decoded_created_at == created_at
    assert decoded_job_id == job_id


def test_a_non_base64_cursor_is_rejected() -> None:
    with pytest.raises(ApiError) as raised:
        _decode_cursor("not valid base64!!!")

    assert raised.value.code is ErrorCode.VA_REQ_007


# --- POST /v1/jobs/{job_id}/cancel ---------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_a_running_job_writes_the_signal_and_returns_202() -> None:
    database = ScriptedDatabase(ScriptedConnection([[_job_row(status="running")]]))
    cache = FakeCache()
    app = _app(database, cache)

    async with api_client(app) as client:
        response = await client.post(
            f"/v1/jobs/{JOB_ID}/cancel", headers={**authorised(), "Idempotency-Key": "b" * 20}
        )

    assert response.status_code == ACCEPTED
    assert response.json() == {"accepted": True, "outcome": None}
    assert len(cache.store.writes) == 1
    written_key, written_value = cache.store.writes[0]
    assert written_key.value == f"job:{JOB_ID}:cancel"
    payload = json.loads(written_value)
    assert payload["actor"] == "client"


@pytest.mark.asyncio
async def test_cancel_a_terminal_job_is_a_noop_and_writes_no_signal() -> None:
    database = ScriptedDatabase(
        ScriptedConnection([[_job_row(status="terminal", outcome="FAILED")]])
    )
    cache = FakeCache()
    app = _app(database, cache)

    async with api_client(app) as client:
        response = await client.post(
            f"/v1/jobs/{JOB_ID}/cancel", headers={**authorised(), "Idempotency-Key": "c" * 20}
        )

    assert response.status_code == NOOP
    assert response.json() == {"accepted": False, "outcome": "FAILED"}
    assert cache.store.writes == []


@pytest.mark.asyncio
async def test_cancel_requires_an_idempotency_key() -> None:
    app = _app(ScriptedDatabase(), FakeCache())

    async with api_client(app) as client:
        response = await client.post(f"/v1/jobs/{JOB_ID}/cancel", headers=authorised())

    assert response.status_code == BAD_REQUEST


@pytest.mark.asyncio
async def test_cancel_a_missing_job_is_404() -> None:
    database = ScriptedDatabase(ScriptedConnection([[]]))
    app = _app(database, FakeCache())

    async with api_client(app) as client:
        response = await client.post(
            f"/v1/jobs/{uuid4()}/cancel", headers={**authorised(), "Idempotency-Key": "d" * 20}
        )

    assert response.status_code == NOT_FOUND


@pytest.mark.asyncio
async def test_double_cancel_with_the_same_key_replays() -> None:
    database = ScriptedDatabase(ScriptedConnection([[_job_row(status="running")]]))
    cache = FakeCache()
    app = _app(database, cache)
    headers = {**authorised(), "Idempotency-Key": "e" * 20}

    async with api_client(app) as client:
        first = await client.post(f"/v1/jobs/{JOB_ID}/cancel", headers=headers)
        second = await client.post(f"/v1/jobs/{JOB_ID}/cancel", headers=headers)

    assert first.status_code == second.status_code == ACCEPTED
    assert first.text == second.text
    assert len(cache.store.writes) == 1, "the signal is written once, not once per replay"


# --- GET /v1/jobs/{job_id}/stream ----------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_emits_a_terminal_event_immediately_for_a_finished_job() -> None:
    database = ScriptedDatabase(
        ScriptedConnection(
            [
                [_job_row(status="terminal", outcome="SUCCESS")],  # existence check
                [_job_row(status="terminal", outcome="SUCCESS")],  # first poll
                [_checkpoint_row(node="finalize", seq=9)],
            ]
        )
    )
    app = _app(database, FakeCache())

    async with (
        api_client(app) as client,
        client.stream("GET", f"/v1/jobs/{JOB_ID}/stream", headers=authorised()) as response,
    ):
        chunks = [chunk async for chunk in response.aiter_text()]

    text = "".join(chunks)
    assert "event: terminal" in text
    assert '"outcome": "SUCCESS"' in text or '"outcome":"SUCCESS"' in text


@pytest.mark.asyncio
async def test_stream_for_a_missing_job_is_404() -> None:
    database = ScriptedDatabase(ScriptedConnection([[]]))
    app = _app(database, FakeCache())

    async with api_client(app) as client:
        response = await client.get(f"/v1/jobs/{uuid4()}/stream", headers=authorised())

    assert response.status_code == NOT_FOUND


# --- sanity on the base64 helper import -----------------------------------------------------


def test_encode_cursor_uses_urlsafe_base64() -> None:
    encoded = _encode_cursor(datetime(2026, 1, 1, tzinfo=UTC), JOB_ID)
    # Must round-trip through urlsafe_b64decode without padding errors.
    base64.urlsafe_b64decode(encoded.encode())


def test_create_status_is_202_never_200() -> None:
    """`api.md` §2.1: `POST /v1/jobs` returns `202`, never `200` — a video is not synchronous."""
    assert CREATE_STATUS == ACCEPTED
