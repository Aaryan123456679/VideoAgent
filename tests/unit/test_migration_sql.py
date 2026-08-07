"""S0.5.1 — S0.5.7 asserted against the SQL the migrations actually emit, with no database.

`alembic upgrade head --sql` renders the whole upgrade path to text without connecting to
anything. That is what lets the expand/contract rules, the RLS audit and the enum inventory all
be checked in CI on a machine with no PostgreSQL — and checked against the statements that will
run, rather than against the Python that produces them. A lint that reads migration *source*
is defeated by moving the DDL into a helper, which is exactly what this repository does.

What this module cannot check is whether PostgreSQL accepts the SQL, whether the policies
actually isolate anything, or whether the downgrade leaves a clean database. Those need a live
server and live in `tests/integration/test_persistence_postgres.py`.
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from video_agent.persistence.enums import ENUM_TYPES, enum_labels
from video_agent.persistence.migration_lint import (
    Phase,
    format_findings,
    lint_migration_script,
    parse_phase,
    split_revisions,
    split_statements,
)
from video_agent.persistence.rls import (
    RLS_EXEMPT_TABLES,
    RLS_PROTECTED_TABLES,
    audit_rls,
    facts_from_ddl_statements,
    format_violations,
)
from video_agent.persistence.schema import TABLE_NAMES

REQUIRED_ENV = {
    "MAGICHOUR_API_KEY": "",
    "DATABASE_URL": "postgresql+asyncpg://videoagent:videoagent@localhost:5432/videoagent",
    "REDIS_URL": "redis://localhost:6379/0",
}
"""Set explicitly so the render does not depend on a developer's `.env` being present.

