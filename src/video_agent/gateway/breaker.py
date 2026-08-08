"""Circuit breaker per `(alias, concrete model)`: five failures in thirty seconds.

`gateway.md` §4.3, as a state machine:

```
CLOSED --5 failures in a 30s sliding window--> OPEN (30s)
OPEN   --cooldown elapsed--> HALF_OPEN (1 probe)
HALF_OPEN --probe ok--> CLOSED     |     --probe fails--> OPEN (doubled, cap 5 min)
```

Four decisions in that diagram are worth stating, because each has an easier wrong version.

**The key is `(alias, concrete model)`, not the model alone and not the alias alone.** Keyed on
the alias, one sick member would open the circuit for its healthy siblings and the failover
group would stop being a failover group at exactly the moment it was needed. Keyed on the model
alone, a model shared between two groups would carry one group's failures into the other.

**The window is a genuine sliding window, not a counter with a reset.** A counter that resets
every thirty seconds opens on five failures that happen to straddle a boundary and misses five
that do not, so the threshold means something different depending on when the process started.
Timestamps are kept and pruned, so "five failures in thirty seconds" is literally what is
measured — five spread over thirty-one seconds do not open it.

**HALF_OPEN admits exactly one probe, and concurrent callers are refused rather than queued.**
Queueing them would mean that recovering from an outage begins by sending the recovering
dependency every request that piled up during it. Refusal is instant and lets those callers
fall over to a sibling.

**Redis being down never fails a call.** `[D-22]`: circuits are treated CLOSED, cross-worker
sharing is disabled, and an alarm counter goes up. The alternative — failing closed on circuit
*state* — would turn a cache outage into a total outage, which inverts the point of the
breaker. The alarm is what stops the degraded mode from being invisible.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol

from video_agent.observability.alarms import AlarmCounter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.config.aliases import Alias
    from video_agent.gateway.clock import Clock

__all__ = [
    "CIRCUIT_STATE_UNAVAILABLE_ALARM",
    "Admission",
    "CircuitBreaker",
    "CircuitConfig",
    "CircuitRecord",
    "CircuitState",
    "CircuitStateStore",
    "InMemoryCircuitStateStore",
    "RedisCircuitStateStore",
    "ResilientCircuitStateStore",
    "dependency_key",
]

FAILURE_THRESHOLD: Final = 5
WINDOW_SECONDS: Final = 30.0
INITIAL_OPEN_SECONDS: Final = 30.0
MAX_OPEN_SECONDS: Final = 300.0
OPEN_BACKOFF_FACTOR: Final = 2.0
PROBE_LOCK_SECONDS: Final = 30.0
KEY_PREFIX: Final = "circuit:llm:"

CIRCUIT_STATE_UNAVAILABLE_ALARM: Final[AlarmCounter] = AlarmCounter("circuit_state_store_down")
"""Counts calls served while the shared circuit store was unreachable `[D-22]`.

Non-zero means workers are no longer sharing one view of which models are sick, so each of them
has to rediscover an outage independently. That is a survivable degradation and an invisible
one, which is why it is counted rather than only logged.
"""


class CircuitState(StrEnum):
    """The three states. A `StrEnum` so the value serialises into Redis and into a log line."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class Admission(StrEnum):
    """What the breaker permits right now.

    `ADMIT_PROBE` is separate from `ADMIT` because the caller's *failure* handling differs: a
    failed probe re-opens the circuit with a doubled cooldown, while a failure in the closed
    state only adds to the window.
    """

    ADMIT = "admit"
    ADMIT_PROBE = "admit_probe"
    REFUSE = "refuse"


def dependency_key(alias: Alias, model: str) -> str:
    """The dependency identity `gateway.md` §4.3 names: `(alias, concrete_model)`."""
    return f"{KEY_PREFIX}{alias.value}|{model}"


@dataclass(frozen=True, slots=True)
class CircuitConfig:
    """The thresholds. Fields rather than constants so a test can shrink them honestly."""

    failure_threshold: int = FAILURE_THRESHOLD
    window_s: float = WINDOW_SECONDS
    initial_open_s: float = INITIAL_OPEN_SECONDS
    max_open_s: float = MAX_OPEN_SECONDS
    backoff_factor: float = OPEN_BACKOFF_FACTOR
    probe_lock_s: float = PROBE_LOCK_SECONDS


