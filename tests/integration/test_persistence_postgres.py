"""S0.5.1 — S0.5.8 against a live PostgreSQL: RLS enforcement, constraints, migration round trip.

Everything here needs a real server. `persistence.md` §10 puts RLS at the top of the test
priority list and says a suite that runs as superuser proves nothing about isolation, so these
tests apply the migrations to a scratch database, `SET ROLE` to the non-owner application role
the migration creates, and assert isolation from there.

**Skipping, not erroring, and not hanging.** The guard is a bounded connection attempt against
the configured `DATABASE_URL`, not a check that `asyncpg` is importable or that a binary is on
`PATH`. A wedged daemon leaves the client installed and every command hanging, which surfaces
as `TimeoutExpired` rather than as the skip the environment actually calls for. One short
probe, once per module, converts that into a skip with a reason.

**A scratch database per module, dropped afterwards.** Migrations are DDL; running them against
whatever the developer has in `videoagent` would destroy it. The scratch database also makes
`alembic downgrade base` a real assertion rather than something nobody dares run.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import Select, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from video_agent.persistence.enums import AttemptState
from video_agent.persistence.repositories import (
    AttemptRequest,
    CostSettlement,
    JobRepository,
    NewJob,
    ProviderSubmission,
    ShotAttemptRepository,
)
from video_agent.persistence.rls import (
    APPLICATION_ROLE,
    CATALOG_RLS_QUERY,
    RLS_EXEMPT_TABLES,
    RLS_PROTECTED_TABLES,
    audit_rls,
    facts_from_catalog_rows,
    format_violations,
)
from video_agent.persistence.schema import TABLE_NAMES, metadata
from video_agent.persistence.session import TenantSession

pytestmark = pytest.mark.integration

PROBE_TIMEOUT_SECONDS = 3.0
"""Short on purpose. This is a liveness question, not an operation worth waiting for."""

TENANT_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
TENANT_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")

VERBS = ("select", "insert", "update", "delete")


def _count(table: str) -> Select[tuple[int]]:
    """`SELECT count(*)` built from the schema rather than formatted into a string.

    The table name is a trusted constant either way; building the statement removes the
    question rather than answering it in a comment.
    """
    return select(func.count()).select_from(metadata.tables[table])


# --- Reachability -------------------------------------------------------------------------


def _configured_url() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://videoagent:videoagent@localhost:5432/videoagent"
    )


async def _probe(url: str) -> str | None:
    engine = create_async_engine(url, pool_pre_ping=False)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    finally:
        await engine.dispose()
    return None


def _unreachable_reason(url: str) -> str | None:
    """None when the server answers within the probe timeout, otherwise why it did not."""

    async def bounded() -> str | None:
        try:
            return await asyncio.wait_for(_probe(url), timeout=PROBE_TIMEOUT_SECONDS)
        except TimeoutError:
            return f"no answer within {PROBE_TIMEOUT_SECONDS}s"

    return asyncio.run(bounded())


# --- The scratch database ---------------------------------------------------------------------


def _maintenance_url(url: str) -> str:
    return url.rsplit("/", 1)[0] + "/postgres"


async def _run_maintenance(url: str, statement: str) -> None:
    engine = create_async_engine(_maintenance_url(url), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


def _alembic(url: str, repo_root: Path, target: str, *, downgrade: bool = False) -> None:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        config = Config(str(repo_root / "alembic.ini"), output_buffer=io.StringIO())
        config.set_main_option("script_location", str(repo_root / "migrations"))
        if downgrade:
            command.downgrade(config, target)
        else:
            command.upgrade(config, target)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture(scope="module")
def scratch_url(repo_root: Path) -> Iterator[str]:
    """A freshly created database with the migrations applied, dropped on the way out."""
    base = _configured_url()
    reason = _unreachable_reason(base)
    if reason is not None:
        pytest.skip(f"PostgreSQL is not reachable at the configured DATABASE_URL: {reason}")

    name = f"videoagent_t05_{uuid.uuid4().hex[:12]}"
    url = base.rsplit("/", 1)[0] + f"/{name}"
    asyncio.run(_run_maintenance(base, f'CREATE DATABASE "{name}"'))
    try:
        _alembic(url, repo_root, "head")
        yield url
    finally:
        asyncio.run(_run_maintenance(base, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest_asyncio.fixture
async def engine(scratch_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine on the current event loop. Function-scoped so loop scoping never bites."""
    created = create_async_engine(scratch_url, poolclass=None)
    try:
        yield created
    finally:
        await created.dispose()


