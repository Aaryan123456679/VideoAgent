"""The envelope as a contract, asserted without building an application.

`api.md` §4 promises three things that are easy to state and easy to lose:

1. **Every code renders.** Not "every code we happened to raise from a route" — every one of
   the 52. A code with no status, no message or no next steps would produce a `500` with an
   empty sentence the first time some worker raised it, which is precisely when nobody is
   watching.
2. **The message comes from the taxonomy.** The renderer takes no message argument, so no
   exception text, stack frame or variable name has a path to a response body.
3. **`details` and `preserved` are scanned.** They are the only two fields a caller fills, and
   therefore the only two through which a presigned URL escapes.
"""

from __future__ import annotations

import inspect
from typing import Final
from uuid import uuid4

import pytest

from video_agent.api.errors import (
    CLIENT_ERROR_FLOOR,
    HTTP_NOT_FOUND,
    HTTP_SERVICE_UNAVAILABLE,
    ApiError,
    ErrorContext,
    ErrorEnvelope,
    build_envelope,
    code_for_status,
    message_for,
    next_steps_for,
    safe_mapping,
    status_for_code,
)
from video_agent.observability.codes import ErrorCode
from video_agent.observability.context import bind_trace

TRACE: Final = "0123456789abcdef0123456789abcdef"

PRESIGNED_URL: Final = (
    "https://bucket.s3.amazonaws.com/final.mp4?X-Amz-Signature=abc123&X-Amz-Expires=900"
)
"""The shape `[D-52]` and `[D-64]` name explicitly: authorisation carried in a query string."""


@pytest.mark.parametrize("code", list(ErrorCode), ids=[code.value for code in ErrorCode])
def test_every_code_renders_a_complete_envelope(code: ErrorCode) -> None:
    """All 52 codes, not only the ones a route currently raises.

    A code that reaches the boundary without a status or without next steps produces a broken
    envelope the first time a worker raises it, which is exactly when nobody is looking.
    """
    envelope = build_envelope(code, TRACE)

    assert envelope.error.code == code.value
    assert envelope.error.message
    assert envelope.error.next_steps
    assert envelope.error.trace_id == TRACE
    assert envelope.error.retryable == code.retryable
    assert ErrorEnvelope.model_validate(envelope.model_dump(mode="json")) == envelope


@pytest.mark.parametrize("code", list(ErrorCode), ids=[code.value for code in ErrorCode])
def test_every_code_maps_to_a_plausible_status(code: ErrorCode) -> None:
    """A code with no declared status renders as `500`, never as a `200` or a `0`."""
    status = status_for_code(code)

    assert CLIENT_ERROR_FLOOR <= status <= HTTP_SERVICE_UNAVAILABLE


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, ErrorCode.VA_REQ_001),
        (401, ErrorCode.VA_AUTH_001),
        (404, ErrorCode.VA_REQ_005),
        (405, ErrorCode.VA_REQ_007),
        (409, ErrorCode.VA_REQ_004),
        (415, ErrorCode.VA_REQ_007),
        (422, ErrorCode.VA_REQ_007),
        (429, ErrorCode.VA_GW_003),
        (500, ErrorCode.VA_INT_001),
        (502, ErrorCode.VA_INT_001),
        (503, ErrorCode.VA_PROV_001),
    ],
    ids=str,
)
def test_a_status_with_no_code_of_its_own_still_gets_one(
    status_code: int, expected: ErrorCode
) -> None:
    """An unmapped `4xx` is the caller's problem, an unmapped `5xx` is ours.

    Collapsing both to `VA-INT-001` would tell someone who sent a `405` to contact support
    about an internal error.
    """
    assert code_for_status(status_code) == expected


def test_the_renderer_takes_no_message_argument() -> None:
    """The signature is the control: there is no parameter through which detail could travel."""
    parameters = set(inspect.signature(build_envelope).parameters)

    assert "message" not in parameters
    assert parameters == {"code", "trace_id", "job_id", "context"}


