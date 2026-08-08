"""Exponential backoff with jitter, retryable errors only, max three attempts.

`gateway.md` §4.1, verbatim:

```
attempt n delay = min(base * 2**(n-1), cap) * uniform(0.5, 1.5)     base=0.5s, cap=8s
```

Two details in that line are easy to get wrong and both are asserted rather than commented.

**"Max 3" is three attempts total, not three retries after the first.** The distinction is a
33% difference in the cost and latency of every persistently failing call, and the natural way
to write a retry loop — `for _ in range(max_retries)` around a call that already happened —
produces four. `attempt_numbers()` yields exactly `1, 2, 3` so the count is a property of the
policy rather than of whoever writes the loop.

**The jitter multiplies the capped delay, not the uncapped one.** `min(...) * uniform(...)`,
not `min(... * uniform(...))`. Written the second way the cap would truncate the jitter's upper
half, so a delay at the cap would never exceed `8s` and the distribution would quietly lose the
spread that stops synchronised workers retrying in lockstep.

The base sequence — `0.5, 1.0, 2.0` — is monotonically non-decreasing by construction, and
stays so after the cap because `min` of a non-decreasing sequence with a constant is
non-decreasing. Jitter is applied per attempt and deliberately *does not* preserve monotonicity
of the realised delays; that is what jitter is for, and the property worth asserting is that
each realised delay stays within `[0.5x, 1.5x]` of its own base.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from video_agent.gateway.clock import JitterSource

__all__ = ["JITTER_HIGH", "JITTER_LOW", "MAX_ATTEMPTS", "RetryPolicy"]

MAX_ATTEMPTS: Final = 3
"""`[CPS §Failure behaviour]`: retry, *max 3*. Attempts, including the first."""

BASE_DELAY_SECONDS: Final = 0.5
CAP_DELAY_SECONDS: Final = 8.0
JITTER_LOW: Final = 0.5
JITTER_HIGH: Final = 1.5


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """The retry schedule. Frozen, because a policy edited at runtime is a policy nobody knows.

    The parameters are fields rather than module constants so a test can shrink the cap and
    still exercise capping, but they default to the specification's numbers and no caller in
    the application passes anything else.
    """

    max_attempts: int = MAX_ATTEMPTS
    base_delay_s: float = BASE_DELAY_SECONDS
    cap_delay_s: float = CAP_DELAY_SECONDS
    jitter_low: float = JITTER_LOW
    jitter_high: float = JITTER_HIGH

    def attempt_numbers(self) -> Iterator[int]:
        """`1 .. max_attempts`. The loop bound, so no call site can invent a fourth attempt."""
        return iter(range(1, self.max_attempts + 1))

    def base_delay(self, attempt: int) -> float:
        """The un-jittered delay after attempt `n`: `min(base * 2**(n-1), cap)`."""
        if attempt < 1:
            message = f"attempt numbers start at 1, got {attempt}"
            raise ValueError(message)
        return min(self.base_delay_s * 2.0 ** (attempt - 1), self.cap_delay_s)

    def base_schedule(self) -> tuple[float, ...]:
        """Every un-jittered delay this policy can impose, in order.

        The observable schedule, which is what a test should assert against. Asserting that the
        implementation called its own helper would pass just as happily if the helper returned
        a constant.
        """
        return tuple(self.base_delay(attempt) for attempt in self.attempt_numbers())

    def delay(self, attempt: int, jitter: JitterSource) -> float:
        """The realised delay after attempt `n`: the capped base scaled by the jitter draw."""
        return self.base_delay(attempt) * jitter.uniform(self.jitter_low, self.jitter_high)

    def is_last(self, attempt: int) -> bool:
        """Whether `attempt` is the final one, so nothing sleeps after the last failure.

        Sleeping after the last attempt would add up to two seconds of pure latency to every
        exhausted call, waiting for a retry that is never going to happen.
        """
        return attempt >= self.max_attempts