@asynccontextmanager
async def owner_scope(engine: AsyncEngine, tenant_id: uuid.UUID) -> AsyncIterator[AsyncConnection]:
    """A transaction as the *owner*, with the tenant set.

    Used for seeding and for `test_force_rls_applies_to_owner`. `FORCE ROW LEVEL SECURITY`
    means the policy applies here too, so seeding still has to name a tenant.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield connection


@asynccontextmanager
async def app_scope(
    engine: AsyncEngine, tenant_id: uuid.UUID | None
) -> AsyncIterator[AsyncConnection]:
    """A transaction as the non-owner application role, optionally with no tenant set.

    `SET LOCAL ROLE` rather than a second connection string: the role is created `NOLOGIN`
    because a migration must never carry a password, so the test borrows the owner's
    connection and drops into the role for the transaction. `current_user` and
    `is_superuser` both reflect the role after that, which is what the suite asserts.
    """
    async with engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {APPLICATION_ROLE}"))
        if tenant_id is not None:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
        yield connection


@pytest_asyncio.fixture
async def seeded(engine: AsyncEngine) -> AsyncIterator[dict[uuid.UUID, uuid.UUID]]:
    """Two tenants, one job each, seeded as the owner. Returns tenant id to job id."""
    async with engine.begin() as connection:
        await connection.execute(text(f"GRANT {APPLICATION_ROLE} TO CURRENT_USER"))
        for tenant_id in (TENANT_A, TENANT_B):
            await connection.execute(
                text("INSERT INTO tenant (id, name) VALUES (:id, :name) ON CONFLICT DO NOTHING"),
                {"id": tenant_id, "name": f"tenant-{tenant_id}"},
            )

    jobs: dict[uuid.UUID, uuid.UUID] = {}
    for tenant_id in (TENANT_A, TENANT_B):
        job_id = uuid.uuid4()
        jobs[tenant_id] = job_id
        async with owner_scope(engine, tenant_id) as connection:
            await connection.execute(
                text(
                    "INSERT INTO job (id, tenant_id, idempotency_key, request_fingerprint, "
                    "prompt, trace_id, budget_caps) VALUES (:id, :tenant_id, :key, :fp, "
                    ":prompt, :trace, '{}'::jsonb)"
                ),
                {
                    "id": job_id,
                    "tenant_id": tenant_id,
                    "key": f"key-{job_id}",
                    "fp": f"fp-{job_id}",
                    "prompt": "a cat walks into a bar",
                    "trace": f"trace-{job_id}",
                },
            )
    yield jobs


# --- The migration round trip ---------------------------------------------------------------


def test_upgrade_then_downgrade_is_clean(repo_root: Path) -> None:
    """S0.5.1 acceptance 1, on its own database so nothing else depends on the result.

    A downgrade nobody runs is a rollback nobody has. This applies the whole tree forward,
    rolls it all the way back, and asserts the database is empty of our tables afterwards —
    which is what catches a `downgrade()` that drops a table but leaves its type, its trigger
    or its policy behind.
    """
    base = _configured_url()
    reason = _unreachable_reason(base)
    if reason is not None:
        pytest.skip(f"PostgreSQL is not reachable at the configured DATABASE_URL: {reason}")

    name = f"videoagent_rt_{uuid.uuid4().hex[:12]}"
    url = base.rsplit("/", 1)[0] + f"/{name}"
    asyncio.run(_run_maintenance(base, f'CREATE DATABASE "{name}"'))
    try:
        _alembic(url, repo_root, "head")
        assert asyncio.run(_table_names(url)) >= set(TABLE_NAMES)
        _alembic(url, repo_root, "base", downgrade=True)
        remaining = asyncio.run(_table_names(url))
        assert remaining & set(TABLE_NAMES) == set()
        assert asyncio.run(_type_names(url)) == set()
    finally:
        asyncio.run(_run_maintenance(base, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def _table_names(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
            )
            return {row[0] for row in result}
    finally:
        await engine.dispose()


async def _type_names(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
                    " WHERE t.typtype = 'e' AND n.nspname = current_schema()"
                )
            )
            return {row[0] for row in result}
    finally:
        await engine.dispose()


# --- RLS: the catalogue agrees with the audit -------------------------------------------------


@pytest.mark.asyncio
async def test_live_schema_passes_the_rls_audit(engine: AsyncEngine) -> None:
    """S0.5.7 acceptance 1 and 4, against `pg_class` and `pg_policy` rather than against SQL."""
    async with engine.connect() as connection:
        result = await connection.execute(text(CATALOG_RLS_QUERY))
        rows = [tuple(row) for row in result]
    facts = [fact for fact in facts_from_catalog_rows(rows) if fact.name in set(TABLE_NAMES)]
    violations = audit_rls(facts)
    assert violations == [], format_violations(violations)


@pytest.mark.asyncio
async def test_the_suite_does_not_run_as_superuser(engine: AsyncEngine) -> None:
    """`persistence.md` §3 rule 5: a suite that runs as superuser proves nothing."""
    async with app_scope(engine, TENANT_A) as connection:
        result = await connection.execute(
            text("SELECT current_setting('is_superuser'), current_user")
        )
        is_superuser, current_user = result.one()
    assert is_superuser == "off"
    assert current_user == APPLICATION_ROLE


@pytest.mark.asyncio
async def test_the_application_role_is_not_the_table_owner(engine: AsyncEngine) -> None:
    """`FORCE` closes the owner hole; not being the owner is the layer in front of it."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = current_schema()"
                " AND tableowner = :role"
            ),
            {"role": APPLICATION_ROLE},
        )
        assert result.scalar_one() == 0


