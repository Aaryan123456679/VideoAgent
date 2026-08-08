"""Alembic environment: async online, plain-text offline, lock budget on both paths.

Offline mode matters more here than it usually does. `alembic upgrade head --sql` renders the
whole upgrade path to text **without connecting to anything**, which is what lets
`migration_lint` check expand/contract in CI on a machine with no database — and check it
against the SQL that will actually run, rather than against the Python that produces it. A
lint that reads the migration source can be defeated by moving the DDL into a helper; one that
reads the emitted SQL cannot.

The URL comes from `Settings.DATABASE_URL` and never from `alembic.ini`. A connection string
is a credential `[CPS §Observability]`, and a second place to configure the database is a
second database a migration can be applied to by accident.

The lock budget is emitted on both paths, first, before any DDL. `persistence.md` §9 requires
a migration that exceeds its lock budget to be *aborted*, and `lock_timeout` is what aborts
it — the alternative is a statement that waits behind an open transaction while every query
issued after it queues behind the lock it is waiting for.
"""

from __future__ import annotations

import asyncio
from configparser import NoOptionError, NoSectionError

from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from video_agent.config.settings import Settings
from video_agent.persistence.schema import metadata

config = context.config
target_metadata = metadata

DEFAULT_LOCK_TIMEOUT_MS = 5000
DEFAULT_STATEMENT_TIMEOUT_MS = 300_000


def _migration_option(name: str, fallback: int) -> int:
    """Read one `[migration]` setting from `alembic.ini`, falling back to a safe default."""
    try:
        return int(config.get_section_option("migration", name) or fallback)
    except (NoSectionError, NoOptionError, ValueError):
        return fallback


def lock_budget_statements() -> tuple[str, ...]:
    """The two timeouts, as SQL, in the order they must be emitted.

    `lock_timeout` before `statement_timeout`: the first bounds how long a statement waits to
    *start*, the second how long it may run once it has. A migration blocked on a lock is
    invisible to `statement_timeout`, which is why setting only the second is the common and
    useless configuration.
    """
    lock_ms = _migration_option("lock_timeout_ms", DEFAULT_LOCK_TIMEOUT_MS)
    statement_ms = _migration_option("statement_timeout_ms", DEFAULT_STATEMENT_TIMEOUT_MS)
    return (
        f"SET lock_timeout = '{lock_ms}ms'",
        f"SET statement_timeout = '{statement_ms}ms'",
    )


def database_url() -> str:
    """The URL every migration runs against: the application's own configured database.

    Unwrapped from `SecretStr` here because Alembic's config takes a plain string. The
    unwrapping is at the point of use, and the result is handed to `context.configure` and to
    `async_engine_from_config` without being logged or interpolated on the way.
    """
    return Settings().DATABASE_URL.get_secret_value()


def run_migrations_offline() -> None:
    """Render the migrations to SQL without a database connection."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        for statement in lock_budget_statements():
            context.execute(statement)
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    for statement in lock_budget_statements():
        connection.execute(text(statement))
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply the migrations over the async engine the application itself uses."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
