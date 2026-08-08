"""`extract_final_frame_node` — last decodable frame, lossless PNG, uniform-frame rejection.
`assembly.md` §3, `[D-44]`/`[D-45]`, T2.3.

Exercises the real ffmpeg pipeline (`graph.frame_extraction`) against tiny synthetic clips built
with ffmpeg itself at test time, rather than mocking frame extraction — the behaviour under test
*is* "does the extracted frame reflect the actual bytes", so faking that would test nothing.
The database is a scripted connection dispatched by statement shape (mirrors
`test_persistence_repositories.py`'s `RecordingConnection`); this node only ever reads one
artifact (the clip) and writes one (the frame), so the dispatch table is small.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Executable, Result
from sqlalchemy.sql import ClauseElement

from video_agent.assembly.media_toolchain import resolve_binary
from video_agent.gateway.models import ArtifactRef
from video_agent.graph.deps import GraphDeps
from video_agent.graph.guard import JobHarness
from video_agent.graph.nodes import extract_final_frame_node
from video_agent.graph.state import GraphInvariantError, JobState, ShotState
from video_agent.harness.budget import BudgetCaps, BudgetLedger
from video_agent.persistence.ddl import postgres_dialect
from video_agent.persistence.enums import BeatKind
from video_agent.persistence.objects import sha256_of

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 1, tzinfo=UTC)


# --- real ffmpeg-built fixture clips ------------------------------------------------------


def _ffmpeg_clip(path: Path, *, source: str) -> None:
    ffmpeg = resolve_binary("ffmpeg")
    assert ffmpeg is not None, "ffmpeg must be on PATH (or FFMPEG_BINARY set) to run this test"
    argv = [ffmpeg, "-y", "-f", "lavfi", "-i", source, "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(argv, check=True, capture_output=True, timeout=30)


def _real_clip(path: Path) -> None:
    """A one-second synthetic test pattern — a genuinely non-uniform last frame."""
    _ffmpeg_clip(path, source="testsrc=duration=1:size=64x64:rate=10")


def _blank_clip(path: Path) -> None:
    """A one-second solid-black clip — every frame, including the last, is uniform."""
    _ffmpeg_clip(path, source="color=c=black:size=64x64:duration=1:rate=10")


# --- fake tenant-scoped connection, dispatched by statement shape --------------------------


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def mappings(self) -> _FakeResult:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def one(self) -> dict[str, Any]:
        assert self._rows, "the node expected a row and the script supplied none"
        return self._rows[0]


def _kind(sql: str) -> str:
    upper = sql.strip().upper()
    if "SET_CONFIG" in upper:
        return "set_tenant"
    if upper.startswith("SELECT") and "FROM ARTIFACT" in upper:
        return "select_artifact"
    if upper.startswith("INSERT INTO ARTIFACT"):
        return "insert_artifact"
    message = f"test double has no dispatch rule for statement: {sql[:160]}"
    raise AssertionError(message)


@dataclass
class _FakeConnection:
    replies: dict[str, list[list[dict[str, Any]]]]
    statements: list[ClauseElement] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)

    async def execute(self, statement: Executable, parameters: object = None) -> Result[Any]:
        del parameters
        assert isinstance(statement, ClauseElement)
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgres_dialect()))
        kind = _kind(sql)
        self.kinds.append(kind)
        if kind == "set_tenant":
            return cast("Result[Any]", _FakeResult([]))
        queue = self.replies.get(kind, [])
        if not queue:
            message = f"no scripted reply left for {kind!r}: {sql[:160]}"
            raise AssertionError(message)
        return cast("Result[Any]", _FakeResult(queue.pop(0)))

    def values(self, kind: str, occurrence: int = 0) -> dict[str, Any]:
        matches = [s for s, k in zip(self.statements, self.kinds, strict=True) if k == kind]
        compiled = matches[occurrence].compile(dialect=postgres_dialect())
        return dict(compiled.params)


class _BeginCtx:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@dataclass
class _FakeEngine:
    connection: _FakeConnection

    def begin(self) -> _BeginCtx:
        return _BeginCtx(self.connection)


@dataclass
class _FakeArtifactStore:
    """`providers.models.ArtifactStore`'s protocol, entirely in memory."""

    frames: dict[str, bytes] = field(default_factory=dict)
    written: list[tuple[str, bytes]] = field(default_factory=list)

    async def read(self, ref: ArtifactRef) -> bytes:
        return self.frames[ref.artifact_id]

    async def write(self, *, content_type: str, data: bytes) -> ArtifactRef:
        artifact_id = f"written-{len(self.written)}"
        self.written.append((content_type, data))
        return ArtifactRef(artifact_id=artifact_id, storage_key=f"scratch/{artifact_id}")


