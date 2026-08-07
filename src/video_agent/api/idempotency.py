"""`Idempotency-Key` on every work-creating POST, and the algorithm behind it.

`[CPS §Non-negotiables]` requires the key. `api.md` §3 specifies exactly what happens to it, and
this module implements that specification in full — the fingerprint, the `NX` claim, the replay,
the two distinct conflicts — against an `IdempotencyStore` protocol.

**What is here and what is not.** The mechanism is complete and tested. What is missing is a
route to hang it on: `POST /v1/jobs` and its siblings land in `T1.3`, and the Postgres unique
constraint on `(tenant_id, idempotency_key)` that backstops a Redis flush belongs to `T0.5`'s
schema. `RedisIdempotencyStore` is the production store and is exercised only under
`@pytest.mark.integration`; `begin_idempotent` and `finish_idempotent` — the parts that decide
whether a second job is created — are exercised against an in-memory store in the unit suite.
Nothing about the mechanism is deferred behind a comment.

**Why the fingerprint exists.** A key alone cannot tell a retry from a client bug. Hashing the
canonical body means "same key, same request" replays and "same key, different request" is
rejected `VA-REQ-003` rather than silently creating a second, differently-shaped job — which is
the failure that costs a customer twice for one video.

**Why Redis being down is a `503` and not a bypass.** `[D-17]`: idempotency is a
non-negotiable, so it degrades to refusal, not to best-effort. A cache may be skipped when it
is unavailable; a correctness mechanism may not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Final, Protocol
from uuid import UUID

from fastapi import Header
from pydantic import BaseModel, ConfigDict

from video_agent.api.errors import ApiError, ErrorContext
from video_agent.observability.codes import ErrorCode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable

    from redis.asyncio import Redis

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
REPLAYED_HEADER: Final = "Idempotency-Replayed"
RETRY_AFTER_HEADER: Final = "Retry-After"

MIN_KEY_LENGTH: Final = 16
MAX_KEY_LENGTH: Final = 255
"""`api.md` §2.3 declares these bounds on the header. A key shorter than 16 characters is not
unique enough to be one, and the upper bound stops a client using the body as its key."""

IDEMPOTENCY_TTL_SECONDS: Final = 86_400
"""24 hours `[D-16]`. Long enough to cover any retry a client will make, short enough that the
key space does not grow without bound."""

IN_FLIGHT_RETRY_AFTER_SECONDS: Final = 2
"""What `VA-REQ-004` puts in `Retry-After`. Short: the first call is still running, not queued
behind a human."""


class IdempotencyState(StrEnum):
    """The two states a claimed key can be in."""

    IN_FLIGHT = "in_flight"
    DONE = "done"


class IdempotencyRecord(BaseModel):
    """What is stored under the key: enough to replay the original response exactly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: IdempotencyState
    fingerprint: str
    status_code: int | None = None
    body: str | None = None
    job_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FirstCall:
    """This key had not been seen. The caller does the work, then calls `finish_idempotent`."""

    storage_key: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Replay:
    """This key has been seen and completed. Return this response, byte for byte."""

    status_code: int
    body: str
    job_id: UUID | None


class IdempotencyStore(Protocol):
    """The two operations the algorithm needs, and nothing else.

    `claim` is a single atomic operation on purpose. A read-then-write would let two concurrent
    identical requests both see "absent" and both create a job, which is the precise thing
    idempotency keys exist to prevent.
    """

    async def claim(self, storage_key: str, record: IdempotencyRecord) -> IdempotencyRecord | None:
        """Store `record` only if the key is absent. Return the existing record if it is not."""
        ...  # pragma: no cover - protocol declaration

    async def complete(self, storage_key: str, record: IdempotencyRecord) -> None:
        """Overwrite the key with the finished record, preserving the original TTL window."""
        ...  # pragma: no cover - protocol declaration


def storage_key_for(tenant_id: UUID, route: str, key: str) -> str:
    """`api.md` §3: `idem:{tenant}:{route}:{key}`. Tenant-first so a scan is tenant-scoped."""
    return f"idem:{tenant_id}:{route}:{key}"


def canonical_json(body: bytes) -> str:
    """`body` in a form where key order and whitespace cannot change the fingerprint.

    A client that re-serialises its retry with a different key order is retrying, not sending a
    different request, and telling it `VA-REQ-003` would be a lie. A body that is not JSON is
    fingerprinted verbatim — guessing at its structure would be worse than treating it as
    opaque.
    """
    try:
        parsed = json.loads(body or b"null")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_fingerprint(tenant_id: UUID, route: str, body: bytes) -> str:
    """`sha256(tenant_id | route | canonical_json(body))` `[api.md` §3 step 1`]`.

    The tenant and route are inside the hash as well as inside the key, so a fingerprint can
    never be compared across either boundary even if a key were reused there.
    """
    material = f"{tenant_id}|{route}|{canonical_json(body)}".encode()
    return hashlib.sha256(material).hexdigest()


