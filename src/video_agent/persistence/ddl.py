"""DDL compiled from `schema.py`, so a migration never re-types the schema by hand.

A migration that contains its own `CREATE TABLE` and a model that claims to match it are two
descriptions of one thing, and the interesting bug is the one where they stop agreeing. Here
the migration asks for the DDL of a named table and gets it compiled from the same `Table`
object the repositories use, which removes the disagreement rather than testing for it.

What is *not* compiled is everything SQLAlchemy has no concept of: the enum types, the
immutability trigger, the RLS statements and the application role's grants. Those are written
out once, here, in the one module a reviewer has to read to know what the database actually
enforces.
"""

from __future__ import annotations

from sqlalchemy import Table, create_mock_engine
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.schema import CreateIndex, CreateTable, DropTable

from video_agent.persistence.enums import (
    ENUM_TYPES,
    create_type_statement,
    drop_type_statement,
)
from video_agent.persistence.rls import APPLICATION_ROLE
from video_agent.persistence.schema import ALL_TABLES, table_by_name


def postgres_dialect() -> Dialect:
    """A PostgreSQL dialect that never connects to anything.

    `create_mock_engine` builds the dialect and a no-op executor, which is exactly what DDL
    compilation needs — the alternative spellings instantiate the dialect class directly, and
    those constructors are unannotated, which a strict type check is right to object to.
    """
    return create_mock_engine(_MOCK_URL, executor=_no_executor).dialect


_MOCK_URL = "postgresql+asyncpg://"


def _no_executor(*_args: object, **_kwargs: object) -> None:
    """Compilation never executes; the mock engine still requires something to hand it to."""


_DIALECT = postgres_dialect()


def _compile(element: CreateTable | DropTable | CreateIndex) -> str:
    return str(element.compile(dialect=_DIALECT)).strip()


def create_type_statements() -> tuple[str, ...]:
    """`CREATE TYPE` for all six enums, in declaration order."""
    return tuple(create_type_statement(name) for name in ENUM_TYPES)


def drop_type_statements() -> tuple[str, ...]:
    """`DROP TYPE` for all six enums, in reverse declaration order."""
    return tuple(drop_type_statement(name) for name in reversed(list(ENUM_TYPES)))


def create_table_statements(*table_names: str) -> tuple[str, ...]:
    """`CREATE TABLE` plus every index declared on the named tables, in the given order.

    Indexes follow their table in the same revision, which is why they are allowed to be
    plain `CREATE INDEX` rather than `CONCURRENTLY`: the table is empty and nothing holds a
    lock on it that anyone could be blocked behind. `migration_lint` encodes exactly that
    exemption and nothing wider.
    """
    statements: list[str] = []
    for name in table_names:
        table = table_by_name(name)
        statements.append(_compile(CreateTable(table)))
        indexes = sorted(table.indexes, key=lambda index: index.name or "")
        statements.extend(_compile(CreateIndex(index)) for index in indexes)
    return tuple(statements)


def drop_table_statements(*table_names: str) -> tuple[str, ...]:
    """`DROP TABLE` for the named tables, in the order given.

    Indexes are not dropped separately: dropping the table takes them with it, and a
    downgrade that drops an index that no longer exists fails on the second attempt.
    """
    return tuple(_compile(DropTable(table_by_name(name))) for name in table_names)


# --- The continuity bible immutability trigger --------------------------------------------
#
# Written as single-line statements on purpose. A dollar-quoted body spanning source lines
# would be one long multi-line string literal, and the repository's inline-prompt guard reads
# any such literal as a prompt somebody pasted into code.

CREATE_BIBLE_TRIGGER_FUNCTION = (
    "CREATE OR REPLACE FUNCTION reject_bible_update() RETURNS trigger AS $$ "
    "BEGIN RAISE EXCEPTION 'VA-BIBLE-002: continuity_bible is immutable (job %)', "
    "OLD.job_id USING ERRCODE = 'raise_exception'; "
    "END $$ LANGUAGE plpgsql"
)
"""The trigger body. `VA-BIBLE-002` is in the message so the code survives into the driver's
exception text, which is the only thing an application layer reliably gets to see."""

CREATE_BIBLE_TRIGGER = (
    "CREATE TRIGGER continuity_bible_immutable BEFORE UPDATE ON continuity_bible "
    "FOR EACH ROW EXECUTE FUNCTION reject_bible_update()"
)
"""A row trigger, not a permission.

`[PRD §How it works 2]` makes the bible immutable for the life of the job, and a `REVOKE` on
`UPDATE` would exempt the owner and every superuser — which is to say, every migration, every
support session and every test that forgot to switch roles. A `BEFORE UPDATE ... FOR EACH ROW`
trigger fires for all of them."""

