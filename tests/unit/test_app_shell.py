"""`S0.4.1` — the app factory, the two probes, the envelope and the lifespan.

The five acceptance criteria map one-to-one onto the five groups below. Each test is written so
that removing the behaviour it covers makes it fail: the readiness tests assert the *code* and
the named dependency rather than merely a non-200, the leak test asserts on the response body
**and** on the log line, and the lifespan tests assert on the resource objects rather than on
`close()` having been reached.
"""

from __future__ import annotations

import io
import json
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final

import pytest

from tests.unit.test_api_support import (
    OK,
    PLANTED_DETAIL,
    FailingFactory,
    RecordingDatabase,
    RecordingProbe,
    api_client,
    build_app,
    build_resources,
)
from video_agent.api.errors import (
    HTTP_INTERNAL_ERROR,
    HTTP_SERVICE_UNAVAILABLE,
    HTTP_UNPROCESSABLE,
    ErrorEnvelope,
)
from video_agent.api.health import CACHE_DEPENDENCY, DATABASE_DEPENDENCY
from video_agent.api.resources import ResourceFactories, Resources, open_resources
from video_agent.observability.codes import ErrorCode
from video_agent.observability.logging import HANDLER_MARKER, build_handler

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

DOWN: Final = ConnectionRefusedError("connection refused")

STATUS_PROBES: Final[tuple[tuple[int, str, str], ...]] = (
    (400, "GET", "/probe/error/VA-REQ-002"),
    (401, "GET", "/probe/error/VA-AUTH-001"),
    (404, "GET", "/probe/error/VA-REQ-005"),
    (409, "GET", "/probe/error/VA-REQ-004"),
    (429, "GET", "/probe/error/VA-GW-003"),
    (503, "GET", "/probe/error/VA-STORE-003"),
    (500, "GET", "/probe/boom"),
    (404, "GET", "/no-such-route"),
    (405, "POST", "/healthz"),
)
"""One request per status `api.md` §4 names, including the two the router raises by itself."""


@contextmanager
def captured_logs() -> Iterator[list[dict[str, Any]]]:
    """Collect every log line emitted inside the block, parsed.

    A second handler on the root logger rather than a replacement for the application's, so the
    real formatter and the real filters run — including the redaction tripwire. A test that
    captured raw records would prove nothing about what is actually written.

    The marker is cleared because `configure_logging` removes every handler carrying it, so a
    capture installed before an app is built would be torn down by the app's own configuration
    and the test would assert against an empty list.
    """
    stream = io.StringIO()
    handler = build_handler(stream=stream)
    setattr(handler, HANDLER_MARKER, False)
    root = logging.getLogger()
    root.addHandler(handler)
    lines: list[dict[str, Any]] = []
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        lines.extend(json.loads(line) for line in stream.getvalue().splitlines() if line)


# --- Acceptance 1: liveness touches nothing -------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_ignores_dependencies() -> None:
    """Both dependencies refuse connections; `/healthz` is still `200`.

    The point of a liveness probe is that a dependency outage must not make an orchestrator
    restart every replica. Wiring `/healthz` to the database turns one outage into two.
    """
    resources = build_resources(
        database=RecordingDatabase(ping_error=DOWN),
        cache=RecordingProbe(ping_error=DOWN),
    )
    app = build_app(resources=resources)

    async with api_client(app) as client:
        response = await client.get("/healthz")

    assert response.status_code == OK
    assert response.json() == {"status": "alive"}


@pytest.mark.asyncio
async def test_healthz_never_probes_a_dependency() -> None:
    """Neither dependency is asked at all — asserted on the probe counters, not on the status.

    A `/healthz` that probed and swallowed the result would still return `200`, so the status
    alone cannot distinguish "touches nothing" from "touches everything and ignores it". The
    counters can.
    """
    database = RecordingDatabase()
    cache = RecordingProbe()
    app = build_app(resources=build_resources(database=database, cache=cache))

    async with api_client(app) as client:
        response = await client.get("/healthz")

    assert response.status_code == OK
    assert database.pings == 0
    assert cache.pings == 0


# --- Acceptance 2: readiness distinguishes each dependency ----------------------------------


