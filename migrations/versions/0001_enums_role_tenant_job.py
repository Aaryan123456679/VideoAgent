"""Enum types, the application role, tenant, tenant_api_key and job.

Phase: expand

Revision ID: 0001
Revises:
Create Date: 2026-08-08

The first revision, and the only one that creates something other than tables: the six enum
types every later revision references, and the non-superuser application role that RLS is
worth anything against.

`tenant` resolves what `persistence.md` §2 previously left dangling — every table carried
`tenant_id NOT NULL` and no table defined what it referenced `[D-70]`. `tenant_api_key` is the
table `[D-68]`'s key resolution reads, and is one of exactly two tables that will not carry an
RLS policy in `0006`, because it is read by the unauthenticated request that is trying to work
out which tenant is calling.

The DDL is compiled from `video_agent.persistence.schema`, not written out here. A migration
with its own copy of the schema is a second description of the tables, and the failure mode of
two descriptions is that they stop agreeing quietly.
"""

from __future__ import annotations

from alembic import op

from video_agent.persistence.ddl import (
    create_application_role_statements,
    create_table_statements,
    create_type_statements,
    drop_application_role_statements,
    drop_table_statements,
    drop_type_statements,
    grant_application_role_statements,
)

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

TABLES = ("tenant", "tenant_api_key", "job")


def upgrade() -> None:
    """Create the enum types, the application role, and the first three tables."""
    for statement in create_type_statements():
        op.execute(statement)
    for statement in create_application_role_statements():
        op.execute(statement)
    for statement in create_table_statements(*TABLES):
        op.execute(statement)
    for statement in grant_application_role_statements(*TABLES):
        op.execute(statement)


def downgrade() -> None:
    """Drop the tables, the role and the types, children first."""
    for statement in drop_table_statements(*reversed(TABLES)):
        op.execute(statement)
    for statement in drop_application_role_statements():
        op.execute(statement)
    for statement in drop_type_statements():
        op.execute(statement)
