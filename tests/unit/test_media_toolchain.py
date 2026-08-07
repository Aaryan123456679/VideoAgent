"""S0.1.4 — the ffmpeg/ffprobe version assertion and its refuse-to-start behaviour."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from video_agent.assembly.media_toolchain import (
    DEFAULT_FFMPEG_VERSION,
    REQUIRED_BINARIES,
    MediaToolchainError,
    assert_media_toolchain,
    pinned_version,
    resolve_binary,
)

WRONG_VERSION = "4.2.7"
SUBPROCESS_TIMEOUT = 60


def test_matching_version_returns_none() -> None:
    """S0.1.4 acceptance 3: returns None (i.e. does not raise) when the version matches.

    The signature is `-> None`, so the assertion that carries meaning is that no
    `MediaToolchainError` escapes.
    """
    assert_media_toolchain(probe=lambda _: f"{pinned_version()}.0")


def test_patch_release_is_accepted() -> None:
    """The pin is MAJOR.MINOR; a patch bump is not drift."""
    assert_media_toolchain(probe=lambda _: f"{pinned_version()}.99")


def test_ffmpeg_version_mismatch_refuses_start() -> None:
    with pytest.raises(MediaToolchainError) as excinfo:
        assert_media_toolchain(probe=lambda _: WRONG_VERSION)

    message = str(excinfo.value)
    assert pinned_version() in message
    assert WRONG_VERSION in message
    assert isinstance(excinfo.value, RuntimeError)


def test_ffmpeg_missing_binary_refuses_start() -> None:
    def missing(binary: str) -> str:
        message = f"{binary} not found"
        raise FileNotFoundError(message)

    with pytest.raises(MediaToolchainError) as excinfo:
        assert_media_toolchain(probe=missing)

    message = str(excinfo.value)
    assert "ffmpeg" in message
    assert "not found on PATH" in message
    assert "Traceback" not in message
    # The FileNotFoundError is suppressed, so no traceback chain leaks to the operator.
    assert excinfo.value.__cause__ is None


def test_both_binaries_are_checked() -> None:
    seen: list[str] = []

    def record(binary: str) -> str:
        seen.append(binary)
        return f"{pinned_version()}.0"

    assert_media_toolchain(probe=record)
    assert seen == list(REQUIRED_BINARIES)
    assert set(REQUIRED_BINARIES) == {"ffmpeg", "ffprobe"}


def _write_fake_toolchain(directory: Path, version: str) -> None:
    for binary in REQUIRED_BINARIES:
        script = directory / binary
        script.write_text(
            f'#!/bin/sh\necho "{binary} version {version} Copyright (c) fake"\n',
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_entrypoint(repo_root: Path, path_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = str(path_dir)
    # The explicit binary overrides win over PATH by design, so a developer machine that
    # sets them would otherwise make this test probe the real toolchain instead of the
    # fake one staged above. Clear them so PATH resolution is what is under test.
    for override in ("FFMPEG_BINARY", "FFPROBE_BINARY"):
        env.pop(override, None)
    env["FFMPEG_REQUIRED_VERSION"] = pinned_version()
    return subprocess.run(
        [sys.executable, "-m", "video_agent"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )


def test_entrypoint_exits_nonzero_on_version_drift(repo_root: Path, tmp_path: Path) -> None:
    """The process refuses to start; nothing is bound because preflight runs first."""
    _write_fake_toolchain(tmp_path, WRONG_VERSION)
    result = _run_entrypoint(repo_root, tmp_path)

    assert result.returncode != 0
    assert "startup preflight failed" in result.stderr
    assert WRONG_VERSION in result.stderr
    assert pinned_version() in result.stderr
    assert "Traceback" not in result.stderr


def test_entrypoint_exits_zero_when_the_pin_matches(repo_root: Path, tmp_path: Path) -> None:
    _write_fake_toolchain(tmp_path, f"{pinned_version()}.0")
    result = _run_entrypoint(repo_root, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "startup preflight passed" in result.stderr


def test_entrypoint_exits_nonzero_when_the_binary_is_absent(
    repo_root: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    result = _run_entrypoint(repo_root, empty)

    assert result.returncode != 0
    assert "not found on PATH" in result.stderr
    assert "Traceback" not in result.stderr


class TestBinaryResolutionOverrides:
    """The explicit `FFMPEG_BINARY` / `FFPROBE_BINARY` overrides and the version pin.

    A developer machine routinely carries more than one ffmpeg. A standalone build in
    `~/.local/bin` commonly ships `ffmpeg` with no `ffprobe` beside it, so bare PATH
    resolution silently pairs binaries from two different releases — the exact mismatch
    the version assertion exists to catch, arrived at by accident rather than by
    misconfiguration. These overrides make the matched pair nameable.
    """

    def test_pin_defaults_when_the_override_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FFMPEG_REQUIRED_VERSION", raising=False)
        assert pinned_version() == DEFAULT_FFMPEG_VERSION

    def test_pin_honours_the_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FFMPEG_REQUIRED_VERSION", "9.9")
        assert pinned_version() == "9.9"

    def test_a_blank_pin_falls_back_rather_than_matching_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty env var is how a `.env` file spells "unset", not a pin of "".

        Treating it literally would compare every real version against the empty string
        and refuse to start with a confusing message.
        """
        monkeypatch.setenv("FFMPEG_REQUIRED_VERSION", "   ")
        assert pinned_version() == DEFAULT_FFMPEG_VERSION

    def test_override_takes_precedence_over_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = tmp_path / "chosen-ffmpeg"
        real.write_text("#!/bin/sh\nexit 0\n")
        real.chmod(real.stat().st_mode | stat.S_IEXEC)
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "ffmpeg").write_text("#!/bin/sh\nexit 0\n")
        (decoy / "ffmpeg").chmod((decoy / "ffmpeg").stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("PATH", str(decoy))
        monkeypatch.setenv("FFMPEG_BINARY", str(real))

        assert resolve_binary("ffmpeg") == str(real)

    def test_falls_back_to_path_when_no_override_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        found = tmp_path / "ffprobe"
        found.write_text("#!/bin/sh\nexit 0\n")
        found.chmod(found.stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("FFPROBE_BINARY", raising=False)

        assert resolve_binary("ffprobe") == str(found)

    def test_an_override_pointing_at_nothing_resolves_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never silently fall back to PATH when an override is set but wrong.

        Falling back would start the app against a binary the operator did not choose,
        which is precisely the ambiguity the override exists to remove. Resolving to
        None makes preflight fail loudly instead.
        """
        monkeypatch.setenv("FFMPEG_BINARY", str(tmp_path / "does-not-exist"))
        monkeypatch.setenv("PATH", str(tmp_path))
        assert resolve_binary("ffmpeg") is None

    def test_a_non_executable_override_resolves_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = tmp_path / "not-executable"
        plain.write_text("i am not a program\n")
        monkeypatch.setenv("FFMPEG_BINARY", str(plain))
        assert resolve_binary("ffmpeg") is None
