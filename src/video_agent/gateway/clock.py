"""Time and randomness as injected collaborators, so failure policy is testable in microseconds.

Every timing rule in `gateway.md` §4 — three attempts with exponential backoff, a thirty-second
sliding window, a cooldown that doubles to a five-minute cap — is a rule about *when*. A test
that proved any of them by actually waiting would take real seconds to run, and `gateway.md` §9
asks for a *time-controlled* test rather than a slow one for a reason that has nothing to do
with patience: a suite that takes seconds to prove backoff is a suite that gets marked
`skip` the first time CI is under load and it flakes. At that point the rule is unproven and
nobody notices, which is worse than never having tested it.

So both non-determinism sources are parameters. `Clock` covers reading the time and waiting;
`JitterSource` covers the `uniform(0.5, 1.5)` multiplier. Neither is patched at the module
level in tests, because a monkeypatched `time.monotonic` is global state that leaks between
tests and cannot express "this breaker is at t=29 while that one is at t=31".
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Final, Protocol

__all__ = ["Clock", "JitterSource", "SystemClock", "SystemJitter"]

_RANDOM_RESOLUTION: Final = 1 << 32
"""Granularity of the uniform draw. Fine enough that the jitter distribution is smooth, and an
exact power of two so the modulo is unbiased."""


class Clock(Protocol):
    """Monotonic time and waiting. Monotonic because every rule here is about elapsed time.

    Wall-clock would let an NTP correction close a circuit early or hold it open for an hour,
    and a breaker whose cooldown depends on whether the host has drifted is not a breaker.
    """

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class JitterSource(Protocol):
    """A draw from `uniform(low, high)`. Injected so a backoff test can pin the multiplier."""

    def uniform(self, low: float, high: float) -> float: ...


class SystemClock:
    """The real clock. `asyncio.sleep`, so a backoff never blocks the event loop."""

    def monotonic(self) -> float:
        """Seconds from an unspecified origin, guaranteed not to go backwards."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Yield to the loop for `seconds`."""
        await asyncio.sleep(seconds)


class SystemJitter:
    """Uniform jitter drawn from the system CSPRNG.

    `secrets` rather than `random` for one practical reason and one lint-shaped one. The
    practical reason: `random` is seeded per process, and workers that start together — which
    is what a container orchestrator does — would draw the *same* backoff sequence and retry in
    lockstep, which is exactly the thundering herd jitter exists to break up. The lint-shaped
    one: `S311` bans the non-cryptographic generator outright, and `AGENT.md` §9 allows no
    inline suppression to argue with it.
    """

    def uniform(self, low: float, high: float) -> float:
        """A draw from `[low, high)`, or `low` when the interval is empty."""
        if high <= low:
            return low
        return low + (high - low) * (secrets.randbelow(_RANDOM_RESOLUTION) / _RANDOM_RESOLUTION)
