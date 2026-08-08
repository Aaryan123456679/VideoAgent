"""Planning-stage failures, pinned to `planning.md` §5's failure-mode table."""

from __future__ import annotations

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError

__all__ = ["BibleTooVagueError", "PlanInvalidError", "PlanUnparseableError", "PlanningError"]


class PlanningError(VideoAgentError):
    """Base for every failure this module raises."""


class PlanUnparseableError(PlanningError):
    """The gateway could not produce JSON matching the plan draft schema. `VA-PLAN-001`."""

    code: ErrorCode = ErrorCode.VA_PLAN_001


class PlanInvalidError(PlanningError):
    """The plan's own deterministic validation failed twice. `VA-PLAN-002` / `VA-PLAN-003`.

    `planning.md` §3.1: one structured re-ask, then a job-scope failure. The code defaults to
    `VA-PLAN-003` (wrong count/kind/order) and is overridden to `VA-PLAN-002` when the specific
    violation is the duration sum, since that is the one the spec calls out separately.
    """

    code: ErrorCode = ErrorCode.VA_PLAN_003


class BibleTooVagueError(PlanningError):
    """The bible failed the specificity gate twice. `VA-BIBLE-001`."""

    code: ErrorCode = ErrorCode.VA_BIBLE_001