The URL is never connected to — offline mode needs a dialect name and nothing more — but
`Settings` has no default for it, and a test that only passes on a machine with a `.env` is a
test that fails in CI for a reason that has nothing to do with migrations.
"""


def _config(repo_root: Path, buffer: io.StringIO) -> Config:
    config = Config(str(repo_root / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(repo_root / "migrations"))
    return config


def _render(repo_root: Path, *, upgrade: bool) -> str:
    buffer = io.StringIO()
    patch = pytest.MonkeyPatch()
    try:
        for name, value in REQUIRED_ENV.items():
            patch.setenv(name, value)
        config = _config(repo_root, buffer)
        if upgrade:
            command.upgrade(config, "head", sql=True)
        else:
            command.downgrade(config, "head:base", sql=True)
    finally:
        patch.undo()
    return buffer.getvalue()


@pytest.fixture(scope="module")
def upgrade_sql(repo_root: Path) -> str:
    """The whole upgrade path, rendered offline."""
    return _render(repo_root, upgrade=True)


@pytest.fixture(scope="module")
def downgrade_sql(repo_root: Path) -> str:
    """The whole downgrade path, rendered offline."""
    return _render(repo_root, upgrade=False)


@pytest.fixture(scope="module")
def revision_phases(repo_root: Path) -> dict[str, Phase]:
    """Every revision's declared `Phase:`, parsed from the module docstring by AST.

    Read from the source rather than by importing, so a revision with an import-time mistake
    fails the migration tests rather than the collection of this module.
    """
    phases: dict[str, Phase] = {}
    for path in sorted((repo_root / "migrations" / "versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        phases[_revision_id(tree, path)] = parse_phase(ast.get_docstring(tree))
    return phases


def _revision_id(tree: ast.Module, path: Path) -> str:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if "revision" in names and isinstance(node.value, ast.Constant):
                return str(node.value.value)
    message = f"{path} declares no `revision`"
    raise AssertionError(message)


# --- The script renders at all ---------------------------------------------------------------


def test_upgrade_renders_without_a_database(upgrade_sql: str) -> None:
    """Offline mode is the whole reason these checks can run in CI without PostgreSQL."""
    assert "CREATE TABLE job" in upgrade_sql
    assert upgrade_sql.strip().endswith("COMMIT;")


def test_every_revision_in_the_tree_is_rendered(
    upgrade_sql: str, revision_phases: dict[str, Phase]
) -> None:
    rendered = {revision for revision, _ in split_revisions(upgrade_sql)}
    assert rendered == set(revision_phases)


# --- Phase declarations -----------------------------------------------------------------------


def test_every_revision_declares_a_valid_phase(revision_phases: dict[str, Phase]) -> None:
    """`parse_phase` raises for a missing or unknown field, so reaching here is the assertion."""
    assert revision_phases
    assert all(isinstance(phase, Phase) for phase in revision_phases.values())


def test_the_template_carries_the_phase_field(repo_root: Path) -> None:
    """A template without the field produces revisions without it, one author at a time."""
    template = (repo_root / "migrations" / "script.py.mako").read_text(encoding="utf-8")
    assert "Phase: expand" in template
    for phase in Phase:
        assert phase.value in template


# --- Expand/contract ---------------------------------------------------------------------------


def test_the_migration_tree_passes_the_expand_contract_lint(
    upgrade_sql: str, revision_phases: dict[str, Phase]
) -> None:
    """The rules of `[CPS §Rollout]` and `AGENT.md` §4, against the emitted statements."""
    findings = lint_migration_script(upgrade_sql, revision_phases)
    assert findings == [], format_findings(findings)


def test_the_lock_budget_is_set_before_any_ddl(upgrade_sql: str) -> None:
    """`persistence.md` §9: a migration exceeding its lock budget is aborted, not queued."""
    statements = split_statements(upgrade_sql)
    first_ddl = next(index for index, sql in enumerate(statements) if "CREATE TABLE" in sql)
    lock = next(index for index, sql in enumerate(statements) if "lock_timeout" in sql)
    assert lock < first_ddl


def test_a_statement_timeout_is_also_set(upgrade_sql: str) -> None:
    """`lock_timeout` bounds the wait to start; `statement_timeout` bounds the run."""
    assert "statement_timeout" in upgrade_sql


# --- The schema the migrations produce ---------------------------------------------------------


def _created_tables(sql: str) -> set[str]:
    return set(re.findall(r"CREATE TABLE (\w+)", sql))


def _dropped_tables(sql: str) -> set[str]:
    return set(re.findall(r"DROP TABLE (\w+)", sql))


def test_every_declared_table_is_created(upgrade_sql: str) -> None:
    assert set(TABLE_NAMES) <= _created_tables(upgrade_sql)


def test_the_downgrade_drops_everything_the_upgrade_created(
    upgrade_sql: str, downgrade_sql: str
) -> None:
    """A rollback that leaves tables behind is a rollback that cannot be re-applied."""
    created = _created_tables(upgrade_sql) - {"alembic_version"}
    assert created == _dropped_tables(downgrade_sql)


@pytest.mark.parametrize("type_name", sorted(ENUM_TYPES))
def test_emitted_enum_types_carry_the_declared_members(upgrade_sql: str, type_name: str) -> None:
    match = re.search(rf"CREATE TYPE {type_name} AS ENUM \(([^)]*)\)", upgrade_sql)
    assert match is not None, f"{type_name} is never created"
    emitted = tuple(part.strip().strip("'") for part in match.group(1).split(","))
    assert emitted == enum_labels(type_name)


def test_no_bytea_column_is_emitted(upgrade_sql: str) -> None:
    """Postgres holds keys and checksums; the object store holds bytes."""
    assert "BYTEA" not in upgrade_sql.upper()


def test_the_immutability_trigger_is_created_with_its_error_code(upgrade_sql: str) -> None:
    """`VA-BIBLE-002` has to survive into the driver's exception text to be actionable."""
    assert "CREATE TRIGGER continuity_bible_immutable" in upgrade_sql
    assert "VA-BIBLE-002" in upgrade_sql


# --- Row-level security -------------------------------------------------------------------------


