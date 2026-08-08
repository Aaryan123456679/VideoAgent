"""`main()` claims it never raises, and claims a boundary about *where* failures are reported.

The first claim was `S0.1.4`'s: the preflight steps raise more than the two exception types
`main()` originally caught — `subprocess.TimeoutExpired` from a hung ffmpeg and `OSError` from
one that cannot be executed both escaped as full tracebacks. An operator staring at a stack
frame has been told nothing.

The second claim arrives with `T0.4`. `configure_logging` takes a `Settings`, so there is a
window at the start of the process where no structured logger can exist, and exactly two
assertions run inside it: the media-toolchain pin and configuration loading. Those report to
`stderr`, which is why `tests/static_guards.py` exempts this one file by exact path. Everything
after `configure_logging` reports through the logger. The tests below pin that boundary from
both sides, because an exemption that quietly widens is how "one file writes to stderr" becomes
"the codebase writes to stderr".
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Final

import pytest
from pydantic import ValidationError

from tests.unit.test_app_shell import captured_logs
from video_agent import __main__ as entrypoint
from video_agent.config.settings import (
    REDACTED_DETAIL,
    Settings,
    describe_validation_error,
    get_settings,
)
from video_agent.observability.codes import ErrorCode

PLANTED: Final = "planted-failure-detail"


def _settings() -> Settings:
    return get_settings()


def unstructured_lines(stderr: str) -> list[str]:
    """Lines on `stderr` that are not JSON.

    This is the assertion that matters, and it is not "nothing reaches stderr": the configured
    handler writes there too, as one JSON object per line. What must not appear after
    `configure_logging` is a *bare sentence* — a `sys.stderr.write` that no aggregator can
    parse and that carries neither a code nor a trace id.
    """
    offenders: list[str] = []
    for line in stderr.splitlines():
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            offenders.append(line)
    return offenders


def test_returns_zero_when_preflight_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The success path reports through the logger, not through `stderr` directly."""
    monkeypatch.setattr(entrypoint, "preflight", _settings)
    monkeypatch.setattr(entrypoint, "post_logging_preflight", lambda: None)

    with captured_logs() as lines:
        exit_code = entrypoint.main()

    assert exit_code == entrypoint.EXIT_OK
    assert unstructured_lines(capsys.readouterr().err) == []
    assert any(line.get("event") == "startup_preflight_passed" for line in lines)


def test_a_runtime_error_is_rendered_as_a_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-logging failure is a plain sentence on `stderr`, never a traceback."""

    def boom() -> Settings:
        raise RuntimeError(PLANTED)

    monkeypatch.setattr(entrypoint, "preflight", boom)

    assert entrypoint.main() == entrypoint.EXIT_PRECONDITION_FAILED
    stderr = capsys.readouterr().err
    assert f"startup preflight failed: {PLANTED}" in unstructured_lines(stderr)
    assert "Traceback" not in stderr


@pytest.mark.parametrize(
    "exception",
    [
        OSError(8, PLANTED),
        ValueError(PLANTED),
        KeyError(PLANTED),
        TypeError(PLANTED),
    ],
    ids=["oserror", "valueerror", "keyerror", "typeerror"],
)
def test_no_exception_type_escapes_main(
    exception: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """None of these is a `RuntimeError` or a `ValidationError`.

    Before the backstop clause, every one of them propagated out of `main()` and printed a
    traceback with a non-zero exit that said nothing about why.
    """

    def boom() -> Settings:
        raise exception

    monkeypatch.setattr(entrypoint, "preflight", boom)

    assert entrypoint.main() == entrypoint.EXIT_PRECONDITION_FAILED
    stderr = capsys.readouterr().err
    assert "startup preflight failed" in stderr
    assert type(exception).__name__ in stderr
    assert PLANTED in stderr
    assert "Traceback" not in stderr


def test_a_keyboard_interrupt_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backstop catches `Exception`, deliberately not `BaseException`.

    Turning Ctrl-C into "startup preflight failed" would misreport an operator's own action
    as a precondition failure, and would swallow `SystemExit`.
    """

    def boom() -> Settings:
        raise KeyboardInterrupt

    monkeypatch.setattr(entrypoint, "preflight", boom)

    with pytest.raises(KeyboardInterrupt):
        entrypoint.main()