async def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> str:
    """The header, or `400 VA-REQ-002`. No exceptions, no "optional in dev" `[api.md` §3`]`.

    Declared optional and checked here rather than declared required, because FastAPI renders a
    missing required header as `422`, and `api.md` §4 assigns a missing idempotency key its own
    code and its own status. Never invent a key for a caller that omitted one: an invented key
    is unique per call, which is the same as having no idempotency at all.
    """
    key = (idempotency_key or "").strip()
    if not key:
        raise ApiError(ErrorCode.VA_REQ_002, log_detail="Idempotency-Key header absent")
    if not MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH:
        raise ApiError(
            ErrorCode.VA_REQ_002,
            log_detail=f"Idempotency-Key length {len(key)} outside bounds",
            context=ErrorContext(
                details={"min_length": MIN_KEY_LENGTH, "max_length": MAX_KEY_LENGTH}
            ),
        )
    return key


async def begin_idempotent(
    store: IdempotencyStore,
    *,
    tenant_id: UUID,
    route: str,
    key: str,
    body: bytes,
) -> FirstCall | Replay:
    """Steps 1-5 of `api.md` §3, atomically. Raises on both conflict shapes."""
    fingerprint = request_fingerprint(tenant_id, route, body)
    storage_key = storage_key_for(tenant_id, route, key)
    existing = await store.claim(
        storage_key,
        IdempotencyRecord(state=IdempotencyState.IN_FLIGHT, fingerprint=fingerprint),
    )
    if existing is None:
        return FirstCall(storage_key=storage_key, fingerprint=fingerprint)
    if existing.fingerprint != fingerprint:
        raise ApiError(
            ErrorCode.VA_REQ_003,
            log_detail="idempotency key reused with a different body",
        )
    if existing.state is IdempotencyState.IN_FLIGHT or existing.body is None:
        raise ApiError(
            ErrorCode.VA_REQ_004,
            log_detail="an identical request is still in flight",
            context=ErrorContext(
                headers={RETRY_AFTER_HEADER: str(IN_FLIGHT_RETRY_AFTER_SECONDS)},
            ),
        )
    return Replay(
        status_code=existing.status_code or 0,
        body=existing.body,
        job_id=existing.job_id,
    )


async def finish_idempotent(
    store: IdempotencyStore,
    first: FirstCall,
    *,
    status_code: int,
    body: str,
    job_id: UUID | None = None,
) -> None:
    """Record the response so an identical retry replays it rather than repeating the work."""
    await store.complete(
        first.storage_key,
        IdempotencyRecord(
            state=IdempotencyState.DONE,
            fingerprint=first.fingerprint,
            status_code=status_code,
            body=body,
            job_id=job_id,
        ),
    )


class RedisIdempotencyStore:
    """The production store. `SET ... NX EX` is the claim; nothing else is atomic enough."""

    def __init__(self, client: Redis, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS) -> None:
        """Hold the client and the TTL the claim is written with."""
        self._client = client
        self._ttl_seconds = ttl_seconds

    async def claim(self, storage_key: str, record: IdempotencyRecord) -> IdempotencyRecord | None:
        """Claim the key, or read back whoever holds it."""
        created = await self._guard(
            self._client.set(storage_key, record.model_dump_json(), nx=True, ex=self._ttl_seconds)
        )
        if created:
            return None
        raw = await self._guard(self._client.get(storage_key))
        if raw is None:
            # The holder's TTL expired between the SET and the GET. Treating this as a first
            # call is the only option left, and it is safe: `api.md` §3 keeps the authoritative
            # `job_id -> key` pair in Postgres under a unique constraint, so a duplicate insert
            # fails there rather than producing a second job.
            return None
        return IdempotencyRecord.model_validate_json(raw)

    async def complete(self, storage_key: str, record: IdempotencyRecord) -> None:
        """Overwrite the claim with the finished response, keeping the 24h window."""
        await self._guard(
            self._client.set(storage_key, record.model_dump_json(), ex=self._ttl_seconds)
        )

    @staticmethod
    async def _guard[T](awaitable: Awaitable[T]) -> T:
        """Translate a Redis failure into the `503` `[D-17]` demands, losing no cause."""
        try:
            return await awaitable
        except Exception as exc:
            raise ApiError(
                ErrorCode.VA_STORE_003,
                log_detail=f"idempotency store unavailable: {type(exc).__name__}",
            ) from exc
