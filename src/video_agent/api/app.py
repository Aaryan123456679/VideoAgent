"""The application factory: the one place where the pieces are wired together.

A factory rather than a module-level `app = FastAPI()`, for a reason that matters more than
style: a module-level application is constructed at import time, which means it is constructed
before configuration has been validated and before logging exists, and it cannot be built twice
with different dependencies. Every test in this task builds an application with a fake database
or a failing Redis; none of that is possible against a global.

**`configure_logging` is called here, immediately after `get_settings()`.** `T0.3` built the
JSON logging substrate — the trace binding, the redaction tripwire, the schema — and nothing
called it. Until this line existed, a production process emitted whatever the default
`logging` configuration produced: unstructured, untraceable and, most importantly,
**unredacted**. The tests exercised redaction; the running process did not go through it.

**Route ordering and the middleware boundary.** `RequestBoundaryMiddleware` is the only
middleware, and it wraps everything: it binds the trace the envelope's `trace_id` comes from,
and it catches what the handlers do not. See `middleware.py` for why that cannot be split.

Job routes are not here. `api.md` §2.1 lists them and `T1.3` implements them; what this task
ships is the surface they will hang on — health, the error envelope, the principal boundary,
the tenant-scoped session and the idempotency mechanism.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

from fastapi import FastAPI

from video_agent.api.artifacts import router as artifacts_router
from video_agent.api.clients import default_factories
from video_agent.api.handlers import register_exception_handlers
from video_agent.api.health import router as health_router
from video_agent.api.jobs import router as jobs_router
from video_agent.api.middleware import RequestBoundaryMiddleware
from video_agent.api.resources import Resources, open_resources
from video_agent.api.webhooks import router as webhooks_router
from video_agent.config.settings import get_settings
from video_agent.observability.logging import configure_logging, get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

    from video_agent.api.principal import ApiKeyVerifier
    from video_agent.config.settings import Settings
    from video_agent.providers.models import ProviderRegistry

_LOGGER: Final = get_logger(__name__)

API_TITLE: Final = "Video Agent"
API_VERSION: Final = "1"
API_DESCRIPTION: Final = "Agentic text-to-video pipeline. See docs/LLD/api.md."


def create_app(
    settings: Settings | None = None,
    *,
    resources: Resources | None = None,
    verifier: ApiKeyVerifier | None = None,
    provider_registry: ProviderRegistry | None = None,
) -> FastAPI:
    """Build a configured application.

    All parameters exist so a test can substitute a dependency, and for no other reason:
    production calls `create_app()` with nothing and gets the real settings, the real clients,
    the deny-everything verifier that `T0.5`'s credential store will replace, and no provider
    webhook registry — `api.webhooks` answers `503` until one is supplied, which is a real,
    working configuration (polling is what runs with none at all), not a placeholder.
    """
    resolved_settings = settings if settings is not None else get_settings()
    configure_logging(resolved_settings)
    resolved_resources = (
        resources if resources is not None else Resources(default_factories(resolved_settings))
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        _LOGGER.info("application starting", extra={"event": "app_starting"})
        async with open_resources(resolved_resources):
            yield
        _LOGGER.info("application stopped", extra={"event": "app_stopped"})

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.resources = resolved_resources
    app.state.api_key_verifier = verifier
    app.state.provider_registry = provider_registry
    app.add_middleware(RequestBoundaryMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(artifacts_router)
    app.include_router(webhooks_router)
    return app
