"""Every way a request can fail, rendered into the one envelope.

There are exactly four routes out of a failing request and all four are covered here, because
a contract that holds for three of them is not a contract:

1. `ApiError` — a failure this module raised on purpose, with a code it chose.
2. `VideoAgentError` — a coded failure from anywhere else in the tree. Its code already maps to
   a status; nothing has to be re-derived at the boundary, which is the point of putting the
   code on the exception `[observability.md §6]`.
3. `HTTPException` — **Starlette's**, not FastAPI's. FastAPI's subclasses it, so registering
   the base catches both; registering the subclass catches only the ones our own code raises
   and leaves the router's own `404` and `405` to be rendered as `{"detail": "Not Found"}`.
   That was not a hypothetical: it is what the first version of this module did. It arrives
   with a status and no code, so it is given the code its status renders as.
4. `RequestValidationError` — FastAPI's own body/path/query validation, `422 VA-REQ-007`.

The fifth route, an exception nobody classified, is closed by `RequestBoundaryMiddleware`,
which has to sit inside the trace binding and therefore cannot be a handler.

**`details` on a validation error carries `loc` and `type` and nothing else.** Pydantic's error
dicts include `input`, the value that failed — which for a rejected credential field is the
credential. Echoing a rejected value back is how a validation error becomes a disclosure, so
the value never reaches the renderer at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from video_agent.api.errors import (
    ApiError,
    ErrorContext,
    build_envelope,
    code_for_status,
    status_for_code,
)
from video_agent.api.responses import envelope_response
from video_agent.observability.codes import ErrorCode
from video_agent.observability.context import current_trace_id, synthesised_trace_id
from video_agent.observability.errors import VideoAgentError
from video_agent.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi.responses import JSONResponse
    from starlette.requests import Request

_LOGGER: Final = get_logger(__name__)

MAX_REPORTED_VALIDATION_ERRORS: Final = 20
"""Cap on the `details.fields` list. A caller that sent 900 invalid items needs the first
handful to fix its client, and an unbounded list turns one bad request into a large response."""


def effective_trace_id(exc: BaseException | None = None) -> str:
    """The trace id to put on the envelope, preferring the one captured at raise time.

    `VideoAgentError` records the id in `__init__`, in the context where the failure actually
    happened `[observability/errors.py]`. Reading the contextvar instead would be right most of
    the time and wrong exactly when it matters — after a task boundary — and an envelope
    pointing at the wrong trace is worse than one pointing at none.
    """
    captured = getattr(exc, "trace_id", None)
    if isinstance(captured, str) and captured:
        return captured
    return current_trace_id() or synthesised_trace_id()


def _log_error(code: ErrorCode, status_code: int, request: Request, reason: str) -> None:
    """One line per rendered failure, carrying the code the client was given.

    Logged at `warning` below `500` and `error` at or above it: a `404` is a client mistake and
    a `500` is ours, and paging on the first would train everyone to ignore the second.
    """
    route = getattr(request.scope.get("route"), "path", "<unmatched>")
    level = (
        _LOGGER.error if status_code >= status_for_code(ErrorCode.VA_INT_001) else _LOGGER.warning
    )
    level(
        "request failed: %s %s",
        request.method,
        route,
        extra={
            "event": "request_failed",
            "code": code.value,
            "http_status": status_code,
            "retryable": code.retryable,
            "reason": reason,
        },
    )


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an `ApiError`, including the headers its code requires (`Retry-After`)."""
    error = exc if isinstance(exc, ApiError) else ApiError(ErrorCode.VA_INT_001)
    _log_error(error.code, error.status_code, request, error.message)
    envelope = build_envelope(
        error.code,
        effective_trace_id(error),
        job_id=error.job_id,
        context=ErrorContext(preserved=error.preserved, details=error.details),
    )
    return envelope_response(envelope, status_code=error.status_code, headers=error.headers)


async def video_agent_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a coded failure raised outside the API module."""
    error = exc if isinstance(exc, VideoAgentError) else VideoAgentError(str(exc))
    status_code = status_for_code(error.code)
    _log_error(error.code, status_code, request, error.message)
    return envelope_response(
        build_envelope(error.code, effective_trace_id(error)),
        status_code=status_code,
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a framework-raised `HTTPException` under the code its status maps to."""
    status_code = (
        exc.status_code if isinstance(exc, HTTPException) else status_for_code(ErrorCode.VA_INT_001)
    )
    code = code_for_status(status_code)
    detail = getattr(exc, "detail", "")
    _log_error(code, status_code, request, str(detail))
    headers = getattr(exc, "headers", None) or {}
    return envelope_response(
        build_envelope(code, effective_trace_id(exc)),
        status_code=status_code,
        headers=headers,
    )


def _validation_fields(exc: RequestValidationError) -> list[dict[str, str]]:
    """The rejected locations and rule names — never the rejected values."""
    fields: list[dict[str, str]] = []
    for entry in exc.errors()[:MAX_REPORTED_VALIDATION_ERRORS]:
        location = entry.get("loc", ())
        fields.append(
            {
                "field": ".".join(str(part) for part in location),
                "rule": str(entry.get("type", "invalid")),
            }
        )
    return fields


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render FastAPI's request validation failure as `422 VA-REQ-007`."""
    code = ErrorCode.VA_REQ_007
    details: dict[str, Any] = (
        {"fields": _validation_fields(exc)} if isinstance(exc, RequestValidationError) else {}
    )
    _log_error(code, status_for_code(code), request, "request schema invalid")
    return envelope_response(
        build_envelope(code, effective_trace_id(exc), context=ErrorContext(details=details)),
        status_code=status_for_code(code),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler on `app`.

    `ApiError` is registered after `VideoAgentError` deliberately: Starlette resolves handlers
    by walking the exception's MRO, so the most specific registration must exist, and an
    `ApiError` would otherwise be rendered by its base class's handler and lose its headers.
    """
    app.add_exception_handler(VideoAgentError, video_agent_error_handler)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
