"""Row-level security: the policy, the two-table exemption list, and the audit that gates it.

RLS **is** the tenant isolation boundary. Not a defence in depth behind a `WHERE` clause the
application remembers to add — the boundary itself. `persistence.md` §10 makes this the
highest-priority test surface in the repository, and this module exists so that the property
is checkable by a machine instead of by a reviewer reading a migration diff.

Three things are worth stating explicitly, because each is a way isolation is normally lost.

**The predicate never joins.** `[D-51]` denormalises `tenant_id` onto every table, so the
policy is one comparison against one local column. A join-based policy depends on the planner,
on the visibility of the joined table and on nobody later wrapping it in a `SECURITY DEFINER`
function — three ways to be bypassed that a single-column predicate does not have. `audit_rls`
therefore rejects a policy expression containing a subquery, not merely one that is missing.

**The predicate is missing-safe, not missing-loud.** `current_setting('app.tenant_id')` with
one argument *raises* when the setting is absent. `persistence.md` §9 requires the opposite
behaviour — *"Policy evaluates false → zero rows, not an error, plus an alarm"* — so the
two-argument form is used and the result is `NULLIF`-guarded against the empty string that
`SET app.tenant_id = ''` would leave behind. `''::uuid` raises just as loudly as the missing
setting did. With the guard the comparison is `NULL`, the row is filtered, reads return
nothing and writes fail `WITH CHECK`. The alarm is the session layer's job, not the policy's.

*(This is a deliberate divergence from the one-argument form written in `persistence.md` §3's
code block, which contradicts §9's own failure-mode table. Reported with the run.)*

**Exactly two tables are exempt, and the list is asserted rather than described.** `[D-70]`
`[D-68]` A third exemption is precisely the change that silently disables isolation for one
table while every other check stays green, so the membership of `RLS_EXEMPT_TABLES` is itself
a test assertion with an independently written copy of the pair.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from video_agent.persistence.schema import TABLE_NAMES

TENANT_SETTING = "app.tenant_id"
"""The session variable the policy reads.

Set from `Principal.tenant_id` and from nothing else `[D-68]` — never from a request body, a
path parameter, a query string or a client-supplied header. A tenant id that can be chosen by
the caller is not an isolation boundary, it is a parameter.
"""

TENANT_PREDICATE = f"tenant_id = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid"
"""The one predicate, used for both `USING` and `WITH CHECK` on every protected table."""

RLS_EXEMPTION_REASONS: Mapping[str, str] = {
    "tenant": (
        "It is the table the policy is defined in terms of. Protecting it with the policy "
        "it bootstraps is circular. Reachable only by the migration role and the admin "
        "path, never by a tenant-scoped connection. [D-70]"
    ),
    "tenant_api_key": (
        "It is read by the unauthenticated path that is establishing which tenant is "
        "calling, before a tenant context exists. Lookup is by the non-secret key_prefix; "
        "the row yields the tenant_id that then sets the session variable. [D-68]"
    ),
}
"""Every exempt table and the reason it is exempt, in one place.

A reason is required because the failure mode this guards against is not someone maliciously
exempting a table, it is someone adding a table to the set to make a test pass and nobody
being able to tell later whether that was justified.
"""

RLS_EXEMPT_TABLES: frozenset[str] = frozenset(RLS_EXEMPTION_REASONS)

RLS_PROTECTED_TABLES: tuple[str, ...] = tuple(
    name for name in TABLE_NAMES if name not in RLS_EXEMPT_TABLES
)
"""Every table that must have RLS enabled, forced, and a policy with `USING` and `WITH CHECK`.

Derived from the schema rather than listed, so a table added to `schema.py` is protected by
default and appears in the audit without anybody remembering to add it here.
"""

APPLICATION_ROLE = "video_agent_app"
"""The non-superuser, non-owner role the application connects as.

