"""S0.5.2 — S0.5.6: the schema is what `persistence.md` §2 says it is.

Every expectation in this module is written out **independently** of the code it checks. The
enum members are literal lists copied from the LLD rather than read back from `ENUM_TYPES`,
the constraint expressions are literal strings rather than rendered from the `Table` objects.
A test that derives its expectation from the thing under test passes no matter what that thing
says, which is the failure mode the T0.1 review found and the reason these lists are duplicated
on purpose. The duplication *is* the check.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import CheckConstraint, Column, DefaultClause, Index, Table, UniqueConstraint

from video_agent.persistence import enums
from video_agent.persistence.ddl import bytea_columns
from video_agent.persistence.schema import (
    ALL_TABLES,
    BIBLE_CONTENT_COLUMNS,
    TABLE_NAMES,
    metadata,
)

# The six enum types, transcribed from `persistence.md` §2 lines 48-54.
LLD_ENUM_MEMBERS: dict[str, tuple[str, ...]] = {
    "job_status": ("queued", "running", "terminal"),
    "job_outcome": ("SUCCESS", "PARTIAL", "FAILED_NO_PROGRESS", "FAILED", "ESCALATED"),
    "shot_status": ("pending", "generating", "qc", "accepted", "abandoned"),
    "beat_kind": ("setup", "development", "turn", "resolution"),
    "attempt_state": ("in_flight", "succeeded", "failed", "orphaned"),
    "artifact_kind": (
        "final_video",
        "shot_clip",
        "thumbnail",
        "continuity_frame",
        "story_plan_json",
        "bible_json",
    ),
}

LLD_TABLES: frozenset[str] = frozenset(
    {
        "tenant",
        "tenant_api_key",
        "job",
        "story_plan",
        "beat",
        "continuity_bible",
        "shot",
        "shot_attempt",
        "artifact",
        "checkpoint",
    }
)


def _table(name: str) -> Table:
    return metadata.tables[name]


def _server_default(column: Column[Any]) -> str:
    """The rendered `DEFAULT` clause of a column, or a failure that says which column."""
    default = column.server_default
    assert isinstance(default, DefaultClause), f"{column} has no server default"
    return str(default.arg)


def _check_expressions(table: Table) -> dict[str, str]:
    return {
        str(constraint.name): str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str)
    }


def _unique_columns(table: Table) -> dict[str, tuple[str, ...]]:
    return {
        str(constraint.name): tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint) and isinstance(constraint.name, str)
    }


def _indexes(table: Table) -> dict[str, Index]:
    """Indexes keyed by their name as a plain `str`, which is how the tests spell them."""
    return {str(index.name): index for index in table.indexes}


# --- Enum types ---------------------------------------------------------------------------


def test_exactly_six_enum_types_are_declared() -> None:
    """`persistence.md` §2 declares six. A seventh belongs in the doc before the schema."""
    assert set(enums.ENUM_TYPES) == set(LLD_ENUM_MEMBERS)


@pytest.mark.parametrize("type_name", sorted(LLD_ENUM_MEMBERS))
def test_enum_members_match_lld(type_name: str) -> None:
    """Members and their order both match. Order is significant: PostgreSQL sorts by it."""
    assert enums.enum_labels(type_name) == LLD_ENUM_MEMBERS[type_name]


def test_create_type_statement_names_every_member() -> None:
    """The DDL the migration emits carries the members, not just the type name."""
    statement = enums.create_type_statement("beat_kind")
    assert statement.startswith("CREATE TYPE beat_kind AS ENUM (")
    for member in LLD_ENUM_MEMBERS["beat_kind"]:
        assert f"'{member}'" in statement


# --- Table inventory and the D-51 denormalisation --------------------------------------


def test_schema_holds_exactly_the_lld_tables() -> None:
    """A table added without a documentation change fails here, which is the CDR drift gate."""
    assert set(TABLE_NAMES) == LLD_TABLES


@pytest.mark.parametrize("table", ALL_TABLES, ids=lambda table: str(table.name))
def test_every_table_carries_tenant_id(table: Table) -> None:
    """[D-51] denormalises `tenant_id` onto every table so the RLS policy never joins.

    Including `tenant` itself, where the column is the primary key: the policy is written in
    terms of a local `tenant_id` and a table that has to be joined to supply one is a table
    whose isolation depends on the planner.
    """
    if table.name == "tenant":
        assert "id" in table.c
        return
    assert "tenant_id" in table.c, f"{table.name} has no tenant_id; RLS would need a join"
    assert not table.c.tenant_id.nullable


@pytest.mark.parametrize(
    "table",
    [table for table in ALL_TABLES if table.name != "tenant"],
    ids=lambda table: str(table.name),
)
def test_every_tenant_id_references_the_tenant_table(table: Table) -> None:
    """[D-70]: the column referenced nothing before the tenant table was defined."""
    targets = {key.column.table.name for key in table.c.tenant_id.foreign_keys}
    assert targets == {"tenant"}


def test_no_bytea_columns_anywhere() -> None:
    """Postgres holds metadata and keys; the object store holds bytes. `[persistence.md §6]`"""
    assert bytea_columns() == ()


# --- job ------------------------------------------------------------------------------------


def test_idempotency_constraint_is_scoped_to_the_tenant() -> None:
    """`job_idem_uq` is per tenant, not global. `[CPS §Non-negotiables]`

    A global constraint would let one tenant's choice of key deny that key to every other
    tenant, which is a cross-tenant effect from a table that is otherwise fully isolated.
    """
    assert _unique_columns(_table("job"))["job_idem_uq"] == ("tenant_id", "idempotency_key")


def test_job_indexes_exist_and_status_index_is_partial() -> None:
    """Both indexes from `persistence.md` §2, and the partial predicate on the second."""
    indexes = _indexes(_table("job"))
    assert set(indexes) == {"job_tenant_created_idx", "job_status_idx"}
    predicate = indexes["job_status_idx"].dialect_options["postgresql"]["where"]
    assert "terminal" in str(predicate)


def test_budget_columns_are_jsonb_and_epoch_defaults_zero() -> None:
    """`budget_epoch` starts at 0 and is only ever incremented on a resume grant. `[D-25]`"""
    job = _table("job")
    assert str(job.c.budget_caps.type) == "JSONB"
    assert str(job.c.budget_used.type) == "JSONB"
    assert not job.c.budget_caps.nullable
    assert not job.c.budget_used.nullable
    assert _server_default(job.c.budget_epoch) == "0"


# --- story_plan and beat ---------------------------------------------------------------------


def test_total_duration_is_pinned_to_forty_seconds() -> None:
    """An equality, not a range. `[PRD]` fixes the film at forty seconds."""
    assert _check_expressions(_table("story_plan"))["story_plan_total_duration_ck"] == (
        "total_duration_s = 40.00"
    )


def test_beat_duration_is_pinned_to_ten_seconds() -> None:
    """`[D-03]` fixes the shot length; 9.99 is not a near miss, it is a wrong-length film."""
    assert _check_expressions(_table("beat"))["beat_duration_ck"] == "duration_s = 10.00"


def test_beat_index_is_bounded_to_the_four_beat_structure() -> None:
    assert _check_expressions(_table("beat"))["beat_idx_ck"] == "idx BETWEEN 0 AND 3"


def test_one_plan_per_job_and_beats_unique_within_it() -> None:
    assert _unique_columns(_table("story_plan"))["story_plan_job_uq"] == ("job_id",)
    assert _unique_columns(_table("beat"))["beat_plan_idx_uq"] == ("story_plan_id", "idx")


def test_plan_is_attributable_to_a_model_and_prompt_version() -> None:
    """A plan nobody can attribute to a model and a prompt version is not reproducible."""
    plan = _table("story_plan")
    assert not plan.c.model_alias.nullable
    assert not plan.c.prompt_version.nullable


@pytest.mark.parametrize(("child", "parent"), [("story_plan", "job"), ("beat", "story_plan")])
def test_plan_and_beats_cascade_from_their_parent(child: str, parent: str) -> None:
    """Deleting a job takes the plan and its beats with it, in the database not the code."""
    column = "job_id" if parent == "job" else "story_plan_id"
    keys = list(_table(child).c[column].foreign_keys)
    assert [key.ondelete for key in keys] == ["CASCADE"]


# --- continuity_bible -------------------------------------------------------------------------


@pytest.mark.parametrize("column", BIBLE_CONTENT_COLUMNS)
def test_bible_content_columns_are_not_null(column: str) -> None:
    """Six dimensions plus the negative constraints; none of them optional."""
    assert not _table("continuity_bible").c[column].nullable


def test_bible_negative_constraints_default_to_an_empty_array() -> None:
    column = _table("continuity_bible").c.negative_constraints
    assert _server_default(column) == "'[]'::jsonb"


def test_one_bible_per_job() -> None:
    assert _unique_columns(_table("continuity_bible"))["continuity_bible_job_uq"] == ("job_id",)


# --- shot and shot_attempt ----------------------------------------------------------------------


def test_repair_cap_is_a_database_constraint() -> None:
    """`[D-01]`: two repairs. The database is the last line of defence for the cap."""
    assert _check_expressions(_table("shot"))["shot_repairs_used_ck"] == "repairs_used <= 2"


def test_attempt_number_is_bounded_to_one_plus_two_repairs() -> None:
    assert _check_expressions(_table("shot_attempt"))["shot_attempt_no_ck"] == (
        "attempt_no BETWEEN 1 AND 3"
    )


def test_request_fingerprint_is_unique() -> None:
    """`[D-24]`: the constraint that makes at-least-once queue delivery `[D-67]` safe.

    Without it, every worker crash between the provider call and the `XACK` is a second paid
    render for work that was already done and already billed.
    """
    assert _unique_columns(_table("shot_attempt"))["shot_attempt_fingerprint_uq"] == (
        "request_fingerprint",
    )


def test_one_attempt_row_per_shot_and_attempt_number() -> None:
    assert _unique_columns(_table("shot_attempt"))["shot_attempt_no_uq"] == (
        "shot_id",
        "attempt_no",
    )


def test_seed_is_nullable_and_seed_supported_is_not() -> None:
    """`[D-59]`: a NULL seed is honest, a fabricated one is a false reproducibility claim."""
    attempt = _table("shot_attempt")
    assert attempt.c.seed.nullable, "a NOT NULL seed would force a fabricated value"
    assert not attempt.c.seed_supported.nullable
    assert _server_default(attempt.c.seed_supported) == "false"


def test_provider_project_id_exists_and_is_nullable() -> None:
    """`[D-59]`, `[D-24]`: the reconciliation handle. NULL until the provider answers."""
    assert _table("shot_attempt").c.provider_project_id.nullable


def test_cost_columns_follow_the_provisional_then_final_shape() -> None:
    """`[D-60]`: `credits_charged` NULL means not yet charged, not charged zero."""
    attempt = _table("shot_attempt")
    assert not attempt.c.cost_usd.nullable
    assert _server_default(attempt.c.cost_usd) == "0"
    assert attempt.c.credits_charged.nullable
    assert _server_default(attempt.c.cost_is_final) == "false"


@pytest.mark.parametrize(
    ("table_name", "constraint", "expression"),
    [
        ("shot", "shot_best_score_ck", "best_score BETWEEN 0 AND 1"),
        ("shot_attempt", "shot_attempt_qc_score_ck", "qc_score BETWEEN 0 AND 1"),
    ],
)
def test_scores_are_bounded_to_the_unit_interval(
    table_name: str, constraint: str, expression: str
) -> None:
    assert _check_expressions(_table(table_name))[constraint] == expression


# --- artifact and checkpoint -----------------------------------------------------------------


def test_storage_key_is_unique_and_checksum_is_required() -> None:
    """The checksum is what makes byte identity assertable. `[PRD §Resilience]`"""
    artifact = _table("artifact")
    assert _unique_columns(artifact)["artifact_storage_key_uq"] == ("storage_key",)
    assert not artifact.c.checksum_sha256.nullable


def test_artifact_lookup_index_exists() -> None:
    indexes = _indexes(_table("artifact"))
    assert "artifact_job_kind_idx" in indexes
    columns = tuple(column.name for column in indexes["artifact_job_kind_idx"].columns)
    assert columns == ("job_id", "kind", "shot_index")


def test_checkpoint_sequence_is_unique_per_thread() -> None:
    """A replayed node cannot fork the checkpoint history for a thread."""
    assert _unique_columns(_table("checkpoint"))["checkpoint_thread_seq_uq"] == (
        "thread_id",
        "seq",
    )


@pytest.mark.parametrize("column", ["state", "budget_used", "failure_signatures"])
def test_checkpoint_json_columns_are_not_null(column: str) -> None:
    checkpoint = _table("checkpoint")
    assert str(checkpoint.c[column].type) == "JSONB"
    assert not checkpoint.c[column].nullable


def test_checkpoint_index_exists() -> None:
    indexes = set(_indexes(_table("checkpoint")))
    assert "checkpoint_thread_seq_idx" in indexes


# --- Timestamps -------------------------------------------------------------------------------


def test_every_timestamp_column_is_timezone_aware() -> None:
    """A naive timestamp means whatever the writing session's time zone happened to be."""
    naive: list[str] = []
    for table in ALL_TABLES:
        for column in table.columns:
            column_type: Any = column.type
            if getattr(column_type, "__visit_name__", "") == "TIMESTAMP" and not getattr(
                column_type, "timezone", False
            ):
                naive.append(f"{table.name}.{column.name}")
    assert naive == []
