"""S0.5.1 — the expand/contract lint, exercised against SQL that breaks each rule.

Every rule gets a pair: SQL that violates it and SQL that does not. A lint tested only on
compliant input is a function nobody has watched say "no", and the rule most likely to be
quietly broken is the one whose check was never seen to fire.

The violating SQL here is written out literally rather than generated, so these tests keep
their meaning if the emitters in `ddl.py` change.
"""

from __future__ import annotations

import pytest

from video_agent.persistence.migration_lint import (
    LintFinding,
    MigrationLintError,
    Phase,
    lint_migration_script,
    lint_statements,
    parse_phase,
    require_lock_budget,
    split_revisions,
    split_statements,
)

TWO_STATEMENTS = 2
"""Named so the split assertions read as "both halves survived" rather than as a magic 2."""


def _rules(findings: list[LintFinding]) -> set[str]:
    return {finding.rule for finding in findings}


def _lint(sql: str, phase: Phase = Phase.EXPAND) -> set[str]:
    return _rules(lint_statements("test", phase, split_statements(sql)))


# --- The required phase declaration --------------------------------------------------------


@pytest.mark.parametrize(
    ("docstring", "expected"),
    [
        ("Add a column.\n\nPhase: expand\n", Phase.EXPAND),
        ("Backfill.\n\nPhase: migrate\n", Phase.MIGRATE),
        ("Drop the old column.\n\nphase:  contract  \n", Phase.CONTRACT),
    ],
)
def test_phase_is_read_from_the_docstring(docstring: str, expected: Phase) -> None:
    assert parse_phase(docstring) is expected


def test_missing_phase_field_is_an_error_not_a_default() -> None:
    """Defaulting would grant the loosest rules to the author who forgot the field."""
    with pytest.raises(MigrationLintError, match="no `Phase:` field"):
        parse_phase("Add a column.\n\nRevision ID: 0007\n")


def test_absent_docstring_is_an_error() -> None:
    with pytest.raises(MigrationLintError, match="no docstring"):
        parse_phase(None)


def test_unknown_phase_value_is_an_error() -> None:
    with pytest.raises(MigrationLintError, match="not one of"):
        parse_phase("Phase: cleanup\n")


# --- Statement splitting -------------------------------------------------------------------


def test_statements_split_on_semicolons() -> None:
    assert split_statements("CREATE TABLE a (id INT); CREATE TABLE b (id INT);") == [
        "CREATE TABLE a (id INT)",
        "CREATE TABLE b (id INT)",
    ]


def test_dollar_quoted_bodies_are_not_split() -> None:
    """The immutability trigger's body contains two semicolons; splitting it loses the RAISE."""
    sql = (
        "CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'no'; END $$ "
        "LANGUAGE plpgsql; CREATE TABLE a (id INT);"
    )
    statements = split_statements(sql)
    assert len(statements) == TWO_STATEMENTS
    assert "RAISE EXCEPTION" in statements[0]
    assert statements[0].endswith("LANGUAGE plpgsql")


def test_semicolons_inside_string_literals_are_not_split_points() -> None:
    statements = split_statements("INSERT INTO a VALUES ('x; y'); SELECT 1;")
    assert len(statements) == TWO_STATEMENTS
    assert "'x; y'" in statements[0]


def test_comments_are_stripped_from_statements() -> None:
    statements = split_statements("-- a note\nCREATE TABLE a (id INT); /* block */ SELECT 1;")
    assert statements == ["CREATE TABLE a (id INT)", "SELECT 1"]


def test_revisions_split_on_the_alembic_banner() -> None:
    sql = (
        "-- Running upgrade  -> 0001\nCREATE TABLE a (id INT);\n"
        "-- Running upgrade 0001 -> 0002\nCREATE TABLE b (id INT);\n"
    )
    sections = split_revisions(sql)
    assert [revision for revision, _ in sections] == ["0001", "0002"]
    assert "CREATE TABLE b" in sections[1][1]


# --- Rule: never add a NOT NULL column without a default -----------------------------------


def test_not_null_column_without_default_is_rejected() -> None:
    """Every existing row fails the constraint the moment the statement runs."""
    assert "not-null-without-default" in _lint("ALTER TABLE job ADD COLUMN owner TEXT NOT NULL;")


def test_not_null_column_with_default_is_accepted() -> None:
    assert _lint("ALTER TABLE job ADD COLUMN owner TEXT NOT NULL DEFAULT '';") == set()


def test_nullable_column_is_accepted() -> None:
    assert _lint("ALTER TABLE job ADD COLUMN owner TEXT;") == set()


def test_set_not_null_on_an_existing_column_is_rejected() -> None:
    """`SET NOT NULL` rewrites and exclusively locks the table."""
    assert "not-null-without-default" in _lint("ALTER TABLE job ALTER COLUMN owner SET NOT NULL;")


# --- Rule: never rename in place -----------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE job RENAME COLUMN prompt TO request_prompt;",
        "ALTER TABLE job RENAME TO job_v2;",
    ],
)
def test_rename_in_place_is_rejected(sql: str) -> None:
    """Old code and new schema cannot both be live across a rename."""
    assert "rename-in-place" in _lint(sql)


def test_adding_the_new_name_alongside_the_old_is_accepted() -> None:
    assert _lint("ALTER TABLE job ADD COLUMN request_prompt TEXT;") == set()


# --- Rule: never drop outside a contract revision ------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE job DROP COLUMN prompt;",
        "DROP TABLE job;",
        "ALTER TABLE job DROP CONSTRAINT job_idem_uq;",
        "DROP INDEX job_status_idx;",
    ],
)
@pytest.mark.parametrize("phase", [Phase.EXPAND, Phase.MIGRATE])
def test_drop_in_a_non_contract_revision_is_rejected(sql: str, phase: Phase) -> None:
    """Dropping in the release that stopped writing makes the rollback lossy."""
    assert "drop-outside-contract" in _lint(sql, phase)


