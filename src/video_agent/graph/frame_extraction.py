"""Last-decodable-frame extraction and uniform-frame rejection. `assembly.md` §3, `[D-44]`/`[D-45]`.

`extract_final_frame_node` (`graph/nodes.py`, T2.3) is the only caller. This module owns the
ffmpeg invocations and nothing else — no DB, no object store, no `NodeContext` — mirroring
`assembly/media_toolchain.py`'s own separation of "the toolchain" from "what uses it".

**Safety.** Every subprocess call is an argv list built entirely from already-validated paths
and numeric literals — no user or model text, no shell. `media_toolchain.resolve_binary` is the
only thing that turns a binary name into the path actually executed, so a developer machine with
more than one `ffmpeg` on `PATH` cannot silently run one for extraction and pass version-pin
validation against another.

**Extraction.** `assembly.md` §3 asks for the *last decodable* frame, not a fixed timestamp — a
truncated tail must not yield a black anchor — and a lossless PNG at native resolution, no
resize, no crop, no colour transform. `ffmpeg -sseof -N -i <clip> -vsync 0 -frames:v 1 -f image2
<out.png>` decodes backward from `N` seconds before end-of-stream and keeps the first frame it
can decode there; `-vsync 0` passes it through untouched rather than duplicating or dropping to
match an output frame rate that is irrelevant to a single still.

**Uniform-frame rejection.** `[D-45]`: reject an all-black or all-uniform frame (variance below
a floor), stepping back to the last frame that passes. There is no image-decoding dependency in
this project's canonical stack (`pyproject.toml`'s `[project.dependencies]` — no Pillow, no
numpy), so the check reuses ffmpeg itself: `-vf format=gray -f rawvideo -pix_fmt gray8` on the
already-extracted PNG emits one grayscale byte per pixel with no container framing, and
`statistics.pvariance` over that byte string is the frame's luma variance without decoding a
single pixel in Python. Measured empirically against a solid-colour test frame (variance
`0.0`) and a synthetic noise/colour-bar test frame (variance in the thousands), `VARIANCE_FLOOR`
sits comfortably below real content and at real-zero for a truly flat source.

**Step-back.** `find_last_usable_frame` retries at `1s, 2s, 3s` before end-of-stream — bounded,
not exhaustive, per this task's own instruction not to over-engineer a cheap check. A clip whose
final three seconds are *all* uniform is treated the same as "no anchor" (`assembly.md` §8):
`degraded=true`, and the pipeline continues rather than blocking on a chaining aid.
"""

from __future__ import annotations

import statistics
import subprocess
from pathlib import Path
from typing import Final

from video_agent.assembly.media_toolchain import MediaToolchainError, resolve_binary

__all__ = [
    "STEP_BACK_ATTEMPTS",
    "VARIANCE_FLOOR",
    "FrameExtractionError",
    "extract_last_frame",
    "find_last_usable_frame",
    "frame_variance",
    "is_uniform_frame",
]

_EXTRACT_TIMEOUT_S: Final = 30.0
_PROBE_TIMEOUT_S: Final = 15.0

VARIANCE_FLOOR: Final = 4.0
"""Below this population variance of grayscale luma, a frame counts as uniform/blank `[D-45]`.

Two orders of magnitude below the thousands measured for ordinary content and comfortably above
the `0.0` measured for a truly flat source — a wide, safe margin without exhaustively tuning a
floor `assembly.md` itself calls "a cheap check"."""

STEP_BACK_ATTEMPTS: Final = 3
"""How many whole seconds before end-of-stream `find_last_usable_frame` will try. Bounded, per
`assembly.md` §8's "step back frame by frame to the last usable one" — a 10s beat has ample
room for three one-second steps without risking seeking before the clip starts."""


class FrameExtractionError(RuntimeError):
    """ffmpeg could not produce or analyse a frame. Caught by `find_last_usable_frame`, which
    treats it the same as a uniform frame — one failed attempt, not a fatal one."""


def _ffmpeg_path() -> str:
    resolved = resolve_binary("ffmpeg")
    if resolved is None:
        message = "ffmpeg is required for frame extraction but was not found on PATH"
        raise MediaToolchainError(message)
    return resolved


def _run(argv: list[str], *, timeout_s: float) -> bytes:
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        message = f"{argv[0]} did not finish within {timeout_s}s"
        raise FrameExtractionError(message) from exc
    except OSError as exc:
        detail = exc.strerror or str(exc)
        message = f"{argv[0]} could not be executed: {detail}"
        raise FrameExtractionError(message) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()[-2000:]
        message = f"{argv[0]} exited {completed.returncode}: {stderr}"
        raise FrameExtractionError(message)
    return completed.stdout


def extract_last_frame(
    clip_path: Path,
    output_path: Path,
    *,
    seek_seconds_before_end: float = 1.0,
    timeout_s: float = _EXTRACT_TIMEOUT_S,
) -> None:
    """Write the clip's last decodable frame, `seek_seconds_before_end` from end-of-stream, as a
    lossless PNG at `output_path`. `assembly.md` §3: native resolution, unmodified.
    """
    ffmpeg = _ffmpeg_path()
    argv = [
        ffmpeg,
        "-y",
        "-sseof",
        f"-{seek_seconds_before_end}",
        "-i",
        str(clip_path),
        "-vsync",
        "0",
        "-frames:v",
        "1",
        "-f",
        "image2",
        str(output_path),
    ]
    _run(argv, timeout_s=timeout_s)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        message = f"ffmpeg exited 0 but produced no frame at {output_path}"
        raise FrameExtractionError(message)


def frame_variance(png_path: Path, *, timeout_s: float = _PROBE_TIMEOUT_S) -> float:
    """Population variance of `png_path`'s grayscale luma. Nearly `0.0` for a solid colour."""
    ffmpeg = _ffmpeg_path()
    argv = [
        ffmpeg,
        "-y",
        "-i",
        str(png_path),
        "-vf",
        "format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray8",
        "-",
    ]
    raw = _run(argv, timeout_s=timeout_s)
    if not raw:
        message = f"ffmpeg produced no pixel data while probing {png_path}"
        raise FrameExtractionError(message)
    return statistics.pvariance(raw)


def is_uniform_frame(png_path: Path, *, variance_floor: float = VARIANCE_FLOOR) -> bool:
    """Whether `png_path` is unusable as a continuity anchor — all-black or all-uniform."""
    return frame_variance(png_path) < variance_floor


def find_last_usable_frame(
    clip_path: Path,
    output_path: Path,
    *,
    max_attempts: int = STEP_BACK_ATTEMPTS,
) -> bool:
    """Write the last non-uniform frame to `output_path`, stepping back up to `max_attempts`
    whole seconds from end-of-stream. `assembly.md` §8.

    Returns `True` and leaves a usable PNG at `output_path` on success. Returns `False` and
    removes any partial file on failure — extraction errors and uniform frames are both just
    "this attempt did not produce a usable anchor", never a reason to raise past this function:
    `assembly.md` §8 treats total extraction failure and uniform-frame rejection identically
    (continue without an anchor, flag degraded), so the caller needs one signal, not two.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            extract_last_frame(clip_path, output_path, seek_seconds_before_end=float(attempt))
            if not is_uniform_frame(output_path):
                return True
        except FrameExtractionError:
            continue
    output_path.unlink(missing_ok=True)
    return False
