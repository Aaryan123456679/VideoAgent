"""The parts of the shell that cannot be asserted without a real Postgres and a real Redis.

Everything here is deliberately *not* faked, because faking it would assert nothing:
`Database.ping` is only interesting against a socket, `SET LOCAL` is only interesting against a
server that has session settings, and `SET ... NX` is only atomic because Redis says so.

Collected always, deselected by default (`-m "not integration"`), selected by
`make test-integration`. The guard is a **short connection attempt**, not the presence of a
client library: a wedged Docker VM leaves every library importable and every connection hanging
until some outer timeout, which surfaces as an error where `S0.1.3` asks for a skip.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text

from video_agent.api.clients import build_cache, build_database
from video_agent.api.database import TENANT_SETTING
from video_agent.api.errors import ApiError
from video_agent.api.idempotency import (
    IdempotencyRecord,
    IdempotencyState,
    RedisIdempotencyStore,
)
from video_agent.config.settings import get_settings
from video_agent.observability.codes import ErrorCode
from video_agent.persistence.session import SET_TENANT_STATEMENT

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

    from video_agent.api.clients import Cache
    from video_agent.api.database import Database

pytestmark = pytest.mark.integration

CONNECT_TIMEOUT: Final = 3.0
"""Seconds to wait for a dependency before deciding it is not there. Short on purpose: a hung
daemon must produce a skip, not a test run that appears to stall."""

UNREACHABLE_REDIS: Final = "redis://127.0.0.1:1/0"
"""Port 1 is reserved and refuses immediately, which is how the `[D-17]` refusal path is
exercised without taking the real Redis down."""


async def _reachable(resource: Database | Cache) -> str | None:
    try:
        await asyncio.wait_for(resource.ping(), timeout=CONNECT_TIMEOUT)
    except TimeoutError:
        return f"did not answer within {CONNECT_TIMEOUT}s"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


@pytest_asyncio.fixture
async def live_database() -> AsyncIterator[Database]:
    """A real engine against `DATABASE_URL`, or a skip explaining why not."""
    database = build_database(get_settings())
    reason = await _reachable(database)
    if reason is not None:
        await database.aclose()
        pytest.skip(f"postgres unavailable: {reason}")
    try:
        yield database
    finally:
        await database.aclose()


@pytest_asyncio.fixture
async def live_cache() -> AsyncIterator[Cache]:
    """A real client against `REDIS_URL`, or a skip explaining why not."""
    cache = build_cache(get_settings())
    reason = await _reachable(cache)
    if reason is not None:
        await cache.aclose()
        pytest.skip(f"redis unavailable: {reason}")
    try:
        yield cache
    finally:
        await cache.aclose()


@pytest.mark.asyncio
async def test_the_readiness_probe_answers_against_a_real_database(
    live_database: Database,
) -> None:
    """`Database.ping` issues a real query. The unit suite can only assert it is called."""
    await live_database.ping()


@pytest.mark.asyncio
async def test_set_local_tenant_is_visible_to_the_transaction(live_database: Database) -> None:
    """The binding the unit tests observe as SQL text actually sets the Postgres setting.

    `current_setting(..., true)` returns the value inside the transaction, which is the whole
    point: this is what every RLS policy reads.
    """
    tenant_id = uuid4()

    async with live_database.tenant_scope(tenant_id) as session:
        result = await session.execute(text(f"SELECT current_setting('{TENANT_SETTING}', true)"))
        bound = result.scalar_one()

    assert bound == str(tenant_id)
    # The statement under test, referenced so its removal is loud. Since `T0.6` it lives in
    # `persistence.session` — `api` no longer keeps a second copy.
    assert str(SET_TENANT_STATEMENT)


@pytest.mark.asyncio
async def test_the_binding_does_not_survive_the_transaction(live_database: Database) -> None:
    """`SET LOCAL` is transaction-scoped, so a pooled connection carries nothing forward.

    Without this, one tenant's id could be read by the next request that reuses the connection
    — and RLS would silently scope the wrong rows.
    """
    first = uuid4()

    async with live_database.tenant_scope(first):
        pass
    async with live_database.tenant_scope(uuid4()) as session:
        result = await session.execute(text(f"SELECT current_setting('{TENANT_SETTING}', true)"))
        bound = result.scalar_one()

    assert bound != str(first)


@pytest.mark.asyncio
async def test_the_redis_store_claims_a_key_exactly_once(live_cache: Cache) -> None:
    """`SET NX` under concurrency: one winner, every other caller sees the incumbent."""
    store = RedisIdempotencyStore(live_cache.client)
    key = f"test:idem:{uuid4()}"
    record = IdempotencyRecord(state=IdempotencyState.IN_FLIGHT, fingerprint="fingerprint")

    try:
        outcomes = await asyncio.gather(*(store.claim(key, record) for _ in range(8)))
    finally:
        await live_cache.client.delete(key)

    assert outcomes.count(None) == 1


@pytest.mark.asyncio
async def test_the_redis_store_round_trips_a_completed_record(live_cache: Cache) -> None:
    """The replay path stores and reads back the exact response body."""
    store = RedisIdempotencyStore(live_cache.client)
    key = f"test:idem:{uuid4()}"
    job_id = uuid4()
    done = IdempotencyRecord(
        state=IdempotencyState.DONE,
        fingerprint="fingerprint",
        status_code=202,
        body='{"status":"queued"}',
        job_id=job_id,
    )

    try:
        await store.complete(key, done)
        read_back = await store.claim(key, done)
    finally:
        await live_cache.client.delete(key)

    assert read_back == done


@pytest.mark.asyncio
async def test_an_unreachable_redis_is_a_503_not_a_bypass() -> None:
    """`[D-17]`: idempotency degrades to refusal, never to best-effort.

    Needs no live Redis — it needs a *dead* one, which is why it points at a reserved port
    rather than taking the dev stack down.
    """
    client = Redis.from_url(UNREACHABLE_REDIS, decode_responses=True)
    store = RedisIdempotencyStore(client)
    record = IdempotencyRecord(state=IdempotencyState.IN_FLIGHT, fingerprint="fingerprint")

    try:
        with pytest.raises(ApiError) as raised:
            await store.claim("test:idem:unreachable", record)
    finally:
        await client.aclose()

    assert raised.value.code is ErrorCode.VA_STORE_003
