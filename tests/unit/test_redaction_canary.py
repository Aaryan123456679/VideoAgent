"""The redaction canary — `observability.md` §8, `[D-54]`. Any leak blocks the build.

`§5` names three enforcement mechanisms and this file is the third: *a CI test replays a full
synthetic job and greps all captured logs for planted secrets and PII.* The unit tests in
`test_redaction.py` check each rule against the function that implements it. This one checks
the **pipeline** — logger, filters, formatter, sink — because a rule that is correct and not
wired in protects nothing, and every real leak in the history of this class of bug happened on
a path somebody forgot existed.

Two properties make it a canary rather than a ritual:

- Every planted value is searched for in the **raw bytes** of the sink, not in a parsed field.
  A secret that survives inside a field nobody thought to check still fails.
- `test_the_canary_can_actually_detect_a_leak` renders the *same* payloads without redaction
  and asserts the search finds every planted value. Without it, a search that silently matched
  nothing would report a clean build forever.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from video_agent.observability.context import bind_span, bind_trace
from video_agent.observability.logging import build_handler, get_logger
from video_agent.observability.redaction import (
    PROMPT_PREVIEW_CHARS,
    REDACTION_TRIPWIRE_ALARM,
    RedactionTripwireError,
    TripwireMode,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x11" * 32
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x22" * 64

PLANTED: dict[str, str] = {
    "provider_api_key": "sk-proj-Kx7mQ2nR9vT4wY6zA0bC3dE5fG8hJ1kL4mN7pQ0r",
    "gateway_master_key": "sk-Lm4nP7qR2sT5uV8wX1yZ4aB7cD0eF3gH6iJ9kL2m",
    "webhook_signing_secret": "whsec_T5uV8wX1yZ4aB7cD0eF3gH6iJ9kL2mN5pQ8rS1t",
    "object_store_secret": "aB3cD6eF9gH2iJ5kL8mN1pQ4rS7tU0vW3xY6zA9bC2d",
    "database_url": "postgresql+asyncpg://videoagent:Hunter2Hunter2@db.internal:5432/videoagent",
    "presigned_download": (
        "https://artifacts.example.com/t/j/final.mp4"
        "?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20260808%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Signature=1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"
    ),
    "provider_upload_url": (
        "https://videos.example.com/uploads/j-9f2c.mp4?token=Zx9Cv2Bn5Mq8Wr1Ty4Ui7Op0As3Df6G"
    ),
    "provider_download_url": (
        "https://videos.example.com/renders/j-9f2c.mp4?Expires=1786000000&Signature=Qw3Er5Ty7Ui9"
    ),
    "user_email": "priya.raghunathan@example.com",
    "user_phone": "+44 7700 900461",
}
"""Everything `AGENT.md` §3 forbids, one of each, in the shapes they really arrive in."""

PLANTED_MEDIA: dict[str, str] = {
    "png_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
    "mp4_base64": base64.b64encode(MP4_BYTES).decode("ascii"),
    "png_data_uri": f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode('ascii')}",
}

PROMPT_PREFACE = "A moody short film that ends at sunrise over a quiet harbour town, please."
"""Deliberately longer than `PROMPT_PREVIEW_CHARS` and deliberately innocuous.

`observability.md` §5 permits the first 64 characters of the prompt to be emitted, so anything
planted inside that window would be a *specified* disclosure rather than a leak. Pushing the
planted PII past the window is what makes the assertions below about the rule and not about
where the truncation happens to land."""

PLANTED_PROMPT = (
    f"{PROMPT_PREFACE} The lead is {PLANTED['user_email']}, reachable on "
    f"{PLANTED['user_phone']}, account key {PLANTED['provider_api_key']}."
)
"""The user prompt is PII by assumption `[§5]`, and users paste secrets into prompts."""

PLANTED_QUERY_ROWS = [
    {"id": 1, "email": PLANTED["user_email"], "prompt": PLANTED_PROMPT},
    {"id": 2, "email": "second.person@example.com", "prompt": "another private prompt"},
]

EXPECTED_JOB_LINES = 13
MINIMUM_TRIPWIRE_HITS = 8
"""The synthetic job plants more than this many forbidden values. An exact count would bind
the test to the shape of the job rather than to the rule it is checking."""


def _synthetic_job(log: logging.Logger) -> None:
    """One job's worth of log lines, every one of them carrying something forbidden.

    Written the way a careless but not malicious engineer writes: values interpolated into
    messages, secrets passed as `extra=` fields, a `SecretStr` handed over unwrapped, query
    rows logged "just for debugging". Every line here is a mistake somebody makes.
    """
    with bind_trace("trace-canary", job_id="job-canary", tenant_id="tenant-canary"):
        log.info("job accepted", extra={"prompt": PLANTED_PROMPT})
        log.info("resolved credentials", extra={"api_key": PLANTED["provider_api_key"]})
        log.info("gateway ready with key %s", PLANTED["gateway_master_key"])
        log.info("connected to %s", PLANTED["database_url"])
        log.debug("webhook secret", extra={"node": SecretStr(PLANTED["webhook_signing_secret"])})
        log.info("object store ready", extra={"reason": PLANTED["object_store_secret"]})

        with bind_span(node="generate_shot"):
            log.info("upload target %s", PLANTED["provider_upload_url"])
            log.info(
                "render finished",
                extra={"storage_key": PLANTED["provider_download_url"], "shot_index": 0},
            )
            log.debug("first frame", extra={"reason": PLANTED_MEDIA["png_base64"]})
            log.debug("clip bytes", extra={"reason": PLANTED_MEDIA["png_data_uri"]})

        with bind_span(node="assemble"):
            log.info("stitched %s", PLANTED_MEDIA["mp4_base64"])

        with bind_span(node="deliver"):
            log.info("manifest ready: %s", PLANTED["presigned_download"])
            log.warning(
                "audit rows",
                extra={"statement_id": "select_job_rows", "rows": PLANTED_QUERY_ROWS},
            )


@pytest.fixture(autouse=True)
def _reset_alarm() -> None:
    REDACTION_TRIPWIRE_ALARM.reset()


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Run the synthetic job into a private sink, in production mode so it completes.

    Production mode, because the question this file asks is *what reached the sink*. In CI
    mode the first offending line raises and there is nothing left to grep — that behaviour
    has its own test below.
    """
    stream = io.StringIO()
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    root.handlers = [build_handler(stream=stream, mode=TripwireMode.DROP)]
    root.setLevel(logging.DEBUG)
    try:
        _synthetic_job(get_logger("video_agent.canary"))
        yield stream
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


