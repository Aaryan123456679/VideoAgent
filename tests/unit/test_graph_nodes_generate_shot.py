"""`generate_shot_node` — compact, non-exhaustive coverage of `graph.md` §4's three-phase
write sequence, chaining vs. degraded-text-only (`[D-05]`), and provider-degrade propagation.

DB access is faked by monkeypatching the repository classes and `tenant_session` that
`graph.nodes` imports, rather than scripting SQLAlchemy statements against a recording
connection (`test_persistence_repositories.py`'s style): this test's subject is the node's
*orchestration* — which repository gets which values, in which order, folded into the returned
partial dict and the mutated budget — not the SQL those repositories build, which
`test_persistence_repositories.py` already covers for the two new methods added alongside this
node (`ShotRepository.ensure`, `StoryPlanRepository.get_beat_id`, `ArtifactRepository.get`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from tests.providers_doubles import FakeProvider, a_result, profile
from video_agent.gateway.models import ArtifactRef
from video_agent.graph import nodes
from video_agent.graph.deps import GraphDeps
from video_agent.graph.guard import JobHarness
from video_agent.graph.state import JobState, ShotState
from video_agent.harness.budget import BudgetCaps, BudgetLedger
from video_agent.persistence.enums import AttemptState, ShotStatus
from video_agent.persistence.enums import BeatKind as PersistenceBeatKind
from video_agent.persistence.repositories import ArtifactRecord as PersistedArtifactRecord
from video_agent.persistence.repositories import (
    AttemptClaim,
    AttemptRequest,
    CheckpointRecord,
    CostSettlement,
    NewArtifact,
    NewCheckpoint,
    ProviderSubmission,
    ShotAttemptRecord,
    ShotRecord,
)
from video_agent.planning.bible import compute_content_hash
from video_agent.planning.models import (
    Beat,
    CameraMove,
    CharacterSpec,
    ContinuityBible,
    LensLanguageSpec,
    LightingSpec,
    LocationSpec,
    PaletteSpec,
    StoryPlan,
    WardrobeSpec,
)
from video_agent.planning.models import BeatKind as PlanBeatKind
from video_agent.providers.models import Capability, ShotRequest, ShotResult

NOW = datetime(2026, 8, 8, tzinfo=UTC)


def a_ledger() -> BudgetLedger:
    caps = BudgetCaps(
        max_iterations=10, max_wall_clock_s=3600, max_tokens=10_000, max_usd=Decimal(10)
    )
    return BudgetLedger(caps=caps, started_at=NOW)


def a_bible(job_id: UUID) -> ContinuityBible:
    provisional = ContinuityBible(
        job_id=job_id,
        character=CharacterSpec(
            name="Mara",
            age_appearance="thirties",
            build="lean",
            skin_tone="olive",
            hair="short black hair",
            facial_features="sharp jaw, freckles across the nose",
        ),
        wardrobe=WardrobeSpec(
            garments=["oilskin coat"], colours=["yellow"], materials=["canvas"], condition="worn"
        ),
        location=LocationSpec(
            setting="lighthouse", time_of_day="dusk", architecture_or_terrain="cliff"
        ),
        lighting=LightingSpec(
            key_light="lamp",
            direction="side",
            quality="hard",
            colour_temperature="warm",
            contrast_ratio="high",
        ),
        palette=PaletteSpec(dominant=["grey", "amber"], saturation="low", grade="cool"),
        lens_language=LensLanguageSpec(
            focal_length="35mm",
            aperture_feel="shallow",
            framing="medium",
            movement_style="handheld",
        ),
        content_hash="pending",
        locked_at=NOW,
        model_alias="reasoning-high",
        prompt_version="v1",
    )
    return provisional.model_copy(update={"content_hash": compute_content_hash(provisional)})


def a_story_plan(job_id: UUID) -> StoryPlan:
    beats = [
        Beat(index=i, kind=kind, action="a beat action long enough", camera_move=CameraMove.STATIC)
        for i, kind in enumerate(
            (
                PlanBeatKind.SETUP,
                PlanBeatKind.DEVELOPMENT,
                PlanBeatKind.TURN,
                PlanBeatKind.RESOLUTION,
            )
        )
    ]
    return StoryPlan(
        job_id=job_id,
        logline="a lighthouse keeper's last night on watch",
        beats=beats,
        model_alias="reasoning-high",
        prompt_version="v1",
        created_at=NOW,
    )


def four_shots() -> tuple[ShotState, ...]:
    kinds = (
        PersistenceBeatKind.SETUP,
        PersistenceBeatKind.DEVELOPMENT,
        PersistenceBeatKind.TURN,
        PersistenceBeatKind.RESOLUTION,
    )
    return tuple(ShotState(index=i, beat_kind=kind) for i, kind in enumerate(kinds))


def a_state(**overrides: object) -> JobState:
    job_id = cast(UUID, overrides.get("job_id", uuid4()))
    bible = a_bible(job_id)
    fields: dict[str, object] = {
        "job_id": job_id,
        "tenant_id": uuid4(),
        "trace_id": "trace-1",
        "prompt": "a short film about a lighthouse",
        "budget": a_ledger(),
        "story_plan": a_story_plan(job_id),
        "bible": bible,
        "bible_hash": bible.content_hash,
        "shots": four_shots(),
    }
    fields.update(overrides)
    return JobState(**fields)  # type: ignore[arg-type]


@dataclass
class FakeDB:
    """In-memory stand-in for the tables `generate_shot_node`'s repositories touch."""

    beat_ids: dict[tuple[UUID, int], UUID] = field(default_factory=dict)
    shots: dict[tuple[UUID, int], ShotRecord] = field(default_factory=dict)
    attempts: dict[str, ShotAttemptRecord] = field(default_factory=dict)
    artifacts: dict[UUID, PersistedArtifactRecord] = field(default_factory=dict)
    checkpoints: list[CheckpointRecord] = field(default_factory=list)
    submissions: list[Any] = field(default_factory=list)
    settlements: list[Any] = field(default_factory=list)


