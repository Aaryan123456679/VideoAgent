"""Turning an envelope into an HTTP response, in exactly one place.

Separate from `errors.py` so that module stays free of a web framework: the envelope is a
contract and is asserted against in tests that never build an application, while this module is
where Starlette appears. It also means there is a single `JSONResponse` construction for
errors, so `mode="json"` — which is what serialises the `UUID` in `job_id` — cannot be
remembered in one handler and forgotten in the next.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi.responses import JSONResponse

from video_agent.api.errors import ErrorEnvelope


def envelope_response(
    envelope: ErrorEnvelope,
    *,
    status_code: int,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Serialise `envelope` as the body of a `status_code` response."""
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=dict(headers) if headers else None,
    )
