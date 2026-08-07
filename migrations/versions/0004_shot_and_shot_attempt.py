"""shot and shot_attempt — the repair cap and the anti-double-bill constraint.

Phase: expand

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-08

Two constraints on this revision are load-bearing for money rather than for correctness.

`UNIQUE (request_fingerprint)` is what makes at-least-once queue delivery safe `[D-67]`. A
redelivered step recomputes the same fingerprint, fails to insert, adopts the existing attempt
and re-reads the render named in `provider_project_id` — instead of submitting a second paid
one `[D-24]`. Without this constraint every worker crash is a duplicate charge.

`repairs_used <= 2` and `attempt_no BETWEEN 1 AND 3` are the repair cap `[D-01]`, expressed
where it cannot be refactored away.

`seed` is nullable and `seed_supported` defaults false `[D-59]`. A NOT NULL seed would force a
fabricated value for providers that expose none, and the delivered reproducibility record
would then claim a guarantee that does not exist. The pair says "no seed, and that is a fact
about the provider" rather than leaving a NULL to be interpreted.
"""

from __future__ import annotations

from alembic import op

from video_agent.persistence.ddl import (
    create_table_statements,
    drop_table_statements,
    grant_application_role_statements,
)

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

TABLES = ("shot", "shot_attempt")


def upgrade() -> None:
    """Create the shot and attempt tables."""
    for statement in create_table_statements(*TABLES):
        op.execute(statement)
    for statement in grant_application_role_statements(*TABLES):
        op.execute(statement)


def downgrade() -> None:
    """Drop them, children first."""
    for statement in drop_table_statements(*reversed(TABLES)):
        op.execute(statement)
