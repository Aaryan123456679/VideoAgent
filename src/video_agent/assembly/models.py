"""The delivery manifest shape. `assembly.md`/`graph.md` §2 — checkpointed, so no URLs in it.

`graph.md`'s checkpoint-contents row excludes presigned URLs from state; a manifest that held
one would smuggle a credentialed link into every checkpoint row and every log line that prints
`JobState`. So this manifest names artifacts by id only — presigning happens once, at the
moment `deliver` builds the API response, via `persistence.presign`.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["DeliveryManifest", "ManifestEntry"]


class ManifestEntry(BaseModel):
    """One deliverable artifact, named by id, never by its bytes or a link to them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["video", "thumbnail"]
    artifact_id: UUID


class DeliveryManifest(BaseModel):
    """What a finished job hands back. `[D-73]`: empty is not a manifest, it is a zero-deliverable
    job pretending to have one — `manifest_entries > 0` is part of the evaluator's `satisfied`
    check for exactly this reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[ManifestEntry] = Field(min_length=1)
