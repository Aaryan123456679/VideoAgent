"""Row-level security: enabled, FORCEd and policed on every table but the documented two.

Phase: expand

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-08

This is the tenant isolation boundary. `persistence.md` §10 makes it the highest-priority test
surface in the repository and `AGENT.md` §4 makes it a merge gate.

The table list is not written here. It is `rls.RLS_PROTECTED_TABLES`, derived from the schema
by removing the two documented exemptions, so a table added to `schema.py` is protected by
this migration without anyone remembering to add it — and a table that is *not* protected can
only be so because someone edited the exemption list, which is itself asserted.

`FORCE` is separate from `ENABLE` and both are emitted. Without `FORCE` the policy does not
apply to the table owner, which is the role every migration and every operator session runs
as, and a test that happens to connect as the owner would report an isolation that is not
there.

The predicate is `NULLIF(current_setting('app.tenant_id', true), '')::uuid` rather than the
one-argument `current_setting('app.tenant_id')::uuid` written in `persistence.md` §3. The
one-argument form *raises* when the setting is absent; §9 of the same document requires the
opposite — "zero rows, not an error, plus an alarm" — and an isolation boundary that throws on
a missing session variable turns a leak-safe default into an outage. The `NULLIF` covers the
empty string, which `''::uuid` would raise on just as loudly. The doc's §3 code block is wrong
and is reported with this run.
"""

from __future__ import annotations

from alembic import op

from video_agent.persistence.rls import (
    RLS_PROTECTED_TABLES,
    disable_rls_statements,
    enable_rls_statements,
)

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Enable, force and police every protected table."""
    for table_name in RLS_PROTECTED_TABLES:
        for statement in enable_rls_statements(table_name):
            op.execute(statement)


def downgrade() -> None:
    """Remove the policies. Only ever run to rebuild the schema from scratch."""
    for table_name in reversed(RLS_PROTECTED_TABLES):
        for statement in disable_rls_statements(table_name):
            op.execute(statement)
