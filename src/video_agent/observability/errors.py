"""The base failure type: a stable code and the trace it happened in, on every error.

`[CPS §Failure behaviour]` — *every error response carries a stable code and a `trace_id`* —
is two separate promises, and each is broken by a different mistake.

The **code** is broken by raising a bare `ValueError` somewhere and mapping it to a generic
`500` at the boundary. `VideoAgentError` makes the code part of the exception's identity: a
subclass declares its code once, as a class attribute, and every instance carries it.

The **`trace_id`** is broken more subtly, by capturing it too late. An exception is often
rendered into the API error envelope far from where it was raised — after the graph node
returned, after the task that held the context finished. Read at *that* point the contextvar
may hold a different trace or none at all, and the envelope would send support to the wrong
trace, which is worse than sending them nowhere. So the id is captured in `__init__`, at the
moment and in the context where the failure actually happened.

The envelope itself belongs to `api.md` §4 and is rendered by the API module; this type is
what it renders.
"""

from __future__ import annotations

from video_agent.observability.codes import ErrorCode
from video_agent.observability.context import current_trace_id


class VideoAgentError(Exception):
    """A failure with a taxonomy code, pinned to the trace in which it was raised.

    Subclasses set `code`. The default is `VA-INT-001` rather than something more specific
    because an unclassified failure *is* an internal error, and defaulting to a plausible
    domain code would file bugs under the wrong heading.
    """

    code: ErrorCode = ErrorCode.VA_INT_001

    def __init__(self, message: str, *, code: ErrorCode | None = None) -> None:
        self.code = code if code is not None else type(self).code
        self.trace_id = current_trace_id()
        self.message = message
        super().__init__(f"{self.code}: {message}")

    @property
    def retryable(self) -> bool:
        """Whether the operation may be attempted again, as the taxonomy defines it.

        Read from the code, never set per raise site. `[D-62]` exists because one call site
        deciding a `402` is worth one more attempt is how credit exhaustion turns into a
        retry storm against a provider that has already said no.
        """
        return self.code.retryable
