"""Liveness and readiness, which are not the same question and must not share an answer.

`/healthz` answers *is this process running*. It touches nothing. That is not laziness: an
orchestrator restarts a container that fails liveness, so a liveness probe wired to the
database turns a database outage into a rolling restart of every replica — removing the
capacity that would have served cached reads and lengthening the outage.

`/readyz` answers *can this process serve a request*, and therefore has to issue a real query
against Postgres and a real `PING` against Redis. Checking that a client object exists would
pass while the dependency is unreachable, which is the exact failure the probe exists to catch:
a process that is up but cannot reach anything is not ready, and calling it healthy defeats the
point of having two probes.

Both dependencies are probed even when the first one fails, so an operator reading the response
learns everything that is wrong rather than the first thing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from video_agent.api.errors import ApiError, ErrorContext
from video_agent.observability.codes import ErrorCode
from video_agent.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.api.resources import ProbedResource, Resources

_LOGGER: Final = get_logger(__name__)

DATABASE_DEPENDENCY: Final = "database"
CACHE_DEPENDENCY: Final = "cache"
"""Dependency labels, named by role rather than by product.

An error body that says `redis` tells an unauthenticated caller what we run. The role is what
an operator needs and is already public in the LLDs."""

NO_STORE: Final = {"Cache-Control": "no-store"}
"""A cached readiness answer is a stale readiness answer, which is worse than none."""

router = APIRouter(tags=["operations"])


class LivenessView(BaseModel):
    """`/healthz` body. Deliberately carries no dependency information."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["alive"] = "alive"


class ReadinessView(BaseModel):
    """`/readyz` body when everything is reachable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ready"] = "ready"
    checks: dict[str, str]


@router.get("/healthz", response_model=LivenessView, summary="Liveness")
async def healthz(response: Response) -> LivenessView:
    """Return `200` for as long as the process is running. Touches no dependency."""
    response.headers.update(NO_STORE)
    return LivenessView()


async def _probe(name: str, resource: ProbedResource) -> str | None:
    """`None` if `resource` answered, otherwise the reason it did not."""
    try:
        await resource.ping()
    except Exception as exc:
        _LOGGER.warning(
            "readiness probe failed for %s",
            name,
            extra={
                "event": "readiness_probe_failed",
                "code": ErrorCode.VA_STORE_003.value,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        return type(exc).__name__
    return None


@router.get("/readyz", response_model=ReadinessView, summary="Readiness")
async def readyz(request: Request, response: Response) -> ReadinessView:
    """Return `200` only when Postgres and Redis both answer; otherwise `503 VA-STORE-003`."""
    response.headers.update(NO_STORE)
    resources: Resources = request.app.state.resources
    probes = (
        (DATABASE_DEPENDENCY, resources.database),
        (CACHE_DEPENDENCY, resources.cache),
    )
    unavailable = [name for name, resource in probes if await _probe(name, resource) is not None]
    if unavailable:
        raise ApiError(
            ErrorCode.VA_STORE_003,
            log_detail=f"dependencies unavailable: {', '.join(unavailable)}",
            context=ErrorContext(
                details={"unavailable": unavailable},
                headers=NO_STORE,
            ),
        )
    return ReadinessView(checks={name: "ok" for name, _ in probes})
