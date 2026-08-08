"""S0.5.8 — the repositories, against a recording connection rather than a database.

What is checkable without PostgreSQL is the part that matters most here: which statement a
repository builds and where the `tenant_id` on it came from. The adoption paths — a duplicate
idempotency key, a duplicate `request_fingerprint` — are driven by scripting the connection to
return no row on the insert and the existing row on the follow-up read, which is exactly what
PostgreSQL does after an `ON CONFLICT DO NOTHING`.

Whether the constraint actually fires is a question for the live database, and
`tests/integration/test_persistence_postgres.py` asks it there.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Executable, Result
from sqlalchemy.sql import ClauseElement

from video_agent.persistence import repositories
from video_agent.persistence.ddl import postgres_dialect
from video_agent.persistence.enums import AttemptState
from video_agent.persistence.repositories import (
    ArtifactRepository,
    AttemptRequest,
    CheckpointRepository,
    ContinuityBibleRepository,
    CostSettlement,
    IdempotencyKeyReusedError,
    JobRepository,
    NewJob,
    ProviderSubmission,
    ShotAttemptRepository,
    ShotRepository,
    StoryPlanRepository,
    _Repository,
)
from video_agent.persistence.schema import metadata
from video_agent.persistence.session import (
    UNSET_TENANT_CONTEXT_ALARM,
    TenantContextMissingError,
    TenantSession,
)

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")

REPOSITORY_TYPES = [
    ArtifactRepository,
    CheckpointRepository,
    ContinuityBibleRepository,
    JobRepository,
    ShotAttemptRepository,
    ShotRepository,
    StoryPlanRepository,
]


class _RecordingResult:
    """The slice of `Result` the repositories use, scripted with rows to hand back."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = list(rows)

    def mappings(self) -> _RecordingResult:
        return self

    def first(self) -> Mapping[str, Any] | None:
        return self._rows[0] if self._rows else None

    def one(self) -> Mapping[str, Any]:
        assert self._rows, "the repository expected a row and the script supplied none"
        return self._rows[0]

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return iter(self._rows)


class RecordingConnection:
    """Records every statement and replies from a scripted queue of result sets."""

    def __init__(self, replies: Sequence[Sequence[Mapping[str, Any]]] = ()) -> None:
        self.statements: list[ClauseElement] = []
        self.parameters: list[Mapping[str, Any] | None] = []
        self._replies = list(replies)

    async def execute(
        self,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Result[Any]:
        assert isinstance(statement, ClauseElement)
        self.statements.append(statement)
        self.parameters.append(parameters)
        rows = self._replies.pop(0) if self._replies else []
        return cast("Result[Any]", _RecordingResult(rows))

    def sql(self, index: int = 0) -> str:
        compiled = self.statements[index].compile(dialect=postgres_dialect())
        return str(compiled)

    def values(self, index: int = 0) -> dict[str, Any]:
        compiled = self.statements[index].compile(dialect=postgres_dialect())
        return dict(compiled.params)


def _session(connection: RecordingConnection, tenant_id: UUID = TENANT) -> TenantSession:
    return TenantSession(connection=connection, tenant_id=tenant_id)


def _job_row(**overrides: object) -> dict[str, Any]:
    row = {
        "id": uuid4(),
        "tenant_id": TENANT,
        "idempotency_key": "key-1",
        "request_fingerprint": "fp-1",
        "status": "queued",
        "trace_id": "trace-1",
        "prompt": "a cat",
        "music_bed": False,
        "budget_caps": {},
        "budget_epoch": 0,
    }
    row.update(overrides)
    return row


def _attempt_row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid4(),
        "shot_id": uuid4(),
        "job_id": uuid4(),
        "attempt_no": 1,
        "state": "in_flight",
        "request_fingerprint": "fp-shot-1",
        "provider_project_id": None,
        "seed": None,
        "seed_supported": False,
        "cost_usd": Decimal("0"),
        "credits_charged": None,
        "cost_is_final": False,
    }
    row.update(overrides)
    return row


# --- The tenant boundary -------------------------------------------------------------------


@pytest.mark.parametrize("repository", REPOSITORY_TYPES, ids=lambda cls: cls.__name__)
def test_no_repository_method_accepts_a_tenant_id(repository: type[_Repository]) -> None:
    """`tenant_id` comes from the authenticated session and has no parameter to arrive by.

    Stronger than validating a supplied id: a validation is a check somebody adds an admin
    bypass to, and an absent parameter is not.
    """
    offenders: list[str] = []
    for name, method in inspect.getmembers(repository, inspect.isfunction):
        if name.startswith("_"):
            continue
        if "tenant_id" in inspect.signature(method).parameters:
            offenders.append(f"{repository.__name__}.{name}")
    assert offenders == []


