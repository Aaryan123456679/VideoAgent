"""Process entrypoint — startup preflight, then structured logging.

Runs every refuse-to-start assertion *before* anything is bound or connected, so a failing
precondition costs a non-zero exit and no listening socket.

The order is deliberate and it is what splits this module in two. The media toolchain pin
(assembly.md S7/S8) reads ``os.environ`` directly and runs first, because it is the one
assertion that must hold even when configuration is unreadable. Configuration is validated
second, so a missing variable is reported as a list of names rather than as a ``KeyError``
raised by whichever module happened to read it first. Both of those run *before a logger can
exist*: ``configure_logging`` takes a ``Settings``, so nothing can be logged structurally until
configuration has loaded, and a failure up to that point has nowhere to go but ``stderr``.

That is why ``tests/static_guards.py`` exempts this one file — and only this file, by exact
path — from the ``sys.stderr.write`` ban. The exemption exists for the pre-logging phase and
must not outgrow it: everything after ``configure_logging`` reports through the structured
logger, including the alias-table check, which is validated last because it is the only one of
the three a running process could survive without noticing.

The HTTP server is **not** started here. The ASGI application is
``video_agent.api.app:create_app``, which runs its own ``configure_logging`` and opens its
pools in a lifespan; a deployment runs this preflight and then serves that factory. Binding a
socket from this module would need a host and a port, and ``.env.example`` — the configuration
contract — declares neither.
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from video_agent.assembly.media_toolchain import assert_media_toolchain
from video_agent.config.aliases import get_alias_table
from video_agent.config.settings import (
    REDACTED_DETAIL,
    Settings,
    describe_validation_error,
    get_settings,
)
from video_agent.observability.codes import ErrorCode
from video_agent.observability.logging import configure_logging, get_logger
from video_agent.observability.redaction import contains_never_logged_value

EXIT_OK = 0
EXIT_PRECONDITION_FAILED = 1

PREFLIGHT_FAILURE_PREFIX = "startup preflight failed: "


def _safe_for_stderr(detail: str) -> str:
    """`detail` unless it carries something that must never be emitted.

    This module is the one place in `src/` exempt from the logging guard, so nothing it writes
    passes through `TripwireFilter` or `redact`. The exemption buys the pre-logging phase a
    place to speak; it does not buy it an exemption from `AGENT.md` §3. Every string this
    module hands to `stderr` goes through the same scanner the logging path uses, which is why
    the scanner has a public entry point that takes plain text.
    """
    return REDACTED_DETAIL if contains_never_logged_value(detail) else detail


def _report_preflight_failure(detail: str) -> int:
    """Write one operator sentence to `stderr` and return the failing exit code."""
    sys.stderr.write(f"{PREFLIGHT_FAILURE_PREFIX}{_safe_for_stderr(detail)}\n")
    return EXIT_PRECONDITION_FAILED


def preflight() -> Settings:
    """The assertions that must hold before a logger can exist. Raises on the first failure."""
    assert_media_toolchain()
    return get_settings()


def post_logging_preflight() -> None:
    """The assertions that run once logging is configured, and report through it."""
    get_alias_table()


def main() -> int:
    """Return a process exit code. Never raises.

    The two ``except`` clauses around ``preflight`` are not redundant. The first names the
    failures preflight is *designed* to produce and renders them as the plain operator sentence
    each already carries. The second is a backstop for everything else a preflight step can
    raise on the way to that sentence — a hung binary, an unreadable file, a bug. Without it,
    "never raises" is a docstring rather than a guarantee, and the operator gets a traceback
    instead of a reason. `[CPS §Failure behaviour]` The exception type is named because
    "failed" with no class is undiagnosable.

    The second block is deliberately shaped differently. By then logging exists, so the failure
    goes to the structured logger with its code and its trace, and ``stderr`` is no longer
    involved — the traceback belongs in the log, which by that point does exist.

    ``ValidationError`` is caught separately from ``RuntimeError`` and never stringified.
    ``str(exc)`` on a settings model embeds ``input_value`` — the whole collected settings dict
    — so the operator sentence for "you forgot ``DATABASE_URL``" would print the API key that
    *was* set. ``describe_validation_error`` renders the same names without it.
    """
    try:
        settings = preflight()
    except ValidationError as exc:
        return _report_preflight_failure(describe_validation_error(exc))
    except RuntimeError as exc:
        return _report_preflight_failure(str(exc))
    except Exception as exc:
        return _report_preflight_failure(f"{type(exc).__name__}: {exc}")

    configure_logging(settings)
    logger = get_logger(__name__)
    try:
        post_logging_preflight()
    except Exception as exc:
        logger.error(
            "startup preflight failed",
            exc_info=exc,
            extra={
                "event": "startup_preflight_failed",
                "code": ErrorCode.VA_GW_002.value,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        return EXIT_PRECONDITION_FAILED
    logger.info("startup preflight passed", extra={"event": "startup_preflight_passed"})
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
