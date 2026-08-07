"""The one shape every non-2xx response takes, and the failure type that produces it.

`api.md` §4 and `[CPS §Failure behaviour]` ask for one envelope carrying a stable code and the
`trace_id`, so that support can paste the id into Langfuse and open the exact trace. Two
mistakes break that promise and both are made by accident, so both are closed mechanically
here rather than by review.

**The message is derived from the code, never from the exception.** `ErrorCode.meaning` is a
compile-time constant, so no exception text, no stack frame and no environment-variable name
can reach a response body — not because every raise site remembered to be careful, but because
there is no parameter through which detail could travel. The detail still exists: it goes to
the log line, where it belongs, joined to the same `trace_id` the client was handed.

**`details` and `preserved` are filtered through the redaction tripwire.** They are the two
fields a caller may legitimately fill with machine-readable specifics, which makes them the two
fields through which a presigned URL or a credential would eventually escape. `safe_mapping`
runs the same detectors the log path runs `[observability.md §5]` and drops any value that
trips one; it is deliberately the existing scanner rather than a second, weaker copy of it.

The code taxonomy itself is owned by `observability.md` §6. This module maps it onto HTTP and
renders it, and does not invent codes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError
from video_agent.observability.redaction import is_credential_key, scan_payload

HTTP_BAD_REQUEST: Final = 400
HTTP_UNAUTHORIZED: Final = 401
HTTP_NOT_FOUND: Final = 404
HTTP_CONFLICT: Final = 409
HTTP_UNPROCESSABLE: Final = 422
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_INTERNAL_ERROR: Final = 500
HTTP_SERVICE_UNAVAILABLE: Final = 503

CLIENT_ERROR_FLOOR: Final = 400
SERVER_ERROR_FLOOR: Final = 500

HTTP_STATUS_BY_CODE: Final[Mapping[ErrorCode, int]] = {
    ErrorCode.VA_REQ_001: HTTP_BAD_REQUEST,
    ErrorCode.VA_REQ_002: HTTP_BAD_REQUEST,
    ErrorCode.VA_REQ_003: HTTP_CONFLICT,
    ErrorCode.VA_REQ_004: HTTP_CONFLICT,
    ErrorCode.VA_REQ_005: HTTP_NOT_FOUND,
    ErrorCode.VA_REQ_006: HTTP_CONFLICT,
    ErrorCode.VA_REQ_007: HTTP_UNPROCESSABLE,
    ErrorCode.VA_AUTH_001: HTTP_UNAUTHORIZED,
    # Never 403 to the client: confirming that a job exists but belongs to someone else is
    # itself the leak. `api.md` §4 renders VA-AUTH-002 as 404. The code stays distinct so the
    # *log* can tell a cross-tenant probe apart from a genuine miss.
    ErrorCode.VA_AUTH_002: HTTP_NOT_FOUND,
    ErrorCode.VA_GW_001: HTTP_SERVICE_UNAVAILABLE,
    ErrorCode.VA_GW_003: HTTP_TOO_MANY_REQUESTS,
    ErrorCode.VA_PROV_001: HTTP_SERVICE_UNAVAILABLE,
    ErrorCode.VA_STORE_002: HTTP_SERVICE_UNAVAILABLE,
    ErrorCode.VA_STORE_003: HTTP_SERVICE_UNAVAILABLE,
    ErrorCode.VA_INT_001: HTTP_INTERNAL_ERROR,
}
"""Every code `api.md` §4 puts on the wire, and the status it is rendered as.