@pytest.mark.asyncio
async def test_inserted_rows_carry_the_session_tenant() -> None:
    connection = RecordingConnection([[_job_row()]])
    await JobRepository(_session(connection)).create(
        NewJob(
            idempotency_key="key-1",
            request_fingerprint="fp-1",
            prompt="a cat",
            trace_id="trace-1",
            budget_caps={"usd": 5},
        )
    )
    assert connection.values()["tenant_id"] == TENANT


@pytest.mark.asyncio
async def test_a_second_session_stamps_its_own_tenant() -> None:
    """The stamp follows the session, so two tenants cannot be conflated by a shared default."""
    connection = RecordingConnection([[_job_row(tenant_id=OTHER_TENANT)]])
    await JobRepository(_session(connection, OTHER_TENANT)).create(
        NewJob(
            idempotency_key="key-1",
            request_fingerprint="fp-1",
            prompt="a cat",
            trace_id="trace-1",
            budget_caps={},
        )
    )
    assert connection.values()["tenant_id"] == OTHER_TENANT


@pytest.mark.asyncio
async def test_use_after_the_transaction_ends_raises_and_alarms() -> None:
    """Zero rows is indistinguishable from an empty table; this layer fails loudly instead."""
    UNSET_TENANT_CONTEXT_ALARM.reset()
    session = _session(RecordingConnection([[_job_row()]]))
    session.close()
    with pytest.raises(TenantContextMissingError):
        await JobRepository(session).get(uuid4())
    assert UNSET_TENANT_CONTEXT_ALARM.count == 1


def test_the_unset_context_error_carries_a_taxonomy_code() -> None:
    """`[CPS §Failure behaviour]`: every error carries a stable code and a trace id."""
    error = TenantContextMissingError("no context")
    assert error.code.value == "VA-STORE-003"


# --- Idempotency ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_creation_uses_the_idempotency_constraint_not_a_prior_read() -> None:
    """A read-then-write leaves a window in which two concurrent requests both insert."""
    connection = RecordingConnection([[_job_row()]])
    record = await JobRepository(_session(connection)).create(
        NewJob(
            idempotency_key="key-1",
            request_fingerprint="fp-1",
            prompt="a cat",
            trace_id="trace-1",
            budget_caps={},
        )
    )
    sql = connection.sql()
    assert "ON CONFLICT ON CONSTRAINT job_idem_uq DO NOTHING" in sql
    assert len(connection.statements) == 1
    assert record.created is True


@pytest.mark.asyncio
async def test_a_replayed_request_adopts_the_existing_job() -> None:
    """The replay path returns the first job, never a second one. `[D-16]`"""
    existing = _job_row()
    connection = RecordingConnection([[], [existing]])
    record = await JobRepository(_session(connection)).create(
        NewJob(
            idempotency_key="key-1",
            request_fingerprint="fp-1",
            prompt="a cat",
            trace_id="trace-1",
            budget_caps={},
        )
    )
    assert record.created is False
    assert record.id == existing["id"]


@pytest.mark.asyncio
async def test_a_reused_key_with_a_different_body_is_rejected() -> None:
    """Returning the first job would answer a question the caller did not ask."""
    connection = RecordingConnection([[], [_job_row(request_fingerprint="fp-OTHER")]])
    with pytest.raises(IdempotencyKeyReusedError) as caught:
        await JobRepository(_session(connection)).create(
            NewJob(
                idempotency_key="key-1",
                request_fingerprint="fp-1",
                prompt="a different cat",
                trace_id="trace-1",
                budget_caps={},
            )
        )
    assert caught.value.code.value == "VA-REQ-003"


# --- The anti-double-bill path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_claiming_an_attempt_conflicts_on_the_fingerprint_constraint() -> None:
    """`[D-24]`: `request_fingerprint` is what collapses a redelivered step to one charge."""
    connection = RecordingConnection([[_attempt_row()]])
    claim = await ShotAttemptRepository(_session(connection)).claim(
        AttemptRequest(
            shot_id=uuid4(),
            job_id=uuid4(),
            attempt_no=1,
            request_fingerprint="fp-shot-1",
            prompt_text="a cat, wide",
            prompt_hash="h1",
            bible_hash="h2",
        )
    )
    assert "ON CONFLICT ON CONSTRAINT shot_attempt_fingerprint_uq DO NOTHING" in connection.sql()
    assert claim.adopted is False


