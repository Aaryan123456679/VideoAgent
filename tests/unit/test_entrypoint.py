"""`main()` claims it never raises. This is the file that makes that true.

The preflight steps raise more than the two exception types `main()` originally caught:
`subprocess.TimeoutExpired` from a hung ffmpeg and `OSError` from one that cannot be
executed both escaped as full tracebacks. The S0.1.4 test spec asks for a clear error rather
than a traceback leak, and an operator staring at a stack frame has been told nothing.
"""

from __future__ import annotations

import pytest

from video_agent import __main__ as entrypoint

PLANTED = "planted-failure-detail"


def test_returns_zero_when_preflight_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(entrypoint, "preflight", lambda: None)

    assert entrypoint.main() == entrypoint.EXIT_OK
    assert "startup preflight passed" in capsys.readouterr().err


def test_a_runtime_error_is_rendered_as_a_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> None:
        raise RuntimeError(PLANTED)

    monkeypatch.setattr(entrypoint, "preflight", boom)

    assert entrypoint.main() == entrypoint.EXIT_PRECONDITION_FAILED
    stderr = capsys.readouterr().err
    assert "startup preflight failed" in stderr
    assert PLANTED in stderr
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

    def boom() -> None:
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

    def boom() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(entrypoint, "preflight", boom)

    with pytest.raises(KeyboardInterrupt):
        entrypoint.main()
