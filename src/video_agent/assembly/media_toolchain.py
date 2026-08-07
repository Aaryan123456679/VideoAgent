"""Startup assertion for the system media toolchain.

``assembly.md`` S7 pins the ffmpeg/ffprobe version in the image and asserts it at startup;
S8 makes version drift a refuse-to-start condition, because a silent encoder change is an
unlogged output change. This module owns that assertion and nothing else — all actual media
manipulation stays in the assembly wrapper (a later task).

The container image must install exactly the pinned ``MAJOR.MINOR``; if the Dockerfile and
the pin disagree, the application refuses to start, which is the intended behaviour rather
than a bug.

Both the pin and the binary locations are overridable by environment variable. Developer
machines routinely carry more than one ffmpeg — a standalone build in ``~/.local/bin``
shadowing a Homebrew install is common, and because such builds often ship ``ffmpeg``
without ``ffprobe``, bare ``PATH`` resolution can silently pair binaries from two different
releases. Resolving each binary explicitly makes that mismatch impossible to reach by
accident rather than merely detectable after the fact.

This module reads ``os.environ`` directly rather than the settings object, because it runs
during preflight, before settings are constructed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

DEFAULT_FFMPEG_VERSION = "7.1"
"""Default pinned ``MAJOR.MINOR`` for both binaries. Patch releases are accepted."""

REQUIRED_BINARIES: tuple[str, ...] = ("ffmpeg", "ffprobe")

_BINARY_PATH_ENV_VARS = {"ffmpeg": "FFMPEG_BINARY", "ffprobe": "FFPROBE_BINARY"}


def pinned_version() -> str:
    """Return the required ``MAJOR.MINOR``, honouring ``FFMPEG_REQUIRED_VERSION``.

    Read per call rather than captured at import so tests and preflight observe the same
    environment the operator actually configured.
    """
    return os.environ.get("FFMPEG_REQUIRED_VERSION", "").strip() or DEFAULT_FFMPEG_VERSION


def resolve_binary(binary: str) -> str | None:
    """Resolve ``binary`` to an absolute path, preferring its explicit override.

    ``FFMPEG_BINARY`` / ``FFPROBE_BINARY`` win over ``PATH`` so a matched pair can be named
    directly. Returns ``None`` when the binary cannot be found at all.
    """
    override = os.environ.get(_BINARY_PATH_ENV_VARS[binary], "").strip()
    if override:
        path = Path(override)
        return override if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(binary)


_PROBE_TIMEOUT_SECONDS = 10
_VERSION_RE = re.compile(r"^(?:ffmpeg|ffprobe) version n?(?P<version>\d+\.\d+(?:\.\d+)?)")

VersionProbe = Callable[[str], str]


class MediaToolchainError(RuntimeError):
    """The media toolchain is absent or is not at the pinned version.

    A ``RuntimeError`` subclass so callers can catch either, per the S0.1.4 acceptance
    criteria which name ``RuntimeError``.
    """


def _probe_version(binary: str) -> str:
    """Return the reported ``MAJOR.MINOR[.PATCH]`` of ``binary``.

    Raises ``FileNotFoundError`` when the binary cannot be resolved, and
    ``MediaToolchainError`` when it runs but does not report a parseable version.
    """
    resolved = resolve_binary(binary)
    if resolved is None:
        message = f"{binary} is not installed or not on PATH"
        raise FileNotFoundError(message)

    completed = subprocess.run(
        [resolved, "-version"],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        message = f"{binary} -version exited {completed.returncode}"
        raise MediaToolchainError(message)

    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    match = _VERSION_RE.match(first_line.strip())
    if match is None:
        message = f"{binary} reported an unparseable version banner: {first_line.strip()!r}"
        raise MediaToolchainError(message)
    return match.group("version")


def _major_minor(version: str) -> str:
    parts = version.split(".")
    return ".".join(parts[:2])


def assert_media_toolchain(probe: VersionProbe = _probe_version) -> None:
    """Verify ffmpeg and ffprobe are present and at ``pinned_version()``.

    Returns ``None`` when every binary matches. Raises ``MediaToolchainError`` (a
    ``RuntimeError``) naming the binary, the expected version and the actual version
    otherwise. The message is a plain sentence, never a traceback, because it is surfaced
    to an operator at startup.

    ``probe`` is injectable so the failure paths are testable without a doctored ``PATH``.
    """
    for binary in REQUIRED_BINARIES:
        try:
            actual = probe(binary)
        except FileNotFoundError:
            message = (
                f"{binary} is required but was not found on PATH; "
                f"expected {binary} {pinned_version()}.x"
            )
            raise MediaToolchainError(message) from None

        if _major_minor(actual) != pinned_version():
            message = (
                f"{binary} version drift: expected {pinned_version()}.x, "
                f"actual {actual}. Refusing to start — a silent encoder change is an "
                f"unlogged output change (assembly.md S8)."
            )
            raise MediaToolchainError(message)
