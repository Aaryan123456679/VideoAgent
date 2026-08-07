"""Counters for the conditions that must never happen silently.

Two rules in `observability.md` §10 are of the form *degrade, but alarm* — a log line with no
`trace_id` is synthesised rather than dropped, a redaction tripwire hit in production drops the
value rather than killing the request. Both are the right runtime behaviour and both are
worthless without a number that goes up, because a fail-safe nobody can see is
indistinguishable from a bug that never happens.

These are counters, not metrics-library gauges. `T4.1` owns the exporter; what has to exist
now is the count itself and a name for it, so the tests that assert "this degraded" have
something to assert against and the exporter later has something to read.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass(eq=False)
class AlarmCounter:
    """A monotonically increasing count of one named abnormal condition.

    Locked because logging is called from whatever thread happens to be running, and a
    counter that loses increments under concurrency understates exactly the incident it
    exists to surface.
    """

    name: str
    _count: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def count(self) -> int:
        """How many times the condition has fired since the process started."""
        with self._lock:
            return self._count

    def increment(self, amount: int = 1) -> int:
        """Record `amount` occurrences and return the new total."""
        with self._lock:
            self._count += amount
            return self._count

    def reset(self) -> None:
        """Zero the counter. For tests; production counters only ever go up."""
        with self._lock:
            self._count = 0
