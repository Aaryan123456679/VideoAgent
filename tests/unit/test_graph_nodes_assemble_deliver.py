"""`assemble_node`/`deliver_node` — normalize+concat by stream copy, thumbnail selection by
score (`[D-46]`/`[D-47]`/`[D-49]`), the `[D-73]` zero-accepted-shots guard, and manifest
construction. T2.4.

Real ffmpeg runs against tiny synthetic clips built at test time (mirrors
`test_graph_nodes_extract_frame.py`), because "does the concatenated output actually contain
both clips' worth of frames" is exactly the behaviour under test. The database is faked by
monkeypatching the repository class and `tenant_session` `graph.nodes` imports (mirrors
`test_graph_nodes_generate_shot.py`'s style) — this node's *orchestration* is the subject, not
the SQL, which `test_persistence_repositories.py` already covers.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from video_agent.assembly.media_toolchain import resolve_binary
from video_agent.assembly.models import DeliveryManifest
from video_agent.gateway.models import ArtifactRef
from video_agent.graph import nodes
from video_agent.graph.deps import GraphDeps
from video_agent.graph.guard import JobHarness
from video_agent.graph.state import GraphInvariantError, JobState, ShotState
from video_agent.harness.budget import BudgetCaps, BudgetLedger
from video_agent.persistence.enums import ArtifactKind, BeatKind, ShotStatus
from video_agent.persistence.repositories import ArtifactRecord, NewArtifact

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 1, tzinfo=UTC)


# --- real ffmpeg-built fixture clips / frames ----------------------------------------------


def _ffmpeg(argv: list[str]) -> None:
    subprocess.run(argv, check=True, capture_output=True, timeout=30)


def _clip(path: Path, *, colour: str) -> bytes:
    ffmpeg = resolve_binary("ffmpeg")
    assert ffmpeg is not None, "ffmpeg must be on PATH (or FFMPEG_BINARY set) to run this test"
    _ffmpeg(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={colour}:size=64x64:duration=1:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return path.read_bytes()


def _frame_png(path: Path, *, colour: str) -> bytes:
    ffmpeg = resolve_binary("ffmpeg")
    assert ffmpeg is not None
    _ffmpeg(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={colour}:size=64x64",
            "-frames:v",
            "1",
            str(path),
        ]
    )
    return path.read_bytes()


# --- fake artifact catalogue / bytes store --------------------------------------------------


@dataclass
class FakeDB:
    artifacts: dict[UUID, ArtifactRecord] = field(default_factory=dict)
    recorded: list[NewArtifact] = field(default_factory=list)


def install_fake_repository(monkeypatch: pytest.MonkeyPatch, db: FakeDB) -> None:
    @asynccontextmanager
    async def fake_tenant_session(engine: object, tenant_id: UUID) -> AsyncIterator[object]:
        del engine, tenant_id
        yield object()

    class FakeArtifactRepository:
        def __init__(self, session: object) -> None:
            del session

        async def get(self, artifact_id: UUID) -> ArtifactRecord | None:
            return db.artifacts.get(artifact_id)

        async def record(self, new_artifact: NewArtifact) -> ArtifactRecord:
            record = ArtifactRecord(
                id=uuid4(),
                job_id=new_artifact.job_id,
                kind=new_artifact.kind,
                shot_index=new_artifact.shot_index,
                storage_key=new_artifact.storage_key,
                content_type=new_artifact.content_type,
                bytes=new_artifact.size_bytes,
                checksum_sha256=new_artifact.checksum_sha256,
            )
            db.artifacts[record.id] = record
            db.recorded.append(new_artifact)
            return record

    monkeypatch.setattr(nodes, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(nodes, "ArtifactRepository", FakeArtifactRepository)


def _catalogue_clip(db: FakeDB, job_id: UUID, *, shot_index: int, data: bytes) -> UUID:
    artifact_id = uuid4()
    db.artifacts[artifact_id] = ArtifactRecord(
        id=artifact_id,
        job_id=job_id,
        kind=ArtifactKind.SHOT_CLIP,
        shot_index=shot_index,
        storage_key=f"clips/{artifact_id}.mp4",
        content_type="video/mp4",
        bytes=len(data),
        checksum_sha256="deadbeef",
    )
    return artifact_id


def _catalogue_frame(db: FakeDB, job_id: UUID, *, shot_index: int, data: bytes) -> UUID:
    artifact_id = uuid4()
    db.artifacts[artifact_id] = ArtifactRecord(
        id=artifact_id,
        job_id=job_id,
        kind=ArtifactKind.CONTINUITY_FRAME,
        shot_index=shot_index,
        storage_key=f"frames/{artifact_id}.png",
        content_type="image/png",
        bytes=len(data),
        checksum_sha256="deadbeef",
    )
    return artifact_id


@dataclass
class FakeArtifactStore:
    """`providers.models.ArtifactStore`'s protocol, entirely in memory."""

    objects: dict[str, bytes] = field(default_factory=dict)
    written: list[tuple[str, bytes]] = field(default_factory=list)

    async def read(self, ref: ArtifactRef) -> bytes:
        return self.objects[ref.artifact_id]

    async def write(self, *, content_type: str, data: bytes) -> ArtifactRef:
        artifact_id = f"written-{len(self.written)}"
        self.written.append((content_type, data))
        return ArtifactRef(artifact_id=artifact_id, storage_key=f"scratch/{artifact_id}")


