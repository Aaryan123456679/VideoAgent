"""`GET /v1/jobs/{job_id}/artifacts` — `api.md` §2.1, the T2.4 artifacts route.

Reads what `graph.nodes`' `generate_shot`/`extract_final_frame`/`assemble` catalogued via
`ArtifactRepository`, and presigns each one at response time. `[D-52]`: no URL is ever stored —
`ArtifactRecord` carries only a `storage_key`, and `persistence.presign.mint_all` mints a fresh
`GET` link, per request, that is returned and then forgotten. A presign failure yields
`url: None` for that one artifact rather than dropping it or failing the whole response
(`persistence.md` §9) — a caller sees every artifact the job produced even if the object store
is briefly unreachable for signing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from video_agent.api.database import tenant_session
from video_agent.api.errors import ApiError
from video_agent.api.principal import Principal, assert_tenant_owns, require_tenant
from video_agent.observability.codes import ErrorCode
from video_agent.persistence.presign import UrlMinter, mint_all, presign_ttl
from video_agent.persistence.repositories import ArtifactRepository, JobRepository
from video_agent.persistence.session import TenantSession

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.api.resources import Resources
    from video_agent.config.settings import Settings

__all__ = ["router"]

router = APIRouter(tags=["artifacts"])

_ARTIFACT_KINDS = Literal[
    "final_video", "shot_clip", "thumbnail", "continuity_frame", "story_plan_json", "bible_json"
]


class ArtifactView(BaseModel):
    """One catalogued artifact, with a freshly-minted link. `url` is `None`, never omitted,
    when presigning failed `[persistence.md §9]` — the artifact still happened and was billed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: UUID
    kind: _ARTIFACT_KINDS
    shot_index: int | None
    content_type: str
    size_bytes: int
    checksum_sha256: str
    url: str | None


class ArtifactListView(BaseModel):
    """`GET /v1/jobs/{job_id}/artifacts`'s body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    artifacts: list[ArtifactView]


class _ArtifactsObjectStore(Protocol):
    """The slice of the opened object store this route needs: a `UrlMinter`.

    `Resources.object_store` is typed as the narrow `ClosableResource` (`aclose` only) so the
    lifespan stays decoupled from what a concrete store can do beyond opening and closing —
    mirroring `api.jobs._JobsCache`'s same narrowing for the cache resource. Production always
    wires `api.clients.ObjectStore`, whose `.transport` (`persistence.objects.S3ObjectTransport`)
    satisfies `UrlMinter` structurally; the one cast in `_object_store_resource` documents that
    assumption in one place instead of scattering it through the handler.
    """

    @property
    def transport(self) -> UrlMinter:
        """The presign-capable transport underneath the store."""
        ...  # pragma: no cover - protocol declaration


def _object_store_resource(request: Request) -> _ArtifactsObjectStore:
    resources: Resources = request.app.state.resources
    return cast("_ArtifactsObjectStore", resources.object_store)


@router.get("/v1/jobs/{job_id}/artifacts", response_model=ArtifactListView)
async def list_job_artifacts(
    request: Request,
    job_id: UUID,
    principal: Annotated[Principal, Depends(require_tenant)],
    session: Annotated[TenantSession, Depends(tenant_session)],
) -> ArtifactListView:
    """Every artifact catalogued for one job, tenant-scoped, each with a fresh presigned link.

    Tenant-scoped twice over, matching `api.jobs.get_job`'s own pattern: RLS (via
    `tenant_session`) is the first line, `assert_tenant_owns` is the second — a caller cannot
    list another tenant's artifacts even if a row somehow reached this handler.
    """
    job = await JobRepository(session).get(job_id)
    if job is None:
        raise ApiError(ErrorCode.VA_REQ_005, job_id=job_id)
    assert_tenant_owns(principal, job.tenant_id, job_id=job_id)

    records = await ArtifactRepository(session).list_for_job(job_id)

    settings: Settings = request.app.state.settings
    minter = _object_store_resource(request).transport
    urls = mint_all(minter, [record.storage_key for record in records], presign_ttl(settings))

    artifacts = [
        ArtifactView(
            artifact_id=record.id,
            kind=record.kind.value,
            shot_index=record.shot_index,
            content_type=record.content_type,
            size_bytes=record.bytes,
            checksum_sha256=record.checksum_sha256,
            url=url.url,
        )
        for record, url in zip(records, urls, strict=True)
    ]
    return ArtifactListView(job_id=job_id, artifacts=artifacts)
