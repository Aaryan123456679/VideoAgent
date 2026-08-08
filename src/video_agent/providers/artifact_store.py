"""`S3ArtifactStore`: the concrete `providers.models.ArtifactStore` implementation.

Nothing in `src/` implemented this protocol before now — the concrete `VideoProvider` adapter
and `graph.deps.GraphDeps` could be typed against it and unit-tested against fakes, but a real
worker had nothing to construct for `artifacts`. This wraps the existing `persistence.objects.
ObjectTransport` (the same S3-dialect client `persistence.objects.ArtifactStore` uses for its
local-file upload/download flow) in the simpler bytes-in/bytes-out shape the provider and graph
layers need.

**Known, documented limitation — not the full tenant-isolation layout.** `persistence.md` §6
gives every key produced by `persistence.objects.storage_key()` a `{tenant_id}/{job_id}/{kind}/
{shot_index}/{artifact_id}` layout specifically so the bucket policy is *a second isolation
layer independent of RLS* — enforced by the store from the key, not by Postgres from a session
variable. `ArtifactStore.write()`'s signature (`content_type`, `data` — nothing else) carries no
tenant, job, kind or shot context to build that key from, and this class is process-wide,
constructed once and shared across every tenant's jobs a worker ever runs, so it cannot be
handed a tenant id at construction either. Every object this class writes therefore lands under
a flat, non-tenant-prefixed prefix (`_provider/{artifact_id}.{ext}`) — the bucket-policy
isolation layer does not yet cover artifacts written through this path. RLS on the `artifact`
metadata row is unaffected and still the authority on which tenant an id belongs to. Closing
this gap for real needs the protocol to carry the location context through to `write()`, which
ripples into every already-shipped call site (the concrete `VideoProvider` adapter,
`graph/nodes.py`) — out of scope for standing this class up, and named here rather than
silently narrowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from video_agent.gateway.models import ArtifactRef
from video_agent.persistence.objects import ObjectTransport

__all__ = ["S3ArtifactStore"]

_UNSCOPED_PREFIX = "_provider"

_EXTENSIONS_BY_CONTENT_TYPE: dict[str, str] = {
    "video/mp4": "mp4",
    "image/png": "png",
    "image/jpeg": "jpg",
}
_DEFAULT_EXTENSION = "bin"


@dataclass(frozen=True, slots=True)
class S3ArtifactStore:
    """Bytes in, bytes out, over an already-configured `ObjectTransport`."""

    transport: ObjectTransport

    async def read(self, ref: ArtifactRef) -> bytes:
        return await self.transport.get(ref.storage_key)

    async def write(self, *, content_type: str, data: bytes) -> ArtifactRef:
        extension = _EXTENSIONS_BY_CONTENT_TYPE.get(content_type, _DEFAULT_EXTENSION)
        artifact_id = uuid4()
        key = f"{_UNSCOPED_PREFIX}/{artifact_id}.{extension}"
        await self.transport.put(key, data, content_type)
        return ArtifactRef(artifact_id=str(artifact_id), storage_key=key)
