"""`S0.3.2` — every line is JSON, every line carries a `trace_id`, errors are never sampled.

The tests read the bytes the handler actually wrote. Nothing here asserts against the record
object or a formatter return value in isolation: the promise in `[CPS §Observability]` is about
what lands in the log sink, and a formatter that is correct but not wired in keeps that promise
to nobody.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Callable, Iterator

import pytest

from video_agent.config.settings import Settings
from video_agent.observability.context import (
    SYNTHESISED_TRACE_PREFIX,
    bind_span,
    bind_trace,
    clear_trace,
    current_job_id,
    current_node,
    current_span_id,
    current_tenant_id,
    current_trace_id,
    is_degraded,
)
from video_agent.observability.logging import (
    MISSING_TRACE_ID_ALARM,
    SCHEMA_KEYS,
    JsonFormatter,
    TraceSampler,
    build_handler,
    configure_logging,
    get_logger,
)
from video_agent.observability.redaction import REDACTION_TRIPWIRE_ALARM, TripwireMode

BASELINE_ENV: dict[str, str] = {
    "MAGICHOUR_API_KEY": "",
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/videoagent",
    "REDIS_URL": "redis://localhost:6379/0",
}

LEVELS = (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)
LINES_TO_CAPTURE = 50
SAMPLED_POPULATION = 1000
UNSAMPLED_POPULATION = 20
LINES_PER_TRACE = 3
EXPECTED_HANDLER_COUNT = 1
SHOT_INDEX = 2
ONE_PERCENT = 0.01
"""A rate low enough that "sampling did nothing" and "sampling worked" cannot be confused."""

MAXIMUM_SAMPLED_SURVIVORS = 100
"""Ten times the expected survivors at 1%. Wide enough not to be flaky, narrow enough that a
sampler which passed everything through would fail it by an order of magnitude."""


class Sink:
    """A stream plus the parsed lines written to it."""

    def __init__(self) -> None:
        self.stream = io.StringIO()

    @property
    def raw(self) -> list[str]:
        return [line for line in self.stream.getvalue().splitlines() if line]

    @property
    def lines(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.raw]


@pytest.fixture(autouse=True)
def _reset_alarms() -> None:
    MISSING_TRACE_ID_ALARM.reset()
    REDACTION_TRIPWIRE_ALARM.reset()


@pytest.fixture
def isolated_root() -> Iterator[logging.Logger]:
    """Give the test the root logger to itself and hand it back untouched."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


@pytest.fixture
def sink(isolated_root: logging.Logger) -> Callable[..., Sink]:
    """Install a JSON handler on the root logger and return the sink it writes to."""

    def install(
        *,
        level: int = logging.DEBUG,
        sample_rate: float = 1.0,
        mode: TripwireMode = TripwireMode.RAISE,
    ) -> Sink:
        captured = Sink()
        isolated_root.addHandler(
            build_handler(stream=captured.stream, mode=mode, sample_rate=sample_rate)
        )
        isolated_root.setLevel(level)
        return captured

    return install


@pytest.fixture
def settings(
    monkeypatch: pytest.MonkeyPatch, env_example: dict[str, str]
) -> Callable[..., Settings]:
    """Build `Settings` from a known environment rather than the developer's own."""
    for name in env_example:
        monkeypatch.delenv(name, raising=False)

    def build(**overrides: str) -> Settings:
        for name, value in {**BASELINE_ENV, **overrides}.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)

    return build


# --- Every line is JSON, and carries a trace_id ------------------------------------------------


def test_every_line_is_json_with_a_trace_id(sink: Callable[..., Sink]) -> None:
    """`S0.3.2` acceptance 1 — fifty lines across every level."""
    captured = sink()
    log = get_logger("video_agent.test")

    with bind_trace("trace-abc", job_id="job-1", tenant_id="tenant-1"):
        for index in range(LINES_TO_CAPTURE):
            log.log(LEVELS[index % len(LEVELS)], "line %d", index)

    assert len(captured.raw) == LINES_TO_CAPTURE
    assert all(line["trace_id"] == "trace-abc" for line in captured.lines)


