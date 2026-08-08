"""`S3ArtifactStore` — the concrete `providers.models.ArtifactStore`. Compact, non-exhaustive."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from video_agent.providers.artifact_store import S3ArtifactStore


@dataclass
class FakeTransport:
    objects: dict[str, bytes] = field(default_factory=dict)

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        del content_type
        self.objects[key] = body

    async def get(self, key: str) -> bytes:
        return self.objects[key]

    def presign_get(self, key: str, ttl_seconds: int) -> str:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_write_then_read_round_trips_the_bytes() -> None:
    store = S3ArtifactStore(transport=FakeTransport())
    ref = await store.write(content_type="video/mp4", data=b"clip-bytes")
    assert await store.read(ref) == b"clip-bytes"


@pytest.mark.asyncio
async def test_write_picks_an_extension_from_content_type() -> None:
    store = S3ArtifactStore(transport=FakeTransport())
    ref = await store.write(content_type="image/png", data=b"png-bytes")
    assert ref.storage_key.endswith(".png")


@pytest.mark.asyncio
async def test_write_falls_back_to_bin_for_an_unknown_content_type() -> None:
    store = S3ArtifactStore(transport=FakeTransport())
    ref = await store.write(content_type="application/x-unknown", data=b"x")
    assert ref.storage_key.endswith(".bin")


@pytest.mark.asyncio
async def test_two_writes_never_collide() -> None:
    store = S3ArtifactStore(transport=FakeTransport())
    first = await store.write(content_type="image/jpeg", data=b"a")
    second = await store.write(content_type="image/jpeg", data=b"b")
    assert first.storage_key != second.storage_key
    assert first.artifact_id != second.artifact_id
