"""Harness failures, each pinned to the taxonomy code its termination will report.

Every type here is raised by the harness itself rather than propagated from a dependency, and
each one exists because the alternative — returning a falsy value and letting the caller decide
— is how the rule it guards gets skipped. A `charge()` that returned `False` on a failed write
would be checked at three of four call sites and the fourth would be the unbounded budget.
"""

from __future__ import annotations

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError

__all__ = [
    "BibleHashMismatchError",
    "ChargeConflictError",
    "HarnessError",
    "LedgerWriteError",
    "SettlementError",
    "UngrantedToolError",
    "UnknownToolError",
]


class HarnessError(VideoAgentError):
    """Base for every failure the harness raises. Defaults to the internal code."""

    code: ErrorCode = ErrorCode.VA_INT_001


class UngrantedToolError(HarnessError):
    """A node called a tool it was not granted. `harness.md` §3.1 rule 4.

    A programming error, not a runtime condition: the grant table is static, so a call that is
    not in it could never have been valid. Raising rather than no-opping is the point — a
    silent no-op turns `plan_story` calling `video.generate` into a shot that never renders
    instead of a test failure.
    """


class UnknownToolError(HarnessError):
    """A grant or a call named a tool the registry does not define."""


class BibleHashMismatchError(HarnessError):
    """The stored continuity bible does not hash to its recorded digest.

    `harness.md` §3.1 rule 2: a mutated bible invalidates every downstream shot, so this ends
    the job rather than degrading it. Continuing would produce four shots that agree with each
    other and with nothing the caller approved.
    """

    code: ErrorCode = ErrorCode.VA_BIBLE_002


class LedgerWriteError(HarnessError):
    """A charge could not be recorded. Terminates the job. `[D-19]`.

    Coded `VA-INT-001` rather than `VA-STORE-003` on purpose. `VA-STORE-003` is retryable, and
    that is the correct classification of *the store being unavailable* — which is why the
    store retries it. What reaches here is the residue: the charge is not recorded and the
    budget is therefore unbounded. That is an invariant breach, and `[D-19]` says a
    non-negotiable cap may not be degraded, so it must not present itself to `decide()` as
    something worth another attempt.
    """


class ChargeConflictError(HarnessError):
    """The same charge id was applied twice with different amounts.

    Re-applying an *identical* charge is the resume path and is a no-op; re-applying a
    different one means two different costs share an identity, and silently keeping either is
    a mis-billed job.
    """


class SettlementError(HarnessError):
    """A charge was settled twice, or settled without ever having been provisional. `[D-60]`.

    A provisional charge may be corrected **exactly once**. A second correction is not a
    late-arriving truth, it is a double count, and the ledger has no way to tell which of the
    two settlements is the real one — so it refuses both rather than picking.
    """