def test_every_line_carries_the_whole_schema(sink: Callable[..., Sink]) -> None:
    """`observability.md` §4. A key that appears only sometimes forces two shapes on readers."""
    captured = sink()

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").info("hello")

    assert set(SCHEMA_KEYS) <= set(captured.lines[0])


def test_a_line_is_one_object_on_one_physical_line(sink: Callable[..., Sink]) -> None:
    """Line-delimited JSON: a pretty-printed object would break every downstream parser."""
    captured = sink()

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").info("hello")

    assert len(captured.raw) == 1
    assert "\n" not in captured.raw[0]


def test_timestamps_are_utc_with_a_z_suffix(sink: Callable[..., Sink]) -> None:
    captured = sink()

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").info("hello")

    assert str(captured.lines[0]["ts"]).endswith("Z")


# --- trace_id comes from the context variable --------------------------------------------------


def test_the_trace_id_comes_from_the_contextvar(sink: Callable[..., Sink]) -> None:
    """`S0.3.2` acceptance — no module passes it by hand, so no module can forget to."""
    captured = sink()
    log = get_logger("video_agent.test")

    with bind_trace("trace-outer", job_id="job-9", tenant_id="tenant-9"), bind_span(node="plan"):
        log.info("inside")

    line = captured.lines[0]
    assert line["trace_id"] == "trace-outer"
    assert line["job_id"] == "job-9"
    assert line["tenant_id"] == "tenant-9"
    assert line["node"] == "plan"


def test_leaving_the_block_unbinds_the_context(sink: Callable[..., Sink]) -> None:
    captured = sink()
    log = get_logger("video_agent.test")

    with bind_trace("trace-outer", job_id="job-9"):
        log.warning("inside")
    with clear_trace():
        log.warning("outside")

    inside, outside = captured.lines
    assert inside["job_id"] == "job-9"
    assert outside["job_id"] is None
    assert outside["trace_id"] != "trace-outer"


def test_a_nested_binding_restores_the_outer_one(sink: Callable[..., Sink]) -> None:
    """Tokens, not remembered values: an inner block must not flatten the outer context."""
    captured = sink()
    log = get_logger("video_agent.test")

    with bind_trace("trace-outer", job_id="job-9"):
        with bind_span(node="inner"):
            log.warning("nested")
        log.warning("back out")

    nested, back_out = captured.lines
    assert nested["node"] == "inner"
    assert back_out["node"] is None
    assert back_out["job_id"] == "job-9"


def test_bind_trace_mints_an_id_when_none_is_bound() -> None:
    with clear_trace(), bind_trace() as trace_id:
        assert trace_id
        assert not trace_id.startswith(SYNTHESISED_TRACE_PREFIX)


# --- The missing-trace alarm -------------------------------------------------------------------


def test_a_missing_trace_is_synthesised_and_alarmed(sink: Callable[..., Sink]) -> None:
    """`S0.3.2` acceptance 2 — the key is never absent, and the gap is counted."""
    captured = sink()

    with clear_trace():
        get_logger("video_agent.test").error("orphaned line")

    line = captured.lines[0]
    assert "trace_id" in line
    assert str(line["trace_id"]).startswith(SYNTHESISED_TRACE_PREFIX)
    assert line["trace_synthesised"] is True
    assert MISSING_TRACE_ID_ALARM.count == 1


def test_a_bound_trace_does_not_raise_the_alarm(sink: Callable[..., Sink]) -> None:
    sink()

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").error("fine")

    assert MISSING_TRACE_ID_ALARM.count == 0


def test_a_synthesised_id_is_visibly_synthetic(sink: Callable[..., Sink]) -> None:
    """Someone pasting the id into a trace search should learn why nothing comes back."""
    captured = sink()

    with clear_trace():
        get_logger("video_agent.test").error("orphaned")

    assert SYNTHESISED_TRACE_PREFIX in str(captured.lines[0]["trace_id"])


# --- Sampling ----------------------------------------------------------------------------------