def test_the_emitted_schema_passes_the_rls_audit(upgrade_sql: str) -> None:
    """S0.5.7 acceptance 1 and 4, checked against the migration output.

    This is the offline half of the highest-priority check in the repository: every table the
    migrations create is enabled, forced and policed with both clauses, or is one of the two
    documented exemptions.
    """
    facts = facts_from_ddl_statements(split_statements(upgrade_sql))
    violations = audit_rls([fact for fact in facts if fact.name != "alembic_version"])
    assert violations == [], format_violations(violations)


@pytest.mark.parametrize("table", RLS_PROTECTED_TABLES)
def test_each_protected_table_is_enabled_forced_and_policed(upgrade_sql: str, table: str) -> None:
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in upgrade_sql
    assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in upgrade_sql
    assert f"CREATE POLICY {table}_tenant_isolation ON {table}" in upgrade_sql


@pytest.mark.parametrize("table", sorted(RLS_EXEMPT_TABLES))
def test_each_exempt_table_gets_no_policy(upgrade_sql: str, table: str) -> None:
    """The exemption is real, not merely undocumented."""
    assert f"CREATE POLICY {table}_tenant_isolation" not in upgrade_sql


def test_the_policy_predicate_is_missing_safe(upgrade_sql: str) -> None:
    """One-argument `current_setting` raises; §9 requires zero rows plus an alarm instead."""
    assert "current_setting('app.tenant_id', true)" in upgrade_sql
    assert "current_setting('app.tenant_id')" not in upgrade_sql


# --- The application role -------------------------------------------------------------------------


def test_the_application_role_is_created_without_bypassrls(upgrade_sql: str) -> None:
    """`BYPASSRLS` is the one attribute that turns every policy in this schema into decoration."""
    assert "CREATE ROLE video_agent_app" in upgrade_sql
    assert "NOBYPASSRLS" in upgrade_sql
    assert not re.search(
        r"ALTER ROLE video_agent_app[^;]*\bBYPASSRLS\b(?<!NOBYPASSRLS)", upgrade_sql
    )


def test_the_application_role_is_never_made_an_owner(upgrade_sql: str) -> None:
    """An owner can create a table with no policy, which is the exemption list by other means."""
    assert "OWNER TO video_agent_app" not in upgrade_sql


def test_the_migration_contains_no_password(upgrade_sql: str) -> None:
    """A migration owns privileges and must never carry a secret, so the role is NOLOGIN."""
    assert "PASSWORD" not in upgrade_sql.upper()
    assert "NOLOGIN" in upgrade_sql


@pytest.mark.parametrize("table", TABLE_NAMES)
def test_the_application_role_is_granted_something_on_every_table(
    upgrade_sql: str, table: str
) -> None:
    """`GRANT ... ON ALL TABLES` is a snapshot, so a table added later needs its own grant."""
    assert f"ON {table} TO video_agent_app" in upgrade_sql


@pytest.mark.parametrize(
    ("table", "privileges"),
    [("tenant", "SELECT"), ("tenant_api_key", "SELECT, UPDATE")],
)
def test_the_two_rls_exempt_tables_get_least_privilege(
    upgrade_sql: str, table: str, privileges: str
) -> None:
    """On the two tables RLS does not protect, the grant *is* the boundary.

    `tenant` is readable so key resolution can check `disabled_at` `[D-70]`, and writable only
    by the admin path. `tenant_api_key` is readable to resolve a key and updatable to stamp
    `last_used_at`; issuing and revoking are the admin path's `[D-68]`. An application role
    with INSERT on either could mint itself a tenant or a key, and no policy would stop it.
    """
    assert f"GRANT {privileges} ON {table} TO video_agent_app" in upgrade_sql


@pytest.mark.parametrize("table", RLS_PROTECTED_TABLES)
def test_rls_protected_tables_get_full_dml(upgrade_sql: str, table: str) -> None:
    """Where the policy is the boundary, DML is the right grant."""
    assert f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO video_agent_app" in upgrade_sql
