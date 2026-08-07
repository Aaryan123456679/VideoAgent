"""JSON logs, one object per line, `trace_id` on every one of them.

`[CPS §Observability]` and `observability.md` §4 ask for exactly one thing and it is worth
being literal about why: a log line that cannot be joined to its trace is a log line that
answers "what happened" without ever answering "to which job, for which tenant, at which
step". `trace_id` is what makes the join possible, so the design treats a missing one as a
defect to be counted, not a field to be omitted.

Three filters sit in front of the formatter, in an order that is itself a decision:

1. `TraceContextFilter` copies the ambient context onto the record and, if nothing is bound,
   **synthesises** an id and increments `MISSING_TRACE_ID_ALARM`. `observability.md` §10:
   *in production, synthesise and alarm.* Dropping the line would lose the event; leaving the
   key absent would break every query that groups by it.
2. `TripwireFilter` runs the redaction scan. It sits in a *filter* rather than in the
   formatter for a mechanical reason: `logging.Handler.emit` swallows exceptions and reports
   them to `stderr`, so a tripwire that raised during formatting would print a warning and
   carry on. Filters run outside that `try`, so the exception propagates out of the
   `logger.info(...)` call itself, the test fails, and the build stops — which is the entire
   point of a canary.
3. `TraceSampler` drops a fraction of low-severity records under load. `observability.md` §10:
   *sample `debug`/`info`; never sample errors and scores.* Errors bypass it unconditionally,
   and the sampling decision is derived from `trace_id`, so a sampled trace keeps **all** of
   its lines rather than a random half — half a trace is worse than none, because it looks
   complete.

The formatter then serialises through `redact()` in drop mode. By the time a record reaches
the formatter, raising is useless, so the two modes divide the work: the filter is the gate
that fails the build, the formatter is the serialiser that fails safe.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, TextIO

from video_agent.observability.alarms import AlarmCounter
from video_agent.observability.context import (
    current_context,
    is_synthesised,
    synthesised_trace_id,
)
from video_agent.observability.redaction import (
    TripwireMode,
    enforce,
    sanitise,
    scan_payload,
    tripwire_mode_for_env,
)

if TYPE_CHECKING:  # pragma: no cover - import for typing only, no runtime dependency
    from video_agent.config.settings import Settings

MISSING_TRACE_ID_ALARM: Final[AlarmCounter] = AlarmCounter("log_line_missing_trace_id")
"""Counts lines logged outside any trace. `observability.md` §10 classifies this as an error
condition: the line cannot join its trace, which defeats the model. Non-zero means a code path
is emitting outside `bind_trace`."""

SCHEMA_KEYS: Final[tuple[str, ...]] = (
    "ts",
    "level",
    "msg",
    "logger",
    "trace_id",
    "span_id",
    "job_id",
    "tenant_id",
    "node",
    "code",
    "degraded",
)
"""The keys `observability.md` §4 puts on every line, in the order it lists them.

Always present, even when the value is `null`. A key that appears only sometimes forces every
consumer to handle both shapes, and the first one that forgets reads a missing `code` as a
success."""

HANDLER_MARKER: Final = "_video_agent_json_handler"
"""Marks the handler this module installs, so re-configuring replaces rather than duplicates."""

SAMPLING_RESOLUTION: Final = 10_000
"""Sampling granularity. One basis point, which is finer than any rate worth configuring."""

NEVER_SAMPLED_LEVEL: Final = logging.WARNING
"""At and above this level nothing is ever dropped `[observability.md §10]`."""

_RESERVED_RECORD_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)
"""Attributes the logging module puts on every record. Anything else came from `extra=`."""


def _timestamp(created: float) -> str:
    """`record.created` as an ISO-8601 UTC instant, matching the schema's `Z` suffix.

    Server-generated and always UTC `[observability.md §10]`: span ordering never depends on
    a worker's wall clock, but a line whose timestamp is in an unstated zone cannot be read at
    all.
    """
    moment = datetime.fromtimestamp(created, tz=UTC).isoformat(timespec="milliseconds")
    return moment.replace("+00:00", "Z")


def record_payload(record: logging.LogRecord) -> dict[str, Any]:
    """The full, *unredacted* field set for one record.

    Unredacted on purpose: this is what the tripwire scans. Redaction happens once, in the
    formatter, so there is no path where a value is filtered before it has been inspected.
    """
    context = current_context()
    payload: dict[str, Any] = {
        "ts": _timestamp(record.created),
        "level": record.levelname.lower(),
        "msg": record.getMessage(),
        "logger": record.name,
        **context,
    }
    for key, value in record.__dict__.items():
        if key.startswith("_") or key in _RESERVED_RECORD_ATTRIBUTES:
            continue
        payload[key] = value
    if record.exc_info is not None and record.exc_info[0] is not None:
        payload["exc_type"] = record.exc_info[0].__name__
    return payload


class TraceContextFilter(logging.Filter):
    """Binds the ambient trace context onto the record, synthesising an id if there is none."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Always returns `True`; this filter enriches rather than selects."""
        trace_id = current_context()["trace_id"]
        if not isinstance(trace_id, str) or not trace_id:
            trace_id = synthesised_trace_id()
            MISSING_TRACE_ID_ALARM.increment()
            record.trace_synthesised = True
        record.trace_id = trace_id
        return True