def test_drop_in_a_contract_revision_is_accepted() -> None:
    assert _lint("ALTER TABLE job DROP COLUMN prompt;", Phase.CONTRACT) == set()


def test_a_contract_revision_may_not_also_add() -> None:
    """One deploy that both changes the write path and removes the old shape is the mistake."""
    sql = "ALTER TABLE job DROP COLUMN prompt; ALTER TABLE job ADD COLUMN note TEXT;"
    assert "contract-revision-is-not-only-drops" in _lint(sql, Phase.CONTRACT)


def test_a_contract_revision_may_carry_alembic_bookkeeping() -> None:
    sql = "ALTER TABLE job DROP COLUMN prompt; UPDATE alembic_version SET version_num='9';"
    assert _lint(sql, Phase.CONTRACT) == set()


# --- Rule: CREATE INDEX must be CONCURRENTLY ------------------------------------------------


def test_plain_create_index_on_an_existing_table_is_rejected() -> None:
    """A non-concurrent build holds a write lock for its whole duration."""
    assert "index-not-concurrent" in _lint("CREATE INDEX job_prompt_idx ON job (prompt);")


def test_concurrent_create_index_is_accepted() -> None:
    assert _lint("CREATE INDEX CONCURRENTLY job_prompt_idx ON job (prompt);") == set()


def test_plain_create_index_on_a_table_created_in_the_same_revision_is_accepted() -> None:
    """The table is empty and nothing else can be waiting on it — the one safe case."""
    sql = "CREATE TABLE note (id UUID, body TEXT); CREATE INDEX note_body_idx ON note (body);"
    assert _lint(sql) == set()


def test_unique_index_is_covered_by_the_same_rule() -> None:
    assert "index-not-concurrent" in _lint("CREATE UNIQUE INDEX job_x_idx ON job (prompt);")


# --- Rule: ADD CONSTRAINT must be NOT VALID on an existing table ---------------------------


def test_check_constraint_without_not_valid_is_rejected() -> None:
    """Validating in place scans and locks the whole table."""
    sql = "ALTER TABLE job ADD CONSTRAINT job_prompt_ck CHECK (length(prompt) > 0);"
    assert "constraint-without-not-valid" in _lint(sql)


def test_check_constraint_with_not_valid_is_accepted() -> None:
    sql = "ALTER TABLE job ADD CONSTRAINT job_prompt_ck CHECK (length(prompt) > 0) NOT VALID;"
    assert _lint(sql) == set()


def test_foreign_key_without_not_valid_is_rejected() -> None:
    sql = "ALTER TABLE job ADD CONSTRAINT job_t_fk FOREIGN KEY (tenant_id) REFERENCES tenant (id);"
    assert "constraint-without-not-valid" in _lint(sql)


def test_constraint_on_a_table_created_in_the_same_revision_is_accepted() -> None:
    sql = (
        "CREATE TABLE note (id UUID, body TEXT); "
        "ALTER TABLE note ADD CONSTRAINT note_body_ck CHECK (length(body) > 0);"
    )
    assert _lint(sql) == set()


def test_unique_constraint_is_not_required_to_be_not_valid() -> None:
    """`NOT VALID` is not supported for UNIQUE, so demanding it would be un-followable."""
    sql = "ALTER TABLE job ADD CONSTRAINT job_x_uq UNIQUE USING INDEX job_x_idx;"
    assert _lint(sql) == set()


# --- Rule: the lock budget -------------------------------------------------------------------


def test_a_script_with_no_lock_timeout_is_rejected() -> None:
    """A migration that waits on a lock queues every query behind it. It must abort instead."""
    findings = require_lock_budget("CREATE TABLE job (id UUID);")
    assert _rules(findings) == {"no-lock-budget"}


def test_a_script_that_sets_lock_timeout_first_is_accepted() -> None:
    sql = "SET lock_timeout = '5000ms'; CREATE TABLE job (id UUID);"
    assert require_lock_budget(sql) == []


def test_a_lock_timeout_set_after_the_first_ddl_is_too_late() -> None:
    sql = "CREATE TABLE job (id UUID); SET lock_timeout = '5000ms';"
    assert _rules(require_lock_budget(sql)) == {"no-lock-budget"}


# --- Whole-script linting ------------------------------------------------------------------


def test_a_revision_with_no_declared_phase_is_a_finding() -> None:
    """ "Cannot be checked" must not read the same as "passed"."""
    sql = "SET lock_timeout = '1s';\n-- Running upgrade  -> 0001\nCREATE TABLE a (id INT);\n"
    findings = lint_migration_script(sql, phases={})
    assert _rules(findings) == {"undeclared-phase"}


def test_a_compliant_script_produces_no_findings() -> None:
    sql = "SET lock_timeout = '1s';\n-- Running upgrade  -> 0001\nCREATE TABLE a (id INT);\n"
    assert lint_migration_script(sql, phases={"0001": Phase.EXPAND}) == []


def test_findings_name_the_revision_and_the_statement() -> None:
    """A finding that does not say where is a finding somebody has to go and look for."""
    sql = "SET lock_timeout = '1s';\n-- Running upgrade  -> 0007\nDROP TABLE job;\n"
    findings = lint_migration_script(sql, phases={"0007": Phase.EXPAND})
    assert len(findings) == 1
    assert findings[0].revision == "0007"
    assert "DROP TABLE job" in str(findings[0])