# --- The pre-logging boundary ----------------------------------------------------------------


def test_logging_is_configured_immediately_after_settings_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`configure_logging` runs between the two preflight phases, in that order.

    `T0.3` built the logging substrate and nothing called it. Until this line existed, a
    production process wrote whatever the stdlib's default configuration produced —
    unstructured, untraceable and, most importantly, **unredacted**.
    """
    order: list[str] = []

    def record_preflight() -> Settings:
        order.append("preflight")
        return get_settings()

    def record_configure(_configured: Settings) -> None:
        order.append("configure_logging")

    def record_post() -> None:
        order.append("post_logging_preflight")

    monkeypatch.setattr(entrypoint, "preflight", record_preflight)
    monkeypatch.setattr(entrypoint, "configure_logging", record_configure)
    monkeypatch.setattr(entrypoint, "post_logging_preflight", record_post)

    assert entrypoint.main() == entrypoint.EXIT_OK
    assert order == ["preflight", "configure_logging", "post_logging_preflight"]


def test_a_post_logging_failure_goes_to_the_logger_not_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once logging exists, `stderr` is no longer the reporting channel.

    The alias table is validated after `configure_logging` precisely so its failure is a
    structured, coded, traceable line rather than a sentence nothing can parse.
    """
    monkeypatch.setattr(entrypoint, "preflight", _settings)

    def boom() -> None:
        raise RuntimeError(PLANTED)

    monkeypatch.setattr(entrypoint, "post_logging_preflight", boom)

    with captured_logs() as lines:
        exit_code = entrypoint.main()

    assert exit_code == entrypoint.EXIT_PRECONDITION_FAILED
    assert unstructured_lines(capsys.readouterr().err) == []

    failures = [line for line in lines if line.get("event") == "startup_preflight_failed"]
    assert failures
    assert failures[0]["code"] == ErrorCode.VA_GW_002.value
    assert PLANTED in failures[0]["reason"]
    assert failures[0]["exc_type"] == "RuntimeError"


def test_the_pre_logging_phase_holds_only_the_two_assertions_that_need_it() -> None:
    """`preflight` may not grow: everything it contains is exempt from the `stderr` ban.

    The exemption in `tests/static_guards.py` names this module by exact path and exists for
    the media-toolchain pin, which must hold when configuration is unreadable. A third
    assertion added to `preflight` would silently inherit an exemption written for two.
    """
    source = inspect.getsource(entrypoint.preflight)

    assert "assert_media_toolchain()" in source
    assert "get_settings()" in source
    assert "get_alias_table" not in source


def test_the_alias_table_is_validated_after_logging_exists() -> None:
    """The other half of the boundary: the third assertion is on the logged side of it."""
    source = inspect.getsource(entrypoint.post_logging_preflight)

    assert "get_alias_table()" in source


def test_the_module_docstring_describes_the_logging_that_now_exists() -> None:
    """The docstring claimed structured logging did not exist yet. It does.

    A comment that has become false is worse than no comment: the next person reads it as a
    reason not to look.
    """
    docstring = entrypoint.__doc__ or ""

    assert "does not exist yet" not in docstring
    assert "configure_logging" in docstring


def test_the_startup_log_line_carries_no_credentials() -> None:
    """The startup path handles `Settings`, which is where every credential lives."""
    with captured_logs() as lines:
        entrypoint.main()

    serialised = json.dumps(lines)
    for marker in ("MAGICHOUR_API_KEY=", "postgresql+asyncpg://", "SecretStr"):
        assert marker not in serialised


# --- The pre-logging phase is exempt from the logger, not from the never-logged list ---------