# --- RLS: the matrix ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded")
async def test_tenant_b_cannot_read_tenant_a_rows(engine: AsyncEngine) -> None:
    """The single most important assertion in the repository. `[persistence.md §10]`"""
    async with app_scope(engine, TENANT_A) as connection:
        mine = await connection.execute(text("SELECT count(*) FROM job"))
        assert mine.scalar_one() == 1

    async with app_scope(engine, TENANT_B) as connection:
        result = await connection.execute(
            text("SELECT count(*) FROM job WHERE tenant_id = :other"), {"other": TENANT_A}
        )
        assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_tenant_b_cannot_update_or_delete_tenant_a_rows(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID]
) -> None:
    """A row you cannot see is a row you cannot change: both affect zero rows, not an error."""
    target = seeded[TENANT_A]
    async with app_scope(engine, TENANT_B) as connection:
        updated = await connection.execute(
            text("UPDATE job SET degraded = true WHERE id = :id"), {"id": target}
        )
        assert updated.rowcount == 0
        deleted = await connection.execute(text("DELETE FROM job WHERE id = :id"), {"id": target})
        assert deleted.rowcount == 0

    async with app_scope(engine, TENANT_A) as connection:
        result = await connection.execute(
            text("SELECT degraded FROM job WHERE id = :id"), {"id": target}
        )
        assert result.scalar_one() is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded")
async def test_with_check_blocks_a_cross_tenant_insert(engine: AsyncEngine) -> None:
    """S0.5.7's `WITH CHECK` test: a write carrying another tenant's id is rejected, not stored."""
    with pytest.raises(DBAPIError) as caught:
        async with app_scope(engine, TENANT_B) as connection:
            await connection.execute(
                text(
                    "INSERT INTO job (id, tenant_id, idempotency_key, request_fingerprint, "
                    "prompt, trace_id, budget_caps) VALUES (:id, :tenant_id, 'k', 'f', 'p', "
                    "'t', '{}'::jsonb)"
                ),
                {"id": uuid.uuid4(), "tenant_id": TENANT_A},
            )
    assert "policy" in str(caught.value).lower()


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded")
async def test_force_rls_applies_to_the_owner(engine: AsyncEngine) -> None:
    """Without `FORCE`, the owner sees both tenants and the isolation is theatre."""
    async with owner_scope(engine, TENANT_A) as connection:
        result = await connection.execute(text("SELECT count(*) FROM job"))
        assert result.scalar_one() == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded")
