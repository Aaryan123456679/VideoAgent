"""`MockVideoProvider`: a real `VideoProvider` that never leaves the machine.

Not a provider name — a testing tool, no different in kind from `tests.providers_doubles.
FakeProvider` except that this one produces genuinely playable MP4s via ffmpeg, so the *whole*
pipeline downstream of `generate()` — frame extraction, uniform-frame rejection, assembly,
concatenation — is exercised for real. Only the pixels are synthetic. Useful whenever a real
account is slow, out of credits, or simply not worth spending against for a wiring check.

**Never wired into a real deployment automatically.** Nothing in `src/` constructs this outside
of what a caller explicitly chooses; it satisfies `VideoProvider` structurally so it drops into
a `ProviderRegistry` the same way a real adapter would, and nothing else.

**Turning an image into a video, literally.** Shot 0 has no conditioning frame, so it gets a
generated colour card. Every later shot has one, and rather than a picture-in-picture overlay,
the conditioning frame *is* the clip's background for its full duration — the most literal
reading of "the image, as a video" — with the job id and shot/attempt numbers burned in as text,
which is what makes chaining visible when four of these clips are stitched together: each shot
starts on the previous shot's own frame.

**Uniform-frame safety.** A flat colour card is exactly the "all-uniform" shape `graph.
frame_extraction`'s variance check rejects, so the id text is not decoration — without it, every
mock shot 0 would look identical to a blank frame and get discarded before it ever mattered.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from video_agent.assembly.media_toolchain import MediaToolchainError, resolve_binary
from video_agent.providers.models import (
    Capability,
    ProviderHealth,
    ProviderProfile,
    ShotResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from video_agent.harness.context import NodeContext
    from video_agent.providers.models import ArtifactStore, ShotRequest

__all__ = ["MockVideoProvider"]

_TIMEOUT_S: Final = 30.0
_FPS: Final = 24
_RESOLUTIONS: Final[dict[str, tuple[int, int]]] = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}

MOCK_PROVIDER_KEY: Final = "mock"


def _ffmpeg_path() -> str:
    resolved = resolve_binary("ffmpeg")
    if resolved is None:
        message = "ffmpeg is required for the mock provider but was not found on PATH"
        raise MediaToolchainError(message)
    return resolved


def _run(argv: list[str]) -> None:
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        message = f"mock provider's {argv[0]} did not finish within {_TIMEOUT_S}s"
        raise RuntimeError(message) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()[-2000:]
        message = f"mock provider's {argv[0]} exited {completed.returncode}: {stderr}"
        raise RuntimeError(message)


def _colour_for(job_id: str) -> str:
    """A stable-per-job, distinguishable-across-jobs hex colour, so a run's four shot-0 cards
    (there is only ever one, but a fresh job looks different from the last one) aren't identical
    by construction."""
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    return f"0x{digest[:6]}"


def _label(*, job_id: str, shot_index: int, attempt_no: int) -> str:
    """A real newline byte, not `\\n` — drawtext's line break is the literal byte, and a
    filtergraph-escaped `\\n` survives ffmpeg's own option parsing as a bare `n`."""
    text = f"{job_id}\nshot {shot_index}  attempt {attempt_no}"
    return text.replace(":", "\\:").replace("'", "")


def _drawtext(label: str) -> str:
    return (
        f"drawtext=text='{label}':fontcolor=white:fontsize=28:"
        "x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.6"
    )


def _render_clip(
    *,
    job_id: str,
    shot_index: int,
    attempt_no: int,
    duration_s: float,
    resolution: str,
    conditioning_png: bytes | None,
) -> bytes:
    """Blocking; the caller runs this in a thread. Never inline in the event loop."""
    width, height = _RESOLUTIONS.get(resolution, _RESOLUTIONS["720p"])
    label = _label(job_id=job_id, shot_index=shot_index, attempt_no=attempt_no)
    ffmpeg = _ffmpeg_path()
    with tempfile.TemporaryDirectory(prefix=f"mock-provider-{job_id}-") as scratch:
        out_path = Path(scratch) / "clip.mp4"
        if conditioning_png is not None:
            frame_path = Path(scratch) / "frame.png"
            frame_path.write_bytes(conditioning_png)
            argv = [
                ffmpeg,
                "-y",
                "-loop",
                "1",
                "-i",
                str(frame_path),
                "-t",
                str(duration_s),
                "-vf",
                f"scale={width}:{height},{_drawtext(label)}",
                "-r",
                str(_FPS),
                "-pix_fmt",
                "yuv420p",
                str(out_path),
            ]
        else:
            colour = _colour_for(job_id)
            argv = [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s={width}x{height}:d={duration_s}:r={_FPS}",
                "-vf",
                _drawtext(label),
                "-pix_fmt",
                "yuv420p",
                str(out_path),
            ]
        _run(argv)
        return out_path.read_bytes()


@dataclass
class MockVideoProvider:
    """A `VideoProvider` that renders real, playable, tiny MP4s locally — no network call, no
    credits, no wait. Trial/testing only; never wired into a real deployment implicitly."""

    artifacts: ArtifactStore
    profile: ProviderProfile = field(
        default_factory=lambda: ProviderProfile(
            provider_key=MOCK_PROVIDER_KEY,
            capabilities=frozenset(
                {
                    Capability.IMAGE_CONDITIONING,
                    Capability.ASPECT_16_9,
                    Capability.RES_480P,
                    Capability.RES_720P,
                    Capability.RES_1080P,
                    Capability.DURATION_10S,
                    Capability.ASYNC_POLL,
                }
            ),
            min_duration_s=1.0,
            max_duration_s=60.0,
            max_resolution="1080p",
            max_prompt_chars=10_000,
            cost_unit="usd",
            price_per_second=Decimal("0"),
            typical_latency_s=0.5,
        )
    )
    _lookups: dict[str, ShotResult] = field(default_factory=dict, init=False)

    async def generate(self, req: ShotRequest, *, ctx: NodeContext) -> ShotResult:
        del ctx  # no session at this layer, same rule as every other adapter
        conditioning_png = (
            await self.artifacts.read(req.conditioning_frame)
            if req.conditioning_frame is not None
            else None
        )
        width, height = _RESOLUTIONS.get(req.resolution, _RESOLUTIONS["720p"])
        clip_bytes = await asyncio.to_thread(
            _render_clip,
            job_id=str(req.job_id),
            shot_index=req.shot_index,
            attempt_no=req.attempt_no,
            duration_s=req.duration_s,
            resolution=req.resolution,
            conditioning_png=conditioning_png,
        )
        clip_ref = await self.artifacts.write(content_type="video/mp4", data=clip_bytes)
        result = ShotResult(
            clip=clip_ref,
            provider_key=self.profile.provider_key,
            provider_model="mock-v1",
            provider_project_id=f"mock-{uuid4()}",
            seed_used=None,
            duration_s=req.duration_s,
            resolution=req.resolution,
            fps=_FPS,
            width=width,
            height=height,
            cost_usd=Decimal("0"),
            credits_charged=None,
            cost_is_final=True,
            latency_ms=0,
        )
        self._lookups[req.request_fingerprint] = result
        return result

    async def lookup(self, request_fingerprint: str) -> ShotResult | None:
        return self._lookups.get(request_fingerprint)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_key=self.profile.provider_key, healthy=True)

    async def handle_webhook(self, *, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        del raw_body, headers
        return False
