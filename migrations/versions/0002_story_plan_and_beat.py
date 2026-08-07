"""story_plan and beat, with the duration CHECKs that make 40 seconds a fact.

Phase: expand

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-08

`total_duration_s = 40.00` and `duration_s = 10.00` are equalities, not ranges. `[D-03]` fixes
the shot length at ten seconds and the PRD fixes the film at forty, so a 9.99-second beat is
not a near miss — it is four of them adding up to a video that is not the length the product
promises. The application checks first; this is where it stops if the application's check is
refactored away.

`beat.idx BETWEEN 0 AND 3` plus `UNIQUE (story_plan_id, idx)` is the four-beat structure
expressed as constraints rather than as a comment.
"""

from __future__ import annotations

from alembic import op

from video_agent.persistence.ddl import (
    create_table_statements,
    drop_table_statements,
    grant_application_role_statements,
)

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

TABLES = ("story_plan", "beat")


def upgrade() -> None:
    """Create the plan and beat tables."""
    for statement in create_table_statements(*TABLES):
        op.execute(statement)
    for statement in grant_application_role_statements(*TABLES):
        op.execute(statement)


def downgrade() -> None:
    """Drop them, children first."""
    for statement in drop_table_statements(*reversed(TABLES)):
        op.execute(statement)