class TripwireFilter(logging.Filter):
    """Runs the redaction scan where an exception can still escape the logging machinery."""

    def __init__(self, mode: TripwireMode = TripwireMode.RAISE) -> None:
        super().__init__()
        self.mode = mode

    def filter(self, record: logging.LogRecord) -> bool:
        """Raise in dev and CI, count in production. Never drops the record itself."""
        enforce(scan_payload(record_payload(record)), self.mode)
        return True


class TraceSampler(logging.Filter):
    """Deterministic per-trace sampling of records below `WARNING`.

    Deterministic because a random decision per line shreds a trace into an unreadable subset
    of itself; hashing the `trace_id` keeps a trace whole or drops it whole.
    """

    def __init__(self, sample_rate: float = 1.0) -> None:
        super().__init__()
        if not 0.0 <= sample_rate <= 1.0:
            message = f"sample_rate must be within [0.0, 1.0], got {sample_rate!r}"
            raise ValueError(message)
        self.sample_rate = sample_rate

    def filter(self, record: logging.LogRecord) -> bool:
        """`True` if the record survives sampling. Errors and warnings always do."""
        if record.levelno >= NEVER_SAMPLED_LEVEL:
            return True
        if self.sample_rate >= 1.0:
            return True
        trace_id = str(getattr(record, "trace_id", ""))
        digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % SAMPLING_RESOLUTION
        return bucket < round(self.sample_rate * SAMPLING_RESOLUTION)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, every schema key present, every value redacted.

    Serialises but does not enforce: `TripwireFilter` has already inspected this record and
    either raised or counted, so scanning again here would only double the alarm.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise the record, dropping anything the allow-list does not admit."""
        payload = record_payload(record)
        payload["trace_id"] = getattr(record, "trace_id", None) or synthesised_trace_id()
        if is_synthesised(str(payload["trace_id"])):
            payload["trace_synthesised"] = True
        redacted = sanitise(payload)
        line: dict[str, Any] = {key: redacted.get(key) for key in SCHEMA_KEYS}
        for key in sorted(set(redacted) - set(SCHEMA_KEYS)):
            line[key] = redacted[key]
        return json.dumps(line, ensure_ascii=False, separators=(",", ":"))


def build_handler(
    *,
    stream: TextIO | None = None,
    mode: TripwireMode = TripwireMode.RAISE,
    sample_rate: float = 1.0,
    level: int = logging.NOTSET,
) -> logging.Handler:
    """A stream handler wired with the three filters and the JSON formatter, in order."""
    handler: logging.Handler = logging.StreamHandler(stream) if stream else logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceContextFilter())
    handler.addFilter(TripwireFilter(mode=mode))
    handler.addFilter(TraceSampler(sample_rate=sample_rate))
    setattr(handler, HANDLER_MARKER, True)
    return handler


def configure_logging(
    settings: Settings,
    *,
    stream: TextIO | None = None,
    sample_rate: float = 1.0,
) -> logging.Handler:
    """Install JSON logging on the root logger and return the handler that was installed.

    The root logger rather than a `video_agent` logger, because a dependency's unstructured
    line is exactly as unparseable as one of ours would be, and the aggregator does not care
    which package emitted it.

    Level comes from `LOG_LEVEL` and the tripwire mode from `ENV`, both via `Settings` — the
    two knobs the deployment owns. Re-configuring removes the handler this function installed
    last time, so calling it twice in a test does not double every line.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, HANDLER_MARKER, False):
            root.removeHandler(existing)
    handler = build_handler(
        stream=stream,
        mode=tripwire_mode_for_env(settings.ENV),
        sample_rate=sample_rate,
    )
    root.addHandler(handler)
    root.setLevel(logging.getLevelNamesMapping()[settings.LOG_LEVEL])
    return handler


def get_logger(name: str) -> logging.Logger:
    """The one sanctioned way to obtain a logger.

    A single accessor exists so the static guard can forbid `logging.getLogger` everywhere
    else: a logger obtained directly is a logger that predates configuration, bypasses the
    filters, or writes a format nothing can parse.
    """
    return logging.getLogger(name)
