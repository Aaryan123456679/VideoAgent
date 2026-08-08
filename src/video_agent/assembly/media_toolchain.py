"""Startup assertion for the system media toolchain.

``assembly.md`` S7 pins the ffmpeg/ffprobe version in the image and asserts it at startup;
S8 makes version drift a refuse-to-start condition, because a silent encoder change is an
unlogged output change. This module owns that assertion and nothing else — all actual media
manipulation stays in the assembly wrapper (a later task).

The container image must install exactly the pinned version; if the Dockerfile and the pin
disagree, the application refuses to start, which is the intended behaviour rather than a bug.

Both the pin and the binary locations are overridable by environment variable. Developer
machines routinely carry more than one ffmpeg — a standalone build in ``~/.local/bin``
shadowing a Homebrew install is common, and because such builds often ship ``ffmpeg``
without ``ffprobe``, bare ``PATH`` resolution can silently pair binaries from two different
releases. Two rules keep that from being reachable by accident:

* An override must be an **absolute** path. A bare name such as ``FFMPEG_BINARY=ffmpeg``
  would be checked for existence against the current working directory but executed via a
  ``PATH`` search, so the file validated and the file run need not be the same one. That is
  the exact silent mismatch this module exists to prevent, so it is a configuration error
  rather than a fallback.
* Both binaries must report the **same** release, not merely two releases that happen to
  share the pinned prefix. ``.env.example`` states this invariant; it is enforced here.

This module reads ``os.environ`` directly rather than the settings object, because it runs
during preflight, before settings are constructed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

DEFAULT_FFMPEG_VERSION = "7.1"
"""Default version pin. Must equal the ``FFMPEG_REQUIRED_VERSION`` default in
``.env.example``; ``tests/contract/test_env_example_contract.py`` asserts that."""

REQUIRED_BINARIES: tuple[str, ...] = ("ffmpeg", "ffprobe")

VERSION_PIN_ENV_VAR = "FFMPEG_REQUIRED_VERSION"
BINARY_PATH_ENV_VARS = {"ffmpeg": "FFMPEG_BINARY", "ffprobe": "FFPROBE_BINARY"}

_PROBE_TIMEOUT_SECONDS = 10
_VERSION_RE = re.compile(r"^(?:ffmpeg|ffprobe) version n?(?P<version>\d+\.\d+(?:\.\d+)?)")
_PIN_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

VersionProbe = Callable[[str], str]


class MediaToolchainError(RuntimeError):
    """The media toolchain is absent, misconfigured, or not at the pinned version.

    A ``RuntimeError`` subclass so callers can catch either, per the S0.1.4 acceptance
    criteria which name ``RuntimeError``.
    """


def pinned_version() -> str:
    """Return the required version, honouring ``FFMPEG_REQUIRED_VERSION``.

    Read per call rather than captured at import so tests and preflight observe the same
    environment the operator actually configured.

    The pin may be ``MAJOR.MINOR`` (patch releases accepted) or ``MAJOR.MINOR.PATCH``
    (exact). Anything else is a configuration error: a pin nobody can satisfy would brick
    startup with a message that reads like a version mismatch rather than a typo.
    """
    raw = os.environ.get(VERSION_PIN_ENV_VAR, "").strip() or DEFAULT_FFMPEG_VERSION
    if not _PIN_RE.match(raw):
        message = (
            f"{VERSION_PIN_ENV_VAR}={raw!r} is not a valid version pin. Expected "
            f"MAJOR.MINOR (for example 7.1, patch releases accepted) or MAJOR.MINOR.PATCH "
            f"(for example 7.1.1, exact). Leave it empty to use the default "
            f"{DEFAULT_FFMPEG_VERSION}."
        )
        raise MediaToolchainError(message)
    return raw


def describe_pin(pin: str) -> str:
    """Render a pin the way an operator reads it: ``7.1.x`` for MAJOR.MINOR, ``7.1.1`` exact."""
    return f"{pin}.x" if pin.count(".") == 1 else pin


def _matches_pin(actual: str, pin: str) -> bool:
    """Compare at the precision the pin asks for, so a three-component pin is honoured.

    Comparing ``_major_minor(actual)`` against a raw ``7.1.1`` pin can never match, which
    would turn a *more precise* configuration into an unstartable one.
    """
    depth = pin.count(".") + 1
    return ".".join(actual.split(".")[:depth]) == pin


def resolve_binary(binary: str) -> str | None:
    """Resolve ``binary`` to an absolute path, preferring its explicit override.

    Returns ``None`` only when no override is set and the binary is not on ``PATH``.
    Raises ``MediaToolchainError`` when an override is set but unusable — a set-but-wrong
    override is a configuration error, never a reason to fall back to ``PATH`` and start
    against a binary the operator did not choose.
    """
    env_var = BINARY_PATH_ENV_VARS[binary]
    override = os.environ.get(env_var, "").strip()

    if not override:
        found = shutil.which(binary)
        return str(Path(found).absolute()) if found is not None else None

    path = Path(override)
    if not path.is_absolute():
        message = (
            f"{env_var}={override!r} must be an absolute path. A bare or relative name is "
            f"checked for existence against the current working directory but executed by a "
            f"PATH search, so the file validated and the file run need not be the same one. "
            f"Leave {env_var} empty to use PATH, or give the full path."
        )
        raise MediaToolchainError(message)
    if not path.is_file() or not os.access(path, os.X_OK):
        message = (
            f"{env_var}={override!r} is not an executable file. Refusing to fall back to "
            f"PATH: an override exists to remove exactly that ambiguity."
        )
        raise MediaToolchainError(message)
    return str(path)


def _probe_version(binary: str) -> str:
    """Return the reported ``MAJOR.MINOR[.PATCH]`` of ``binary``.

    Raises ``FileNotFoundError`` when the binary cannot be resolved, and
    ``MediaToolchainError`` for every other failure — a hung binary, one that cannot be
    executed, a non-zero exit or an unparseable banner. None of those may reach the operator
    as a traceback.
    """
    resolved = resolve_binary(binary)
    if resolved is None:
        message = f"{binary} is not installed or not on PATH"
        raise FileNotFoundError(message)

    try:
        completed = subprocess.run(
            [resolved, "-version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        message = (
            f"{binary} at {resolved} did not answer `-version` within "
            f"{_PROBE_TIMEOUT_SECONDS}s. Refusing to start rather than hanging preflight."
        )
        raise MediaToolchainError(message) from None
    except OSError as exc:
        detail = exc.strerror or str(exc)
        message = f"{binary} at {resolved} could not be executed: {detail}."
        raise MediaToolchainError(message) from None

    if completed.returncode != 0:
        message = f"{binary} -version exited {completed.returncode}"
        raise MediaToolchainError(message)

    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    match = _VERSION_RE.match(first_line.strip())
    if match is None:
        message = f"{binary} reported an unparseable version banner: {first_line.strip()!r}"
        raise MediaToolchainError(message)
    return match.group("version")


def assert_media_toolchain(probe: VersionProbe = _probe_version) -> None:
    """Verify ffmpeg and ffprobe are present, at ``pinned_version()``, and the same release.

    Returns ``None`` when every check passes. Raises ``MediaToolchainError`` (a
    ``RuntimeError``) naming the binary, the expected version and the actual version
    otherwise. The message is a plain sentence, never a traceback, because it is surfaced
    to an operator at startup.

    ``probe`` is injectable so the failure paths are testable without a doctored ``PATH``.
    """
    pin = pinned_version()
    reported: dict[str, str] = {}

    for binary in REQUIRED_BINARIES:
        try:
            reported[binary] = probe(binary)
        except FileNotFoundError:
            message = (
                f"{binary} is required but was not found on PATH; "
                f"expected {binary} {describe_pin(pin)}"
            )
            raise MediaToolchainError(message) from None

    for binary, actual in reported.items():
        if not _matches_pin(actual, pin):
            message = (
                f"{binary} version drift: expected {describe_pin(pin)}, "
                f"actual {actual}. Refusing to start — a silent encoder change is an "
                f"unlogged output change (assembly.md S8)."
            )
            raise MediaToolchainError(message)

    distinct = set(reported.values())
    if len(distinct) > 1:
        pairs = ", ".join(f"{name} {version}" for name, version in reported.items())
        message = (
            f"media toolchain is not a matched pair: {pairs}. Both binaries must report the "
            f"same release — a patch-level encoder difference is still an unlogged output "
            f"change (assembly.md S8). Set "
            f"{' and '.join(BINARY_PATH_ENV_VARS[b] for b in REQUIRED_BINARIES)} to the "
            f"absolute paths of one matched install."
        )
        raise MediaToolchainError(message)


# ---------------------------------------------------------------------------------------------
# T2.4 — normalize / concatenate / thumbnail. `assembly.md` §4.
#
# These three functions are the only place this module (or `graph.nodes.assemble_node`, their
# one caller) actually shells out to manipulate media, as opposed to merely asserting the
# toolchain that will. Same safety rules as `assert_media_toolchain`/`frame_extraction.py`:
# argv-list only, never `shell=True`, and every binary path comes from `resolve_binary` — never
# a bare name that could resolve differently from the one version-pin-checked at startup.
# ---------------------------------------------------------------------------------------------

CANONICAL_WIDTH: Final = 1280
CANONICAL_HEIGHT: Final = 720
CANONICAL_FPS: Final = 24
CANONICAL_VIDEO_CODEC: Final = "libx264"
CANONICAL_VIDEO_PROFILE: Final = "high"
CANONICAL_PIXEL_FORMAT: Final = "yuv420p"
"""`assembly.md` §4.1's canonical delivery profile (`[D-46]`): MP4 / H.264 High `yuv420p` at
1280x720, 24fps CFR, BT.709 limited range, no audio unless a music bed is mixed in (v1 mixes
none — see `graph.nodes.assemble_node`'s docstring, `[D-69]`), `faststart`.

Hardcoded rather than read from the configured target-resolution setting: `graph.deps.GraphDeps`
carries no settings object — the same documented v1 gap `graph/nodes.py`'s `_PROVIDER_TIMEOUT_S`
names — and every shot is already requested from a provider at `Capability.RES_720P`
(`providers.models.ShotRequest.resolution` defaults to `"720p"`), so 1280x720 is not a guess, it
is the resolution every accepted clip is already generated at. Threading `Settings` through the
graph so this tracks that setting at runtime is out of scope for T2.4."""

_NORMALIZE_TIMEOUT_S: Final = 60.0
_CONCAT_TIMEOUT_S: Final = 30.0
_THUMBNAIL_TIMEOUT_S: Final = 15.0


class AssemblyError(RuntimeError):
    """One ffmpeg invocation in the assemble pipeline failed — a bad exit, a timeout, or an
    exit-0 with no output file. Distinct from `MediaToolchainError`, which is about the
    toolchain's presence or version, never about one call's outcome."""


def _ffmpeg_path() -> str:
    resolved = resolve_binary("ffmpeg")
    if resolved is None:
        message = "ffmpeg is required for assembly but was not found on PATH"
        raise MediaToolchainError(message)
    return resolved


def _run(argv: list[str], *, timeout_s: float) -> None:
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        message = f"{argv[0]} did not finish within {timeout_s}s"
        raise AssemblyError(message) from exc
    except OSError as exc:
        detail = exc.strerror or str(exc)
        message = f"{argv[0]} could not be executed: {detail}"
        raise AssemblyError(message) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()[-2000:]
        message = f"{argv[0]} exited {completed.returncode}: {stderr}"
        raise AssemblyError(message)


def _require_output(output_path: Path, *, what: str) -> None:
    if not output_path.is_file() or output_path.stat().st_size == 0:
        message = f"ffmpeg exited 0 but produced no {what} at {output_path}"
        raise AssemblyError(message)


def normalize_clip(
    input_path: Path, output_path: Path, *, timeout_s: float = _NORMALIZE_TIMEOUT_S
) -> None:
    """Normalize one clip to the canonical profile so the concat that follows is a pure stream
    copy. `assembly.md` §4.1: "normalise every clip to one canonical profile, then concatenate."

    Scales-and-pads to preserve aspect ratio rather than a plain `scale` that could distort a
    provider's output, retimes to `CANONICAL_FPS` CFR, and strips audio unconditionally — v1
    never mixes in a music bed at this stage (`[D-69]`; see `assemble_node`).
    """
    ffmpeg = _ffmpeg_path()
    video_filter = (
        f"scale={CANONICAL_WIDTH}:{CANONICAL_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={CANONICAL_WIDTH}:{CANONICAL_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        f"fps={CANONICAL_FPS}"
    )
    argv = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-vf",
        video_filter,
        "-c:v",
        CANONICAL_VIDEO_CODEC,
        "-profile:v",
        CANONICAL_VIDEO_PROFILE,
        "-pix_fmt",
        CANONICAL_PIXEL_FORMAT,
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-color_range",
        "tv",
        "-an",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    _run(argv, timeout_s=timeout_s)
    _require_output(output_path, what="normalized clip")


def concat_clips(
    clip_paths: Sequence[Path], output_path: Path, *, timeout_s: float = _CONCAT_TIMEOUT_S
) -> None:
    """Concatenate already-normalized clips, in order, by stream copy. `[D-47]`: hard cuts
    only — there is no crossfade parameter here and none is ever added while that decision
    holds.

    Uses the concat demuxer over a generated file list rather than the concat *filter*, because
    every input already shares the canonical profile — nothing to filter, only to copy.
    `clip_paths` are internally generated scratch-directory paths, never user or model text
    (`assembly.md` §6), so writing them into the list file carries no injection risk.
    """
    if not clip_paths:
        message = "concat_clips called with zero clips"
        raise AssemblyError(message)
    ffmpeg = _ffmpeg_path()
    list_path = output_path.parent / f"{output_path.stem}-concat-list.txt"
    list_path.write_text("".join(f"file '{clip.resolve()}'\n" for clip in clip_paths))
    argv = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    _run(argv, timeout_s=timeout_s)
    _require_output(output_path, what="concatenated video")


def build_thumbnail(
    png_path: Path, output_path: Path, *, timeout_s: float = _THUMBNAIL_TIMEOUT_S
) -> None:
    """Re-encode an already-extracted continuity-frame PNG into the canonical JPEG thumbnail.

    `[D-49]`: the thumbnail is the highest-scoring accepted shot's frame, and `assemble_node`
    reuses the PNG `extract_final_frame_node` already produced for that shot rather than
    re-extracting one from the clip — this function only re-encodes and fits it to the
    canonical geometry, it never touches a video stream.
    """
    ffmpeg = _ffmpeg_path()
    video_filter = (
        f"scale={CANONICAL_WIDTH}:{CANONICAL_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={CANONICAL_WIDTH}:{CANONICAL_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
    )
    argv = [
        ffmpeg,
        "-y",
        "-i",
        str(png_path),
        "-vf",
        video_filter,
        "-frames:v",
        "1",
        str(output_path),
    ]
    _run(argv, timeout_s=timeout_s)
    _require_output(output_path, what="thumbnail")
