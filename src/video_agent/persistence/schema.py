"""The schema of `persistence.md` §2, as one SQLAlchemy `MetaData`.

This module is the **single definition** of the schema. The migrations compile their DDL from
these table objects, the repositories build their statements from these columns, and the tests
assert against them. There is deliberately no second hand-written copy of the DDL anywhere in
the tree: the alternative — SQL in the migration and a model that claims to mirror it — has
exactly one failure mode, and it is the one that matters, which is the two drifting apart in a
direction nobody notices until a write fails in production.

**Core `Table` objects rather than declarative ORM classes.** Two reasons, both about the
tenant boundary. First, a mapped instance carries `tenant_id` as an ordinary writable
attribute, so "the caller must not set `tenant_id`" becomes a convention held up by code
review; with Core the repository builds the values mapping itself and there is no attribute
for a caller to set. Second, `MetaData` is directly comparable against `information_schema`,
which is what makes the drift test a comparison rather than a reimplementation.

**Every table carries `tenant_id`, including children.** `[D-51]` That denormalisation is not
a shortcut, it is the RLS design: a policy that reads a local column is a single-column
predicate, and a policy that joins to find the tenant is a policy that a future index change,
a view or a `SECURITY DEFINER` function can quietly bypass. `[D-70]` adds
`REFERENCES tenant(id)` for referential integrity on top; the policy still never joins.

**Naming.** Every constraint and index is named explicitly. PostgreSQL will invent a name for
an anonymous `CHECK`, and an invented name cannot be asserted on, dropped by a later migration
or found in a failure message by anyone who did not write it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from video_agent.persistence.enums import (
    ARTIFACT_KIND,
    ATTEMPT_STATE,
    BEAT_KIND,
    JOB_OUTCOME,
    JOB_STATUS,
    SHOT_STATUS,
    pg_enum,
)

metadata = MetaData()
"""The one `MetaData` for the whole schema. Nothing outside this module adds to it."""

_NOW = text("now()")
_NEW_UUID = text("gen_random_uuid()")
_EMPTY_OBJECT = text("'{}'::jsonb")
_EMPTY_ARRAY = text("'[]'::jsonb")
_FALSE = text("false")
_ZERO = text("0")


def _timestamp(name: str, *, nullable: bool, default: bool = False) -> Column[datetime]:
    """A timezone-aware timestamp column.

    Always `TIMESTAMPTZ`, never `TIMESTAMP`. A naive timestamp column is a column whose
    meaning depends on the session time zone of whoever last wrote to it, and the ruff `DTZ`
    rules ban the Python half of the same mistake.
    """
    return Column(
        name,
        TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=_NOW if default else None,
    )


# --- tenant: the table the policy is defined in terms of [D-70] ---------------------------

tenant = Table(
    "tenant",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=_NEW_UUID),
    Column("name", Text, nullable=False),
    # NULL means *inherit the global cap*, never *unlimited*. `Settings.max_usd_for_tenant`
    # is the only reader and it resolves NULL to `BUDGET_MAX_USD_PER_JOB` [D-70].
    Column("max_usd_per_job", Numeric(10, 4), nullable=True),
    Column("retention_days", Integer, nullable=False, server_default=text("30")),
    _timestamp("created_at", nullable=False, default=True),
    _timestamp("disabled_at", nullable=True),
)

# --- tenant_api_key: resolved before a tenant context exists [D-68] -----------------------

tenant_api_key = Table(
    "tenant_api_key",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=_NEW_UUID),
    Column(
        "tenant_id", UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    ),
    # Argon2id. The plaintext is shown once at issuance and is never stored, so a database
    # dump cannot be replayed as a set of working credentials.
    Column("key_hash", Text, nullable=False),
    # Non-secret. Lookup is by prefix so verification is one Argon2id comparison against one
    # candidate row rather than a scan that verifies against every key in the table.
    Column("key_prefix", Text, nullable=False),
    Column("label", Text, nullable=True),
    _timestamp("created_at", nullable=False, default=True),
    _timestamp("last_used_at", nullable=True),
    _timestamp("revoked_at", nullable=True),
    UniqueConstraint("key_prefix", name="tenant_api_key_prefix_uq"),
)

Index(
    "tenant_api_key_tenant_idx",
    tenant_api_key.c.tenant_id,
    postgresql_where=tenant_api_key.c.revoked_at.is_(None),
)

# --- job ----------------------------------------------------------------------------------

job = Table(
    "job",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("request_fingerprint", Text, nullable=False),
    # User content. Stored because resume and reproducibility need it; redacted in every log
    # line and span attribute, never rendered raw. See `persistence.md` §7.
    Column("prompt", Text, nullable=False),
    Column("music_bed", Boolean, nullable=False, server_default=_FALSE),
    Column("status", pg_enum(JOB_STATUS), nullable=False, server_default=text("'queued'")),
    Column("outcome", pg_enum(JOB_OUTCOME), nullable=True),
    Column("degraded", Boolean, nullable=False, server_default=_FALSE),
    Column("degraded_reason", Text, nullable=True),
    Column("terminal_reason_code", Text, nullable=True),
    Column("trace_id", Text, nullable=False),
    Column("budget_caps", JSONB, nullable=False),
    Column("budget_used", JSONB, nullable=False, server_default=_EMPTY_OBJECT),
    # Incremented on a resume grant, never reset [D-25]. Resetting it would re-authorise a
    # budget that has already been spent.
    Column("budget_epoch", Integer, nullable=False, server_default=_ZERO),
    _timestamp("created_at", nullable=False, default=True),
    _timestamp("updated_at", nullable=False, default=True),
    # Scoped to the tenant, not global: two tenants choosing the same key is a coincidence,
    # not a replay, and a global constraint would let one tenant deny keys to another.
    UniqueConstraint("tenant_id", "idempotency_key", name="job_idem_uq"),
)

Index("job_tenant_created_idx", job.c.tenant_id, job.c.created_at.desc())
# Partial: the hot query is "what is still running", and terminal rows are the ones that
# accumulate forever. Excluding them keeps the index proportional to the work in flight
# rather than to the history of the account.
Index("job_status_idx", job.c.status, postgresql_where=job.c.status != text("'terminal'"))

# --- story_plan and beat ------------------------------------------------------------------

story_plan = Table(
    "story_plan",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("job_id", UUID(as_uuid=True), ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False),
    Column("logline", Text, nullable=False),
    Column("total_duration_s", Numeric(5, 2), nullable=False),
    # The plan is attributable to a model and a prompt version, or it is not reproducible.
    Column("model_alias", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    _timestamp("created_at", nullable=False, default=True),
    UniqueConstraint("job_id", name="story_plan_job_uq"),
    # Exactly 40 seconds. `[PRD]` fixes the duration; a plan that sums to anything else is a
    # planning bug, and the database is where it stops rather than where it is discovered.
    CheckConstraint("total_duration_s = 40.00", name="story_plan_total_duration_ck"),
)

beat = Table(
    "beat",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "story_plan_id",
        UUID(as_uuid=True),
        ForeignKey("story_plan.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False),
    Column("idx", SmallInteger, nullable=False),
    Column("kind", pg_enum(BEAT_KIND), nullable=False),
    Column("action", Text, nullable=False),
    Column("camera_move", Text, nullable=False),
    Column("duration_s", Numeric(4, 2), nullable=False),
    Column("continuity_note", Text, nullable=True),
    UniqueConstraint("story_plan_id", "idx", name="beat_plan_idx_uq"),
    CheckConstraint("idx BETWEEN 0 AND 3", name="beat_idx_ck"),
    # v1 fixes the shot length at 10s [D-03]. Not a range: a 9.99 beat and a 10.01 beat both
    # produce a video that is not 40 seconds long.
    CheckConstraint("duration_s = 10.00", name="beat_duration_ck"),
)

# --- continuity_bible ---------------------------------------------------------------------

continuity_bible = Table(
    "continuity_bible",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("job_id", UUID(as_uuid=True), ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False),
    Column("character", JSONB, nullable=False),
    Column("wardrobe", JSONB, nullable=False),
    Column("location", JSONB, nullable=False),
    Column("lighting", JSONB, nullable=False),
    Column("palette", JSONB, nullable=False),
    Column("lens_language", JSONB, nullable=False),
    Column("negative_constraints", JSONB, nullable=False, server_default=_EMPTY_ARRAY),
    Column("content_hash", Text, nullable=False),
    _timestamp("locked_at", nullable=False, default=True),
    Column("model_alias", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    UniqueConstraint("job_id", name="continuity_bible_job_uq"),
)

BIBLE_CONTENT_COLUMNS: tuple[str, ...] = (
    "character",
    "wardrobe",
    "location",
    "lighting",
    "palette",
    "lens_language",
    "negative_constraints",
)
"""The seven columns the immutability trigger protects, named for the parametrised test."""

# --- shot and shot_attempt ----------------------------------------------------------------

shot = Table(
    "shot",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("job_id", UUID(as_uuid=True), ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False),
    Column("beat_id", UUID(as_uuid=True), ForeignKey("beat.id"), nullable=False),
    Column("idx", SmallInteger, nullable=False),
    Column("status", pg_enum(SHOT_STATUS), nullable=False, server_default=text("'pending'")),
    Column("attempts_used", SmallInteger, nullable=False, server_default=_ZERO),
    Column("repairs_used", SmallInteger, nullable=False, server_default=_ZERO),
    Column("best_attempt_id", UUID(as_uuid=True), nullable=True),
    Column("best_score", Numeric(4, 3), nullable=True),
    UniqueConstraint("job_id", "idx", name="shot_job_idx_uq"),
    CheckConstraint("idx BETWEEN 0 AND 3", name="shot_idx_ck"),
    # Two repairs, and the database is the last line of defence for the cap [D-01]. The
    # harness enforces it first; this is what catches the enforcement being refactored away.
    CheckConstraint("repairs_used <= 2", name="shot_repairs_used_ck"),
    CheckConstraint("best_score BETWEEN 0 AND 1", name="shot_best_score_ck"),
)

shot_attempt = Table(
    "shot_attempt",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "shot_id", UUID(as_uuid=True), ForeignKey("shot.id", ondelete="CASCADE"), nullable=False
    ),
    Column("job_id", UUID(as_uuid=True), ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False),
    Column("attempt_no", SmallInteger, nullable=False),
    Column(
        "state",
        pg_enum(ATTEMPT_STATE),
        nullable=False,
        server_default=text("'in_flight'"),
    ),
    Column("provider_key", Text, nullable=True),
    Column("provider_model", Text, nullable=True),
    # The reproducibility handle and the crash-reconciliation handle [D-59], [D-24]. Written
    # as soon as the provider returns an id, so a worker that dies waiting on the render can
    # still find what was submitted.
    Column("provider_project_id", Text, nullable=True),
    # Nullable BY DESIGN [D-59]. A NOT NULL column here would force a fabricated value for
    # providers that expose no seed, and the reproducibility record would then claim a
    # guarantee the provider does not offer. `seed_supported` says which case this is, so a
    # NULL is never ambiguous.
    Column("seed", BigInteger, nullable=True),
    Column("seed_supported", Boolean, nullable=False, server_default=_FALSE),
    Column("prompt_text", Text, nullable=False),
    Column("prompt_hash", Text, nullable=False),
    Column("bible_hash", Text, nullable=False),
    Column("conditioning_frame_id", UUID(as_uuid=True), nullable=True),
    Column("request_fingerprint", Text, nullable=False),
    Column("cost_usd", Numeric(10, 4), nullable=False, server_default=_ZERO),
    # Provisional until the attempt is terminal [D-60]. NULL means "not yet charged", which
    # is a different fact from "charged zero".
    Column("credits_charged", Numeric(12, 4), nullable=True),
    Column("cost_is_final", Boolean, nullable=False, server_default=_FALSE),
    Column("qc_score", Numeric(4, 3), nullable=True),
    Column("qc_dimensions", JSONB, nullable=True),
    Column("qc_findings", JSONB, nullable=True),
    Column("qc_hard_fail", Boolean, nullable=False, server_default=_FALSE),
    Column("clip_artifact_id", UUID(as_uuid=True), nullable=True),
    Column("final_frame_artifact_id", UUID(as_uuid=True), nullable=True),
    Column("error_code", Text, nullable=True),
    _timestamp("started_at", nullable=False, default=True),
    _timestamp("ended_at", nullable=True),
    UniqueConstraint("shot_id", "attempt_no", name="shot_attempt_no_uq"),
    # The anti-double-bill constraint [D-24]. Global rather than per-tenant on purpose: the
    # fingerprint already contains the tenant, and a per-tenant scope would need a join to
    # check, which is the thing this constraint exists to avoid depending on.
    UniqueConstraint("request_fingerprint", name="shot_attempt_fingerprint_uq"),
    # One attempt plus two repairs [D-01].
    CheckConstraint("attempt_no BETWEEN 1 AND 3", name="shot_attempt_no_ck"),
    CheckConstraint("qc_score BETWEEN 0 AND 1", name="shot_attempt_qc_score_ck"),
)

# --- artifact -----------------------------------------------------------------------------

artifact = Table(
    "artifact",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("job_id", UUID(as_uuid=True), ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False),
    Column("kind", pg_enum(ARTIFACT_KIND), nullable=False),
    Column("shot_index", SmallInteger, nullable=True),
    # Tenant-prefixed, so the bucket policy is a second isolation layer behind RLS. The bytes
    # live in the object store; this column is the only thing that points at them.
    Column("storage_key", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("bytes", BigInteger, nullable=False),
    # What makes byte-identity assertable. Verified on read; a mismatch is `VA-STORE-004`.
    Column("checksum_sha256", Text, nullable=False),
    Column("width", Integer, nullable=True),
    Column("height", Integer, nullable=True),
    Column("duration_s", Numeric(6, 2), nullable=True),
    _timestamp("created_at", nullable=False, default=True),
    UniqueConstraint("storage_key", name="artifact_storage_key_uq"),
)

Index("artifact_job_kind_idx", artifact.c.job_id, artifact.c.kind, artifact.c.shot_index)

# --- checkpoint ---------------------------------------------------------------------------

checkpoint = Table(
    "checkpoint",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    # Equal to `job.id`. Not declared as a foreign key: LangGraph owns the thread identifier
    # and writes the checkpoint, and an FK here would make the checkpointer's write ordering
    # depend on the domain row already existing.
    Column("thread_id", UUID(as_uuid=True), nullable=False),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False),
    Column("node", Text, nullable=False),
    Column("seq", Integer, nullable=False),
    # `JobState`, never media bytes. Asserted at write time, because a checkpoint is
    # serialised into logs and traces and a byte payload in it defeats every redaction rule.
    Column("state", JSONB, nullable=False),
    Column("budget_used", JSONB, nullable=False),
    Column("failure_signatures", JSONB, nullable=False, server_default=_EMPTY_OBJECT),
    _timestamp("created_at", nullable=False, default=True),
    UniqueConstraint("thread_id", "seq", name="checkpoint_thread_seq_uq"),
)

Index("checkpoint_thread_seq_idx", checkpoint.c.thread_id, checkpoint.c.seq.desc())


ALL_TABLES: tuple[Table, ...] = (
    tenant,
    tenant_api_key,
    job,
    story_plan,
    beat,
    continuity_bible,
    shot,
    shot_attempt,
    artifact,
    checkpoint,
)
"""Every table, in dependency order — parents before children.

The order is the creation order and its reverse is the drop order, so a migration never has
to rediscover it and never gets it subtly wrong on the downgrade path.
"""

TABLE_NAMES: tuple[str, ...] = tuple(table.name for table in ALL_TABLES)


def table_by_name(name: str) -> Table:
    """Look one table up by SQL name, raising `KeyError` for an unknown one."""
    return metadata.tables[name]
