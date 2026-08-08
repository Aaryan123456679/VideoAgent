"""`GET /v1/jobs/{job_id}/artifacts` — T2.4's artifacts route.

Driven the same way `test_api_jobs.py` drives the job routes: a real `create_app()`, a real
`Resources`, and fakes only at the seams a unit test cannot cross without a live Postgres or
object store — the database connection and the presigning transport.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import pytest

from tests.unit.test_api_support import (
    TENANT_A,
    TENANT_B,
    StaticVerifier,
    api_client,
    authorised,
    build_app,
    build_resources,
)
from video_agent.observability.codes import ErrorCode
from video_agent.persistence.session import TenantSession

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from fastapi import FastAPI
    from sqlalchemy import Executable, Result

NOT_FOUND = 404
OK = 200
EXPECTED_ARTIFACT_COUNT = 2

JOB_ID: UUID = UUID("44444444-4444-4444-4444-444444444444")


# --- fakes -------------------------------------------------------------------------------


class _ScriptedResult:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = list(rows)

    def mappings(self) -> _ScriptedResult:
        return self

    def first(self) -> Mapping[str, Any] | None:
        return self._rows[0] if self._rows else None

    def one(self) -> Mapping[str, Any]:
        assert self._rows
        return self._rows[0]

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return iter(self._rows)


class ScriptedConnection:
    """Answers each `execute` from a queue of scripted row-sets, in call order."""

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
    def __init__(self, connection: ScriptedConnection | None = None) -> None:
        self.connection = connection if connection is not None else ScriptedConnection()

    def tenant_scope(self, tenant_id: UUID) -> AbstractAsyncContextManager[TenantSession]:
        return self._scope(tenant_id)

    def _scope(self, tenant_id: UUID) -> AbstractAsyncContextManager[TenantSession]:
        @asynccontextmanager
        async def _open() -> AsyncIterator[TenantSession]:
            session = TenantSession(connection=self.connection, tenant_id=tenant_id)
            try:
                yield session
            finally:
                session.close()

        return _open()

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


@dataclass
class FakeTransport:
    """`persistence.presign.UrlMinter`, in memory."""

    fail_for: frozenset[str] = frozenset()
    calls: list[tuple[str, int]] = field(default_factory=list)

    def presign_get(self, key: str, ttl_seconds: int) -> str:
        self.calls.append((key, ttl_seconds))
        if key in self.fail_for:
            message = "simulated presign failure"
            raise RuntimeError(message)
        return f"https://objects.example.com/{key}?sig=abc&ttl={ttl_seconds}"


@dataclass
class FakeObjectStore:
    """`api.clients.ObjectStore`'s shape: something with a presign-capable `.transport`."""

    transport: FakeTransport

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
        "prompt": "a lighthouse at dusk, waves against the rocks",
        "music_bed": False,
        "budget_caps": {},
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


def _artifact_row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid4(),
        "job_id": JOB_ID,
        "kind": "final_video",
        "shot_index": None,
        "storage_key": "final/x.mp4",
        "content_type": "video/mp4",
        "bytes": 100,
        "checksum_sha256": "deadbeef",
    }
    row.update(overrides)
    return row


def _app(database: ScriptedDatabase, transport: FakeTransport) -> FastAPI:
    return build_app(
        resources=build_resources(database=database, object_store=FakeObjectStore(transport)),
        verifier=StaticVerifier(),
    )


# --- tests ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_artifacts_with_presigned_urls() -> None:
    video_row = _artifact_row(kind="final_video", storage_key="final/vid.mp4")
    thumb_row = _artifact_row(
        id=uuid4(), kind="thumbnail", storage_key="final/thumb.jpg", content_type="image/jpeg"
    )
    database = ScriptedDatabase(ScriptedConnection([[_job_row()], [video_row, thumb_row]]))
    transport = FakeTransport()
    app = _app(database, transport)

    async with api_client(app) as client:
        response = await client.get(f"/v1/jobs/{JOB_ID}/artifacts", headers=authorised())

    assert response.status_code == OK
    body = response.json()
    assert body["job_id"] == str(JOB_ID)
    assert len(body["artifacts"]) == EXPECTED_ARTIFACT_COUNT
    kinds = {entry["kind"] for entry in body["artifacts"]}
    assert kinds == {"final_video", "thumbnail"}
    for entry in body["artifacts"]:
        assert entry["url"] is not None
        assert entry["url"].startswith("https://objects.example.com/")
    assert transport.calls == [
        ("final/vid.mp4", 3600),
        ("final/thumb.jpg", 3600),
    ]


@pytest.mark.asyncio
async def test_a_presign_failure_yields_a_null_url_not_a_dropped_artifact() -> None:
    row = _artifact_row(storage_key="final/vid.mp4")
    database = ScriptedDatabase(ScriptedConnection([[_job_row()], [row]]))
    transport = FakeTransport(fail_for=frozenset({"final/vid.mp4"}))
    app = _app(database, transport)

    async with api_client(app) as client:
        response = await client.get(f"/v1/jobs/{JOB_ID}/artifacts", headers=authorised())

    assert response.status_code == OK
    body = response.json()
    assert len(body["artifacts"]) == 1
    assert body["artifacts"][0]["url"] is None


@pytest.mark.asyncio
async def test_job_not_found_is_404() -> None:
    database = ScriptedDatabase(ScriptedConnection([[]]))
    app = _app(database, FakeTransport())

    async with api_client(app) as client:
        response = await client.get(f"/v1/jobs/{uuid4()}/artifacts", headers=authorised())

    assert response.status_code == NOT_FOUND
    assert response.json()["error"]["code"] == ErrorCode.VA_REQ_005.value


@pytest.mark.asyncio
async def test_cross_tenant_listing_is_404_not_another_tenants_artifacts() -> None:
    database = ScriptedDatabase(ScriptedConnection([[_job_row(tenant_id=TENANT_B)]]))
    app = _app(database, FakeTransport())

    async with api_client(app) as client:
        response = await client.get(f"/v1/jobs/{JOB_ID}/artifacts", headers=authorised())

    assert response.status_code == NOT_FOUND
