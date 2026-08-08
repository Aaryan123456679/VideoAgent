"""`S0.6.1` — a miss is `None`, an outage is `VA-STORE-003`, and no key escapes without a TTL.

The distinction in the title is the whole module. `[D-17]` makes idempotency non-negotiable:
when Redis is unavailable a work-creating `POST` is *rejected*, not waved through. That policy
is only expressible if the store tells its caller which of the two things happened, and the
change that breaks it — `except Exception: return None` — leaves every other test green.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID, uuid4

import pytest

from tests.unit.test_persistence_redis_support import FakeConnectionError, FakeRedis
from video_agent.observability.codes import ErrorCode
from video_agent.observability.redaction import contains_never_logged_value
from video_agent.persistence.keys import (
    KeyName,
    RedisKey,
    idempotency_key,
    job_lock_key,
    jobs_stream_key,
    llm_cache_key,
    progress_key,
)
from video_agent.persistence.redis_client import (
    MissingKeyTtlError,
    RedisStore,
    RedisUnavailableError,
    require_ttl,
)

TENANT: Final = UUID("11111111-1111-1111-1111-111111111111")
JOB: Final = UUID("22222222-2222-2222-2222-222222222222")

CREDENTIALED_URL: Final = "redis://default:s3cr3t-p4ssw0rd@redis.internal:6379/0"
"""A `REDIS_URL` in the shape the driver puts into its own connection errors. Low-entropy and
hyphenated so it is findable, and credential-shaped enough that the redaction scanner agrees it
is one — asserted below rather than assumed."""


def down(message: str = "Error 61 connecting to localhost:6379. Connection refused.") -> FakeRedis:
    """A client that cannot reach its server."""
    return FakeRedis(fail=FakeConnectionError(message))


# --- A miss is not an outage -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_miss_returns_none() -> None:
    """An absent key is `None`, which is the answer a cache is entitled to act on."""
    store = RedisStore(FakeRedis())

    assert await store.get(llm_cache_key("never-written")) is None


@pytest.mark.asyncio
async def test_connection_error_is_typed_not_a_miss() -> None:
    """A down Redis raises `VA-STORE-003` rather than returning `None` `[D-17]`."""
    store = RedisStore(down())

    with pytest.raises(RedisUnavailableError) as raised:
        await store.get(llm_cache_key("anything"))

    assert raised.value.code is ErrorCode.VA_STORE_003
    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_every_operation_reports_the_outage_not_a_default() -> None:
    """`set`, `set_if_absent`, `delete`, `ttl_of` and `ping` all raise rather than answering.

    One of them silently returning `False` would be the same defect wearing a different type: a
    caller cannot distinguish "the lock was already held" from "there is no lock table".
    """
    store = RedisStore(down())
    key = job_lock_key(JOB)

    for operation in (
        store.set(key, "v"),
        store.set_if_absent(key, "v"),
        store.delete(key),
        store.ttl_of(key),
        store.ping(),
    ):
        with pytest.raises(RedisUnavailableError):
            await operation


@pytest.mark.asyncio
async def test_the_failure_message_names_the_key_registry_entry_not_the_key_value() -> None:
    """The message says *which key pattern*, never the rendered key.

    The rendered idempotency key contains the client's `Idempotency-Key`, which clients
    routinely derive from an order id or a customer reference. An exception message reaches an
    HTTP body and a traceback, neither of which the redaction serialiser inspects.
    """
    store = RedisStore(down())
    key = idempotency_key(TENANT, "POST /v1/jobs", "customer-reference-12345")

    with pytest.raises(RedisUnavailableError) as raised:
        await store.get(key)

    assert str(KeyName.IDEMPOTENCY) in raised.value.message
    assert "customer-reference-12345" not in raised.value.message


@pytest.mark.asyncio
async def test_the_failure_message_carries_no_connection_target() -> None:
    """`REDIS_URL` carries a password. It never reaches a message this module produces.

    The driver's own exception text is reduced to its type, so even a driver that put the whole
    DSN into `str(exc)` — some do — cannot leak it through here.
    """
    assert contains_never_logged_value(CREDENTIALED_URL), (
        "the planted URL must itself be recognised as a credential, or this test proves nothing"
    )
    store = RedisStore(down(f"could not connect to {CREDENTIALED_URL}"))

    with pytest.raises(RedisUnavailableError) as raised:
        await store.ping()

    assert "s3cr3t-p4ssw0rd" not in raised.value.message
    assert not contains_never_logged_value(raised.value.message)


@pytest.mark.asyncio
async def test_a_programming_error_is_not_relabelled_as_a_store_outage() -> None:
    """A `MissingKeyTtlError` raised on the way in must not be reported as Redis being down.

    The two send an operator to different systems, and the one that is actually broken is this
    process. `guard` re-raises `VideoAgentError` untouched for exactly this reason.
    """
    store = RedisStore(FakeRedis())
    hand_built = RedisKey(name=KeyName.PROGRESS, value="progress:x", ttl_seconds=None)

    with pytest.raises(MissingKeyTtlError):
        await store.set(hand_built, "v")


# --- No key escapes without a TTL --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_registry_ttl_reaches_the_server() -> None:
    """The number in `persistence.md` §5 is the number the command carries."""
    fake = FakeRedis()
    store = RedisStore(fake)
    key = progress_key(JOB)

    await store.set(key, "event")

    assert fake.values[key.value].ttl_seconds == key.ttl_seconds
    assert await store.ttl_of(key) == key.ttl_seconds


@pytest.mark.asyncio
async def test_write_without_ttl_asserts() -> None:
    """A key whose registry entry demands a TTL is refused when it arrives without one.

    Refused *before* the command is issued: the assertion below is that nothing was written, not
    merely that an exception was raised. A key created and then complained about is still a key
    that never expires.
    """
    fake = FakeRedis()
    store = RedisStore(fake)
    hand_built = RedisKey(name=KeyName.JOB_LOCK, value=f"job:{JOB}", ttl_seconds=None)

    with pytest.raises(MissingKeyTtlError):
        await store.set(hand_built, "token")

    assert fake.values == {}
    assert fake.commands == []


@pytest.mark.asyncio
async def test_a_zero_ttl_is_refused_rather_than_treated_as_forever() -> None:
    """Redis rejects `EX 0`; treating it as "no expiry" would be the worst possible reading."""
    store = RedisStore(FakeRedis())
    zero = RedisKey(name=KeyName.LLM_CACHE, value="cache:llm:x", ttl_seconds=0)

    with pytest.raises(MissingKeyTtlError):
        await store.set(zero, "v")


def test_the_queue_key_is_the_only_one_allowed_through_without_a_ttl() -> None:
    """`require_ttl` returns `None` for `jobs:stream` and for nothing else."""
    assert require_ttl(jobs_stream_key()) is None
    assert require_ttl(job_lock_key(JOB)) == job_lock_key(JOB).ttl_seconds


# --- The atomic claim ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_if_absent_is_a_single_atomic_claim() -> None:
    """`SET NX EX` — one command. The second caller loses and is told so."""
    fake = FakeRedis()
    store = RedisStore(fake)
    key = job_lock_key(uuid4())

    first = await store.set_if_absent(key, "worker-1")
    second = await store.set_if_absent(key, "worker-2")

    assert first is True
    assert second is False
    assert await store.get(key) == "worker-1"
    assert fake.commands == ["SET", "SET", "GET"]


@pytest.mark.asyncio
async def test_the_claim_carries_the_lock_ttl() -> None:
    """A fencing token with no expiry is a job nobody can ever take over `[D-10]`."""
    fake = FakeRedis()
    store = RedisStore(fake)
    key = job_lock_key(JOB)

    await store.set_if_absent(key, "worker-1")

    assert fake.values[key.value].ttl_seconds == key.ttl_seconds


@pytest.mark.asyncio
async def test_delete_reports_whether_the_key_existed() -> None:
    """Releasing a lock nobody held is worth distinguishing from releasing one that was."""
    store = RedisStore(FakeRedis())
    key = job_lock_key(JOB)

    assert await store.delete(key) is False
    await store.set_if_absent(key, "worker-1")
    assert await store.delete(key) is True


@pytest.mark.asyncio
async def test_close_releases_the_pool() -> None:
    """`S0.6.1` acceptance 5: the lifespan closes the client on shutdown."""
    fake = FakeRedis()

    await RedisStore(fake).aclose()

    assert fake.closed is True
