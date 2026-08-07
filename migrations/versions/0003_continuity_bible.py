"""continuity_bible and the trigger that makes it immutable.

Phase: expand

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-08

`[PRD §How it works 2]` makes the bible immutable for the life of the job, and `AGENT.md` §8
says the database enforces it and the trigger is not to be removed. It is a `BEFORE UPDATE ...
FOR EACH ROW` trigger rather than a revoked privilege on purpose: a `REVOKE` exempts the table
owner and every superuser, which is every migration, every support session and every test that
forgot to switch role. A row trigger fires for all of them, which is what
`test_persistence_postgres.py::test_update_as_owner_also_raises` asserts.

The exception message carries `VA-BIBLE-002` because the driver's exception text is the only
part of a database error an application layer reliably gets to read.

`DELETE` is deliberately not blocked. The bible goes when the job goes, via
`ON DELETE CASCADE`, and a trigger that blocked deletes would make a job undeletable — the
application simply never issues a direct one. `[persistence.md §2]`
"""

from __future__ import annotations

from alembic import op

from video_agent.persistence.ddl import (
    CREATE_BIBLE_TRIGGER,
    CREATE_BIBLE_TRIGGER_FUNCTION,
    DROP_BIBLE_TRIGGER,
    DROP_BIBLE_TRIGGER_FUNCTION,
    create_table_statements,
    drop_table_statements,
    grant_application_role_statements,
)

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

TABLES = ("continuity_bible",)


def upgrade() -> None:
    """Create the table, then the function, then the trigger that uses it."""
    for statement in create_table_statements(*TABLES):
        op.execute(statement)
    for statement in grant_application_role_statements(*TABLES):
        op.execute(statement)
    op.execute(CREATE_BIBLE_TRIGGER_FUNCTION)
    op.execute(CREATE_BIBLE_TRIGGER)


def downgrade() -> None:
    """Remove the trigger, its function, and the table."""
    op.execute(DROP_BIBLE_TRIGGER)
    op.execute(DROP_BIBLE_TRIGGER_FUNCTION)
    for statement in drop_table_statements(*TABLES):
        op.execute(statement)