def _clip_artifact_row(artifact_id: UUID, storage_key: str) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "job_id": uuid4(),
        "kind": "shot_clip",
        "shot_index": 0,
        "storage_key": storage_key,
        "content_type": "video/mp4",
        "bytes": 1,
        "checksum_sha256": "deadbeef",
    }


def _frame_artifact_row(artifact_id: UUID, **overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": artifact_id,
        "job_id": uuid4(),
        "kind": "continuity_frame",
        "shot_index": 0,
        "storage_key": "frames/x.png",
        "content_type": "image/png",
        "bytes": 1,
        "checksum_sha256": "deadbeef",
    }
    row.update(overrides)
    return row


# --- state / deps builders --------------------------------------------------------------------


def _ledger() -> BudgetLedger:
    caps = BudgetCaps(
        max_iterations=10, max_wall_clock_s=3600.0, max_tokens=50_000, max_usd=Decimal(20)
    )
    return BudgetLedger(caps=caps, started_at=NOW)


def _shots(*, clip_artifact_id: UUID | None) -> tuple[ShotState, ...]:
    kinds = (BeatKind.SETUP, BeatKind.DEVELOPMENT, BeatKind.TURN, BeatKind.RESOLUTION)
    shots = [ShotState(index=i, beat_kind=kind) for i, kind in enumerate(kinds)]
    shots[0] = shots[0].model_copy(update={"clip_artifact_id": clip_artifact_id})
    return tuple(shots)


def _state(*, clip_artifact_id: UUID | None) -> JobState:
    return JobState(
        job_id=uuid4(),
        tenant_id=uuid4(),
        trace_id="trace-1",
        prompt="a lighthouse keeper's last night on watch",
        shots=_shots(clip_artifact_id=clip_artifact_id),
        budget=_ledger(),
    )


def _deps(connection: _FakeConnection, artifacts: _FakeArtifactStore) -> GraphDeps:
    return GraphDeps(
        engine=cast(Any, _FakeEngine(connection)),
        gateway=cast(Any, None),
        checkpointer=cast(Any, None),
        harness=JobHarness(job_id=uuid4(), shots_required=4),
        now=lambda: NOW,
        providers=cast(Any, None),
        artifacts=cast(Any, artifacts),
    )


# --- tests ---------------------------------------------------------------------------------


async def test_happy_path_extracts_and_records_the_final_frame(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.mp4"
    _real_clip(clip_path)
    clip_bytes = clip_path.read_bytes()

    clip_artifact_id = uuid4()
    frame_artifact_id = uuid4()
    state = _state(clip_artifact_id=clip_artifact_id)
    artifacts = _FakeArtifactStore(frames={str(clip_artifact_id): clip_bytes})
    connection = _FakeConnection(
        replies={
            "select_artifact": [[_clip_artifact_row(clip_artifact_id, "clips/x.mp4")]],
            "insert_artifact": [[_frame_artifact_row(frame_artifact_id)]],
        }
    )
    deps = _deps(connection, artifacts)

    partial = await extract_final_frame_node(state, deps)

    updated_shot = next(s for s in partial["shots"] if s.index == 0)
    assert updated_shot.final_frame_artifact_id == frame_artifact_id
    assert "degraded" not in partial

    # the frame actually made it through: something was written to the artifact store, and
    # the checksum recorded is the checksum of exactly those bytes.
    assert artifacts.written, "extract_final_frame_node never wrote a frame to the store"
    content_type, png_bytes = artifacts.written[0]
    assert content_type == "image/png"
    assert png_bytes.startswith(b"\x89PNG")
    recorded = connection.values("insert_artifact")
    assert recorded["checksum_sha256"] == sha256_of(png_bytes)
    assert recorded["bytes"] == len(png_bytes)


async def test_uniform_last_frame_is_rejected_and_leaves_no_anchor(tmp_path: Path) -> None:
    clip_path = tmp_path / "blank.mp4"
    _blank_clip(clip_path)
    clip_bytes = clip_path.read_bytes()

    clip_artifact_id = uuid4()
    state = _state(clip_artifact_id=clip_artifact_id)
    artifacts = _FakeArtifactStore(frames={str(clip_artifact_id): clip_bytes})
    connection = _FakeConnection(
        replies={"select_artifact": [[_clip_artifact_row(clip_artifact_id, "clips/blank.mp4")]]}
    )
    deps = _deps(connection, artifacts)

    partial = await extract_final_frame_node(state, deps)

    assert partial == {
        "degraded": True,
        "degraded_reason": partial["degraded_reason"],
    }
    assert "no non-uniform frame" in partial["degraded_reason"]
    assert not artifacts.written, "a uniform frame must never be catalogued as an anchor"


async def test_missing_clip_artifact_id_is_a_programming_error() -> None:
    state = _state(clip_artifact_id=None)
    deps = _deps(_FakeConnection(replies={}), _FakeArtifactStore())

    with pytest.raises(GraphInvariantError):
        await extract_final_frame_node(state, deps)