def test_errors_are_never_sampled(sink: Callable[..., Sink]) -> None:
    """`S0.3.2` acceptance 4 — 1% sampling, a thousand errors, a thousand lines."""
    captured = sink(sample_rate=ONE_PERCENT)
    log = get_logger("video_agent.test")

    for index in range(SAMPLED_POPULATION):
        with bind_trace(f"trace-{index:05d}"):
            log.error("failure %d", index)

    assert len(captured.raw) == SAMPLED_POPULATION


def test_warnings_are_never_sampled(sink: Callable[..., Sink]) -> None:
    """`observability.md` §10 samples `debug`/`info`; anything actionable passes through."""
    captured = sink(sample_rate=ONE_PERCENT)
    log = get_logger("video_agent.test")

    for index in range(SAMPLED_POPULATION):
        with bind_trace(f"trace-{index:05d}"):
            log.warning("degraded %d", index)

    assert len(captured.raw) == SAMPLED_POPULATION


def test_low_severity_records_really_are_sampled(sink: Callable[..., Sink]) -> None:
    """The other half of the previous test: without this, "never sampled" proves nothing."""
    captured = sink(sample_rate=ONE_PERCENT)
    log = get_logger("video_agent.test")

    for index in range(SAMPLED_POPULATION):
        with bind_trace(f"trace-{index:05d}"):
            log.info("chatter %d", index)

    assert 0 < len(captured.raw) < MAXIMUM_SAMPLED_SURVIVORS


def test_a_sampled_trace_keeps_all_of_its_lines(sink: Callable[..., Sink]) -> None:
    """Half a trace is worse than none, because it looks complete.

    The decision is a function of the `trace_id` alone, so ten lines on one trace are ten
    lines or zero, never four.
    """
    captured = sink(sample_rate=ONE_PERCENT)
    log = get_logger("video_agent.test")
    survivors: set[str] = set()

    for index in range(SAMPLED_POPULATION):
        trace_id = f"trace-{index:05d}"
        with bind_trace(trace_id):
            for _ in range(LINES_PER_TRACE):
                log.info("chatter")
        if any(line["trace_id"] == trace_id for line in captured.lines):
            survivors.add(trace_id)

    counts = {
        trace_id: sum(1 for line in captured.lines if line["trace_id"] == trace_id)
        for trace_id in survivors
    }
    assert survivors
    assert set(counts.values()) == {LINES_PER_TRACE}


def test_sampling_is_deterministic(sink: Callable[..., Sink]) -> None:
    """Same trace, same decision — otherwise a retry of the same job logs differently."""
    first = sink(sample_rate=ONE_PERCENT)
    log = get_logger("video_agent.test")
    for index in range(SAMPLED_POPULATION):
        with bind_trace(f"trace-{index:05d}"):
            log.info("chatter")
    surviving = [line["trace_id"] for line in first.lines]

    second = sink(sample_rate=ONE_PERCENT)
    for index in range(SAMPLED_POPULATION):
        with bind_trace(f"trace-{index:05d}"):
            log.info("chatter")

    assert [line["trace_id"] for line in second.lines] == surviving


def test_a_sample_rate_of_one_keeps_everything(sink: Callable[..., Sink]) -> None:
    captured = sink(sample_rate=1.0)
    log = get_logger("video_agent.test")

    for index in range(UNSAMPLED_POPULATION):
        with bind_trace(f"trace-{index:05d}"):
            log.debug("chatter")

    assert len(captured.raw) == UNSAMPLED_POPULATION


@pytest.mark.parametrize("rate", [-0.1, 1.1, 2.0])
def test_a_sample_rate_outside_zero_to_one_is_rejected(rate: float) -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        TraceSampler(sample_rate=rate)


# --- Settings ----------------------------------------------------------------------------------


@pytest.mark.usefixtures("isolated_root")
def test_the_log_level_comes_from_settings(settings: Callable[..., Settings]) -> None:
    """`S0.3.2` acceptance 5 — `LOG_LEVEL=WARNING` suppresses info lines."""
    stream = io.StringIO()
    configure_logging(settings(LOG_LEVEL="WARNING"), stream=stream)
    log = get_logger("video_agent.test")

    with bind_trace("trace-abc"):
        log.info("should not appear")
        log.warning("should appear")

    emitted = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    assert [line["level"] for line in emitted] == ["warning"]