async def test_an_unset_tenant_yields_zero_rows_rather_than_an_error(engine: AsyncEngine) -> None:
    """`persistence.md` §9: silent full-table access is impossible, and so is a hard failure.

    This is why the predicate uses two-argument `current_setting` and a `NULLIF` rather than
    the one-argument form printed in §3 — that form raises, which turns a leak-safe default
    into an outage.
    """
    async with app_scope(engine, None) as connection:
        result = await connection.execute(text("SELECT count(*) FROM job"))
        assert result.scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("table", RLS_PROTECTED_TABLES)
async def test_every_protected_table_is_empty_without_a_tenant(
    engine: AsyncEngine, table: str
) -> None:
    """The matrix, applied to every table rather than to the one that was easy to seed."""
    async with app_scope(engine, None) as connection:
        result = await connection.execute(_count(table))
        assert result.scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("table", sorted(RLS_EXEMPT_TABLES))
async def test_the_exempt_tables_are_readable_without_a_tenant(
    engine: AsyncEngine, table: str
) -> None:
    """The exemption is what it claims to be: key resolution runs before a tenant exists."""
    async with app_scope(engine, None) as connection:
        await connection.execute(_count(table))


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded")
async def test_the_application_role_cannot_insert_a_tenant(engine: AsyncEngine) -> None:
    """On the RLS-exempt tables the grant is the boundary, so it has to actually hold."""
    with pytest.raises(DBAPIError) as caught:
        async with app_scope(engine, TENANT_A) as connection:
            await connection.execute(
                text("INSERT INTO tenant (id, name) VALUES (:id, 'mine')"),
                {"id": uuid.uuid4()},
            )
    assert "permission denied" in str(caught.value).lower()


# --- Constraints ------------------------------------------------------------------------------


async def _insert(connection: AsyncConnection, statement: str, params: dict[str, Any]) -> None:
    await connection.execute(text(statement), params)


@pytest.mark.asyncio
async def test_a_duplicate_idempotency_key_is_rejected(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID]
) -> None:
    """`job_idem_uq`. The constraint, not the application, is what makes creation happen once."""
    existing = seeded[TENANT_A]
    with pytest.raises(IntegrityError):
        async with owner_scope(engine, TENANT_A) as connection:
            await _insert(
                connection,
                "INSERT INTO job (id, tenant_id, idempotency_key, request_fingerprint, prompt,"
                " trace_id, budget_caps) VALUES (:id, :tenant_id, :key, 'fp2', 'p', 't',"
                " '{}'::jsonb)",
                {"id": uuid.uuid4(), "tenant_id": TENANT_A, "key": f"key-{existing}"},
            )


@pytest.mark.asyncio
async def test_the_same_key_is_allowed_for_a_different_tenant(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID]
) -> None:
    """Scoped to the tenant. A global constraint would let one tenant deny keys to another."""
    borrowed = f"key-{seeded[TENANT_A]}"
    async with owner_scope(engine, TENANT_B) as connection:
        await _insert(
            connection,
            "INSERT INTO job (id, tenant_id, idempotency_key, request_fingerprint, prompt,"
            " trace_id, budget_caps) VALUES (:id, :tenant_id, :key, :fp, 'p', 't', '{}'::jsonb)",
            {
                "id": uuid.uuid4(),
                "tenant_id": TENANT_B,
                "key": borrowed,
                "fp": f"fp-{uuid.uuid4()}",
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("duration", "accepted"), [("39.99", False), ("40.01", False), ("40.00", True)]
)
async def test_total_duration_must_be_exactly_forty(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID], duration: str, accepted: bool
) -> None:
    statement = (
        "INSERT INTO story_plan (id, job_id, tenant_id, logline, total_duration_s, model_alias,"
        " prompt_version) VALUES (:id, :job_id, :tenant_id, 'l', :duration, 'a', 'v1')"
    )
    params = {
        "id": uuid.uuid4(),
        "job_id": seeded[TENANT_A],
        "tenant_id": TENANT_A,
        "duration": Decimal(duration),
    }
    if accepted:
        async with owner_scope(engine, TENANT_A) as connection:
            await _insert(connection, statement, params)
        return
    with pytest.raises(IntegrityError):
        async with owner_scope(engine, TENANT_A) as connection:
            await _insert(connection, statement, params)