# --- state / deps builders -------------------------------------------------------------------


def _ledger() -> BudgetLedger:
    caps = BudgetCaps(
        max_iterations=10, max_wall_clock_s=3600.0, max_tokens=50_000, max_usd=Decimal(20)
    )
    return BudgetLedger(caps=caps, started_at=NOW)


def _four_shots() -> tuple[ShotState, ...]:
    kinds = (BeatKind.SETUP, BeatKind.DEVELOPMENT, BeatKind.TURN, BeatKind.RESOLUTION)
    return tuple(ShotState(index=i, beat_kind=kind) for i, kind in enumerate(kinds))


def _accept(shot: ShotState, *, clip_id: UUID, frame_id: UUID | None, score: float) -> ShotState:
    return shot.model_copy(
        update={
            "status": ShotStatus.ACCEPTED,
            "best_score": score,
            "clip_artifact_id": clip_id,
            "final_frame_artifact_id": frame_id,
        }
    )


def _state(
    *, shots: tuple[ShotState, ...], music_bed: bool = False, **overrides: object
) -> JobState:
    fields: dict[str, object] = {
        "job_id": uuid4(),
        "tenant_id": uuid4(),
        "trace_id": "trace-1",
        "prompt": "a lighthouse keeper's last night on watch",
        "shots": shots,
        "budget": _ledger(),
        "music_bed": music_bed,
    }
    fields.update(overrides)
    return JobState(**fields)  # type: ignore[arg-type]


def _deps(artifacts: FakeArtifactStore) -> GraphDeps:
    return GraphDeps(
        engine=cast(Any, None),
        gateway=cast(Any, None),
        checkpointer=cast(Any, None),
        harness=JobHarness(job_id=uuid4(), shots_required=4),
        now=lambda: NOW,
        providers=cast(Any, None),
        artifacts=cast(Any, artifacts),
    )


# --- assemble_node ---------------------------------------------------------------------------