@pytest.mark.usefixtures("isolated_root")
def test_a_debug_level_admits_debug_lines(settings: Callable[..., Settings]) -> None:
    stream = io.StringIO()
    configure_logging(settings(LOG_LEVEL="DEBUG"), stream=stream)

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").debug("visible")

    assert '"level":"debug"' in stream.getvalue()


@pytest.mark.usefixtures("isolated_root")
def test_configuring_twice_does_not_duplicate_lines(settings: Callable[..., Settings]) -> None:
    """A doubled handler doubles every line, which corrupts every count computed from logs."""
    stream = io.StringIO()
    configure_logging(settings(), stream=stream)
    configure_logging(settings(), stream=stream)

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").warning("once")

    assert len([line for line in stream.getvalue().splitlines() if line]) == EXPECTED_HANDLER_COUNT


@pytest.mark.usefixtures("isolated_root")
def test_a_production_environment_gets_the_dropping_tripwire(
    settings: Callable[..., Settings],
) -> None:
    """`[D-57]` — telemetry never takes the product down. `S0.3.3` acceptance 5, end to end."""
    stream = io.StringIO()
    configure_logging(settings(ENV="production"), stream=stream)

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").warning("token=Qm5xR8vT2wY6zA0bC4dE7fH1jK3lM9nP")

    assert REDACTION_TRIPWIRE_ALARM.count == 1
    assert "Qm5xR8vT2wY6zA0bC4dE7fH1jK3lM9nP" not in stream.getvalue()


# --- Structured fields -------------------------------------------------------------------------


def test_allow_listed_extras_reach_the_line(sink: Callable[..., Sink]) -> None:
    captured = sink()

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").info(
            "shot accepted",
            extra={"shot_index": SHOT_INDEX, "attempt_no": 1, "score": 0.83},
        )

    line = captured.lines[0]
    assert line["shot_index"] == SHOT_INDEX
    assert line["attempt_no"] == 1
    assert line["score"] == pytest.approx(0.83)


def test_an_unlisted_extra_is_dropped_from_the_line(sink: Callable[..., Sink]) -> None:
    captured = sink()

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").info("hello", extra={"invented_field": "value"})

    assert "invented_field" not in captured.lines[0]


def test_the_error_code_is_carried_on_the_line(sink: Callable[..., Sink]) -> None:
    """`[CPS §Failure behaviour]` — the code and the trace on the same line, or neither helps."""
    captured = sink()

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").error("provider said no", extra={"code": "VA-PROV-009"})

    line = captured.lines[0]
    assert line["code"] == "VA-PROV-009"
    assert line["trace_id"] == "trace-abc"


def test_degradation_is_carried_from_the_context(sink: Callable[..., Sink]) -> None:
    captured = sink()

    with bind_trace("trace-abc"), bind_span(node="qc_shot", degraded=True):
        get_logger("video_agent.test").warning("provisional accept")

    assert captured.lines[0]["degraded"] is True


def test_an_exception_is_recorded_by_type_and_never_by_traceback(sink: Callable[..., Sink]) -> None:
    """`AGENT.md` §3 — no stack traces. A traceback renders arguments, and arguments carry PII."""
    captured = sink()

    with bind_trace("trace-abc"):
        try:
            raise ValueError("a secret-bearing detail")
        except ValueError:
            get_logger("video_agent.test").exception("node failed")

    line = captured.lines[0]
    assert line["exc_type"] == "ValueError"
    assert "Traceback" not in json.dumps(line)
    assert "a secret-bearing detail" not in json.dumps(line)


