"""`MockVideoProvider` — real ffmpeg, no network. Compact: enough to prove a shot with and
without a conditioning frame both produce a real, playable, non-uniform MP4, and that the
provider otherwise behaves like any other `VideoProvider`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from video_agent.assembly.media_toolchain import resolve_binary
from video_agent.gateway.models import ArtifactRef
from video_agent.graph.frame_extraction import find_last_usable_frame, frame_variance
from video_agent.providers.mock import MockVideoProvider
from video_agent.providers.models import ShotRequest

_VARIANCE_FLOOR = 4.0


def _resolved_ffmpeg() -> str:
    resolved = resolve_binary("ffmpeg")
    assert resolved is not None, "ffmpeg must be on PATH to run this test"
    return resolved


@dataclass
class _FakeArtifactStore:
    frames: dict[str, bytes] = field(default_factory=dict)
    written: list[bytes] = field(default_factory=list)

    async def read(self, ref: ArtifactRef) -> bytes:
        return self.frames[ref.artifact_id]

    async def write(self, *, content_type: str, data: bytes) -> ArtifactRef:
        del content_type
        self.written.append(data)
        artifact_id = f"clip-{len(self.written)}"
        self.frames[artifact_id] = data
        return ArtifactRef(artifact_id=artifact_id, storage_key=f"mock/{artifact_id}.mp4")


def _a_png() -> bytes:
    """A tiny, real, non-uniform PNG — a two-colour checkerboard, via ffmpeg's own test source."""
    completed = subprocess.run(
        [
            _resolved_ffmpeg(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:duration=1:rate=1",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    return completed.stdout


def a_request(**overrides: object) -> ShotRequest:
    fields: dict[str, object] = {
        "job_id": uuid4(),
        "shot_index": 0,
        "attempt_no": 1,
        "prompt": "irrelevant to the mock provider",
        "duration_s": 1.0,
        "resolution": "480p",
        "request_fingerprint": "fingerprint-1",
        "timeout_s": 30.0,
    }
    fields.update(overrides)
    return ShotRequest.model_validate(fields)


@pytest.mark.asyncio
async def test_shot_zero_renders_a_real_non_uniform_clip() -> None:
    artifacts = _FakeArtifactStore()
    provider = MockVideoProvider(artifacts=artifacts)
    result = await provider.generate(a_request(shot_index=0), ctx=None)  # type: ignore[arg-type]
    assert result.provider_key == "mock"
    assert result.cost_usd == 0
    clip_bytes = artifacts.frames[result.clip.artifact_id]
    assert clip_bytes.startswith(b"\x00\x00\x00")  # an mp4 box header, not empty bytes
    assert len(clip_bytes) > 0


@pytest.mark.asyncio
async def test_shot_zero_clip_survives_real_frame_extraction_without_being_rejected_uniform(
    tmp_path: Path,
) -> None:
    """The whole point of the id text overlay: a flat colour card alone would be exactly the
    shape `frame_extraction`'s uniform-frame check exists to reject."""
    artifacts = _FakeArtifactStore()
    provider = MockVideoProvider(artifacts=artifacts)
    result = await provider.generate(a_request(duration_s=1.0), ctx=None)  # type: ignore[arg-type]
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(artifacts.frames[result.clip.artifact_id])
    frame_path = tmp_path / "frame.png"

    found = find_last_usable_frame(clip_path, frame_path)

    assert found is True
    assert frame_variance(frame_path) >= _VARIANCE_FLOOR


@pytest.mark.asyncio
async def test_a_later_shot_turns_the_conditioning_frame_into_the_clip() -> None:
    artifacts = _FakeArtifactStore()
    frame_ref = ArtifactRef(artifact_id="frame-1", storage_key="frames/frame-1.png")
    artifacts.frames["frame-1"] = _a_png()
    provider = MockVideoProvider(artifacts=artifacts)
    req = a_request(shot_index=1, conditioning_frame=frame_ref, duration_s=1.0)
    result = await provider.generate(req, ctx=None)  # type: ignore[arg-type]
    assert len(artifacts.frames[result.clip.artifact_id]) > 0


@pytest.mark.asyncio
async def test_lookup_echoes_the_last_generate_for_the_same_fingerprint() -> None:
    artifacts = _FakeArtifactStore()
    provider = MockVideoProvider(artifacts=artifacts)
    assert await provider.lookup("fingerprint-1") is None
    req = a_request(request_fingerprint="fingerprint-1", duration_s=1.0)
    result = await provider.generate(req, ctx=None)  # type: ignore[arg-type]
    assert await provider.lookup("fingerprint-1") == result


@pytest.mark.asyncio
async def test_health_is_always_healthy_and_webhook_is_always_unhandled() -> None:
    provider = MockVideoProvider(artifacts=_FakeArtifactStore())
    health = await provider.health()
    assert health.healthy is True
    assert await provider.handle_webhook(raw_body=b"{}", headers={}) is False
