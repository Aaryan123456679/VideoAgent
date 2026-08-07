"""persistence — Postgres schema, RLS, migrations. See ``docs/LLD/persistence.md``.

The bottom layer: this module depends on no other module in the repository and every other
module depends on it. `[persistence.md §8]`

What `T0.5` lands is the schema and the boundary around it — the `MetaData` that is the single
definition of the tables, the row-level security policy and the audit that gates it, the
expand/contract lint that runs against the SQL a migration actually emits, and the
tenant-scoped session that is the only place `app.tenant_id` is ever set. Redis and the object
store are `T0.6`'s and are deliberately not here.

Nothing outside this package constructs an engine or a session. That is enforced by
`tests/unit/test_persistence_boundary.py` rather than asked for in a review, because a
connection opened elsewhere is a connection with no tenant scope on it.
"""

from video_agent.persistence.enums import (
    ARTIFACT_KIND,
    ATTEMPT_STATE,
    BEAT_KIND,
    ENUM_TYPES,
    JOB_OUTCOME,
    JOB_STATUS,
    SHOT_STATUS,
    ArtifactKind,
    AttemptState,
    BeatKind,
    JobOutcome,
    JobStatus,
    ShotStatus,
    enum_labels,
)
from video_agent.persistence.migration_lint import (
    LintFinding,
    MigrationLintError,
    Phase,
    lint_migration_script,
    lint_statements,
    parse_phase,
    split_statements,
)
from video_agent.persistence.repositories import (
    ArtifactRepository,
    AttemptClaim,
    CheckpointRepository,
    ContinuityBibleRepository,
    IdempotencyKeyReusedError,
    JobRepository,
    ShotAttemptRepository,
    ShotRepository,
    StoryPlanRepository,
)
from video_agent.persistence.rls import (
    APPLICATION_ROLE,
    RLS_EXEMPT_TABLES,
    RLS_EXEMPTION_REASONS,
    RLS_PROTECTED_TABLES,
    TENANT_SETTING,
    PolicyFacts,
    RlsViolation,
    TableRlsFacts,
    audit_rls,
)
from video_agent.persistence.schema import ALL_TABLES, TABLE_NAMES, metadata
from video_agent.persistence.session import (
    UNSET_TENANT_CONTEXT_ALARM,
    TenantContextMissingError,
    TenantSession,
    create_database_engine,
    tenant_session,
)

__all__ = [
    "ALL_TABLES",
    "APPLICATION_ROLE",
    "ARTIFACT_KIND",
    "ATTEMPT_STATE",
    "BEAT_KIND",
    "ENUM_TYPES",
    "JOB_OUTCOME",
    "JOB_STATUS",
    "RLS_EXEMPTION_REASONS",
    "RLS_EXEMPT_TABLES",
    "RLS_PROTECTED_TABLES",
    "SHOT_STATUS",
    "TABLE_NAMES",
    "TENANT_SETTING",
    "UNSET_TENANT_CONTEXT_ALARM",
    "ArtifactKind",
    "ArtifactRepository",
    "AttemptClaim",
    "AttemptState",
    "BeatKind",
    "CheckpointRepository",
    "ContinuityBibleRepository",
    "IdempotencyKeyReusedError",
    "JobOutcome",
    "JobRepository",
    "JobStatus",
    "LintFinding",
    "MigrationLintError",
    "Phase",
    "PolicyFacts",
    "RlsViolation",
    "ShotAttemptRepository",
    "ShotRepository",
    "ShotStatus",
    "StoryPlanRepository",
    "TableRlsFacts",
    "TenantContextMissingError",
    "TenantSession",
    "audit_rls",
    "create_database_engine",
    "enum_labels",
    "lint_migration_script",
    "lint_statements",
    "metadata",
    "parse_phase",
    "split_statements",
    "tenant_session",
]