PLANTED_STARTUP_KEY: Final = "mhk_live_PLANTED_REALKEY_ABC"
"""Set in the environment of a deployment whose `.env` is otherwise incomplete."""


def _sparse_settings_error(monkeypatch: pytest.MonkeyPatch, env_example: dict[str, str]) -> None:
    """The misconfigured-deploy shape: one variable set, the required ones absent."""
    for name in env_example:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAGICHOUR_API_KEY", PLANTED_STARTUP_KEY)


def test_a_settings_failure_names_every_missing_variable_without_printing_any_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env_example: dict[str, str]
) -> None:
    """Pydantic's `missing` error carries `input_value` — the whole collected settings dict.

    `str(exc)` therefore prints the credentials that *were* configured while explaining the one
    that was not, straight to `stderr`, and this module is deliberately exempt from the logging
    guard so redaction never sees it. `AGENT.md` §3 names API keys and DB URLs while forbidding
    exactly this.

    The leak is intermittent, which is what makes it dangerous rather than merely wrong:
    pydantic truncates a long `input_value` repr, so a fully-populated environment hides it and
    the sparse, half-configured deployment does not.
    """
    _sparse_settings_error(monkeypatch, env_example)
    monkeypatch.setattr(entrypoint, "preflight", lambda: Settings(_env_file=None))

    assert entrypoint.main() == entrypoint.EXIT_PRECONDITION_FAILED
    stderr = capsys.readouterr().err

    assert PLANTED_STARTUP_KEY not in stderr
    assert "input_value" not in stderr
    # The acceptance criterion the redaction must not cost us: every missing name, one message.
    assert len(unstructured_lines(stderr)) == 1
    for name in ("DATABASE_URL", "REDIS_URL"):
        assert name in stderr


def test_describe_validation_error_names_the_fields_and_omits_the_input(
    monkeypatch: pytest.MonkeyPatch, env_example: dict[str, str]
) -> None:
    """The renderer on its own, so a change to it fails here and not only through `main()`."""
    _sparse_settings_error(monkeypatch, env_example)

    with pytest.raises(ValidationError) as raised:
        Settings(_env_file=None)

    described = describe_validation_error(raised.value)

    assert PLANTED_STARTUP_KEY not in described
    assert "DATABASE_URL" in described
    assert "REDIS_URL" in described


def test_a_stderr_sentence_carrying_a_secret_is_replaced_rather_than_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scanner runs on this path too, whatever the exception happens to carry.

    `describe_validation_error` fixes the one failure mode that was reproduced. This asserts
    the general property the exemption needs: **nothing** `main()` writes to `stderr` before
    logging exists carries a never-logged value, including the message of an exception raised
    by a preflight step nobody has written yet.
    """
    presigned = (
        "https://artifacts.example.com/t/j/shot-0.mp4?X-Amz-Signature="
        "8f4b2c1d9e7a6f5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b"
    )

    def boom() -> Settings:
        message = f"upload failed for <{presigned}>"
        raise RuntimeError(message)

    monkeypatch.setattr(entrypoint, "preflight", boom)

    assert entrypoint.main() == entrypoint.EXIT_PRECONDITION_FAILED
    stderr = capsys.readouterr().err

    assert "X-Amz-Signature" not in stderr
    assert entrypoint.PREFLIGHT_FAILURE_PREFIX in stderr
    assert REDACTED_DETAIL in stderr


def test_an_ordinary_failure_sentence_is_still_written_in_full(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scanner must not cost the operator the reason in the overwhelmingly common case."""

    def boom() -> Settings:
        message = "ffmpeg 6.1 found, 7.1 required. Install it and retry."
        raise RuntimeError(message)

    monkeypatch.setattr(entrypoint, "preflight", boom)

    entrypoint.main()

    assert "ffmpeg 6.1 found, 7.1 required." in capsys.readouterr().err


def test_main_returns_an_int_and_never_none() -> None:
    """`SystemExit(None)` is a zero exit, so a missing return would report success."""
    result: Any = entrypoint.main()

    assert isinstance(result, int)