@pytest.mark.parametrize(
    ("database_down", "cache_down", "expected"),
    [
        (True, False, [DATABASE_DEPENDENCY]),
        (False, True, [CACHE_DEPENDENCY]),
        (True, True, [DATABASE_DEPENDENCY, CACHE_DEPENDENCY]),
    ],
    ids=["postgres-down", "redis-down", "both-down"],
)
@pytest.mark.asyncio
async def test_readyz_reports_each_dependency(
    database_down: bool,
    cache_down: bool,
    expected: list[str],
) -> None:
    """Every unreachable dependency yields `503 VA-STORE-003` and is named in `details`."""
    resources = build_resources(
        database=RecordingDatabase(ping_error=DOWN if database_down else None),
        cache=RecordingProbe(ping_error=DOWN if cache_down else None),
    )
    app = build_app(resources=resources)

    async with api_client(app) as client:
        response = await client.get("/readyz")

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.VA_STORE_003.value
    assert envelope.error.retryable is True
    assert envelope.error.details["unavailable"] == expected


@pytest.mark.asyncio
async def test_readyz_is_ready_when_both_dependencies_answer() -> None:
    """The negative case, so a `/readyz` hard-wired to `503` would fail."""
    app = build_app()

    async with api_client(app) as client:
        response = await client.get("/readyz")

    assert response.status_code == OK
    assert response.json() == {
        "status": "ready",
        "checks": {DATABASE_DEPENDENCY: "ok", CACHE_DEPENDENCY: "ok"},
    }


@pytest.mark.asyncio
async def test_readyz_probes_the_cache_even_when_the_database_is_down() -> None:
    """Both are probed, so an operator learns everything that is wrong in one request."""
    cache = RecordingProbe()
    resources = build_resources(database=RecordingDatabase(ping_error=DOWN), cache=cache)
    app = build_app(resources=resources)

    async with api_client(app) as client:
        response = await client.get("/readyz")

    assert response.json()["error"]["details"]["unavailable"] == [DATABASE_DEPENDENCY]
    assert cache.pings == 1


# --- Acceptance 3: one envelope for every non-2xx -------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "method", "path"),
    STATUS_PROBES,
    ids=[f"{status}-{path}" for status, _, path in STATUS_PROBES],
)
@pytest.mark.asyncio
async def test_error_envelope_shape_for_every_status(
    status_code: int,
    method: str,
    path: str,
) -> None:
    """Every non-2xx body validates as `ErrorEnvelope` and carries all six required fields."""
    app = build_app()

    async with api_client(app) as client:
        response = await client.request(method, path)

    assert response.status_code == status_code
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code.startswith("VA-")
    assert envelope.error.message
    assert envelope.error.trace_id
    assert envelope.error.next_steps
    assert isinstance(envelope.error.retryable, bool)
    assert isinstance(envelope.error.preserved, dict)


@pytest.mark.asyncio
async def test_validation_failure_is_422_and_names_the_field_not_the_value() -> None:
    """`VA-REQ-007` lists where the request was wrong, never what was in it.

    Pydantic's error entries include `input` — the rejected value. Echoing that back turns a
    validation error into a disclosure the first time someone posts a credential to the wrong
    field.
    """
    app = build_app()
    rejected = "not-a-uuid-and-also-a-secret"

    async with api_client(app) as client:
        response = await client.post("/probe/validate", json={"tenant_id": rejected})

    assert response.status_code == HTTP_UNPROCESSABLE
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.VA_REQ_007.value
    assert envelope.error.details["fields"][0]["field"].endswith("tenant_id")
    assert rejected not in response.text


@pytest.mark.asyncio
async def test_bare_framework_404_is_not_a_detail_dict() -> None:
    """The router's own `404` goes through the envelope too.

    Starlette raises its own `HTTPException`, not FastAPI's. Registering the subclass leaves
    this path rendering `{"detail": "Not Found"}` — which is exactly what it did before the
    handler was registered against the base class.
    """
    app = build_app()

    async with api_client(app) as client:
        response = await client.get("/no-such-route")

    assert "detail" not in response.json()
    assert response.json()["error"]["code"] == ErrorCode.VA_REQ_005.value


@pytest.mark.asyncio
async def test_every_response_carries_the_trace_header() -> None:
    """Success as well as failure: a caller reporting a bad video has only a `200` to quote."""
    app = build_app()

    async with api_client(app) as client:
        healthy = await client.get("/healthz")
        failed = await client.get("/probe/error/VA-REQ-005")

    assert healthy.headers["X-Trace-Id"]
    assert failed.headers["X-Trace-Id"] == failed.json()["error"]["trace_id"]