@pytest.mark.asyncio
async def test_the_attempt_row_is_written_in_flight_before_the_provider_call() -> None:
    """The crash window is between the request and the response; the row has to precede it."""
    connection = RecordingConnection([[_attempt_row()]])
    await ShotAttemptRepository(_session(connection)).claim(
        AttemptRequest(
            shot_id=uuid4(),
            job_id=uuid4(),
            attempt_no=1,
            request_fingerprint="fp-shot-1",
            prompt_text="a cat, wide",
            prompt_hash="h1",
            bible_hash="h2",
        )
    )
    assert connection.values()["state"] == AttemptState.IN_FLIGHT


@pytest.mark.asyncio
async def test_a_redelivered_step_adopts_the_existing_attempt() -> None:
    """`[D-67]`: at-least-once delivery means this runs twice for one unit of work.

    The second run must return the first attempt — carrying the `provider_project_id` the
    recovery path re-reads — rather than a fresh row that would justify a second paid render.
    """
    existing = _attempt_row(provider_project_id="mh-project-9", state="in_flight")
    connection = RecordingConnection([[], [existing]])
    claim = await ShotAttemptRepository(_session(connection)).claim(
        AttemptRequest(
            shot_id=uuid4(),
            job_id=uuid4(),
            attempt_no=1,
            request_fingerprint="fp-shot-1",
            prompt_text="a cat, wide",
            prompt_hash="h1",
            bible_hash="h2",
        )
    )
    assert claim.adopted is True
    assert claim.attempt.provider_project_id == "mh-project-9"
    assert claim.attempt.id == existing["id"]


@pytest.mark.asyncio
async def test_the_provider_render_id_is_recorded_separately_from_completion() -> None:
    """A crash after submission and before completion still leaves the handle to reconcile by."""
    attempt_id = uuid4()
    connection = RecordingConnection([[_attempt_row(id=attempt_id, provider_project_id="mh-1")]])
    record = await ShotAttemptRepository(_session(connection)).record_submission(
        attempt_id,
        ProviderSubmission(
            provider_project_id="mh-1",
            provider_key="key",
            provider_model="model",
            seed=None,
            seed_supported=False,
        ),
    )
    assert record.provider_project_id == "mh-1"
    assert record.seed is None
    assert record.seed_supported is False


@pytest.mark.asyncio
async def test_settling_cost_marks_it_final() -> None:
    """`[D-60]`: the sweeper finds unreconciled spend by `cost_is_final = false`."""
    attempt_id = uuid4()
    connection = RecordingConnection(
        [[_attempt_row(id=attempt_id, cost_is_final=True, cost_usd=Decimal("1.25"))]]
    )
    record = await ShotAttemptRepository(_session(connection)).settle_cost(
        attempt_id,
        CostSettlement(
            state=AttemptState.SUCCEEDED,
            cost_usd=Decimal("1.25"),
            credits_charged=Decimal("1388.8889"),
        ),
    )
    assert connection.values()["cost_is_final"] is True
    assert record.cost_is_final is True


@pytest.mark.asyncio
async def test_an_attempt_can_be_found_by_its_fingerprint() -> None:
    """The reconciliation path's entry point after a unique violation."""
    connection = RecordingConnection([[_attempt_row(provider_project_id="mh-7")]])
    record = await ShotAttemptRepository(_session(connection)).get_by_fingerprint("fp-shot-1")
    assert record is not None
    assert record.provider_project_id == "mh-7"


# --- Record projections ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table_name", "columns"),
    sorted(repositories.RECORD_COLUMN_PROJECTIONS.items()),
)
def test_every_projected_column_exists_on_its_table(
    table_name: str, columns: tuple[str, ...]
) -> None:
    """A record that reads a column the schema no longer has fails at runtime, not at import."""
    available = set(metadata.tables[table_name].c.keys())
    assert set(columns) <= available


@pytest.mark.asyncio
async def test_reading_another_tenants_row_returns_none_not_an_error() -> None:
    """RLS filters rather than rejecting, so a cross-tenant read is a miss. `[persistence.md §9]`"""
    connection = RecordingConnection([[]])
    assert await JobRepository(_session(connection)).get(uuid4()) is None