@dataclass(frozen=True, slots=True)
class CircuitRecord:
    """One dependency's state. Immutable; every transition produces a new record.

    Immutable because the store may be Redis, and a mutable record would make "read, mutate,
    write" look like a local operation when it is three round trips with a race in the middle.
    """

    state: CircuitState = CircuitState.CLOSED
    failures: tuple[float, ...] = ()
    opened_at: float | None = None
    open_duration_s: float = INITIAL_OPEN_SECONDS

    def to_json(self) -> str:
        """Serialise for a shared store. Plain JSON: readable in `redis-cli` during an incident."""
        return json.dumps(
            {
                "state": self.state.value,
                "failures": list(self.failures),
                "opened_at": self.opened_at,
                "open_duration_s": self.open_duration_s,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> CircuitRecord:
        """Parse a stored record, treating anything unparseable as a fresh closed circuit.

        A corrupt or schema-drifted record must not fail the call it was consulted for. The
        worst case of misreading it as CLOSED is one extra request to a sick model, which the
        window will re-detect within five failures.
        """
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            return cls()
        if not isinstance(payload, dict):
            return cls()
        try:
            state = CircuitState(payload.get("state", CircuitState.CLOSED.value))
        except ValueError:
            state = CircuitState.CLOSED
        failures = payload.get("failures", [])
        opened_at = payload.get("opened_at")
        duration = payload.get("open_duration_s", INITIAL_OPEN_SECONDS)
        return cls(
            state=state,
            failures=tuple(float(value) for value in failures)
            if isinstance(failures, list)
            else (),
            opened_at=float(opened_at) if isinstance(opened_at, int | float) else None,
            open_duration_s=float(duration) if isinstance(duration, int | float) else 0.0,
        )


class CircuitStateStore(Protocol):
    """Where circuit state lives. Redis in production; process-local in tests and when it is down.

    `acquire_probe` is on the store rather than in the breaker because it is the only operation
    that must be atomic across workers: two workers that both read HALF_OPEN and both decided
    to probe would send two requests to a dependency that is being asked whether it can handle
    one.

    Parameters are positional-only. That is not a style preference: an implementation that has
    no use for `ttl_s` must be able to name it `_ttl_s` — which is how this codebase spells a
    deliberately unused argument, since `AGENT.md` §9 leaves no room for an inline suppression
    — and a positional-or-keyword protocol parameter would make that a type error.
    """

    async def read(self, key: str, /) -> CircuitRecord | None: ...

    async def write(self, key: str, record: CircuitRecord, /) -> None: ...

    async def acquire_probe(self, key: str, ttl_s: float, /) -> bool: ...

    async def release_probe(self, key: str, /) -> None: ...


class InMemoryCircuitStateStore:
    """Process-local state. Used by tests, and as the fallback when the shared store is down."""

    def __init__(self) -> None:
        self._records: dict[str, CircuitRecord] = {}
        self._probes: set[str] = set()
        self._lock = asyncio.Lock()

    async def read(self, key: str) -> CircuitRecord | None:
        """The stored record, or `None` if this dependency has never failed."""
        async with self._lock:
            return self._records.get(key)

    async def write(self, key: str, record: CircuitRecord) -> None:
        """Replace the stored record."""
        async with self._lock:
            self._records[key] = record

    async def acquire_probe(self, key: str, _ttl_s: float) -> bool:
        """Take the single probe slot, or report that someone else holds it.

        The TTL is accepted and unused here: the in-memory store cannot outlive the process, so
        a lock that leaked would die with the worker. It is part of the protocol because the
        Redis implementation genuinely needs it — a worker that acquires a probe and is killed
        mid-request must not hold the slot for that dependency forever.
        """
        async with self._lock:
            if key in self._probes:
                return False
            self._probes.add(key)
            return True

    async def release_probe(self, key: str) -> None:
        """Give the probe slot back."""
        async with self._lock:
            self._probes.discard(key)


class CircuitRedis(Protocol):
    """The three Redis commands the shared store needs, and no more.

    A structural type rather than an import of a concrete client, so this module does not
    depend on how the application builds its Redis connection — that belongs to the persistence
    layer, and duplicating its construction here would give the process two pools.

    The method names are the Redis command names, including `set`. Renaming them to avoid the
    resemblance to a builtin would break the structural match against the real client, which is
    the only reason this protocol exists.
    """

    async def get(self, name: str) -> object: ...

    async def set(
        self, name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> object: ...

    async def delete(self, *names: str) -> object: ...


class CircuitStoreUnavailableError(RuntimeError):
    """The shared store could not be reached. Never propagates to a caller `[D-22]`."""


class RedisCircuitStateStore:
    """Circuit state shared across workers, in Redis. `gateway.md` §4.3.

    Every method converts a client exception into `CircuitStoreUnavailableError`, which
    `ResilientCircuitStateStore` catches. The conversion happens here rather than in the
    breaker so the breaker has no opinion about which client library is in use.
    """

    def __init__(self, client: CircuitRedis) -> None:
        self._client = client

    async def read(self, key: str) -> CircuitRecord | None:
        """The shared record, or `None` if there is none."""
        try:
            raw = await self._client.get(key)
        except Exception as exc:
            raise CircuitStoreUnavailableError(type(exc).__name__) from exc
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, bytes | bytearray) else str(raw)
        return CircuitRecord.from_json(text)

    async def write(self, key: str, record: CircuitRecord) -> None:
        """Publish the record with a TTL, so a permanently healthy key does not persist forever."""
        try:
            await self._client.set(key, record.to_json(), ex=int(MAX_OPEN_SECONDS * 2))
        except Exception as exc:
            raise CircuitStoreUnavailableError(type(exc).__name__) from exc

    async def acquire_probe(self, key: str, ttl_s: float) -> bool:
        """`SET probe NX EX ttl` — the atomicity that makes "exactly one probe" true."""
        try:
            acquired = await self._client.set(f"{key}|probe", "1", ex=max(1, int(ttl_s)), nx=True)
        except Exception as exc:
            raise CircuitStoreUnavailableError(type(exc).__name__) from exc
        return bool(acquired)

    async def release_probe(self, key: str) -> None:
        """Drop the probe lock so a recovered dependency is not held half-open for the TTL."""
        try:
            await self._client.delete(f"{key}|probe")
        except Exception as exc:
            raise CircuitStoreUnavailableError(type(exc).__name__) from exc


class ResilientCircuitStateStore:
    """The shared store, degrading to a process-local one when it is unreachable `[D-22]`.

    On the first failure the alarm goes up and every subsequent operation goes to the local
    fallback, which starts empty — so circuits read CLOSED and calls are admitted, exactly as
    `[D-22]` requires. Cross-worker sharing is what is lost, and that is the degradation the
    counter records.

    The fallback is not a cache of the shared state and deliberately does not try to be one.
    Reconstructing shared state from a local copy after an outage would resurrect stale
    decisions about models that have since recovered.
    """

    def __init__(
        self, primary: CircuitStateStore, fallback: CircuitStateStore | None = None
    ) -> None:
        self._primary = primary
        self._fallback = fallback if fallback is not None else InMemoryCircuitStateStore()

    async def read(self, key: str) -> CircuitRecord | None:
        """The shared record, or the local one if the shared store is down."""
        try:
            return await self._primary.read(key)
        except CircuitStoreUnavailableError:
            CIRCUIT_STATE_UNAVAILABLE_ALARM.increment()
            return await self._fallback.read(key)

    async def write(self, key: str, record: CircuitRecord) -> None:
        """Publish, falling back to the local store."""
        try:
            await self._primary.write(key, record)
        except CircuitStoreUnavailableError:
            CIRCUIT_STATE_UNAVAILABLE_ALARM.increment()
            await self._fallback.write(key, record)

    async def acquire_probe(self, key: str, ttl_s: float) -> bool:
        """Take the probe slot, falling back to a per-process slot."""
        try:
            return await self._primary.acquire_probe(key, ttl_s)
        except CircuitStoreUnavailableError:
            CIRCUIT_STATE_UNAVAILABLE_ALARM.increment()
            return await self._fallback.acquire_probe(key, ttl_s)

    async def release_probe(self, key: str) -> None:
        """Release the probe slot in whichever store holds it."""
        try:
            await self._primary.release_probe(key)
        except CircuitStoreUnavailableError:
            CIRCUIT_STATE_UNAVAILABLE_ALARM.increment()
            await self._fallback.release_probe(key)


class CircuitBreaker:
    """The state machine. Holds no state itself; every decision is read from the store."""

    def __init__(
        self,
        *,
        store: CircuitStateStore,
        clock: Clock,
        config: CircuitConfig | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._config = config if config is not None else CircuitConfig()

    @property
    def config(self) -> CircuitConfig:
        """The thresholds in force, for `health()` and for tests."""
        return self._config

    async def state(self, key: str) -> CircuitState:
        """The state right now, with an elapsed cooldown reported as HALF_OPEN.

        Read-only: consulting the state does not consume the probe slot, so `health()` can
        report a circuit as half-open without stealing the probe from a real call.
        """
        record = await self._store.read(key)
        if record is None:
            return CircuitState.CLOSED
        if record.state is CircuitState.OPEN and self._cooldown_elapsed(record):
            return CircuitState.HALF_OPEN
        return record.state

    async def allows(self, key: str) -> Admission:
        """Whether this dependency may be called, and whether the call is the single probe."""
        record = await self._store.read(key)
        if record is None or record.state is CircuitState.CLOSED:
            return Admission.ADMIT
        if record.state is CircuitState.OPEN:
            if not self._cooldown_elapsed(record):
                return Admission.REFUSE
            await self._store.write(key, replace(record, state=CircuitState.HALF_OPEN))
        acquired = await self._store.acquire_probe(key, self._config.probe_lock_s)
        return Admission.ADMIT_PROBE if acquired else Admission.REFUSE

    async def record_success(self, key: str) -> None:
        """Close the circuit and forget the window. A success is the end of an incident."""
        record = await self._store.read(key)
        if record is None:
            return
        await self._store.write(
            key,
            CircuitRecord(
                state=CircuitState.CLOSED,
                failures=(),
                opened_at=None,
                open_duration_s=self._config.initial_open_s,
            ),
        )
        await self._store.release_probe(key)

    async def record_failure(self, key: str) -> None:
        """Add a failure, opening the circuit at the threshold or re-opening a failed probe."""
        now = self._clock.monotonic()
        record = await self._store.read(key) or CircuitRecord(
            open_duration_s=self._config.initial_open_s
        )
        if record.state is CircuitState.HALF_OPEN:
            await self._store.write(key, self._reopen(record, now))
            await self._store.release_probe(key)
            return
        failures = (*self._within_window(record.failures, now), now)
        if len(failures) >= self._config.failure_threshold:
            await self._store.write(
                key,
                CircuitRecord(
                    state=CircuitState.OPEN,
                    failures=failures,
                    opened_at=now,
                    open_duration_s=self._config.initial_open_s,
                ),
            )
            return
        await self._store.write(key, replace(record, state=CircuitState.CLOSED, failures=failures))

    def _reopen(self, record: CircuitRecord, now: float) -> CircuitRecord:
        """A failed probe: open again for double the last duration, capped `[gateway.md §4.3]`."""
        doubled = record.open_duration_s * self._config.backoff_factor
        return CircuitRecord(
            state=CircuitState.OPEN,
            failures=record.failures,
            opened_at=now,
            open_duration_s=min(doubled, self._config.max_open_s),
        )

    def _within_window(self, failures: tuple[float, ...], now: float) -> tuple[float, ...]:
        """Failures still inside the sliding window. Anything older is not evidence any more."""
        cutoff = now - self._config.window_s
        return tuple(moment for moment in failures if moment > cutoff)

    def _cooldown_elapsed(self, record: CircuitRecord) -> bool:
        if record.opened_at is None:
            return True
        return self._clock.monotonic() - record.opened_at >= record.open_duration_s
