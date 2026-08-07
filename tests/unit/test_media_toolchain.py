"""S0.1.4 — the ffmpeg/ffprobe version assertion and its refuse-to-start behaviour."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from video_agent.assembly import media_toolchain
from video_agent.assembly.media_toolchain import (
    BINARY_PATH_ENV_VARS,
    DEFAULT_FFMPEG_VERSION,
    REQUIRED_BINARIES,
    VERSION_PIN_ENV_VAR,
    MediaToolchainError,
    assert_media_toolchain,
    describe_pin,
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


def _write_fake_binary(directory: Path, binary: str, version: str) -> None:
    script = directory / binary
    script.write_text(
        f'#!/bin/sh\necho "{binary} version {version} Copyright (c) fake"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_fake_toolchain(directory: Path, version: str) -> None:
    for binary in REQUIRED_BINARIES:
        _write_fake_binary(directory, binary, version)


def _run_entrypoint(repo_root: Path, path_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = str(path_dir)
    # The explicit binary overrides win over PATH by design, so a developer machine that
    # sets them would otherwise make this test probe the real toolchain instead of the
    # fake one staged above. Clear them so PATH resolution is what is under test.
    for override in BINARY_PATH_ENV_VARS.values():
        env.pop(override, None)
    # Pin the child to the module constant rather than to `pinned_version()`. Feeding the
    # child whatever the parent's environment says would make the entrypoint tests agree
    # with the pin by construction, which is how a wrong default went unnoticed. The
    # constant itself is anchored to `.env.example` by the contract test.
    env[VERSION_PIN_ENV_VAR] = DEFAULT_FFMPEG_VERSION
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
    assert DEFAULT_FFMPEG_VERSION in result.stderr
    assert "Traceback" not in result.stderr


def test_entrypoint_exits_zero_when_the_pin_matches(repo_root: Path, tmp_path: Path) -> None:
    _write_fake_toolchain(tmp_path, f"{DEFAULT_FFMPEG_VERSION}.0")
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
        """A real executable is staged on PATH, so the fallback has something to find.

        Staging nothing and asserting `is None` cannot distinguish "fell back to PATH and
        found nothing" from "did not fall back at all" — the earlier version of this test
        made exactly that mistake and survived deleting the fallback branch.
        """
        found = tmp_path / "ffprobe"
        found.write_text("#!/bin/sh\nexit 0\n")
        found.chmod(found.stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("FFPROBE_BINARY", raising=False)

        assert resolve_binary("ffprobe") == str(found)

    def test_path_resolution_returns_none_only_when_nothing_is_staged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The negative half of the pair above, on a PATH proven empty of the binary."""
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("FFPROBE_BINARY", raising=False)
        assert not (tmp_path / "ffprobe").exists()
        assert resolve_binary("ffprobe") is None

    def test_path_resolution_is_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative PATH entry must not yield a relative command.

        `subprocess` hands a name containing no separator to a PATH search, so a relative
        result would be re-resolved at exec time against a PATH that may have changed.
        """
        staged = tmp_path / "ffmpeg"
        staged.write_text("#!/bin/sh\nexit 0\n")
        staged.chmod(staged.stat().st_mode | stat.S_IEXEC)

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PATH", ".")
        monkeypatch.delenv("FFMPEG_BINARY", raising=False)

        resolved = resolve_binary("ffmpeg")
        assert resolved is not None
        assert Path(resolved).is_absolute()

    def test_a_relative_override_is_a_configuration_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`FFMPEG_BINARY=ffmpeg` validates one file and would execute another.

        `Path("ffmpeg").is_file()` resolves against the *current working directory*, while
        `subprocess.run(["ffmpeg", ...])` resolves against *PATH*. Staging a decoy in cwd and
        a different binary on PATH is precisely that divergence, and it is the silent
        mismatch this whole module exists to prevent — so it is rejected, not resolved.
        """
        decoy = tmp_path / "ffmpeg"
        decoy.write_text("#!/bin/sh\nexit 0\n")
        decoy.chmod(decoy.stat().st_mode | stat.S_IEXEC)

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        on_path = elsewhere / "ffmpeg"
        on_path.write_text("#!/bin/sh\nexit 0\n")
        on_path.chmod(on_path.stat().st_mode | stat.S_IEXEC)

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PATH", str(elsewhere))
        monkeypatch.setenv("FFMPEG_BINARY", "ffmpeg")

        with pytest.raises(MediaToolchainError) as excinfo:
            resolve_binary("ffmpeg")

        message = str(excinfo.value)
        assert "FFMPEG_BINARY" in message
        assert "absolute" in message

    def test_a_dot_relative_override_is_also_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`./ffmpeg` exists and is executable, but is still cwd-dependent at exec time."""
        staged = tmp_path / "ffmpeg"
        staged.write_text("#!/bin/sh\nexit 0\n")
        staged.chmod(staged.stat().st_mode | stat.S_IEXEC)

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FFMPEG_BINARY", "./ffmpeg")

        with pytest.raises(MediaToolchainError, match="absolute"):
            resolve_binary("ffmpeg")

    def test_an_override_pointing_at_nothing_is_rejected_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never silently fall back to PATH when an override is set but wrong.

        Falling back would start the app against a binary the operator did not choose,
        which is precisely the ambiguity the override exists to remove. The message must
        name the variable and its value, not claim the binary is missing from PATH — it may
        well be on PATH, which is what made the old wording misleading.
        """
        missing = tmp_path / "does-not-exist"
        monkeypatch.setenv("FFMPEG_BINARY", str(missing))
        monkeypatch.setenv("PATH", str(tmp_path))

        with pytest.raises(MediaToolchainError) as excinfo:
            resolve_binary("ffmpeg")

        message = str(excinfo.value)
        assert "FFMPEG_BINARY" in message
        assert str(missing) in message
        assert "not found on PATH" not in message

    def test_a_non_executable_override_is_rejected_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = tmp_path / "not-executable"
        plain.write_text("i am not a program\n")
        monkeypatch.setenv("FFMPEG_BINARY", str(plain))

        with pytest.raises(MediaToolchainError, match="not an executable file"):
            resolve_binary("ffmpeg")

    def test_every_required_binary_has_an_override_variable(self) -> None:
        assert set(BINARY_PATH_ENV_VARS) == set(REQUIRED_BINARIES)


