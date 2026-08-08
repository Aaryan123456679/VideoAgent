"""`S0.7.4` — the circuit breaker: five failures in thirty seconds, per `(alias, model)`.

State machines break at their boundaries, so the boundaries are what is tested: the fifth
failure inside the window trips it and the fifth spread across a longer window does not; the
cooldown is over at exactly its duration and not a moment before; a failed probe doubles the
next cooldown and stops doubling at the cap.

Everything runs on a clock the test owns. The alternative — `asyncio.sleep(30)` — would make
this file take five minutes and would be deleted within a month.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from tests.gateway_doubles import (
    MODEL_A,
    MODEL_B,
    HarnessOverrides,
    ManualClock,
    ScriptedTransport,
    a_request,
    ok,
)
from tests.gateway_doubles import build_harness as build
from video_agent.config.aliases import Alias
from video_agent.gateway import CallContext
from video_agent.gateway.breaker import (
    CIRCUIT_STATE_UNAVAILABLE_ALARM,
    Admission,
    CircuitBreaker,
    CircuitConfig,
    CircuitRecord,
    CircuitState,
    InMemoryCircuitStateStore,
    RedisCircuitStateStore,
    ResilientCircuitStateStore,
    dependency_key,
)
from video_agent.gateway.transport import UpstreamStatusError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.gateway.breaker import CircuitStateStore

SPEC_FAILURE_THRESHOLD = 5
"""`[CPS §Failure behaviour]`, spelled out here so the assertion below reads as a restatement of
the specification rather than as a magic number chasing the implementation."""

SPEC_WINDOW_SECONDS = 30.0
SPEC_MAX_OPEN_SECONDS = 300.0
CONCURRENT_CALLERS = 10

KEY = "circuit:llm:reasoning-high|vendor-a/model-one"
OTHER_KEY = "circuit:llm:reasoning-high|vendor-b/model-two"


def build_breaker(clock: ManualClock, store: CircuitStateStore | None = None) -> CircuitBreaker:
    """A breaker on a test-owned clock and, by default, a process-local store."""
    return CircuitBreaker(
        store=store if store is not None else InMemoryCircuitStateStore(),
        clock=clock,
    )


async def fail_times(breaker: CircuitBreaker, clock: ManualClock, gaps: list[float]) -> None:
    """Record one failure, then one more after each gap. `len(gaps) + 1` failures in total."""
    await breaker.record_failure(KEY)
    for gap in gaps:
        clock.advance(gap)
        await breaker.record_failure(KEY)


@pytest.mark.asyncio
async def test_five_failures_within_thirty_seconds_open_the_circuit() -> None:
    """Acceptance 2, the positive half: five failures spanning 29s trip it."""
    clock = ManualClock()
    breaker = build_breaker(clock)
    await fail_times(breaker, clock, [7.25, 7.25, 7.25, 7.25])
    assert await breaker.state(KEY) is CircuitState.OPEN
    assert await breaker.allows(KEY) is Admission.REFUSE


@pytest.mark.asyncio
async def test_five_failures_spread_over_thirty_one_seconds_do_not_open_it() -> None:
    """Acceptance 2, the half that a counter-with-reset would fail.

    The first failure is 31s old when the fifth lands, so only four are still evidence. A
    naive counter that reset on a fixed boundary would open here, and would *not* open in the
    test above depending on where the boundary fell.
    """
    clock = ManualClock()
    breaker = build_breaker(clock)
    await fail_times(breaker, clock, [7.75, 7.75, 7.75, 7.75])
    assert await breaker.state(KEY) is CircuitState.CLOSED
    assert await breaker.allows(KEY) is Admission.ADMIT


@pytest.mark.asyncio
async def test_four_failures_do_not_open_it() -> None:
    """The threshold is five. Four is the boundary immediately below it."""
    clock = ManualClock()
    breaker = build_breaker(clock)
    await fail_times(breaker, clock, [1.0, 1.0, 1.0])
    assert await breaker.state(KEY) is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_a_success_clears_the_window() -> None:
    """A success ends the incident; four old failures plus one new must not trip it."""
    clock = ManualClock()
    breaker = build_breaker(clock)
    await fail_times(breaker, clock, [1.0, 1.0])
    await breaker.record_success(KEY)
    await fail_times(breaker, clock, [1.0])
    assert await breaker.state(KEY) is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_the_open_circuit_becomes_half_open_after_exactly_the_cooldown() -> None:
    """Acceptance: OPEN for 30s, then one probe. Asserted at the boundary from both sides.

    The advances are halves rather than thousandths so the clock lands on 30.0 exactly. A
    boundary test built from 29.999 + 0.001 asserts a property of binary floating point rather
    than a property of the breaker, and fails for a reason that has nothing to do with it.
    """
    clock = ManualClock()
    breaker = build_breaker(clock)
    await fail_times(breaker, clock, [1.0, 1.0, 1.0, 1.0])
    clock.advance(29.5)
    assert await breaker.state(KEY) is CircuitState.OPEN
    clock.advance(0.5)
    assert await breaker.state(KEY) is CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_half_open_admits_exactly_one_probe_and_refuses_the_rest() -> None:
    """Acceptance 3: concurrent callers are refused, not queued.

    Queueing them would mean that recovery begins by sending the recovering dependency every
    request that piled up while it was down.
    """
    clock = ManualClock()
    breaker = build_breaker(clock)
    await fail_times(breaker, clock, [1.0, 1.0, 1.0, 1.0])
    clock.advance(30.0)
    admissions = await asyncio.gather(*(breaker.allows(KEY) for _ in range(CONCURRENT_CALLERS)))
    assert admissions.count(Admission.ADMIT_PROBE) == 1
    assert admissions.count(Admission.REFUSE) == CONCURRENT_CALLERS - 1


@pytest.mark.asyncio
async def test_a_successful_probe_closes_the_circuit_and_frees_the_slot() -> None:
    """HALF_OPEN → CLOSED on success, and the next caller is admitted normally."""
    clock = ManualClock()
    breaker = build_breaker(clock)
    await fail_times(breaker, clock, [1.0, 1.0, 1.0, 1.0])
    clock.advance(30.0)
    assert await breaker.allows(KEY) is Admission.ADMIT_PROBE
    await breaker.record_success(KEY)
    assert await breaker.state(KEY) is CircuitState.CLOSED
    assert await breaker.allows(KEY) is Admission.ADMIT


@pytest.mark.asyncio
async def test_a_failed_probe_doubles_the_open_duration_capped_at_five_minutes() -> None:
    """Acceptance 4: `30, 60, 120, 240, 300, 300`. The cap truncates rather than resetting."""
    clock = ManualClock()
    store = InMemoryCircuitStateStore()
    breaker = build_breaker(clock, store)
    await fail_times(breaker, clock, [1.0, 1.0, 1.0, 1.0])
    durations: list[float] = []
    for _ in range(6):
        record = await store.read(KEY)
        assert record is not None
        durations.append(record.open_duration_s)
        clock.advance(record.open_duration_s)
        assert await breaker.allows(KEY) is Admission.ADMIT_PROBE
        await breaker.record_failure(KEY)
    assert durations == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]


@pytest.mark.asyncio
async def test_the_dependency_key_separates_models_inside_one_group() -> None:
    """Acceptance 1: one sick model does not open the circuit for its healthy siblings."""
    clock = ManualClock()
    breaker = build_breaker(clock)
    await fail_times(breaker, clock, [1.0, 1.0, 1.0, 1.0])
    assert await breaker.state(KEY) is CircuitState.OPEN
    assert await breaker.state(OTHER_KEY) is CircuitState.CLOSED


def test_the_dependency_key_is_alias_and_model_together() -> None:
    """Keyed on the pair. On the model alone, two groups sharing a model would share its health."""
    assert dependency_key(Alias.REASONING_HIGH, MODEL_A) != dependency_key(
        Alias.VISION_DEFAULT, MODEL_A
    )
    assert MODEL_A in dependency_key(Alias.REASONING_HIGH, MODEL_A)
    assert Alias.REASONING_HIGH.value in dependency_key(Alias.REASONING_HIGH, MODEL_A)


@pytest.mark.asyncio
async def test_an_open_circuit_makes_the_gateway_skip_straight_to_the_fallback() -> None:
    """`gateway.md` §4.3: an open circuit skips to the fallback rather than spending attempts."""
    clock = ManualClock()
    breaker = build_breaker(clock)
    await fail_times(breaker, clock, [1.0, 1.0, 1.0, 1.0])
    transport = ScriptedTransport({MODEL_B: [ok()]})
    harness = build(transport, HarnessOverrides(clock=clock, breaker=breaker))
    response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert response.model_used == MODEL_B
    assert transport.calls_for(MODEL_A) == []


@pytest.mark.asyncio
async def test_a_sick_model_does_not_stop_its_healthy_sibling_serving() -> None:
    """Acceptance 1, end to end: five failures on the primary, and the group still serves."""
    clock = ManualClock()
    breaker = build_breaker(clock)
    transport = ScriptedTransport({MODEL_A: [UpstreamStatusError(503, "{}")], MODEL_B: [ok()]})
    harness = build(transport, HarnessOverrides(clock=clock, breaker=breaker))
    for _ in range(3):
        response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="p"))
        assert response.model_used == MODEL_B
    assert await breaker.state(dependency_key(Alias.REASONING_HIGH, MODEL_A)) is CircuitState.OPEN
    assert await breaker.state(dependency_key(Alias.REASONING_HIGH, MODEL_B)) is CircuitState.CLOSED


class BrokenRedis:
    """A Redis client whose every command fails, standing in for a Redis that is down."""

    def __init__(self) -> None:
        self.attempts = 0

    async def get(self, name: str) -> object:
        """Fail."""
        self.attempts += 1
        message = f"connection refused for {name}"
        raise ConnectionError(message)

    async def set(
        self, name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> object:
        """Fail."""
        self.attempts += 1
        message = f"connection refused for {name} ({len(value)} bytes, ex={ex}, nx={nx})"
        raise ConnectionError(message)

    async def delete(self, *names: str) -> object:
        """Fail."""
        self.attempts += 1
        message = f"connection refused for {names}"
        raise ConnectionError(message)


@pytest.mark.asyncio
async def test_redis_down_treats_circuits_as_closed_and_alarms() -> None:
    """`[D-22]`: a circuit-state outage degrades sharing, alarms, and never fails the call.

    Failing closed on circuit *state* would turn a cache outage into a total outage, which
    inverts the point of the breaker.
    """
    CIRCUIT_STATE_UNAVAILABLE_ALARM.reset()
    clock = ManualClock()
    client = BrokenRedis()
    store = ResilientCircuitStateStore(RedisCircuitStateStore(client))
    breaker = build_breaker(clock, store)
    assert await breaker.allows(KEY) is Admission.ADMIT
    assert CIRCUIT_STATE_UNAVAILABLE_ALARM.count > 0
    assert client.attempts > 0


@pytest.mark.asyncio
async def test_a_call_still_succeeds_while_the_circuit_store_is_down() -> None:
    """`[D-22]` again, end to end: the job runs, the counter records that sharing was lost."""
    CIRCUIT_STATE_UNAVAILABLE_ALARM.reset()
    clock = ManualClock()
    store = ResilientCircuitStateStore(RedisCircuitStateStore(BrokenRedis()))
    transport = ScriptedTransport({MODEL_A: [ok()]})
    harness = build(transport, HarnessOverrides(clock=clock, breaker=build_breaker(clock, store)))
    response = await harness.gateway.call(a_request(), ctx=CallContext(job_id="j", node="plan"))
    assert response.model_used == MODEL_A
    assert CIRCUIT_STATE_UNAVAILABLE_ALARM.count > 0


@pytest.mark.asyncio
async def test_a_corrupt_stored_record_reads_as_a_fresh_closed_circuit() -> None:
    """Schema drift in the store must not fail the call it was consulted for."""
    assert CircuitRecord.from_json("not json").state is CircuitState.CLOSED
    assert CircuitRecord.from_json('{"state": "sideways"}').state is CircuitState.CLOSED
    assert CircuitRecord.from_json("[1, 2]").failures == ()


@pytest.mark.asyncio
async def test_a_record_survives_a_round_trip_through_the_shared_store() -> None:
    """Serialisation is not lossy: an open circuit read by another worker is still open."""
    original = CircuitRecord(
        state=CircuitState.OPEN,
        failures=(1.0, 2.0),
        opened_at=2.0,
        open_duration_s=60.0,
    )
    restored = CircuitRecord.from_json(original.to_json())
    assert restored == original


@pytest.mark.asyncio
async def test_the_configuration_is_the_specified_thresholds() -> None:
    """`[CPS §Failure behaviour]`: per dependency, five failures in thirty seconds."""
    config = CircuitConfig()
    assert config.failure_threshold == SPEC_FAILURE_THRESHOLD
    assert config.window_s == SPEC_WINDOW_SECONDS
    assert config.initial_open_s == SPEC_WINDOW_SECONDS
    assert config.max_open_s == SPEC_MAX_OPEN_SECONDS
