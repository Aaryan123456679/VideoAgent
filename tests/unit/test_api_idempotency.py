"""`Idempotency-Key` — the mechanism, tested where it can actually be exercised.

`api.md` §3 is an algorithm, not a policy, and the part that decides whether a customer is
billed twice is the fingerprint comparison and the atomic claim — neither of which needs Redis
to test. The in-memory store gives the algorithm the same compare-and-set semantics `SET NX`
gives it in production, so what is asserted here is the real `begin_idempotent`.

What is **not** covered here, stated plainly rather than left to be discovered:

- `RedisIdempotencyStore` needs a live Redis and is asserted under `@pytest.mark.integration`.
- The Postgres unique constraint on `(tenant_id, idempotency_key)` that survives a Redis flush
  belongs to `T0.5`'s schema.
- No route uses the dependency yet; `POST /v1/jobs` and its siblings are `T1.3`.
"""

from __future__ import annotations

import asyncio
from typing import Final
from uuid import UUID, uuid4

import pytest

from tests.unit.test_api_support import TENANT_A, TENANT_B, InMemoryIdempotencyStore
from video_agent.api.errors import HTTP_BAD_REQUEST, ApiError
from video_agent.api.idempotency import (
    IN_FLIGHT_RETRY_AFTER_SECONDS,
    MAX_KEY_LENGTH,
    MIN_KEY_LENGTH,
    RETRY_AFTER_HEADER,
    FirstCall,
    Replay,
    begin_idempotent,
    canonical_json,
    finish_idempotent,
    request_fingerprint,
    require_idempotency_key,
    storage_key_for,
)
from video_agent.observability.codes import ErrorCode

ROUTE: Final = "/v1/jobs"
KEY: Final = "client-supplied-key-0001"
BODY: Final = b'{"prompt":"a lighthouse at dusk","metadata":{"a":"1"}}'
REORDERED_BODY: Final = b'{"metadata":{"a":"1"},"prompt":"a lighthouse at dusk"}'
OTHER_BODY: Final = b'{"prompt":"a different film entirely"}'

ACCEPTED: Final = 202
CONCURRENT_CALLS: Final = 12


async def _begin(store: InMemoryIdempotencyStore, *, body: bytes = BODY) -> FirstCall | Replay:
    return await begin_idempotent(store, tenant_id=TENANT_A, route=ROUTE, key=KEY, body=body)


# --- The header dependency ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_key_is_400_not_422() -> None:
    """`api.md` §3: missing key is `VA-REQ-002`.

    FastAPI's own answer for a missing required header is `422 VA-REQ-007`, which tells the
    caller its schema is wrong rather than that it skipped a non-negotiable. That is why the
    header is declared optional and checked by hand.
    """
    with pytest.raises(ApiError) as raised:
        await require_idempotency_key(None)

    assert raised.value.code is ErrorCode.VA_REQ_002
    assert raised.value.status_code == HTTP_BAD_REQUEST


@pytest.mark.parametrize(
    "key",
    ["", "   ", "x" * (MIN_KEY_LENGTH - 1), "x" * (MAX_KEY_LENGTH + 1)],
    ids=["empty", "whitespace", "too-short", "too-long"],
)
@pytest.mark.asyncio
async def test_an_unusable_key_is_rejected_rather_than_invented(key: str) -> None:
    """Never substitute a key for a caller that did not send a usable one.

    An invented key is unique per call, which is indistinguishable from having no idempotency
    at all — except that it looks like the feature is working.
    """
    with pytest.raises(ApiError) as raised:
        await require_idempotency_key(key)

    assert raised.value.code is ErrorCode.VA_REQ_002


@pytest.mark.asyncio
async def test_a_usable_key_is_returned_stripped() -> None:
    """The positive case, without which every rejection above passes vacuously."""
    assert await require_idempotency_key(f"  {KEY}  ") == KEY


# --- The fingerprint ------------------------------------------------------------------------


def test_reordering_a_body_does_not_change_the_fingerprint() -> None:
    """A client re-serialising its retry is retrying, not sending a different request.

    Telling it `VA-REQ-003` would be a lie, and a lie that makes retries impossible for any
    client whose JSON library does not preserve key order.
    """
    assert canonical_json(BODY) == canonical_json(REORDERED_BODY)
    assert request_fingerprint(TENANT_A, ROUTE, BODY) == request_fingerprint(
        TENANT_A, ROUTE, REORDERED_BODY
    )


