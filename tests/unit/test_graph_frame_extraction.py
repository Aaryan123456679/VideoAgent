"""`frame_extraction.py` against real ffmpeg — synthetic clips, not golden media fixtures.

Skipped wholesale when ffmpeg is not on `PATH`, mirroring `media_toolchain.py`'s own
`resolve_binary` contract rather than asserting a hard dependency CI may not provide. Every
other T2.3 test avoids this dependency entirely by monkeypatching `find_last_usable_frame`;
this file is the one place the real subprocess behaviour is exercised.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from video_agent.graph.frame_extraction import (
    FrameExtractionError,
    extract_last_frame,
    find_last_usable_frame,
    is_uniform_frame,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _make_clip(path: Path, *, color: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    if color == "testsrc":
        source = "testsrc=duration=2:size=64x64:rate=10"
    else:
        source = f"color=c={color}:duration=2:size=64x64:rate=10"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", source, "-pix_fmt", "yuv420p", str(path)],
        capture_output=True,
        timeout=30,
        check=True,
    )


def test_extract_last_frame_writes_a_decodable_png(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    _make_clip(clip, color="testsrc")
    frame = tmp_path / "frame.png"
    extract_last_frame(clip, frame)
    assert frame.is_file()
    assert frame.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_uniform_frame_has_near_zero_variance_and_colour_frame_does_not(tmp_path: Path) -> None:
    color_clip = tmp_path / "color.mp4"
    black_clip = tmp_path / "black.mp4"
    _make_clip(color_clip, color="testsrc")
    _make_clip(black_clip, color="black")

    color_frame = tmp_path / "color.png"
    black_frame = tmp_path / "black.png"
    extract_last_frame(color_clip, color_frame)
    extract_last_frame(black_clip, black_frame)

    assert is_uniform_frame(black_frame) is True
    assert is_uniform_frame(color_frame) is False


def test_find_last_usable_frame_succeeds_for_colour_content(tmp_path: Path) -> None:
    clip = tmp_path / "color.mp4"
    _make_clip(clip, color="testsrc")
    output = tmp_path / "out.png"
    assert find_last_usable_frame(clip, output) is True
    assert output.is_file()


def test_find_last_usable_frame_gives_up_on_an_entirely_black_clip(tmp_path: Path) -> None:
    clip = tmp_path / "black.mp4"
    _make_clip(clip, color="black")
    output = tmp_path / "out.png"
    assert find_last_usable_frame(clip, output) is False
    assert not output.exists()


def test_extraction_from_a_nonexistent_clip_raises_frame_extraction_error(tmp_path: Path) -> None:
    with pytest.raises(FrameExtractionError):
        extract_last_frame(tmp_path / "does-not-exist.mp4", tmp_path / "out.png")