# --- The canary --------------------------------------------------------------------------


def test_the_synthetic_job_produced_lines(captured: io.StringIO) -> None:
    """Guards every assertion below: an empty sink would pass all of them."""
    lines = [line for line in captured.getvalue().splitlines() if line]

    assert len(lines) == EXPECTED_JOB_LINES
    assert all(json.loads(line)["trace_id"] == "trace-canary" for line in lines)


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_no_planted_credential_or_pii_reaches_the_sink(captured: io.StringIO, name: str) -> None:
    """`[D-54]` — any leak blocks the build."""
    assert PLANTED[name] not in captured.getvalue(), f"{name} leaked into the log sink"


@pytest.mark.parametrize("name", sorted(PLANTED_MEDIA))
def test_no_planted_media_payload_reaches_the_sink(captured: io.StringIO, name: str) -> None:
    assert PLANTED_MEDIA[name] not in captured.getvalue(), f"{name} leaked into the log sink"


def test_no_query_row_reaches_the_sink(captured: io.StringIO) -> None:
    """`[§5]` — statement identity and row count, never the rows."""
    text = captured.getvalue()

    assert "second.person@example.com" not in text
    assert "another private prompt" not in text
    assert "select_job_rows" in text


def test_the_full_prompt_never_reaches_the_sink(captured: io.StringIO) -> None:
    """`[§5]` — `prompt_sha256` plus the first 64 characters, never the full text."""
    text = captured.getvalue()

    assert PLANTED_PROMPT not in text
    assert PLANTED["user_email"] not in text
    assert PLANTED["user_phone"] not in text
    assert '"prompt_sha256"' in text


def test_the_prompt_preview_is_exactly_the_permitted_window(captured: io.StringIO) -> None:
    """The specified disclosure, asserted so that widening it silently is impossible."""
    previews = [
        line["prompt_preview"]
        for line in (json.loads(raw) for raw in captured.getvalue().splitlines() if raw)
        if line.get("prompt_preview")
    ]

    assert len(PROMPT_PREFACE) > PROMPT_PREVIEW_CHARS
    assert previews == [PLANTED_PROMPT[:PROMPT_PREVIEW_CHARS]]


def test_the_secret_wrapper_leaks_neither_its_value_nor_its_length(
    captured: io.StringIO,
) -> None:
    text = captured.getvalue()

    assert PLANTED["webhook_signing_secret"] not in text
    assert "**********" not in text


def test_every_surviving_line_is_still_usable(captured: io.StringIO) -> None:
    """Redaction that emptied every line would pass every test above and help nobody."""
    lines = [json.loads(line) for line in captured.getvalue().splitlines() if line]

    assert all(line["trace_id"] for line in lines)
    assert {line["node"] for line in lines} >= {"generate_shot", "assemble", "deliver"}
    assert any(line.get("prompt_sha256") for line in lines)


def test_the_tripwire_counted_every_planted_value(captured: io.StringIO) -> None:
    """Dropping silently is fail-safe; dropping *and counting* is what makes it visible."""
    assert captured.getvalue()
    assert REDACTION_TRIPWIRE_ALARM.count >= MINIMUM_TRIPWIRE_HITS


# --- The canary can fail -----------------------------------------------------------------------


def test_the_canary_can_actually_detect_a_leak() -> None:
    """The control. The same search, against the same payloads, with redaction removed.

    Without this, a search that matched nothing would report a clean build forever — which is
    the exact failure mode the review of `T0.1` found in tests that could not fail.
    """
    unredacted = json.dumps(
        {
            "prompt": PLANTED_PROMPT,
            "rows": PLANTED_QUERY_ROWS,
            **PLANTED,
            **PLANTED_MEDIA,
        }
    )

    leaked = [name for name, value in PLANTED.items() if value in unredacted]
    leaked_media = [name for name, value in PLANTED_MEDIA.items() if value in unredacted]

    assert sorted(leaked) == sorted(PLANTED)
    assert sorted(leaked_media) == sorted(PLANTED_MEDIA)


def test_the_canary_stops_the_build_in_ci_mode() -> None:
    """`observability.md` §5: a hit raises in dev and CI. `S0.3.3` acceptance 5."""
    stream = io.StringIO()
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    root.handlers = [build_handler(stream=stream, mode=TripwireMode.RAISE)]
    root.setLevel(logging.DEBUG)
    try:
        with pytest.raises(RedactionTripwireError):
            _synthetic_job(get_logger("video_agent.canary"))
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