def test_an_api_error_keeps_its_detail_out_of_the_rendered_body() -> None:
    """`log_detail` reaches `VideoAgentError.message` and stops there."""
    planted = "the DATABASE_URL variable is unset"
    error = ApiError(ErrorCode.VA_STORE_003, log_detail=planted)

    envelope = build_envelope(error.code, TRACE)

    assert planted in error.message
    assert planted not in envelope.model_dump_json()
    assert envelope.error.message == message_for(ErrorCode.VA_STORE_003)


def test_next_steps_is_never_empty() -> None:
    """`[CPS §Failure behaviour]` asks what to do next; an empty string answers nothing."""
    assert all(next_steps_for(code).strip() for code in ErrorCode)


def test_next_steps_differs_by_retryability_for_unlisted_codes() -> None:
    """The fallback is derived, not constant, so it cannot tell a caller to retry a `402`."""
    assert next_steps_for(ErrorCode.VA_PROV_003) != next_steps_for(ErrorCode.VA_PROV_009)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("api_key", "anything at all"),
        ("authorization", "Bearer abcdefghijklmnop"),
        ("download_url", PRESIGNED_URL),
        ("innocuous", PRESIGNED_URL),
        ("database_url", "postgresql://user:hunter2@host/db"),
        ("token", "AKIAIOSFODNN7EXAMPLE"),
    ],
    ids=[
        "key-by-name",
        "authorization",
        "presigned",
        "presigned-under-innocent-name",
        "dsn",
        "aws",
    ],
)
def test_safe_mapping_drops_anything_the_tripwire_objects_to(key: str, value: str) -> None:
    """Dropped, not masked. A masked value still reports that a secret existed and how long."""
    assert safe_mapping({key: value}) == {}


def test_safe_mapping_keeps_ordinary_machine_readable_detail() -> None:
    """The negative case: a filter that dropped everything would satisfy the test above."""
    kept = safe_mapping({"unavailable": ["database"], "min_length": 16, "shot_index": 2})

    assert kept == {"unavailable": ["database"], "min_length": 16, "shot_index": 2}


def test_a_presigned_url_cannot_be_smuggled_through_preserved() -> None:
    """`preserved` is scanned on the way in *and* on the way out, so neither path leaks."""
    error = ApiError(
        ErrorCode.VA_BUDGET_001,
        context=ErrorContext(preserved={"manifest": PRESIGNED_URL, "shots_accepted": 3}),
    )
    envelope = build_envelope(
        error.code, TRACE, context=ErrorContext(preserved={"manifest": PRESIGNED_URL})
    )

    assert error.preserved == {"shots_accepted": 3}
    assert envelope.error.preserved == {}


def test_the_envelope_rejects_unknown_fields() -> None:
    """`extra="forbid"`, so a field added to the wire shape without a schema change fails here."""
    with pytest.raises(ValueError, match="extra_forbidden"):
        ErrorEnvelope.model_validate(
            {
                "error": {
                    "code": "VA-INT-001",
                    "message": "m",
                    "retryable": False,
                    "trace_id": TRACE,
                    "next_steps": "n",
                    "hint": "leaked",
                }
            }
        )


def test_the_trace_id_is_the_one_captured_at_raise_time() -> None:
    """An error raised inside a trace keeps that trace, even when rendered outside it.

    Reading the contextvar at render time is right most of the time and wrong exactly when it
    matters, and an envelope pointing at someone else's trace is worse than one pointing
    nowhere.
    """
    with bind_trace(TRACE):
        error = ApiError(ErrorCode.VA_INT_001)

    envelope = build_envelope(error.code, error.trace_id or "")

    assert envelope.error.trace_id == TRACE


def test_a_cross_tenant_error_carries_the_job_id_it_refused() -> None:
    """`job_id` on the envelope is what lets support correlate a `404` with a real row."""
    job_id = uuid4()

    envelope = build_envelope(ErrorCode.VA_REQ_005, TRACE, job_id=job_id)

    assert envelope.error.job_id == job_id
    assert status_for_code(ErrorCode.VA_REQ_005) == HTTP_NOT_FOUND


def test_tenant_forbidden_is_never_rendered_as_403() -> None:
    """`VA-AUTH-002` exists so the log can say `403`; the wire must still say `404`."""
    assert status_for_code(ErrorCode.VA_AUTH_002) == HTTP_NOT_FOUND
