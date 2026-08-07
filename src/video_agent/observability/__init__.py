"""observability — traces, structured logs, redaction, error codes.

See ``docs/LLD/observability.md``. This module depends on no other module in the repo;
every other module depends on it.

What `T0.3` lands is the *substrate*: the stable code taxonomy and its append-only register,
JSON logging carrying `trace_id` from a context variable, and deny-by-default redaction with a
tripwire. Langfuse traces, spans, generations and scores are `T4.1`'s and are deliberately not
here — the substrate has to exist first, because every other module raises a code and writes a
log line before any of it can be traced.
"""

from video_agent.observability.alarms import AlarmCounter
from video_agent.observability.codes import ErrorCode, Retryability
from video_agent.observability.context import (
    bind_trace,
    clear_trace,
    current_context,
    current_trace_id,
    new_trace_id,
)
from video_agent.observability.errors import VideoAgentError
from video_agent.observability.logging import (
    MISSING_TRACE_ID_ALARM,
    JsonFormatter,
    configure_logging,
    get_logger,
)
from video_agent.observability.redaction import (
    ALLOWED_FIELDS,
    REDACTION_TRIPWIRE_ALARM,
    RedactionTripwireError,
    TripwireMode,
    redact,
    sanitise,
    scan_payload,
    summarise_prompt,
    tripwire_mode_for_env,
)
from video_agent.observability.registry import check_registry, load_registry, taxonomy_facts

__all__ = [
    "ALLOWED_FIELDS",
    "MISSING_TRACE_ID_ALARM",
    "REDACTION_TRIPWIRE_ALARM",
    "AlarmCounter",
    "ErrorCode",
    "JsonFormatter",
    "RedactionTripwireError",
    "Retryability",
    "TripwireMode",
    "VideoAgentError",
    "bind_trace",
    "check_registry",
    "clear_trace",
    "configure_logging",
    "current_context",
    "current_trace_id",
    "get_logger",
    "load_registry",
    "new_trace_id",
    "redact",
    "sanitise",
    "scan_payload",
    "summarise_prompt",
    "taxonomy_facts",
    "tripwire_mode_for_env",
]