`FORCE ROW LEVEL SECURITY` closes the owner-bypass hole, so the role choice is a second layer
rather than the only one — but the two together are what make the isolation hold against a
mistake in either. The role is created `NOLOGIN` by the migration: a migration owns the
*privileges* and must never contain the *secret*, so granting `LOGIN` with a password is a
deployment step against the secret store.
"""


def policy_name(table_name: str) -> str:
    """The name of the isolation policy on one table."""
    return f"{table_name}_tenant_isolation"


def enable_rls_statements(table_name: str) -> tuple[str, ...]:
    """Enable, force and police one table.

    `FORCE` matters as much as `ENABLE`. Without it the table owner — which is the role every
    migration and every `psql` session by the deploying engineer runs as — reads every tenant's
    rows, and a test that happens to connect as the owner reports isolation that is not there.
    """
    return (
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {policy_name(table_name)} ON {table_name}"
        f" USING ({TENANT_PREDICATE})"
        f" WITH CHECK ({TENANT_PREDICATE})",
    )


def disable_rls_statements(table_name: str) -> tuple[str, ...]:
    """Undo `enable_rls_statements`, for a migration downgrade."""
    return (
        f"DROP POLICY {policy_name(table_name)} ON {table_name}",
        f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY",
    )


# --- The audit ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyFacts:
    """One policy as the catalogue reports it, or as the migration SQL declares it."""

    name: str
    using_expression: str | None
    check_expression: str | None


@dataclass(frozen=True, slots=True)
class TableRlsFacts:
    """The RLS state of one table, from whichever source is being audited."""

    name: str
    rls_enabled: bool
    rls_forced: bool
    policies: tuple[PolicyFacts, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class RlsViolation:
    """One way one table fails the isolation contract."""

    table: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table}: {self.detail}"


def _predicate_violations(table: str, policy: PolicyFacts) -> list[RlsViolation]:
    violations: list[RlsViolation] = []
    clauses = (("USING", policy.using_expression), ("WITH CHECK", policy.check_expression))
    for label, expression in clauses:
        if expression is None:
            violations.append(
                RlsViolation(
                    table,
                    f"policy {policy.name!r} has no {label} clause; a policy without "
                    f"WITH CHECK stops cross-tenant reads and permits cross-tenant writes",
                )
            )
            continue
        lowered = expression.lower()
        if "tenant_id" not in lowered or TENANT_SETTING not in lowered:
            violations.append(
                RlsViolation(
                    table,
                    f"policy {policy.name!r} {label} clause does not compare the local "
                    f"tenant_id column against {TENANT_SETTING}: {expression}",
                )
            )
        if "select" in lowered:
            violations.append(
                RlsViolation(
                    table,
                    f"policy {policy.name!r} {label} clause contains a subquery; [D-51] "
                    f"denormalises tenant_id so the predicate never joins: {expression}",
                )
            )
    return violations


def audit_rls(facts: Iterable[TableRlsFacts]) -> list[RlsViolation]:
    """Every way the supplied tables fail the isolation contract.

    One checker, three callers: the offline test feeds it the RLS state parsed out of the
    emitted migration SQL, the integration test feeds it the state read from `pg_class` and
    `pg_policies`, and the negative test feeds it a table that has no policy at all. A checker
    that only ever sees the real schema is a checker nobody has watched fail.
    """
    violations: list[RlsViolation] = []
    seen: set[str] = set()

    for table in facts:
        seen.add(table.name)
        if table.name in RLS_EXEMPT_TABLES:
            continue
        if not table.rls_enabled:
            violations.append(
                RlsViolation(
                    table.name,
                    "row level security is not enabled and the table is not on the "
                    "two-table exemption list",
                )
            )
        if not table.rls_forced:
            violations.append(
                RlsViolation(
                    table.name,
                    "row level security is not FORCEd, so the table owner bypasses it",
                )
            )
        if not table.policies:
            violations.append(RlsViolation(table.name, "no policy is defined"))
        for policy in table.policies:
            violations.extend(_predicate_violations(table.name, policy))

    for missing in sorted(set(RLS_PROTECTED_TABLES) - seen):
        violations.append(
            RlsViolation(missing, "declared in the schema but absent from the audited source")
        )

    return violations


def format_violations(violations: Iterable[RlsViolation]) -> str:
    """Render violations one per line, for an assertion message that names the table."""
    return "\n".join(str(violation) for violation in violations)


CATALOG_RLS_QUERY = " ".join(
    (
        "SELECT c.relname AS table_name,",
        "c.relrowsecurity AS rls_enabled,",
        "c.relforcerowsecurity AS rls_forced,",
        "p.polname AS policy_name,",
        "pg_get_expr(p.polqual, p.polrelid) AS using_expression,",
        "pg_get_expr(p.polwithcheck, p.polrelid) AS check_expression",
        "FROM pg_class c",
        "JOIN pg_namespace n ON n.oid = c.relnamespace",
        "LEFT JOIN pg_policy p ON p.polrelid = c.oid",
        "WHERE c.relkind = 'r' AND n.nspname = current_schema()",
        "ORDER BY c.relname, p.polname",
    )
)
"""Read the live RLS state out of the catalogue.

