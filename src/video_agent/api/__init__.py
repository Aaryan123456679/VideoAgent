"""api — HTTP surface, idempotency, auth boundary. See ``docs/LLD/api.md``.

`T0.4` lands the *shell*: the application factory and its lifespan, liveness and readiness, the
single error envelope, the principal boundary and the tenant-scoped database session, and the
idempotency mechanism every work-creating `POST` will use. The job routes themselves — create,
status, events, artifacts — are `api.md` §2.1's table and arrive with `T1.3`.

The engine is deliberately not exported from anywhere in this package `[api.md` §6`]`: a route
that can open a connection can open one without the row-level-security binding, and then
tenancy is a convention rather than a guarantee.
"""

from video_agent.api.app import create_app
from video_agent.api.database import (
    SET_LOCAL_TENANT_SQL,
    TENANT_SETTING,
    Database,
    tenant_session,
)
from video_agent.api.errors import ApiError, ErrorContext, ErrorEnvelope, build_envelope
from video_agent.api.idempotency import (
    IdempotencyRecord,
    IdempotencyStore,
    begin_idempotent,
    finish_idempotent,
    request_fingerprint,
    require_idempotency_key,
)
from video_agent.api.middleware import RequestBoundaryMiddleware
from video_agent.api.principal import ApiKeyVerifier, Principal, require_tenant
from video_agent.api.resources import ResourceFactories, Resources

__all__ = [
    "SET_LOCAL_TENANT_SQL",
    "TENANT_SETTING",
    "ApiError",
    "ApiKeyVerifier",
    "Database",
    "ErrorContext",
    "ErrorEnvelope",
    "IdempotencyRecord",
    "IdempotencyStore",
    "Principal",
    "RequestBoundaryMiddleware",
    "ResourceFactories",
    "Resources",
    "begin_idempotent",
    "build_envelope",
    "create_app",
    "finish_idempotent",
    "request_fingerprint",
    "require_idempotency_key",
    "require_tenant",
    "tenant_session",
]
