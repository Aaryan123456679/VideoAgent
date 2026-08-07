"""The request boundary: one trace per request, and nothing escaping outside it.

The subtle failure this file exists to catch is not "the middleware is missing". It is "the
middleware is there, and the trace it binds is not the one the envelope reports" — which
happens the moment the last-resort catch moves outside the binding, or the moment the
middleware is rewritten as a `BaseHTTPMiddleware` whose `ContextVar` the endpoint cannot see.
Both produce a working application, passing status codes, and a `trace_id` that opens nothing.

So the assertions are about *identity* and *isolation*: the id in the envelope is the id the
handler logged under, and two concurrent requests never share one.
"""

from __future__ import annotations

import asyncio
from typing import Final

import pytest

from tests.unit.test_api_support import OK, api_client, build_app
from tests.unit.test_app_shell import captured_logs
from video_agent.api.middleware import TRACE_HEADER
from video_agent.observability.codes import ErrorCode
from video_agent.observability.context import SYNTHESISED_TRACE_PREFIX, current_trace_id

CONCURRENT_REQUESTS: Final = 8


@pytest.mark.asyncio
async def test_a_trace_is_bound_for_every_request() -> None:
    """A real trace, not the formatter's synthesised stand-in.

    A synthesised id is the observability module telling us nothing was bound
    `[observability.md §10]`. Seeing one on a request path means the middleware did not run.
    """
    app = build_app()

    async with api_client(app) as client:
        response = await client.get("/healthz")

    trace_id = response.headers[TRACE_HEADER]
    assert trace_id
    assert not trace_id.startswith(SYNTHESISED_TRACE_PREFIX)


@pytest.mark.asyncio
async def test_the_envelope_reports_the_trace_the_failure_was_logged_under() -> None:
    """The id the client is handed opens the trace the log line belongs to, or it is useless."""
    app = build_app()

    with captured_logs() as lines:
        async with api_client(app) as client:
            response = await client.get("/probe/boom")

    reported = response.json()["error"]["trace_id"]
    failures = [line for line in lines if line.get("code") == ErrorCode.VA_INT_001.value]

    assert failures
    assert failures[0]["trace_id"] == reported
    assert response.headers[TRACE_HEADER] == reported


@pytest.mark.asyncio
async def test_the_trace_does_not_leak_out_of_the_request() -> None:
    """The binding is scoped. A trace still bound afterwards would attribute the next job here."""
    app = build_app()

    async with api_client(app) as client:
        await client.get("/healthz")

    assert current_trace_id() is None


@pytest.mark.asyncio
async def test_concurrent_requests_get_distinct_traces() -> None:
    """One trace per request, even when they interleave.

    A `ContextVar` set on a shared object rather than in the request's own context would give
    every concurrent request the last writer's id — and every log line the wrong job.
    """
    app = build_app()

    async with api_client(app) as client:
        responses = await asyncio.gather(
            *(client.get("/healthz") for _ in range(CONCURRENT_REQUESTS))
        )

    traces = {response.headers[TRACE_HEADER] for response in responses}
    assert len(traces) == CONCURRENT_REQUESTS


@pytest.mark.asyncio
async def test_a_client_supplied_trace_id_is_ignored() -> None:
    """Accepting one would let any caller merge their requests into another tenant's trace."""
    planted = "ffffffffffffffffffffffffffffffff"
    app = build_app()

    async with api_client(app) as client:
        response = await client.get("/healthz", headers={TRACE_HEADER: planted})

    assert response.headers[TRACE_HEADER] != planted


@pytest.mark.asyncio
async def test_the_middleware_records_the_route_template_not_the_raw_path() -> None:
    """`msg` names the route as it is declared, not as it was requested.

    A raw path carries job ids and a query string, and a query string is where a presigned URL
    ends up in a log line `[D-52]`. The marker is deliberately present elsewhere in the record,
    so this asserts the *choice* of template over path rather than the absence of the value.
    """
    marker = "concrete-path-segment"
    app = build_app()

    with captured_logs() as lines:
        async with api_client(app) as client:
            await client.get(f"/probe/boom/{marker}")

    unhandled = [line for line in lines if line.get("event") == "unhandled_exception"]
    assert unhandled
    assert unhandled[0]["msg"] == "unhandled exception in GET /probe/boom/{marker}"
    assert marker not in unhandled[0]["msg"]
    assert marker in unhandled[0]["reason"]


@pytest.mark.asyncio
async def test_a_successful_response_is_not_rewritten() -> None:
    """The middleware wraps `send` to add a header; it must not touch anything else."""
    app = build_app()

    async with api_client(app) as client:
        response = await client.get("/healthz")

    assert response.status_code == OK
    assert response.json() == {"status": "alive"}
    assert response.headers["cache-control"] == "no-store"
