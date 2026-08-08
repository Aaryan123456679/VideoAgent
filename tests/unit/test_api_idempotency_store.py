"""`[D-17]` — a dead Redis rejects the request. It does not quietly become "no idempotency".

`T0.4` implemented the idempotency *mechanism* in full and exercised it against an in-memory
store, but `RedisIdempotencyStore` — the only implementation whose failure mode `[D-17]` is
about — was reachable only under `@pytest.mark.integration`, and nothing in the process held
one. So the rule that a work-creating `POST` is *rejected* when Redis is unavailable had no
unit-level evidence at all, and the environment where the integration suite skips is the
environment where it had none.

`T0.6` wires it: `api.clients.Cache.idempotency_store()` builds one over the shared client, and
this file drives the real store against the same in-memory Redis the rest of the persistence
suite uses — including its outage switch.

**The change that would break this is small and quiet.** Catching the connection error in
`claim` and returning `None` would make a dead Redis look like an unseen key: every request
would be treated as a first call, every retry would create a second job, and every test that
does not simulate an outage would still pass. `AGENT.md` §1.3 is explicit — *if Redis is
unavailable, reject the request. Idempotency is not a cache and may not be degraded.*
"""

from __future__ import annotations

from typing import Final
from uuid import UUID, uuid4

import pytest

from tests.unit.test_persistence_redis_support import FakeConnectionError, FakeRedis
from video_agent.api.clients import Cache
from video_agent.api.errors import HTTP_SERVICE_UNAVAILABLE, ApiError, status_for_code
from video_agent.api.idempotency import (
    FirstCall,
    IdempotencyRecord,
    IdempotencyState,
    RedisIdempotencyStore,
    Replay,
    begin_idempotent,
    finish_idempotent,
    request_fingerprint,
    storage_key_for,
)
from video_agent.observability.codes import ErrorCode
from video_agent.persistence.keys import IDEMPOTENCY_TTL_SECONDS, KeyName, spec_for

TENANT: Final = UUID("11111111-1111-1111-1111-111111111111")
ROUTE: Final = "POST /v1/jobs"
KEY: Final = "client-supplied-idempotency-key"
BODY: Final = b'{"prompt":"a cat"}'
ACCEPTED: Final = 202

CLAIMED: Final = IdempotencyRecord(state=IdempotencyState.IN_FLIGHT, fingerprint="f")


def store_over(client: FakeRedis) -> RedisIdempotencyStore:
    """The production store, over an in-memory server."""
    return RedisIdempotencyStore(client)


def dead() -> FakeRedis:
    """A Redis that refuses every command."""
    return FakeRedis(fail=FakeConnectionError("Error 61 connecting to redis. Connection refused."))


# --- The store is real ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_claim_is_a_single_atomic_set_nx() -> None:
    """One command, not a read followed by a write.

    Two concurrent identical requests would both see "absent" under a read-then-write, and both
    would create a job — which is the precise thing an idempotency key exists to prevent.
    """
    client = FakeRedis()
    store = store_over(client)
    key = storage_key_for(TENANT, ROUTE, KEY)

    first = await store.claim(key, CLAIMED)
    second = await store.claim(key, CLAIMED)

    assert first is None
    assert second == CLAIMED
    assert client.commands == ["SET", "SET", "GET"]


@pytest.mark.asyncio
async def test_the_claim_carries_the_registry_ttl() -> None:
    """24h `[D-16]`, from `persistence.md` §5's table rather than from a literal in `api`."""
    client = FakeRedis()
    key = storage_key_for(TENANT, ROUTE, KEY)

    await store_over(client).claim(key, CLAIMED)

    assert client.values[key].ttl_seconds == spec_for(KeyName.IDEMPOTENCY).ttl_seconds
    assert client.values[key].ttl_seconds == IDEMPOTENCY_TTL_SECONDS


