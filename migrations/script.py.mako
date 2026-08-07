"""${message}

Phase: expand

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

The `Phase:` field above is REQUIRED and is parsed, not read. `migration_lint.parse_phase`
raises if it is missing or is not one of `expand`, `migrate`, `contract`, and the rules
applied to this revision's SQL depend on which one it says. The template writes `expand`
because that is what a new revision almost always is — change it deliberately, and know that:

  expand    Add nullable columns, new tables, new enum values. Backfill in batches.
            Deployable alongside the old code. May not contain any DROP.
  migrate   New code reads and writes the new shape; the old shape is still populated.
            Deployable alongside the old code. May not contain any DROP.
  contract  Drop the old column, constraint or table. A SEPARATE deploy, after the new code
            is fully rolled out. May contain NOTHING BUT drops.

Hard rules, all of them checked by `tests/unit/test_migration_sql.py` against the SQL this
revision actually emits:

  * never add a NOT NULL column without a DEFAULT in one step
  * never rename in place — add, dual-write, backfill, then drop in a contract revision
  * never drop in the same release that stopped writing the column
  * CREATE INDEX must be CONCURRENTLY unless the table is created in this same revision
  * ADD CONSTRAINT of a CHECK or FOREIGN KEY on an existing table must be NOT VALID,
    followed by a separate VALIDATE CONSTRAINT

Every revision needs a tested rollback: `downgrade()` is not optional and is exercised by
`alembic downgrade base` in CI. `[CPS §Rollout]`, `AGENT.md` §4
"""

from __future__ import annotations

from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "raise NotImplementedError"}


def downgrade() -> None:
    """Roll this revision back. Required, and tested."""
    ${downgrades if downgrades else "raise NotImplementedError"}