def install_fake_repositories(monkeypatch: pytest.MonkeyPatch, db: FakeDB) -> None:
    @asynccontextmanager
    async def fake_tenant_session(engine: object, tenant_id: UUID) -> AsyncIterator[object]:
        del engine, tenant_id
        yield object()

    class FakeStoryPlanRepository:
        def __init__(self, session: object) -> None:
            del session

        async def get_beat_id(self, job_id: UUID, idx: int) -> UUID | None:
            return db.beat_ids.get((job_id, idx))

    class FakeShotRepository:
        def __init__(self, session: object) -> None:
            del session

        async def ensure(self, *, job_id: UUID, beat_id: UUID, idx: int) -> ShotRecord:
            del beat_id
            key = (job_id, idx)
            if key not in db.shots:
                db.shots[key] = ShotRecord(
                    id=uuid4(),
                    job_id=job_id,
                    idx=idx,
                    status="pending",
                    attempts_used=0,
                    repairs_used=0,
                    best_attempt_id=None,
                    best_score=None,
                )
            return db.shots[key]

    class FakeShotAttemptRepository:
        def __init__(self, session: object) -> None:
            del session

        async def claim(self, request: AttemptRequest) -> AttemptClaim:
            existing = db.attempts.get(request.request_fingerprint)
            if existing is not None:
                return AttemptClaim(attempt=existing, adopted=True)
            record = ShotAttemptRecord(
                id=uuid4(),
                shot_id=request.shot_id,
                job_id=request.job_id,
                attempt_no=request.attempt_no,
                state=AttemptState.IN_FLIGHT,
                request_fingerprint=request.request_fingerprint,
                provider_project_id=None,
                seed=None,
                seed_supported=False,
                cost_usd=Decimal(0),
                credits_charged=None,
                cost_is_final=False,
            )
            db.attempts[request.request_fingerprint] = record
            return AttemptClaim(attempt=record, adopted=False)

        async def record_submission(
            self, attempt_id: UUID, submission: ProviderSubmission
        ) -> ShotAttemptRecord:
            db.submissions.append((attempt_id, submission))
            fingerprint, record = next(
                (fp, r) for fp, r in db.attempts.items() if r.id == attempt_id
            )
            updated = ShotAttemptRecord(
                id=record.id,
                shot_id=record.shot_id,
                job_id=record.job_id,
                attempt_no=record.attempt_no,
                state=record.state,
                request_fingerprint=record.request_fingerprint,
                provider_project_id=submission.provider_project_id,
                seed=submission.seed,
                seed_supported=submission.seed_supported,
                cost_usd=record.cost_usd,
                credits_charged=record.credits_charged,
                cost_is_final=record.cost_is_final,
            )
            db.attempts[fingerprint] = updated
            return updated

        async def settle_cost(
            self, attempt_id: UUID, settlement: CostSettlement
        ) -> ShotAttemptRecord:
            db.settlements.append((attempt_id, settlement))
            fingerprint, record = next(
                (fp, r) for fp, r in db.attempts.items() if r.id == attempt_id
            )
            updated = ShotAttemptRecord(
                id=record.id,
                shot_id=record.shot_id,
                job_id=record.job_id,
                attempt_no=record.attempt_no,
                state=settlement.state,
                request_fingerprint=record.request_fingerprint,
                provider_project_id=record.provider_project_id,
                seed=record.seed,
                seed_supported=record.seed_supported,
                cost_usd=settlement.cost_usd,
                credits_charged=settlement.credits_charged,
                cost_is_final=True,
            )
            db.attempts[fingerprint] = updated
            return updated

    class FakeArtifactRepository:
        def __init__(self, session: object) -> None:
            del session

        async def get(self, artifact_id: UUID) -> PersistedArtifactRecord | None:
            return db.artifacts.get(artifact_id)

        async def record(self, new_artifact: NewArtifact) -> PersistedArtifactRecord:
            record = PersistedArtifactRecord(
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
            return record

    class FakeCheckpointRepository:
        def __init__(self, session: object) -> None:
            del session

        async def latest(self, thread_id: UUID) -> CheckpointRecord | None:
            del thread_id
            return db.checkpoints[-1] if db.checkpoints else None

        async def write(self, new_checkpoint: NewCheckpoint) -> CheckpointRecord:
            record = CheckpointRecord(
                id=len(db.checkpoints),
                thread_id=new_checkpoint.thread_id,
                node=new_checkpoint.node,
                seq=new_checkpoint.seq,
                state=new_checkpoint.state,
                budget_used=new_checkpoint.budget_used,
            )
            db.checkpoints.append(record)
            return record

    monkeypatch.setattr(nodes, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(nodes, "StoryPlanRepository", FakeStoryPlanRepository)
    monkeypatch.setattr(nodes, "ShotRepository", FakeShotRepository)
    monkeypatch.setattr(nodes, "ShotAttemptRepository", FakeShotAttemptRepository)
    monkeypatch.setattr(nodes, "ArtifactRepository", FakeArtifactRepository)
    monkeypatch.setattr(nodes, "CheckpointRepository", FakeCheckpointRepository)


@dataclass
class FakeBytesArtifactStore:
    """`providers.models.ArtifactStore`: bytes in, bytes out, by `ArtifactRef`."""

    frames: dict[str, bytes] = field(default_factory=dict)
    written: list[bytes] = field(default_factory=list)

    async def read(self, ref: ArtifactRef) -> bytes:
        return self.frames[ref.artifact_id]

    async def write(self, *, content_type: str, data: bytes) -> ArtifactRef:
        del content_type
        self.written.append(data)
        return ArtifactRef(
            artifact_id=f"obj-{len(self.written)}", storage_key=f"objects/{len(self.written)}.bin"
        )


@dataclass
class FakeRegistry:
    """`providers.models.ProviderRegistry`, delegating straight to one `FakeProvider`."""

    provider: FakeProvider

    def select(self, required: frozenset[Capability]) -> list[FakeProvider]:
        del required
        return [self.provider]

    async def generate(self, req: ShotRequest, *, ctx: object) -> ShotResult:
        return await self.provider.generate(req, ctx=ctx)


def a_deps(*, registry: FakeRegistry, artifacts: FakeBytesArtifactStore) -> GraphDeps:
    return GraphDeps(
        engine=cast(Any, None),
        gateway=cast(Any, None),
        checkpointer=cast(Any, None),
        harness=JobHarness(job_id=uuid4(), shots_required=4),
        now=lambda: NOW,
        providers=cast(Any, registry),
        artifacts=cast(Any, artifacts),
    )


@pytest.mark.asyncio
async def test_shot_zero_generates_text_only_and_is_not_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDB()
    install_fake_repositories(monkeypatch, db)
    job_id = uuid4()
    db.beat_ids[(job_id, 0)] = uuid4()

    clip_ref = ArtifactRef(artifact_id="clip-1", storage_key="clips/clip-1.mp4")
    result_provider = a_result(
        "full", clip=clip_ref, cost_usd=Decimal("0.50"), credits_charged=Decimal("5")
    )
    provider = FakeProvider(profile=profile("full"), outcomes=[result_provider])
    artifacts = FakeBytesArtifactStore(frames={"clip-1": b"fake mp4 bytes"})
    deps = a_deps(registry=FakeRegistry(provider), artifacts=artifacts)
    state = a_state(job_id=job_id)

    result = await nodes.generate_shot_node(state, deps)

    assert "degraded" not in result
    assert len(provider.calls) == 1
    assert provider.calls[0].conditioning_frame is None

    updated_shot = result["shots"][0]
    assert updated_shot.attempts_used == 1
    assert updated_shot.clip_artifact_id is not None
    assert result["budget"].usd_spent == Decimal("0.50")
    assert len(db.checkpoints) == 1
    assert len(db.submissions) == 1
    assert len(db.settlements) == 1


@pytest.mark.asyncio
async def test_a_later_shot_chains_the_accepted_predecessor_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDB()
    install_fake_repositories(monkeypatch, db)
    job_id = uuid4()
    db.beat_ids[(job_id, 1)] = uuid4()

    frame_artifact_id = uuid4()
    db.artifacts[frame_artifact_id] = PersistedArtifactRecord(
        id=frame_artifact_id,
        job_id=job_id,
        kind=cast(Any, "continuity_frame"),
        shot_index=0,
        storage_key="frames/frame-0.png",
        content_type="image/png",
        bytes=100,
        checksum_sha256="deadbeef",
    )

    clip_ref = ArtifactRef(artifact_id="clip-2", storage_key="clips/clip-2.mp4")
    result_provider = a_result("full", clip=clip_ref, cost_usd=Decimal("0.50"))
    provider = FakeProvider(profile=profile("full"), outcomes=[result_provider])
    artifacts = FakeBytesArtifactStore(frames={"clip-2": b"fake mp4 bytes"})
    deps = a_deps(registry=FakeRegistry(provider), artifacts=artifacts)

    shots = list(four_shots())
    shots[0] = shots[0].model_copy(update={"status": ShotStatus.ACCEPTED, "attempts_used": 1})
    state = a_state(
        job_id=job_id,
        shot_index=1,
        shots=tuple(shots),
        last_good_frame_artifact_id=frame_artifact_id,
    )

    result = await nodes.generate_shot_node(state, deps)

    assert "degraded" not in result
    sent = provider.calls[0].conditioning_frame
    assert sent is not None
    assert sent.artifact_id == str(frame_artifact_id)
    assert sent.storage_key == "frames/frame-0.png"


@pytest.mark.asyncio
async def test_a_later_shot_with_no_accepted_predecessor_generates_text_only_and_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDB()
    install_fake_repositories(monkeypatch, db)
    job_id = uuid4()
    db.beat_ids[(job_id, 1)] = uuid4()

    clip_ref = ArtifactRef(artifact_id="clip-3", storage_key="clips/clip-3.mp4")
    result_provider = a_result("full", clip=clip_ref, cost_usd=Decimal("0.50"))
    provider = FakeProvider(profile=profile("full"), outcomes=[result_provider])
    artifacts = FakeBytesArtifactStore(frames={"clip-3": b"fake mp4 bytes"})
    deps = a_deps(registry=FakeRegistry(provider), artifacts=artifacts)

    shots = list(four_shots())
    shots[0] = shots[0].model_copy(update={"status": ShotStatus.ABANDONED, "attempts_used": 1})
    state = a_state(
        job_id=job_id, shot_index=1, shots=tuple(shots), last_good_frame_artifact_id=None
    )

    result = await nodes.generate_shot_node(state, deps)

    assert result["degraded"] is True
    assert "no accepted predecessor frame" in cast(str, result["degraded_reason"])
    assert provider.calls[0].conditioning_frame is None