@pytest.mark.asyncio
async def test_repairs_used_of_three_is_rejected(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID]
) -> None:
    """`[D-01]`: the database is the last line of defence for the repair cap."""
    with pytest.raises(IntegrityError):
        async with owner_scope(engine, TENANT_A) as connection:
            await _insert(
                connection,
                "INSERT INTO shot (id, job_id, tenant_id, beat_id, idx, repairs_used)"
                " VALUES (:id, :job_id, :tenant_id, :beat_id, 0, 3)",
                {
                    "id": uuid.uuid4(),
                    "job_id": seeded[TENANT_A],
                    "tenant_id": TENANT_A,
                    "beat_id": uuid.uuid4(),
                },
            )


@pytest.mark.asyncio
async def test_updating_the_bible_raises_the_documented_code(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID]
) -> None:
    """`VA-BIBLE-002`, raised by a row trigger so it fires for the owner too."""
    bible_id = uuid.uuid4()
    async with owner_scope(engine, TENANT_A) as connection:
        await _insert(
            connection,
            "INSERT INTO continuity_bible (id, job_id, tenant_id, character, wardrobe, location,"
            " lighting, palette, lens_language, content_hash, model_alias, prompt_version)"
            " VALUES (:id, :job_id, :tenant_id, '{}', '{}', '{}', '{}', '{}', '{}', 'h', 'a',"
            " 'v1')",
            {"id": bible_id, "job_id": seeded[TENANT_A], "tenant_id": TENANT_A},
        )

    with pytest.raises(DBAPIError) as caught:
        async with owner_scope(engine, TENANT_A) as connection:
            await connection.execute(
                text("UPDATE continuity_bible SET content_hash = 'x' WHERE id = :id"),
                {"id": bible_id},
            )
    assert "VA-BIBLE-002" in str(caught.value)


@pytest.mark.asyncio
async def test_a_null_seed_is_accepted_with_seed_supported_false(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID]
) -> None:
    """`[D-59]`: an honest NULL beats a fabricated value in the reproducibility record."""
    shot_id = await _make_shot(engine, seeded[TENANT_A])
    async with owner_scope(engine, TENANT_A) as connection:
        result = await connection.execute(
            text(
                "INSERT INTO shot_attempt (id, shot_id, job_id, tenant_id, attempt_no,"
                " prompt_text, prompt_hash, bible_hash, request_fingerprint)"
                " VALUES (:id, :shot_id, :job_id, :tenant_id, 1, 'p', 'h', 'b', :fp)"
                " RETURNING seed, seed_supported, cost_is_final, cost_usd"
            ),
            {
                "id": uuid.uuid4(),
                "shot_id": shot_id,
                "job_id": seeded[TENANT_A],
                "tenant_id": TENANT_A,
                "fp": f"fp-{uuid.uuid4()}",
            },
        )
        seed, seed_supported, cost_is_final, cost_usd = result.one()
    assert seed is None
    assert seed_supported is False
    assert cost_is_final is False
    assert cost_usd == Decimal("0")


async def _make_shot(engine: AsyncEngine, job_id: uuid.UUID) -> uuid.UUID:
    """A shot to hang attempts off, with the beat it references."""
    plan_id, beat_id, shot_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_scope(engine, TENANT_A) as connection:
        await connection.execute(
            text(
                "INSERT INTO story_plan (id, job_id, tenant_id, logline, total_duration_s,"
                " model_alias, prompt_version) VALUES (:id, :job_id, :tenant_id, 'l', 40.00,"
                " 'a', 'v1') ON CONFLICT (job_id) DO NOTHING"
            ),
            {"id": plan_id, "job_id": job_id, "tenant_id": TENANT_A},
        )
        found = await connection.execute(
            text("SELECT id FROM story_plan WHERE job_id = :job_id"), {"job_id": job_id}
        )
        plan_id = found.scalar_one()
        await connection.execute(
            text(
                "INSERT INTO beat (id, story_plan_id, tenant_id, idx, kind, action, camera_move,"
                " duration_s) VALUES (:id, :plan_id, :tenant_id,"
                " (SELECT coalesce(max(idx), -1) + 1 FROM beat WHERE story_plan_id = :plan_id),"
                " 'setup', 'a', 'pan', 10.00)"
            ),
            {"id": beat_id, "plan_id": plan_id, "tenant_id": TENANT_A},
        )
        await connection.execute(
            text(
                "INSERT INTO shot (id, job_id, tenant_id, beat_id, idx) VALUES"
                " (:id, :job_id, :tenant_id, :beat_id,"
                " (SELECT coalesce(max(idx), -1) + 1 FROM shot WHERE job_id = :job_id))"
            ),
            {"id": shot_id, "job_id": job_id, "tenant_id": TENANT_A, "beat_id": beat_id},
        )
    return shot_id