Absence is not an error: a code raised from a worker has no HTTP meaning, and
`status_for_code` renders anything unmapped as `500`, because a code that reached the boundary
without a declared status is an internal fault rather than a client one."""

DEFAULT_CODE_BY_STATUS: Final[Mapping[int, ErrorCode]] = {
    HTTP_BAD_REQUEST: ErrorCode.VA_REQ_001,
    HTTP_UNAUTHORIZED: ErrorCode.VA_AUTH_001,
    HTTP_NOT_FOUND: ErrorCode.VA_REQ_005,
    HTTP_CONFLICT: ErrorCode.VA_REQ_004,
    HTTP_UNPROCESSABLE: ErrorCode.VA_REQ_007,
    HTTP_TOO_MANY_REQUESTS: ErrorCode.VA_GW_003,
    HTTP_INTERNAL_ERROR: ErrorCode.VA_INT_001,
    HTTP_SERVICE_UNAVAILABLE: ErrorCode.VA_PROV_001,
}
"""The code a bare `HTTPException(status_code=...)` is rendered under.

A framework-raised status with no code of its own still has to arrive as a coded envelope,
otherwise the contract holds only for the paths someone remembered to route through it."""

NEXT_STEPS_BY_CODE: Final[Mapping[ErrorCode, str]] = {
    ErrorCode.VA_REQ_002: (
        "Send an Idempotency-Key header with a unique value of 16 to 255 characters "
        "and repeat the request."
    ),
    ErrorCode.VA_REQ_003: (
        "This idempotency key was already used with a different request body. "
        "Use a new key, or repeat the original body to receive the original response."
    ),
    ErrorCode.VA_REQ_004: "An identical request is still in flight. Retry after the interval "
    "in the Retry-After header; do not send a new idempotency key.",
    ErrorCode.VA_REQ_005: "Check the job id. Jobs are visible only to the tenant that created "
    "them.",
    ErrorCode.VA_REQ_006: "This job is not resumable. Fetch its manifest instead.",
    ErrorCode.VA_REQ_007: "Correct the fields listed in details and resend.",
    ErrorCode.VA_AUTH_001: "Present a valid API key as 'Authorization: Bearer <key>'.",
    ErrorCode.VA_STORE_003: "A dependency is unavailable. Retry with exponential backoff; the "
    "request had no effect.",
    ErrorCode.VA_INT_001: "Retrying is unlikely to help. Contact support quoting the trace_id.",
}
"""Concrete next steps where a concrete one exists.

`[CPS §Failure behaviour]` asks failures to say *what to do next*, and "retry with backoff" is
not an answer to a missing header. Codes with no entry fall back to the retryability-derived
sentence in `next_steps_for`, which is always populated — an empty `next_steps` would satisfy
the schema while breaking the promise."""

RETRYABLE_NEXT_STEPS: Final = "Retry the request with exponential backoff."
NON_RETRYABLE_NEXT_STEPS: Final = (
    "Do not retry this request unchanged. Quote the trace_id when contacting support."
)


def status_for_code(code: ErrorCode) -> int:
    """The HTTP status `code` is rendered as, defaulting to `500`."""
    return HTTP_STATUS_BY_CODE.get(code, HTTP_INTERNAL_ERROR)


def code_for_status(status_code: int) -> ErrorCode:
    """The code a status with no code of its own is rendered under.

    The fallback splits on the status class rather than collapsing to `VA-INT-001`, because a
    `405` is not an internal error and rendering it as one tells the caller to contact support
    about their own request. Anything in the `4xx` range the table does not name is "your
    request was not acceptable"; anything else is ours.
    """
    named = DEFAULT_CODE_BY_STATUS.get(status_code)
    if named is not None:
        return named
    if CLIENT_ERROR_FLOOR <= status_code < SERVER_ERROR_FLOOR:
        return ErrorCode.VA_REQ_007
    return ErrorCode.VA_INT_001


PUBLIC_MESSAGE_BY_CODE: Final[Mapping[ErrorCode, str]] = {
    # The taxonomy's own sentence — "Job not found (also returned cross-tenant)" — is written
    # for whoever is reading a log. Putting it on the wire hands a caller the parenthetical
    # that `api.md` §4 exists to withhold.
    ErrorCode.VA_REQ_005: "Job not found.",
}
"""The few codes whose taxonomy meaning is operator-facing and needs a client-facing rewrite.