def test_a_different_body_changes_the_fingerprint() -> None:
    """Otherwise the mismatch branch could never fire and the `409` would be unreachable."""
    assert request_fingerprint(TENANT_A, ROUTE, BODY) != request_fingerprint(
        TENANT_A, ROUTE, OTHER_BODY
    )


@pytest.mark.parametrize(
    ("tenant", "route"),
    [(TENANT_B, ROUTE), (TENANT_A, "/v1/jobs/x/resume")],
    ids=["other-tenant", "other-route"],
)
def test_the_fingerprint_is_scoped_to_tenant_and_route(tenant: UUID, route: str) -> None:
    """The same body under a different tenant or route is a different request."""
    assert request_fingerprint(TENANT_A, ROUTE, BODY) != request_fingerprint(tenant, route, BODY)


def test_a_non_json_body_is_fingerprinted_verbatim() -> None:
    """Guessing at the structure of an opaque body is worse than treating it as opaque."""
    assert canonical_json(b"not json at all") == "not json at all"


def test_the_storage_key_is_tenant_scoped() -> None:
    """`idem:{tenant}:{route}:{key}` — tenant first, so a scan cannot cross a tenant."""
    key = storage_key_for(TENANT_A, ROUTE, KEY)

    assert key.startswith(f"idem:{TENANT_A}:")
    assert key != storage_key_for(TENANT_B, ROUTE, KEY)


# --- The algorithm --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_call_claims_the_key() -> None:
    """Nothing stored means this caller does the work."""
    store = InMemoryIdempotencyStore()

    outcome = await _begin(store)

    assert isinstance(outcome, FirstCall)
    assert outcome.storage_key in store.records


@pytest.mark.asyncio
async def test_a_repeat_of_a_finished_call_replays_the_stored_response() -> None:
    """Byte-identical replay, with the original status. Exactly one job was ever created."""
    store = InMemoryIdempotencyStore()
    first = await _begin(store)
    assert isinstance(first, FirstCall)
    job_id = uuid4()
    body = f'{{"job_id":"{job_id}","status":"queued"}}'

    await finish_idempotent(store, first, status_code=ACCEPTED, body=body, job_id=job_id)
    replay = await _begin(store)

    assert isinstance(replay, Replay)
    assert replay.status_code == ACCEPTED
    assert replay.body == body
    assert replay.job_id == job_id


@pytest.mark.asyncio
async def test_the_same_key_with_a_different_body_is_409() -> None:
    """`VA-REQ-003`. Never silently create a second, differently-shaped job."""
    store = InMemoryIdempotencyStore()
    first = await _begin(store)
    assert isinstance(first, FirstCall)
    await finish_idempotent(store, first, status_code=ACCEPTED, body="{}")

    with pytest.raises(ApiError) as raised:
        await _begin(store, body=OTHER_BODY)

    assert raised.value.code is ErrorCode.VA_REQ_003


@pytest.mark.asyncio
async def test_a_duplicate_still_in_flight_is_409_with_retry_after() -> None:
    """`VA-REQ-004`. The first call has not finished, so there is no response to replay."""
    store = InMemoryIdempotencyStore()
    await _begin(store)

    with pytest.raises(ApiError) as raised:
        await _begin(store)

    assert raised.value.code is ErrorCode.VA_REQ_004
    assert raised.value.headers[RETRY_AFTER_HEADER] == str(IN_FLIGHT_RETRY_AFTER_SECONDS)


@pytest.mark.asyncio
async def test_concurrent_identical_requests_claim_exactly_once() -> None:
    """`api.md` §9: N concurrent identical posts create exactly one job.

    Every loser is told `VA-REQ-004` rather than being allowed to proceed, which is the
    difference between one video and twelve.
    """
    store = InMemoryIdempotencyStore()

    outcomes = await asyncio.gather(
        *(_begin(store) for _ in range(CONCURRENT_CALLS)),
        return_exceptions=True,
    )

    winners = [outcome for outcome in outcomes if isinstance(outcome, FirstCall)]
    conflicts = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, ApiError) and outcome.code is ErrorCode.VA_REQ_004
    ]

    assert len(winners) == 1
    assert len(conflicts) == CONCURRENT_CALLS - 1


@pytest.mark.asyncio
async def test_a_key_reused_across_tenants_is_a_different_key() -> None:
    """One tenant's key must not collide with another's, however unimaginative both are."""
    store = InMemoryIdempotencyStore()

    await begin_idempotent(store, tenant_id=TENANT_A, route=ROUTE, key=KEY, body=BODY)
    other = await begin_idempotent(store, tenant_id=TENANT_B, route=ROUTE, key=KEY, body=BODY)

    assert isinstance(other, FirstCall)