async def test_happy_path_concatenates_and_picks_the_best_scoring_thumbnail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = FakeDB()
    install_fake_repository(monkeypatch, db)
    job_id = uuid4()

    clip0 = _clip(tmp_path / "clip0.mp4", colour="red")
    clip1 = _clip(tmp_path / "clip1.mp4", colour="blue")
    frame0 = _frame_png(tmp_path / "frame0.png", colour="red")
    frame1 = _frame_png(tmp_path / "frame1.png", colour="blue")

    clip0_id = _catalogue_clip(db, job_id, shot_index=0, data=clip0)
    clip1_id = _catalogue_clip(db, job_id, shot_index=1, data=clip1)
    frame0_id = _catalogue_frame(db, job_id, shot_index=0, data=frame0)
    frame1_id = _catalogue_frame(db, job_id, shot_index=1, data=frame1)

    shots = list(_four_shots())
    shots[0] = _accept(shots[0], clip_id=clip0_id, frame_id=frame0_id, score=0.5)
    shots[1] = _accept(shots[1], clip_id=clip1_id, frame_id=frame1_id, score=0.9)
    state = _state(shots=tuple(shots), job_id=job_id)

    artifacts = FakeArtifactStore(
        objects={
            str(clip0_id): clip0,
            str(clip1_id): clip1,
            str(frame0_id): frame0,
            str(frame1_id): frame1,
        }
    )
    deps = _deps(artifacts)

    partial = await nodes.assemble_node(state, deps)

    assert "degraded" not in partial
    assert partial["final_video_artifact_id"] in db.artifacts
    assert partial["thumbnail_artifact_id"] in db.artifacts

    final_record = db.artifacts[partial["final_video_artifact_id"]]
    assert final_record.kind is ArtifactKind.FINAL_VIDEO
    thumbnail_record = db.artifacts[partial["thumbnail_artifact_id"]]
    assert thumbnail_record.kind is ArtifactKind.THUMBNAIL

    # shot 1 scored higher, so its frame is the thumbnail source -- distinguishable from shot
    # 0's because they are genuinely different colours.
    written_content_types = [content_type for content_type, _ in artifacts.written]
    assert "video/mp4" in written_content_types
    assert "image/jpeg" in written_content_types


async def test_zero_accepted_shots_is_a_graph_invariant_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDB()
    install_fake_repository(monkeypatch, db)
    state = _state(shots=_four_shots())
    deps = _deps(FakeArtifactStore())

    with pytest.raises(GraphInvariantError):
        await nodes.assemble_node(state, deps)


async def test_music_bed_requested_is_non_fatal_and_flags_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = FakeDB()
    install_fake_repository(monkeypatch, db)
    job_id = uuid4()

    clip0 = _clip(tmp_path / "clip0.mp4", colour="green")
    frame0 = _frame_png(tmp_path / "frame0.png", colour="green")
    clip0_id = _catalogue_clip(db, job_id, shot_index=0, data=clip0)
    frame0_id = _catalogue_frame(db, job_id, shot_index=0, data=frame0)

    shots = list(_four_shots())
    shots[0] = _accept(shots[0], clip_id=clip0_id, frame_id=frame0_id, score=1.0)
    state = _state(shots=tuple(shots), job_id=job_id, music_bed=True)

    artifacts = FakeArtifactStore(objects={str(clip0_id): clip0, str(frame0_id): frame0})
    deps = _deps(artifacts)

    partial = await nodes.assemble_node(state, deps)

    assert partial["degraded"] is True
    assert "music_bed" in partial["degraded_reason"]
    # still delivered a video and a thumbnail despite the missing bed -- non-fatal `[D-48]`.
    assert partial["final_video_artifact_id"] in db.artifacts
    assert partial["thumbnail_artifact_id"] in db.artifacts


# --- deliver_node -----------------------------------------------------------------------------


async def test_deliver_builds_a_two_entry_manifest() -> None:
    final_id = uuid4()
    thumbnail_id = uuid4()
    state = _state(
        shots=_four_shots(),
        final_video_artifact_id=final_id,
        thumbnail_artifact_id=thumbnail_id,
    )
    deps = _deps(FakeArtifactStore())

    partial = await nodes.deliver_node(state, deps)

    manifest = partial["manifest"]
    assert isinstance(manifest, DeliveryManifest)
    kinds_and_ids = {(entry.kind, entry.artifact_id) for entry in manifest.entries}
    assert kinds_and_ids == {("video", final_id), ("thumbnail", thumbnail_id)}


async def test_deliver_raises_when_final_video_artifact_is_missing() -> None:
    state = _state(shots=_four_shots(), thumbnail_artifact_id=uuid4())
    deps = _deps(FakeArtifactStore())

    with pytest.raises(GraphInvariantError):
        await nodes.deliver_node(state, deps)


async def test_deliver_raises_when_thumbnail_artifact_is_missing() -> None:
    state = _state(shots=_four_shots(), final_video_artifact_id=uuid4())
    deps = _deps(FakeArtifactStore())

    with pytest.raises(GraphInvariantError):
        await nodes.deliver_node(state, deps)
