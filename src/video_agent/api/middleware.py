"""The request boundary: one trace per request, and nothing escaping it uncoded.

Two jobs, in one middleware, because separating them would break the thing they exist for.

`[CPS §Failure behaviour]` requires the error envelope to carry the `trace_id` of the trace the
failure happened in. The trace is bound here, at the outermost point of the request, and the
last-resort exception catch has to sit **inside** that binding — otherwise the context has
already unwound by the time the 500 is rendered and the envelope sends support to a trace that
does not exist. Starlette's own `ServerErrorMiddleware` sits outside every user middleware, so
an exception left to it would be rendered exactly there: outside the trace.

Pure ASGI rather than `BaseHTTPMiddleware` for the same reason. `BaseHTTPMiddleware` runs the
downstream application in a separate task, and a `ContextVar` set in its `dispatch` is not
reliably the one the endpoint reads. A `ContextVar` is the entire propagation mechanism
`[observability.md §2]`, so a middleware that cannot set one is not a candidate.

The catch is `Exception`, not `BaseException`: a cancelled request is the client hanging up,
not a fault, and reporting it as `VA-INT-001` would fill the alarm with disconnections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders

from video_agent.api.errors import ErrorContext, build_envelope, status_for_code
from video_agent.api.responses import envelope_response
from video_agent.observability.codes import ErrorCode
from video_agent.observability.context import bind_trace
from video_agent.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

TRACE_HEADER: Final = "X-Trace-Id"
"""Echoes the trace id on every response, success or failure.

The envelope carries it on errors, but a caller reporting "the video looks wrong" has a `200`
in their hand and nothing to quote. The id is server-minted and never read from the request:
accepting a client-supplied trace id would let any caller merge their requests into someone
else's trace."""

_LOGGER: Final = get_logger(__name__)

_UNHANDLED_EVENT: Final = "unhandled_exception"


def _route_label(scope: Scope) -> str:
    """The matched route template, or the method alone when nothing matched.

    The template and never the raw path: a path carries job ids and query strings, and a query
    string is where a presigned URL would end up in a log line `[D-52]`.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    method = scope.get("method", "?")
    return f"{method} {path}" if isinstance(path, str) else f"{method} <unmatched>"


class RequestBoundaryMiddleware:
    """Binds a trace for the request and renders anything that escapes as `VA-INT-001`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI event stream."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with bind_trace() as trace_id:
            await self._handle(scope, receive, send, trace_id)

    async def _handle(self, scope: Scope, receive: Receive, send: Send, trace_id: str) -> None:
        started = False

        async def send_with_trace(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)[TRACE_HEADER] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        except Exception as exc:
            _log_unhandled(scope, exc)
            if started:
                # The status line is already on the wire; a second response would corrupt the
                # stream. Re-raising lets the server abort the connection, which is the only
                # honest signal left.
                raise
            # Sent through the wrapper, not through `send`: the last-resort 500 is precisely
            # the response a caller most needs the trace id from.
            await _internal_error_response(trace_id)(scope, receive, send_with_trace)


def _log_unhandled(scope: Scope, exc: Exception) -> None:
    """Record the failure where the detail is allowed to live.

    `reason` carries the exception's own text — the sentence naming the unset variable or the
    row that was missing — because the response deliberately cannot. `exc_info` is passed so
    the record carries `exc_type`; the formatter emits the type, never the frames, since
    `observability.md` §4 has no field for a traceback and the frames belong in the trace.

    A value in `reason` that trips the redaction tripwire raises here in dev and CI. That is
    the canary working, not a defect in this function: an exception message that contains a
    credential is a leak whether or not anyone renders it.
    """
    _LOGGER.error(
        "unhandled exception in %s",
        _route_label(scope),
        exc_info=exc,
        extra={
            "event": _UNHANDLED_EVENT,
            "code": ErrorCode.VA_INT_001.value,
            "http_status": status_for_code(ErrorCode.VA_INT_001),
            "retryable": ErrorCode.VA_INT_001.retryable,
            "reason": str(exc),
        },
    )


def _internal_error_response(trace_id: str) -> JSONResponse:
    """The generic 500 envelope. Carries no detail from the exception, by construction."""
    return envelope_response(
        build_envelope(ErrorCode.VA_INT_001, trace_id, context=ErrorContext()),
        status_code=status_for_code(ErrorCode.VA_INT_001),
    )