def test_the_formatter_never_calls_format_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """The omission above is a decision, and this is what makes it one.

    `logging.Formatter.format` appends `formatException(record.exc_info)` to the message.
    `JsonFormatter` does not call it at all, so the property holds by construction rather than
    because no test has planted a long enough traceback yet. Asserted by making the method
    raise: if the formatter ever reaches it, this fails loudly instead of quietly emitting a
    stack.

    Driven against the formatter directly rather than through a handler, and with `exc_text`
    explicitly cleared. `Formatter.format` skips `formatException` when `exc_text` is already
    populated, and pytest's own logging plugin populates it on every record it sees — so a
    version of this test that logged through `get_logger` passed whether the formatter
    delegated or not. A test that cannot fail is the defect this whole review is about.
    """

    def refuse(*_unused: object) -> str:
        message = "formatException must never be reached"
        raise AssertionError(message)

    monkeypatch.setattr(JsonFormatter, "formatException", refuse)

    try:
        raise ValueError("a secret-bearing detail")
    except ValueError:
        record = logging.LogRecord(
            name="video_agent.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="node failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    record.exc_text = None
    record.trace_id = "trace-abc"

    line = json.loads(JsonFormatter().format(record))

    assert line["exc_type"] == "ValueError"
    assert "Traceback" not in json.dumps(line)
    assert "a secret-bearing detail" not in json.dumps(line)


def test_an_exception_message_reaches_the_log_only_through_reason(
    sink: Callable[..., Sink],
) -> None:
    """The sanctioned, opt-in route, and it goes through redaction like everything else.

    The capability the caller actually wants — *what did the other end say* — is available;
    what is not available is getting it by accident. `reason` is an allow-listed `TEXT` field,
    so it is scanned and truncated, and a caller who chose to include a message that turns out
    to carry a credential loses the field rather than publishing it.
    """
    captured = sink()

    with bind_trace("trace-abc"):
        try:
            raise ValueError("upstream returned 503 after 3 attempts")
        except ValueError as exc:
            get_logger("video_agent.test").error(
                "node failed",
                exc_info=exc,
                extra={"reason": f"{type(exc).__name__}: {exc}"},
            )

    line = captured.lines[0]
    assert line["exc_type"] == "ValueError"
    assert line["reason"] == "ValueError: upstream returned 503 after 3 attempts"


def test_a_reason_carrying_a_credential_is_dropped_and_the_line_survives(
    sink: Callable[..., Sink],
) -> None:
    """The other half: opting in does not opt out of the never-logged list."""
    captured = sink(mode=TripwireMode.DROP)
    presigned = (
        "https://artifacts.example.com/t/j/shot-0.mp4?X-Amz-Signature="
        "8f4b2c1d9e7a6f5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b"
    )

    with bind_trace("trace-abc"):
        get_logger("video_agent.test").warning(
            "download failed", extra={"reason": f"GET <{presigned}> returned 403"}
        )

    line = captured.lines[0]
    assert line.get("reason") is None
    assert "X-Amz-Signature" not in json.dumps(line)
    assert line["msg"] == "download failed"


# --- The individual context accessors ------------------------------------------------------


def test_each_accessor_reads_its_own_variable() -> None:
    """One accessor per field, so a caller reads what it needs without a dict lookup."""
    with (
        bind_trace("trace-abc", job_id="job-1", tenant_id="tenant-1"),
        bind_span(span_id="span-1", node="plan_story", degraded=True),
    ):
        assert current_trace_id() == "trace-abc"
        assert current_span_id() == "span-1"
        assert current_job_id() == "job-1"
        assert current_tenant_id() == "tenant-1"
        assert current_node() == "plan_story"
        assert is_degraded() is True


def test_the_accessors_report_absence_outside_a_trace() -> None:
    with clear_trace():
        assert current_trace_id() is None
        assert current_span_id() is None
        assert current_job_id() is None
        assert current_tenant_id() is None
        assert current_node() is None
        assert is_degraded() is False


def test_binding_a_span_without_an_id_leaves_it_unset() -> None:
    """`T4.1` issues span ids; inventing one locally would join to nothing."""
    with bind_trace("trace-abc"), bind_span(node="plan_story"):
        assert current_span_id() is None
        assert current_node() == "plan_story"
