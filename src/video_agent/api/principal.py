"""Who is calling, and the only place that answer is allowed to come from.

`[D-68]` settles the scheme: static per-tenant API keys presented as
`Authorization: Bearer <key>`, stored as an Argon2id hash in `tenant_api_key`, looked up by a
non-secret `key_prefix` and resolved to `Principal{tenant_id, key_id}`.

This module ships the **boundary** and the **verifier protocol**, not the credential store.
The store is a table `persistence` owns and Argon2id needs a dependency the project does not
declare yet, so `UnconfiguredApiKeyVerifier` is what an application gets until one is wired: it
rejects every credential. That is the safe direction for a missing verifier to fail, and it is
loud in the log while staying indistinguishable to the caller.

Three properties are enforced here rather than left to each route:

- **Every rejection is the same rejection.** Missing header, wrong scheme, malformed key,
  unknown prefix, revoked key, disabled tenant — one `401 VA-AUTH-001` with one body. `api.md`
  §6 asks for unknown and revoked keys to be indistinguishable, and a helpfully specific
  "key revoked" is an oracle.
- **The tenant comes from the `Principal` and from nothing else.** There is no function here
  that reads a tenant from a header, a path or a body, so a route cannot call one by mistake.
- **The key is never logged.** Not the plaintext, not the hash `[D-52]`. `parse_bearer` returns
  the secret in a `PresentedKey` whose `__repr__` shows the prefix only, so the value cannot
  reach a log line through an f-string or an exception's argument list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, Protocol
from uuid import UUID

from fastapi import Header, Request
from pydantic import BaseModel, ConfigDict

from video_agent.api.errors import ApiError
from video_agent.observability.codes import ErrorCode
from video_agent.observability.context import bind_trace
from video_agent.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

_LOGGER: Final = get_logger(__name__)

BEARER_SCHEME: Final = "bearer"
KEY_PREFIX_LENGTH: Final = 12
"""Characters of the presented key that form the non-secret lookup handle.

Non-secret by construction: it is stored in the clear so the row can be found without a table
scan of Argon2id verifications, which is what a lookup by hash would degenerate into."""

MIN_KEY_LENGTH: Final = KEY_PREFIX_LENGTH + 16
"""Shorter than this and there is no secret left after the prefix, so it cannot be a key."""


class Principal(BaseModel):
    """The resolved caller. `api.md` §6: a tenant and the key that proved it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    key_id: UUID


@dataclass(frozen=True, slots=True)
class PresentedKey:
    """A credential taken off the wire, split into its lookup handle and its secret."""

    prefix: str
    secret: str

    def __repr__(self) -> str:
        """Show the prefix only. The secret has no representation that prints it."""
        return f"PresentedKey(prefix={self.prefix!r}, secret=<redacted>)"


class ApiKeyVerifier(Protocol):
    """Resolves a presented key to a `Principal`, or to `None` for every failure.

    `None` rather than an exception per reason, so a caller cannot accidentally render "revoked"
    differently from "unknown". An implementation must also take the **same time** either way —
    verify against a dummy hash when the prefix is unknown — or the timing becomes the oracle
    the single response shape was there to prevent.
    """

    async def verify(self, presented: PresentedKey, /) -> Principal | None:
        """Return the principal this key resolves to, or `None` if it resolves to nothing."""
        ...  # pragma: no cover - protocol declaration


class UnconfiguredApiKeyVerifier:
    """Rejects everything, because no credential store is wired yet.

    The alternative — accepting some hard-coded key so routes can be exercised — is how a
    development shortcut reaches production. Refusing every key makes the missing store
    impossible to ignore and impossible to exploit.
    """

    async def verify(self, _presented: PresentedKey, /) -> Principal | None:
        """Always `None`, with one log line naming the reason for whoever is debugging.

        The credential is not read at all, let alone logged: there is nothing to check it
        against, and a rejection path that touches the secret is a rejection path that can
        leak it.
        """
        _LOGGER.warning(
            "api key rejected: no verifier is configured",
            extra={"event": "api_key_rejected", "code": ErrorCode.VA_AUTH_001.value},
        )
        return None


def parse_bearer(authorization: str | None) -> PresentedKey | None:
    """Split an `Authorization` header into a `PresentedKey`, or `None` if it is not one."""
    if not authorization:
        return None
    scheme, _, credential = authorization.partition(" ")
    if scheme.strip().lower() != BEARER_SCHEME:
        return None
    key = credential.strip()
    if len(key) < MIN_KEY_LENGTH:
        return None
    return PresentedKey(prefix=key[:KEY_PREFIX_LENGTH], secret=key[KEY_PREFIX_LENGTH:])


def unauthenticated(reason: str) -> ApiError:
    """The single rejection. `reason` is for the log; the client sees only the code."""
    return ApiError(ErrorCode.VA_AUTH_001, log_detail=reason)


def get_verifier(request: Request) -> ApiKeyVerifier:
    """The verifier this application was built with.

    Read from application state rather than imported, so swapping the store is a composition
    change in `create_app` and never an import in a route.
    """
    verifier: ApiKeyVerifier | None = getattr(request.app.state, "api_key_verifier", None)
    if verifier is None:
        return UnconfiguredApiKeyVerifier()
    return verifier


async def require_tenant(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AsyncIterator[Principal]:
    """Resolve the caller, and bind the tenant onto the trace for the rest of the request.

    Declared as an optional header on purpose: making it required would have FastAPI reject a
    missing credential as `422 VA-REQ-007`, and "your request schema is invalid" is the wrong
    thing to tell someone who forgot to authenticate. `api.md` §4 says `401 VA-AUTH-001`.

    A generator dependency so the binding is scoped: `tenant_id` reaches every log line emitted
    while handling this request `[observability.md §4]` and is unbound when it ends, rather than
    leaking into whatever the worker thread handles next.
    """
    presented = parse_bearer(authorization)
    if presented is None:
        raise unauthenticated("no usable bearer credential presented")
    principal = await get_verifier(request).verify(presented)
    if principal is None:
        raise unauthenticated("credential did not resolve to a principal")
    with bind_trace(tenant_id=str(principal.tenant_id)):
        yield principal


def assert_tenant_owns(principal: Principal, owner_tenant_id: UUID, *, job_id: UUID) -> None:
    """Raise the `404` a cross-tenant read gets, and log it as what it actually was.

    `api.md` §4 and §6: never confirm existence. The client is told `VA-REQ-005`, exactly what a
    genuine miss returns, while the log records `VA-AUTH-002` — so a tenant probing for other
    tenants' job ids is visible to us and invisible to itself.

    This is the second line of defence, not the first. Row-level security is the first; a query
    run inside the tenant-scoped session cannot return another tenant's row at all. This
    function covers the case where a row was fetched by an admin path or a cache.
    """
    if principal.tenant_id == owner_tenant_id:
        return
    _LOGGER.warning(
        "cross-tenant access denied",
        extra={
            "event": "cross_tenant_denied",
            "code": ErrorCode.VA_AUTH_002.value,
            "job_id": str(job_id),
        },
    )
    raise ApiError(ErrorCode.VA_REQ_005, log_detail="cross-tenant read", job_id=job_id)
