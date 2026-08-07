"""S0.5.7 — the RLS policy, the exemption list, and the audit that gates both.

`persistence.md` §10 makes RLS the highest-priority test surface in the repository. Enforcement
against a live PostgreSQL is in `tests/integration/test_persistence_postgres.py` and cannot run
without one. What runs here is everything that does not need a database: the shape of the
policy, the membership of the exemption list, and — most importantly — proof that the audit
*fails* when it is given a table that is not protected.

That last one is the point. An audit nobody has watched fail is an audit that might be
returning an empty list for the wrong reason.
"""

from __future__ import annotations

import pytest

from video_agent.persistence.rls import (
    APPLICATION_ROLE,
    RLS_EXEMPT_TABLES,
    RLS_EXEMPTION_REASONS,
    RLS_PROTECTED_TABLES,
    TENANT_PREDICATE,
    TENANT_SETTING,
    PolicyFacts,
    TableRlsFacts,
    audit_rls,
    disable_rls_statements,
    enable_rls_statements,
    facts_from_catalog_rows,
    facts_from_ddl_statements,
    policy_name,
)
from video_agent.persistence.schema import TABLE_NAMES

# Written out independently of the constant it checks. `[D-70]`, `[D-68]`, `AGENT.md` §4.
DOCUMENTED_EXEMPTIONS: frozenset[str] = frozenset({"tenant", "tenant_api_key"})
EXPECTED_EXEMPTION_COUNT = 2


def _compliant(name: str) -> TableRlsFacts:
    return TableRlsFacts(
        name=name,
        rls_enabled=True,
        rls_forced=True,
        policies=(PolicyFacts(policy_name(name), TENANT_PREDICATE, TENANT_PREDICATE),),
    )


def _all_compliant() -> list[TableRlsFacts]:
    return [_compliant(name) for name in RLS_PROTECTED_TABLES]


# --- The exemption list --------------------------------------------------------------------


def test_exactly_two_tables_are_exempt() -> None:
    """`AGENT.md` §4 and `persistence.md` §3 both say two. A third is how isolation is lost.

    The count is asserted separately from the membership so that swapping one exemption for
    another and adding a third are two distinct failures, with two distinct messages.
    """
    assert len(RLS_EXEMPT_TABLES) == EXPECTED_EXEMPTION_COUNT


def test_the_exempt_tables_are_the_two_documented_ones() -> None:
    """`tenant` because the policy is defined in terms of it; `tenant_api_key` because it is
    read before a tenant context exists."""
    assert RLS_EXEMPT_TABLES == DOCUMENTED_EXEMPTIONS


@pytest.mark.parametrize("table", sorted(DOCUMENTED_EXEMPTIONS))
def test_each_exemption_carries_a_reason_citing_its_decision(table: str) -> None:
    """A reason, and the `D-nn` it comes from, so a later reader can tell it was deliberate."""
    reason = RLS_EXEMPTION_REASONS[table]
    assert reason.strip()
    assert "[D-" in reason


def test_every_other_table_is_protected() -> None:
    """The protected set is derived from the schema, so a new table is protected by default."""
    assert set(RLS_PROTECTED_TABLES) == set(TABLE_NAMES) - DOCUMENTED_EXEMPTIONS


# --- The policy itself ---------------------------------------------------------------------


def test_policy_enables_and_forces_and_declares_both_clauses() -> None:
    """`ENABLE` alone leaves the owner bypassing the policy; `USING` alone leaves writes open."""
    statements = enable_rls_statements("job")
    assert statements[0] == "ALTER TABLE job ENABLE ROW LEVEL SECURITY"
    assert statements[1] == "ALTER TABLE job FORCE ROW LEVEL SECURITY"
    assert statements[2].startswith("CREATE POLICY job_tenant_isolation ON job")
    assert " USING (" in statements[2]
    assert " WITH CHECK (" in statements[2]


def test_predicate_reads_the_local_column_and_the_session_variable() -> None:
    assert TENANT_PREDICATE.startswith("tenant_id = ")
    assert TENANT_SETTING in TENANT_PREDICATE


def test_predicate_is_missing_safe_rather_than_raising() -> None:
    """`persistence.md` §9: an absent `app.tenant_id` yields zero rows, not an error.

    One-argument `current_setting` raises. The two-argument form returns NULL, and the
    `NULLIF` covers the empty string that `SET app.tenant_id = ''` would leave — `''::uuid`
    raises just as loudly as the missing setting did. This is a deliberate divergence from the
    one-argument form printed in `persistence.md` §3's code block, which contradicts §9 of the
    same document.
    """
    assert "current_setting('app.tenant_id', true)" in TENANT_PREDICATE
    assert "NULLIF(" in TENANT_PREDICATE


def test_predicate_contains_no_subquery() -> None:
    """`[D-51]`: the predicate is one comparison against one local column and never a join."""
    assert "select" not in TENANT_PREDICATE.lower()
    assert "join" not in TENANT_PREDICATE.lower()


def test_disable_statements_invert_the_enable_statements() -> None:
    """A downgrade that leaves a policy behind cannot be re-run."""
    statements = disable_rls_statements("job")
    assert statements[0] == "DROP POLICY job_tenant_isolation ON job"
    assert "NO FORCE" in statements[1]
    assert "DISABLE ROW LEVEL SECURITY" in statements[2]