@pytest.mark.asyncio
async def test_completing_replaces_the_claim_and_keeps_the_window() -> None:
    """The finished response is stored under the same key, with the same 24h window."""
    client = FakeRedis()
    store = store_over(client)
    key = storage_key_for(TENANT, ROUTE, KEY)
    job_id = uuid4()
    await store.claim(key, CLAIMED)

    await store.complete(
        key,
        IdempotencyRecord(
            state=IdempotencyState.DONE,
            fingerprint="f",
            status_code=ACCEPTED,
            body="{}",
            job_id=job_id,
        ),
    )
    replayed = await store.claim(key, CLAIMED)

    assert replayed is not None
    assert replayed.state is IdempotencyState.DONE
    assert replayed.job_id == job_id
    assert client.values[key].ttl_seconds == IDEMPOTENCY_TTL_SECONDS


# --- `[D-17]`: a dead store rejects ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dead_store_rejects_the_claim_rather_than_returning_a_miss() -> None:
    """The whole of `[D-17]`. `VA-STORE-003`, which the envelope renders as `503`."""
    store = store_over(dead())

    with pytest.raises(ApiError) as raised:
        await store.claim(storage_key_for(TENANT, ROUTE, KEY), CLAIMED)

    assert raised.value.code is ErrorCode.VA_STORE_003
    assert status_for_code(raised.value.code) == HTTP_SERVICE_UNAVAILABLE


@pytest.mark.asyncio
async def test_a_dead_store_also_rejects_the_completion() -> None:
    """The second half of the window. A completion that silently failed would leave the record
    `in_flight` for 24 hours and answer every retry with `VA-REQ-004`."""
    store = store_over(dead())

    with pytest.raises(ApiError) as raised:
        await store.complete(storage_key_for(TENANT, ROUTE, KEY), CLAIMED)

    assert raised.value.code is ErrorCode.VA_STORE_003


@pytest.mark.asyncio
async def test_begin_idempotent_propagates_the_refusal_rather_than_creating_work() -> None:
    """The route-facing entry point refuses too — this is the call a `POST /v1/jobs` makes.

    Asserted through `begin_idempotent` and not only through the store, because the bypass that
    `[D-17]` forbids would most naturally be written here: a `try/except` around the claim that
    "degrades gracefully" and lets the request through.
    """
    store = store_over(dead())

    with pytest.raises(ApiError) as raised:
        await begin_idempotent(store, tenant_id=TENANT, route=ROUTE, key=KEY, body=BODY)

    assert status_for_code(raised.value.code) == HTTP_SERVICE_UNAVAILABLE


@pytest.mark.asyncio
async def test_a_live_store_still_replays_rather_than_creating_a_second_job() -> None:
    """The positive case, so the refusal above is not passing because everything refuses."""
    store = store_over(FakeRedis())

    first = await begin_idempotent(store, tenant_id=TENANT, route=ROUTE, key=KEY, body=BODY)
    assert isinstance(first, FirstCall)
    await finish_idempotent(store, first, status_code=ACCEPTED, body='{"id":"1"}', job_id=None)
    replay = await begin_idempotent(store, tenant_id=TENANT, route=ROUTE, key=KEY, body=BODY)

    assert first.fingerprint == request_fingerprint(TENANT, ROUTE, BODY)
    assert isinstance(replay, Replay)
    assert replay.status_code == ACCEPTED


# --- The wiring -----------------------------------------------------------------------------------


def test_the_cache_hands_out_a_real_redis_backed_store() -> None:
    """`S0.6.1` acceptance 5 and `[D-17]` together: the process actually holds one.

    Before `T0.6` nothing constructed a `RedisIdempotencyStore` outside the integration suite,
    so a deployment had the mechanism and no store to run it against.
    """
    cache = Cache(FakeRedis())

    assert isinstance(cache.idempotency_store(), RedisIdempotencyStore)


def test_the_cache_shares_one_client_between_the_store_the_queue_and_the_probe() -> None:
    """One connection pool, three consumers. Two pools would double the file descriptors and
    let the readiness probe pass while the store's own pool was exhausted."""
    client = FakeRedis()
    cache = Cache(client)

    assert cache.store._client is client
    assert cache.queue._client is client