class TestPinPrecision:
    """`FFMPEG_REQUIRED_VERSION` may be MAJOR.MINOR or MAJOR.MINOR.PATCH."""

    def test_a_three_component_pin_matches_that_exact_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`.env.example` says the image installs *exactly* this version, which invites a
        precise value. Comparing a two-component slice of the actual against a raw
        three-component pin could never match, so a more precise pin bricked startup.
        """
        monkeypatch.setenv(VERSION_PIN_ENV_VAR, "7.1.1")
        assert_media_toolchain(probe=lambda _: "7.1.1")

    def test_a_three_component_pin_rejects_a_different_patch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VERSION_PIN_ENV_VAR, "7.1.1")
        with pytest.raises(MediaToolchainError) as excinfo:
            assert_media_toolchain(probe=lambda _: "7.1.2")

        message = str(excinfo.value)
        assert "7.1.1" in message
        assert "7.1.2" in message
        assert "7.1.1.x" not in message, "an exact pin must not be rendered as a wildcard"

    def test_a_two_component_pin_still_accepts_any_patch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VERSION_PIN_ENV_VAR, "7.1")
        assert_media_toolchain(probe=lambda _: "7.1.99")

    @pytest.mark.parametrize("bad", ["7", "seven.one", "7.1.1.1", "v7.1", "7.1-custom"])
    def test_a_malformed_pin_is_rejected_with_a_clear_message(
        self, bad: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VERSION_PIN_ENV_VAR, bad)
        with pytest.raises(MediaToolchainError) as excinfo:
            pinned_version()

        message = str(excinfo.value)
        assert VERSION_PIN_ENV_VAR in message
        assert bad in message

    def test_describe_pin_renders_precision_honestly(self) -> None:
        assert describe_pin("7.1") == "7.1.x"
        assert describe_pin("7.1.1") == "7.1.1"


class TestMatchedPair:
    """`.env.example`: ffmpeg and ffprobe must be the SAME release, not merely both pinned."""

    def test_binaries_from_different_patch_releases_are_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both satisfy a 7.1 pin, yet they are two different encoders."""
        monkeypatch.setenv(VERSION_PIN_ENV_VAR, "7.1")
        versions = {"ffmpeg": "7.1.1", "ffprobe": "7.1.99"}

        with pytest.raises(MediaToolchainError) as excinfo:
            assert_media_toolchain(probe=lambda binary: versions[binary])

        message = str(excinfo.value)
        assert "7.1.1" in message, "the message must name both versions"
        assert "7.1.99" in message
        assert "ffmpeg" in message
        assert "ffprobe" in message

    def test_a_matched_pair_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VERSION_PIN_ENV_VAR, "7.1")
        assert_media_toolchain(probe=lambda _: "7.1.1")

    def test_the_entrypoint_refuses_a_mismatched_pair(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """End to end: two real scripts reporting different patch releases."""
        _write_fake_binary(tmp_path, "ffmpeg", f"{DEFAULT_FFMPEG_VERSION}.1")
        _write_fake_binary(tmp_path, "ffprobe", f"{DEFAULT_FFMPEG_VERSION}.2")
        result = _run_entrypoint(repo_root, tmp_path)

        assert result.returncode != 0
        assert "not a matched pair" in result.stderr
        assert f"{DEFAULT_FFMPEG_VERSION}.1" in result.stderr
        assert f"{DEFAULT_FFMPEG_VERSION}.2" in result.stderr
        assert "Traceback" not in result.stderr


class TestProbeFailuresAreNotTracebacks:
    """S0.1.4 test spec: a clear error, not a traceback leak.

    The injectable `probe` never exercises these paths, so they are driven with real
    binaries that hang or cannot be executed.
    """

    def test_a_hung_binary_is_reported_as_a_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for binary in REQUIRED_BINARIES:
            script = tmp_path / binary
            # Absolute path: PATH is about to be narrowed to tmp_path, so a bare `sleep`
            # would exit 127 and be reported as a non-zero exit rather than a hang.
            script.write_text("#!/bin/sh\nexec /bin/sleep 30\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("PATH", str(tmp_path))
        for override in BINARY_PATH_ENV_VARS.values():
            monkeypatch.delenv(override, raising=False)
        monkeypatch.setattr(media_toolchain, "_PROBE_TIMEOUT_SECONDS", 1)

        with pytest.raises(MediaToolchainError) as excinfo:
            assert_media_toolchain()

        message = str(excinfo.value)
        assert "did not answer" in message
        assert "ffmpeg" in message
        assert "Traceback" not in message

    def test_an_unexecutable_binary_is_reported_as_such(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated or wrong-architecture binary raises OSError from `subprocess`."""
        for binary in REQUIRED_BINARIES:
            script = tmp_path / binary
            script.write_bytes(b"\x00\x01\x02\x03")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("PATH", str(tmp_path))
        for override in BINARY_PATH_ENV_VARS.values():
            monkeypatch.delenv(override, raising=False)

        with pytest.raises(MediaToolchainError) as excinfo:
            assert_media_toolchain()

        message = str(excinfo.value)
        assert "could not be executed" in message
        assert "Traceback" not in message

    def test_the_entrypoint_survives_an_unexecutable_binary(
        self, repo_root: Path, tmp_path: Path
    ) -> None:
        """The real `main()` path: exit 1 with a sentence, never a traceback."""
        for binary in REQUIRED_BINARIES:
            script = tmp_path / binary
            script.write_bytes(b"\x00\x01\x02\x03")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)

        result = _run_entrypoint(repo_root, tmp_path)

        assert result.returncode != 0
        assert "startup preflight failed" in result.stderr
        assert "could not be executed" in result.stderr
        assert "Traceback" not in result.stderr