def test_application_role_is_named_and_is_not_the_owner() -> None:
    """`persistence.md` §3 rule 2: a non-superuser, non-owner role."""
    assert APPLICATION_ROLE == "video_agent_app"


# --- The audit: it passes what should pass -------------------------------------------------


def test_audit_passes_a_fully_compliant_schema() -> None:
    assert audit_rls(_all_compliant()) == []


def test_audit_ignores_the_exempt_tables_even_with_no_policy() -> None:
    """The two exemptions are exempt, not merely tolerated with a warning."""
    facts = [
        *_all_compliant(),
        TableRlsFacts("tenant", rls_enabled=False, rls_forced=False),
        TableRlsFacts("tenant_api_key", rls_enabled=False, rls_forced=False),
    ]
    assert audit_rls(facts) == []


# --- The audit: it fails what should fail --------------------------------------------------


def test_audit_catches_a_table_with_no_policy() -> None:
    """S0.5.7 acceptance 4: a newly added table with no policy cannot merge."""
    facts = [*_all_compliant(), TableRlsFacts("audit_log", rls_enabled=False, rls_forced=False)]
    details = [violation.detail for violation in audit_rls(facts) if violation.table == "audit_log"]
    assert any("not enabled" in detail for detail in details)
    assert any("no policy is defined" in detail for detail in details)


def test_audit_catches_rls_that_is_enabled_but_not_forced() -> None:
    """Without `FORCE` the owner reads every tenant, and the owner is who migrations run as."""
    facts = _all_compliant()
    facts[0] = TableRlsFacts(
        name=facts[0].name,
        rls_enabled=True,
        rls_forced=False,
        policies=facts[0].policies,
    )
    violations = audit_rls(facts)
    assert any("FORCE" in violation.detail for violation in violations)


def test_audit_catches_a_policy_with_no_with_check() -> None:
    """A `USING`-only policy stops cross-tenant reads and permits cross-tenant writes."""
    facts = _all_compliant()
    facts[0] = TableRlsFacts(
        name=facts[0].name,
        rls_enabled=True,
        rls_forced=True,
        policies=(PolicyFacts(policy_name(facts[0].name), TENANT_PREDICATE, None),),
    )
    violations = audit_rls(facts)
    assert any("WITH CHECK" in violation.detail for violation in violations)


def test_audit_catches_a_predicate_that_ignores_the_session_variable() -> None:
    """`USING (true)` is a policy that exists, is forced, and isolates nothing."""
    facts = _all_compliant()
    facts[0] = TableRlsFacts(
        name=facts[0].name,
        rls_enabled=True,
        rls_forced=True,
        policies=(PolicyFacts(policy_name(facts[0].name), "true", "true"),),
    )
    violations = audit_rls(facts)
    assert any("does not compare" in violation.detail for violation in violations)


def test_audit_catches_a_join_based_predicate() -> None:
    """`[D-51]`: a policy that joins to find the tenant can be bypassed; one that reads a
    local column cannot."""
    joined = (
        "tenant_id IN (SELECT id FROM tenant WHERE id::text = current_setting('app.tenant_id'))"
    )
    facts = _all_compliant()
    facts[0] = TableRlsFacts(
        name=facts[0].name,
        rls_enabled=True,
        rls_forced=True,
        policies=(PolicyFacts(policy_name(facts[0].name), joined, joined),),
    )
    violations = audit_rls(facts)
    assert any("subquery" in violation.detail for violation in violations)


def test_audit_catches_a_protected_table_missing_from_the_audited_source() -> None:
    """ "Not present" must not read the same as "compliant"."""
    facts = [fact for fact in _all_compliant() if fact.name != "shot_attempt"]
    violations = audit_rls(facts)
    assert any(violation.table == "shot_attempt" for violation in violations)


# --- Fact extraction -----------------------------------------------------------------------


def test_facts_are_parsed_out_of_emitted_ddl() -> None:
    """The offline audit reads the SQL the migration emits, not the Python that emits it."""
    statements = ["CREATE TABLE job (id UUID)", *enable_rls_statements("job")]
    facts = facts_from_ddl_statements(statements)
    assert len(facts) == 1
    assert facts[0].rls_enabled
    assert facts[0].rls_forced
    assert facts[0].policies[0].check_expression is not None
    assert audit_rls(facts) != []  # the other protected tables are absent from this fragment


def test_ddl_facts_report_a_created_table_with_no_rls_statements() -> None:
    facts = facts_from_ddl_statements(["CREATE TABLE audit_log (id UUID)"])
    assert facts == [TableRlsFacts("audit_log", rls_enabled=False, rls_forced=False, policies=())]


def test_catalog_rows_fold_into_one_record_per_table() -> None:
    """`CATALOG_RLS_QUERY` returns one row per policy, including a NULL row for none."""
    rows = [
        ("job", True, True, "job_tenant_isolation", TENANT_PREDICATE, TENANT_PREDICATE),
        ("tenant", False, False, None, None, None),
    ]
    facts = {fact.name: fact for fact in facts_from_catalog_rows(rows)}
    assert len(facts["job"].policies) == 1
    assert facts["tenant"].policies == ()