# --- The anti-double-bill path, end to end -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_duplicate_request_fingerprint_is_rejected(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID]
) -> None:
    """`[D-24]`: the constraint that makes a redelivered queue entry safe."""
    shot_id = await _make_shot(engine, seeded[TENANT_A])
    fingerprint = f"fp-{uuid.uuid4()}"
    async with owner_scope(engine, TENANT_A) as connection:
        await _insert_attempt(connection, shot_id, seeded[TENANT_A], 1, fingerprint)
    with pytest.raises(IntegrityError):
        async with owner_scope(engine, TENANT_A) as connection:
            await _insert_attempt(connection, shot_id, seeded[TENANT_A], 2, fingerprint)


async def _insert_attempt(
    connection: AsyncConnection,
    shot_id: uuid.UUID,
    job_id: uuid.UUID,
    attempt_no: int,
    fingerprint: str,
) -> None:
    await connection.execute(
        text(
            "INSERT INTO shot_attempt (id, shot_id, job_id, tenant_id, attempt_no, prompt_text,"
            " prompt_hash, bible_hash, request_fingerprint) VALUES (:id, :shot_id, :job_id,"
            " :tenant_id, :attempt_no, 'p', 'h', 'b', :fp)"
        ),
        {
            "id": uuid.uuid4(),
            "shot_id": shot_id,
            "job_id": job_id,
            "tenant_id": TENANT_A,
            "attempt_no": attempt_no,
            "fp": fingerprint,
        },
    )


@pytest.mark.asyncio
async def test_a_crashed_attempt_is_adopted_rather_than_resubmitted(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID]
) -> None:
    """The crash case `[D-67]` and `[D-24]` exist for, not the happy path.

    The first delivery writes the in-flight row, records the provider render id, and then the
    worker dies. The redelivery claims the same fingerprint, gets `adopted=True` and the same
    row back — carrying the `provider_project_id` the reconciliation re-reads. Exactly one
    attempt row exists at the end, which is exactly one paid render.
    """
    shot_id = await _make_shot(engine, seeded[TENANT_A])
    fingerprint = f"fp-{uuid.uuid4()}"
    request = AttemptRequest(
        shot_id=shot_id,
        job_id=seeded[TENANT_A],
        attempt_no=1,
        request_fingerprint=fingerprint,
        prompt_text="a cat, wide",
        prompt_hash="h1",
        bible_hash="h2",
    )

    async with app_scope(engine, TENANT_A) as connection:
        repository = ShotAttemptRepository(TenantSession(connection, TENANT_A))
        first = await repository.claim(request)
        assert first.adopted is False
        await repository.record_submission(
            first.attempt.id,
            ProviderSubmission(
                provider_project_id="mh-project-42",
                provider_key="k",
                provider_model="m",
                seed=None,
                seed_supported=False,
            ),
        )

    # The worker dies here. The queue redelivers the same step.
    async with app_scope(engine, TENANT_A) as connection:
        repository = ShotAttemptRepository(TenantSession(connection, TENANT_A))
        second = await repository.claim(request)
        assert second.adopted is True
        assert second.attempt.id == first.attempt.id
        assert second.attempt.provider_project_id == "mh-project-42"
        assert second.attempt.state is AttemptState.IN_FLIGHT

        total = await connection.execute(
            text("SELECT count(*) FROM shot_attempt WHERE request_fingerprint = :fp"),
            {"fp": fingerprint},
        )
        assert total.scalar_one() == 1


