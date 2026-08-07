"""artifact and checkpoint — metadata and graph state, never bytes.

Phase: expand

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-08

Neither table holds media. `artifact` holds a tenant-prefixed `storage_key` and a
`checksum_sha256`, which together are what make byte-identity assertable `[PRD §Resilience]`;
the bytes are in the object store and the bucket policy is a second isolation layer behind RLS
`[persistence.md §6]`. `checkpoint.state` holds `JobState` and is asserted at write time to
contain no media payload and no URL-shaped string `[persistence.md §7]`.

There is no `BYTEA` column anywhere in this schema and
`test_schema_definition.py::test_no_bytea_columns_anywhere` keeps it that way. The rule is
worth a test rather than a convention because it is broken one thumbnail at a time.

`checkpoint.thread_id` equals `job.id` but is not a foreign key: LangGraph owns the thread
identifier and writes the checkpoint, and an FK would make the checkpointer's write ordering
depend on the domain row already existing — which `[D-23]`'s same-transaction requirement does
not guarantee for the first node.
"""

from __future__ import annotations

from alembic import op

from video_agent.persistence.ddl import (
    create_table_statements,
    drop_table_statements,
    grant_application_role_statements,
)

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

TABLES = ("artifact", "checkpoint")


def upgrade() -> None:
    """Create the artifact and checkpoint tables."""
    for statement in create_table_statements(*TABLES):
        op.execute(statement)
    for statement in grant_application_role_statements(*TABLES):
        op.execute(statement)


def downgrade() -> None:
    """Drop them."""
    for statement in drop_table_statements(*reversed(TABLES)):
        op.execute(statement)