# --- Acceptance 4: an unhandled exception leaks nothing --------------------------------------


@pytest.mark.asyncio
async def test_unhandled_exception_leaks_nothing() -> None:
    """`500 VA-INT-001`, a generic message, and the detail only in the log."""
    app = build_app()

    with captured_logs() as lines:
        async with api_client(app) as client:
            response = await client.get("/probe/boom")

    assert response.status_code == HTTP_INTERNAL_ERROR
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.VA_INT_001.value
    assert envelope.error.message == ErrorCode.VA_INT_001.meaning
    assert PLANTED_DETAIL not in response.text
    assert "Traceback" not in response.text
    assert "ValueError" not in response.text

    failures = [line for line in lines if line.get("code") == ErrorCode.VA_INT_001.value]
    assert failures, "the failure must be logged under its code"
    assert any(PLANTED_DETAIL in str(line.get("reason")) for line in failures)
    assert any(line.get("exc_type") == "ValueError" for line in failures)


@pytest.mark.asyncio
async def test_the_envelope_message_never_comes_from_the_exception() -> None:
    """Two different internal failures produce the identical public sentence.

    This is the property that makes a leak structurally impossible rather than merely absent:
    there is no path from an exception's text to a response body.
    """
    app = build_app()

    async with api_client(app) as client:
        first = await client.get("/probe/boom")
        second = await client.get("/probe/error/VA-INT-001")

    assert first.json()["error"]["message"] == second.json()["error"]["message"]


# --- Acceptance 5: shutdown closes everything that opened ------------------------------------


@pytest.mark.asyncio
async def test_lifespan_closes_pools_on_partial_startup_failure() -> None:
    """The cache refuses to open; the database pool that already opened is still closed."""
    database = RecordingDatabase()
    failing_cache = FailingFactory(DOWN)
    store = RecordingProbe()

    async def open_database() -> RecordingDatabase:
        return database

    async def open_store() -> RecordingProbe:
        return store

    resources = Resources(
        ResourceFactories(
            database=open_database,
            cache=failing_cache,
            object_store=open_store,
        )
    )

    with pytest.raises(ConnectionRefusedError):
        async with open_resources(resources):
            pytest.fail("the body must not run when startup failed")

    assert failing_cache.calls == 1
    assert database.closed is True
    assert resources.closed_names == ("database",)
    assert store.closed is False


@pytest.mark.asyncio
async def test_lifespan_closes_all_three_on_a_clean_shutdown() -> None:
    """The ordinary path, so the partial-failure test is not the only thing keeping it honest."""
    database = RecordingDatabase()
    cache = RecordingProbe()
    resources = build_resources(database=database, cache=cache)

    async with open_resources(resources):
        assert resources.open_names == ("database", "cache", "object_store")

    assert database.closed is True
    assert cache.closed is True
    assert resources.closed_names == ("object_store", "cache", "database")


@pytest.mark.asyncio
async def test_a_failing_close_does_not_abort_the_rest() -> None:
    """One pool that cannot close must not strand the other two open."""
    database = RecordingDatabase()
    cache = RecordingProbe(close_error=OSError("socket already gone"))
    resources = build_resources(database=database, cache=cache)

    await resources.open()
    await resources.close()

    assert cache.closed is True
    assert database.closed is True
    assert resources.closed_names == ("object_store", "cache", "database")


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    """Shutdown may be reached twice; the second pass must not raise or double-count."""
    resources = build_resources()

    await resources.open()
    await resources.close()
    await resources.close()

    assert resources.closed_names == ("object_store", "cache", "database")


# --- The factory itself ----------------------------------------------------------------------


def test_create_app_configures_logging_before_serving_anything() -> None:
    """`configure_logging` runs inside `create_app`, not somewhere a deployment must remember.

    Asserted by observing the root logger: before `T0.4` nothing called `configure_logging`, so
    a production process logged unstructured and, more to the point, **unredacted** lines.
    """
    logging.getLogger().handlers.clear()

    build_app()

    handlers = logging.getLogger().handlers
    assert any(getattr(handler, "_video_agent_json_handler", False) for handler in handlers)
