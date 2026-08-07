"""The six PostgreSQL enum types, declared once in Python.

`persistence.md` §2 defines six `CREATE TYPE ... AS ENUM` statements. Repeating those member
lists in a migration, in a model and in a test gives three places to disagree, and the way
they disagree is silent: a value the database accepts and the application never writes, or a
value the application writes and the database rejects at 3am.

So the members live here, once. The migration emits `CREATE TYPE` from these classes, the
SQLAlchemy column types bind to them, and the drift test introspects `pg_enum` and compares
against them. `StrEnum` because these values cross the driver boundary as strings and an
adapter that translates them is one more thing that can translate them wrongly.

Member *order* is significant and is preserved: PostgreSQL orders enum labels by the order of
declaration, so `ORDER BY status` depends on it, and reordering members in a later migration
is a behaviour change rather than a tidy-up.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM


class JobStatus(StrEnum):
    """Lifecycle of a job row. `terminal` is one state, not a family of them."""

    QUEUED = "queued"
    RUNNING = "running"
    TERMINAL = "terminal"


class JobOutcome(StrEnum):
    """How a terminal job ended. NULL while the job is not terminal."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED_NO_PROGRESS = "FAILED_NO_PROGRESS"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


class ShotStatus(StrEnum):
    """Lifecycle of one shot within a job."""

    PENDING = "pending"
    GENERATING = "generating"
    QC = "qc"
    ACCEPTED = "accepted"
    ABANDONED = "abandoned"


class BeatKind(StrEnum):
    """The four-beat story structure. Exactly four members, matching the four beats."""

    SETUP = "setup"
    DEVELOPMENT = "development"
    TURN = "turn"
    RESOLUTION = "resolution"


class AttemptState(StrEnum):
    """Lifecycle of one provider render attempt.

    `in_flight` is written *before* the provider call, not after it. That ordering is the
    whole point: a crash between the write and the provider's response leaves a row that
    names the request, so reconciliation can find the render that was already paid for
    instead of submitting a second one `[D-24]`, `[D-67]`.

    `orphaned` is the state for an attempt whose provider render can no longer be located —
    distinct from `failed`, because a failure is known to have produced nothing and an
    orphan may have produced something that was billed.
    """

    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ORPHANED = "orphaned"


class ArtifactKind(StrEnum):
    """What an artifact row points at in the object store."""

    FINAL_VIDEO = "final_video"
    SHOT_CLIP = "shot_clip"
    THUMBNAIL = "thumbnail"
    CONTINUITY_FRAME = "continuity_frame"
    STORY_PLAN_JSON = "story_plan_json"
    BIBLE_JSON = "bible_json"


JOB_STATUS = "job_status"
JOB_OUTCOME = "job_outcome"
SHOT_STATUS = "shot_status"
BEAT_KIND = "beat_kind"
ATTEMPT_STATE = "attempt_state"
ARTIFACT_KIND = "artifact_kind"

ENUM_TYPES: dict[str, type[StrEnum]] = {
    JOB_STATUS: JobStatus,
    JOB_OUTCOME: JobOutcome,
    SHOT_STATUS: ShotStatus,
    BEAT_KIND: BeatKind,
    ATTEMPT_STATE: AttemptState,
    ARTIFACT_KIND: ArtifactKind,
}
"""PostgreSQL type name to the Python enum that defines its members.

Keyed by the SQL type name because that is what `pg_type.typname` returns, so the drift test
is a dictionary comparison rather than a hand-maintained translation table.
"""


def enum_labels(type_name: str) -> tuple[str, ...]:
    """The declared labels of one enum type, in declaration order."""
    return tuple(member.value for member in ENUM_TYPES[type_name])


def pg_enum(type_name: str) -> ENUM:
    """The SQLAlchemy column type for one declared enum.

    `create_type=False` because type creation belongs to an explicit `CREATE TYPE` in the
    first migration, not to a side effect of the first table that happens to reference it.
    Letting SQLAlchemy create it implicitly means the type appears and disappears with a
    table, and a downgrade that drops the table then leaves the type behind — or drops one
    another table is still using.
    """
    return ENUM(*enum_labels(type_name), name=type_name, create_type=False)


def create_type_statement(type_name: str) -> str:
    """The `CREATE TYPE ... AS ENUM (...)` statement for one declared enum."""
    labels = ", ".join(f"'{label}'" for label in enum_labels(type_name))
    return f"CREATE TYPE {type_name} AS ENUM ({labels})"


def drop_type_statement(type_name: str) -> str:
    """The `DROP TYPE` statement for one declared enum."""
    return f"DROP TYPE {type_name}"