DROP_BIBLE_TRIGGER = "DROP TRIGGER continuity_bible_immutable ON continuity_bible"
DROP_BIBLE_TRIGGER_FUNCTION = "DROP FUNCTION reject_bible_update()"


# --- The application role -----------------------------------------------------------------


def create_application_role_statements() -> tuple[str, ...]:
    """Create the non-owner application role and grant it exactly DML.

    `NOLOGIN` and no password: a migration owns privileges and must never carry a secret, so
    the deployment grants `LOGIN` against the secret store. `NOBYPASSRLS` is stated rather
    than assumed — it is the default, but it is also the single attribute whose accidental
    presence turns every policy in this schema into decoration.

    No `CREATE` on the schema and no ownership, so the role cannot make itself a table that
    has no policy. `[persistence.md §3]`
    """
    return (
        _create_role_if_absent(),
        f"ALTER ROLE {APPLICATION_ROLE} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS",
        f"GRANT USAGE ON SCHEMA public TO {APPLICATION_ROLE}",
    )


def _create_role_if_absent() -> str:
    """`CREATE ROLE` has no `IF NOT EXISTS`, so guard it.

    `to_regrole` rather than a lookup in `pg_roles`: the role may already exist because a
    previous deploy created it, and a migration whose first statement fails on a re-run is a
    migration that cannot be re-run.
    """
    return (
        "DO $$ BEGIN "
        f"IF to_regrole('{APPLICATION_ROLE}') IS NULL THEN "
        f"CREATE ROLE {APPLICATION_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS; "
        "END IF; END $$"
    )


def drop_application_role_statements() -> tuple[str, ...]:
    """Revoke and drop the application role, for a downgrade.

    The role is dropped last and its grants revoked first, because PostgreSQL refuses to drop
    a role that still owns a privilege — which is a downgrade that fails halfway, which is the
    thing expand/contract exists to make impossible.
    """
    return (
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APPLICATION_ROLE}",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APPLICATION_ROLE}",
        f"REVOKE USAGE ON SCHEMA public FROM {APPLICATION_ROLE}",
        f"DROP ROLE IF EXISTS {APPLICATION_ROLE}",
    )


def grant_application_role_statements(*table_names: str) -> tuple[str, ...]:
    """Grant DML on tables created after the role existed.

    `GRANT ... ON ALL TABLES` is a snapshot taken when the statement runs, not a standing rule,
    so a revision that grants before it creates grants on nothing and every later revision that
    adds a table has to say this again. A table nobody grants on is a table the application
    gets a permission error from, which at least fails loudly — unlike the reverse mistake.

    The sequence grant is repeated for the same reason: `BIGSERIAL` creates its sequence with
    the table, and a role that may insert into a table whose sequence it may not use gets a
    permission error on the insert.
    """
    grants = [
        f"GRANT {APPLICATION_PRIVILEGES.get(name, FULL_DML)} ON {name} TO {APPLICATION_ROLE}"
        for name in table_names
    ]
    return (
        *grants,
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APPLICATION_ROLE}",
    )


FULL_DML = "SELECT, INSERT, UPDATE, DELETE"

APPLICATION_PRIVILEGES: dict[str, str] = {
    # The admin path and the migration role write tenants; the application only ever reads
    # one, to check `disabled_at` at key resolution [D-70]. The table is RLS-exempt, so an
    # application role that could write it could write any tenant's row.
    "tenant": "SELECT",
    # Read to resolve a key, update to stamp `last_used_at`. No INSERT and no DELETE:
    # issuing and revoking keys is the admin path's job, and this table is the other
    # RLS-exempt one [D-68].
    "tenant_api_key": "SELECT, UPDATE",
}
"""Where the application role gets less than full DML, and why.

Least privilege matters most on exactly the two tables RLS does not protect. Everywhere else
the policy is the boundary and DML is fine; on these two the grant *is* the boundary.
"""


ALL_TABLE_NAMES_IN_CREATE_ORDER: tuple[str, ...] = tuple(table.name for table in ALL_TABLES)


def bytea_columns() -> tuple[str, ...]:
    """Every `BYTEA` column in the schema. Must be empty: Postgres never holds media bytes.

    `[CPS §Observability]` and `persistence.md` §6 — bytes live in the object store and the
    database holds a key and a checksum. A `BYTEA` column is how that stops being true, one
    "just this one thumbnail" at a time.
    """
    found: list[str] = []
    for table in ALL_TABLES:
        found.extend(_bytea_columns_of(table))
    return tuple(found)


def _bytea_columns_of(table: Table) -> list[str]:
    names: list[str] = []
    for column in table.columns:
        rendered = column.type.compile(dialect=_DIALECT).upper()
        if "BYTEA" in rendered or "BLOB" in rendered:
            names.append(f"{table.name}.{column.name}")
    return names