@pytest.mark.asyncio
async def test_cost_is_settled_once_and_marked_final(
    engine: AsyncEngine, seeded: dict[uuid.UUID, uuid.UUID]
) -> None:
    """`[D-60]`: provisional until terminal, then final exactly once."""
    shot_id = await _make_shot(engine, seeded[TENANT_A])
    async with app_scope(engine, TENANT_A) as connection:
        repository = ShotAttemptRepository(TenantSession(connection, TENANT_A))
        claim = await repository.claim(
            AttemptRequest(
                shot_id=shot_id,
                job_id=seeded[TENANT_A],
                attempt_no=1,
                request_fingerprint=f"fp-{uuid.uuid4()}",
                prompt_text="p",
                prompt_hash="h",
                bible_hash="b",
            )
        )
        assert claim.attempt.cost_is_final is False
        settled = await repository.settle_cost(
            claim.attempt.id,
            CostSettlement(
                state=AttemptState.SUCCEEDED,
                cost_usd=Decimal("1.2500"),
                credits_charged=Decimal("1388.8889"),
            ),
        )
    assert settled.cost_is_final is True
    assert settled.cost_usd == Decimal("1.2500")


# --- Repositories through RLS ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded")
async def test_the_job_repository_round_trips_under_rls(engine: AsyncEngine) -> None:
    """S0.5.8's round trip: created, read back, and invisible to the other tenant."""
    async with app_scope(engine, TENANT_A) as connection:
        repository = JobRepository(TenantSession(connection, TENANT_A))
        created = await repository.create(
            NewJob(
                idempotency_key=f"key-{uuid.uuid4()}",
                request_fingerprint=f"fp-{uuid.uuid4()}",
                prompt="a cat",
                trace_id="trace",
                budget_caps={"usd": "5.00"},
            )
        )
        assert created.created is True
        assert (await repository.get(created.id)) is not None

    async with app_scope(engine, TENANT_B) as connection:
        other = JobRepository(TenantSession(connection, TENANT_B))
        assert await other.get(created.id) is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded")
async def test_the_repository_stamps_the_session_tenant_on_the_stored_row(
    engine: AsyncEngine,
) -> None:
    """`tenant_id` reaches the row from the session, and `WITH CHECK` agrees with it."""
    async with app_scope(engine, TENANT_B) as connection:
        repository = JobRepository(TenantSession(connection, TENANT_B))
        created = await repository.create(
            NewJob(
                idempotency_key=f"key-{uuid.uuid4()}",
                request_fingerprint=f"fp-{uuid.uuid4()}",
                prompt="a dog",
                trace_id="trace",
                budget_caps={},
            )
        )
        assert created.tenant_id == TENANT_B


# --- Model / live schema drift ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("table", TABLE_NAMES)
async def test_declared_columns_match_the_live_schema(engine: AsyncEngine, table: str) -> None:
    """S0.5.8 acceptance 1: the `MetaData` and the migrated database describe the same tables."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        )
        live = {row[0]: row[1] == "YES" for row in result}

    declared = {column.name: column.nullable for column in metadata.tables[table].columns}
    assert live == declared


@pytest.mark.asyncio
async def test_no_bytea_column_exists_in_the_live_schema(engine: AsyncEngine) -> None:
    """The rule holds against what was actually created, not only against the model."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND data_type = 'bytea'"
            )
        )
        assert list(result) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.usefixtures("seeded")
async def test_the_full_verb_matrix_isolates_tenant_a_from_tenant_b(
    engine: AsyncEngine, verb: str
) -> None:
    """`persistence.md` §10's matrix, spelled out one verb at a time so a failure names it."""
    async with app_scope(engine, TENANT_B) as connection:
        if verb == "select":
            result = await connection.execute(
                text("SELECT count(*) FROM job WHERE tenant_id = :other"), {"other": TENANT_A}
            )
            assert result.scalar_one() == 0
        elif verb == "insert":
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO job (id, tenant_id, idempotency_key, request_fingerprint,"
                        " prompt, trace_id, budget_caps) VALUES (:id, :other, 'k2', 'f2', 'p',"
                        " 't', '{}'::jsonb)"
                    ),
                    {"id": uuid.uuid4(), "other": TENANT_A},
                )
        elif verb == "update":
            updated = await connection.execute(
                text("UPDATE job SET degraded = true WHERE tenant_id = :other"),
                {"other": TENANT_A},
            )
            assert updated.rowcount == 0
        else:
            deleted = await connection.execute(
                text("DELETE FROM job WHERE tenant_id = :other"), {"other": TENANT_A}
            )
            assert deleted.rowcount == 0