An override table rather than a second column in the enum, because `observability.md` §6 owns
that enum and a public rendering is not its concern. Kept as small as it can be: every entry is
a place where two sentences can drift apart."""


def message_for(code: ErrorCode) -> str:
    """The client-facing sentence for `code`. Constant per code, never derived from an
    exception — that is what makes a leak structurally impossible rather than merely unlikely.
    """
    return PUBLIC_MESSAGE_BY_CODE.get(code, code.meaning)


def next_steps_for(code: ErrorCode) -> str:
    """What the caller should do about `code`. Never empty."""
    specific = NEXT_STEPS_BY_CODE.get(code)
    if specific is not None:
        return specific
    return RETRYABLE_NEXT_STEPS if code.retryable else NON_RETRYABLE_NEXT_STEPS


def safe_mapping(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """`values` with every entry the redaction tripwire objects to removed.

    Dropped, not masked: `observability.md` §5 is deny-by-default, and a masked value still
    tells a reader how long the secret was and that it existed. The scan is the same one the
    log path runs, so a value that could never be logged can never be returned either.
    """
    if not values:
        return {}
    return {
        key: value
        for key, value in values.items()
        if not is_credential_key(key) and not scan_payload({key: value})
    }


class ErrorEnvelope(BaseModel):
    """`api.md` §4: the single body shape for every non-2xx response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class Error(BaseModel):
        """The envelope's only member. Nested because the wire shape is `{"error": {...}}`."""

        model_config = ConfigDict(frozen=True, extra="forbid")

        code: str
        message: str
        retryable: bool
        trace_id: str
        job_id: UUID | None = None
        preserved: dict[str, Any] = Field(default_factory=dict)
        next_steps: str
        details: dict[str, Any] = Field(default_factory=dict)

    error: Error


class ErrorContext(BaseModel):
    """The client-visible extras a raise site may attach, grouped so signatures stay small."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    preserved: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


class ApiError(VideoAgentError):
    """A failure that the API renders directly, with the status its code maps to.

    Everything a raise site can influence — `job_id`, `preserved`, `details`, response headers
    — is data the client may see. The human sentence it passes as `log_detail` is not: it goes
    to `VideoAgentError.message`, which the renderer never reads. That asymmetry is the whole
    design, and it is why `ApiError` has no `public_message` parameter.
    """

    def __init__(
        self,
        code: ErrorCode,
        *,
        log_detail: str | None = None,
        job_id: UUID | None = None,
        context: ErrorContext | None = None,
    ) -> None:
        super().__init__(log_detail or code.meaning, code=code)
        self.job_id = job_id
        resolved = context if context is not None else ErrorContext()
        self.preserved = safe_mapping(resolved.preserved)
        self.details = safe_mapping(resolved.details)
        self.headers = dict(resolved.headers or {})

    @property
    def status_code(self) -> int:
        """The HTTP status this error is rendered as."""
        return status_for_code(self.code)


def build_envelope(
    code: ErrorCode,
    trace_id: str,
    *,
    job_id: UUID | None = None,
    context: ErrorContext | None = None,
) -> ErrorEnvelope:
    """Render `code` into the envelope, with the message taken from the taxonomy.

    `trace_id` is a required positional rather than something read from the ambient context,
    because the id has to be the one the failure happened in. `VideoAgentError` captures that
    at raise time; re-reading the contextvar here would send support to whichever trace
    happened to be bound when the response was serialised.
    """
    return ErrorEnvelope(
        error=ErrorEnvelope.Error(
            code=code.value,
            message=message_for(code),
            retryable=code.retryable,
            trace_id=trace_id,
            job_id=job_id,
            preserved=safe_mapping(context.preserved if context else None),
            next_steps=next_steps_for(code),
            details=safe_mapping(context.details if context else None),
        )
    )