Reads `pg_class` and `pg_policy` directly rather than the `pg_policies` view because the view
hides policies whose table the current role cannot see, and an audit that cannot see a table
must report that, not skip it.
"""


_CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_ENABLE_RLS = re.compile(
    r"\bALTER\s+TABLE\s+(\w+)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY", re.IGNORECASE
)
_FORCE_RLS = re.compile(r"\bALTER\s+TABLE\s+(\w+)\s+FORCE\s+ROW\s+LEVEL\s+SECURITY", re.IGNORECASE)
_CREATE_POLICY = re.compile(
    r"\bCREATE\s+POLICY\s+(\w+)\s+ON\s+(\w+)\s+USING\s*\((?P<using>.*?)\)\s*"
    r"(?:WITH\s+CHECK\s*\((?P<check>.*)\))?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def facts_from_ddl_statements(statements: Iterable[str]) -> list[TableRlsFacts]:
    """Read the RLS state out of the DDL a migration emits, without a database.

    The offline half of the audit. It reads the SQL the migration *will run*, not the Python
    that produces it, so moving the DDL behind a helper — which is exactly what this
    repository does — cannot hide a table that never got a policy.

    A `USING` expression is matched non-greedily up to the `WITH CHECK` that follows it, which
    works because the policy statements are generated by `enable_rls_statements` and have one
    shape. A hand-written policy with a differently nested predicate would parse as missing a
    clause, which fails the audit — the safe direction for a parser to be wrong in.
    """
    enabled: set[str] = set()
    forced: set[str] = set()
    tables: list[str] = []
    policies: dict[str, list[PolicyFacts]] = {}

    for statement in statements:
        collapsed = " ".join(statement.split())
        created = _CREATE_TABLE.search(collapsed)
        if created is not None:
            tables.append(created.group(1).lower())
        enable = _ENABLE_RLS.search(collapsed)
        if enable is not None:
            enabled.add(enable.group(1).lower())
        force = _FORCE_RLS.search(collapsed)
        if force is not None:
            forced.add(force.group(1).lower())
        policy = _CREATE_POLICY.search(collapsed)
        if policy is not None:
            table = policy.group(2).lower()
            policies.setdefault(table, []).append(
                PolicyFacts(
                    name=policy.group(1),
                    using_expression=policy.group("using"),
                    check_expression=policy.group("check"),
                )
            )

    return [
        TableRlsFacts(
            name=name,
            rls_enabled=name in enabled,
            rls_forced=name in forced,
            policies=tuple(policies.get(name, ())),
        )
        for name in tables
    ]


def facts_from_catalog_rows(
    rows: Iterable[tuple[str, bool, bool, str | None, str | None, str | None]],
) -> list[TableRlsFacts]:
    """Fold `CATALOG_RLS_QUERY`'s one-row-per-policy result into one record per table."""
    enabled: dict[str, tuple[bool, bool]] = {}
    policies: dict[str, list[PolicyFacts]] = {}
    for name, rls_enabled, rls_forced, policy, using_expression, check_expression in rows:
        enabled[name] = (rls_enabled, rls_forced)
        policies.setdefault(name, [])
        if policy is not None:
            policies[name].append(PolicyFacts(policy, using_expression, check_expression))
    return [
        TableRlsFacts(
            name=name,
            rls_enabled=flags[0],
            rls_forced=flags[1],
            policies=tuple(policies[name]),
        )
        for name, flags in sorted(enabled.items())
    ]
