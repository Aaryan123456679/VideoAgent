"""One comprehensive, credit-aware trial against the *real* Magic Hour API: three chained
10-second shots, submitted directly through `MagicHourProvider.generate()` (no job, no
Postgres, no graph — just the provider, exactly as `graph.nodes.generate_shot_node` calls it),
proving key rotation (`providers.magichour.RotatingApiKey`) actually happens against the real
API when the first credential runs out mid-sequence.

Shot count is a CLI argument (default 3) — at ~240 credits per 10s/480p shot, size it to
comfortably fit the current combined balance across both configured keys.

Usage: uv run python scripts/real_provider_trial.py [shot_count]
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path
from typing import Any, Final, Literal, cast
from uuid import uuid4

from video_agent.config.settings import get_settings
from video_agent.gateway.models import ArtifactRef
from video_agent.graph.frame_extraction import find_last_usable_frame
from video_agent.persistence.objects import S3ObjectTransport, create_s3_client
from video_agent.providers.artifact_store import S3ArtifactStore
from video_agent.providers.magichour import build_magichour_provider
from video_agent.providers.models import ShotRequest, compute_request_fingerprint

SHOT_PROMPTS = [
    "a violinist plays on a rooftop at dawn as the city wakes below, wide establishing shot",
    "the violinist closes their eyes, fully absorbed in the music, medium close-up",
    "the sun rises fully over the skyline behind the violinist, warm golden light, wide shot",
]
DURATION_S = 10.0
RESOLUTION: Final[Literal["480p", "720p", "1080p"]] = "480p"
TIMEOUT_S = 3600.0
"""Generous: real queue time on a free-tier account has been observed to exceed 40 minutes."""


async def main() -> None:
    requested = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    shot_prompts = SHOT_PROMPTS[: min(requested, len(SHOT_PROMPTS))]

    settings = get_settings()
    job_id = uuid4()

    object_transport = S3ObjectTransport(create_s3_client(settings), settings.ARTIFACT_BUCKET)
    artifacts = S3ArtifactStore(transport=object_transport)
    provider, http_client = build_magichour_provider(settings, artifacts=artifacts)
    rotator = provider.key_rotator
    print(  # noqa: T201
        f"job_id={job_id}  keys_configured={len(settings.magichour_api_keys())}  "
        f"shots={len(shot_prompts)}  model={settings.MAGICHOUR_MODEL}"
    )

    conditioning: ArtifactRef | None = None
    try:
        for shot_index, prompt in enumerate(shot_prompts):
            key_before = rotator.index if rotator is not None else 0
            fingerprint = compute_request_fingerprint(
                job_id=job_id,
                shot_index=shot_index,
                attempt_no=1,
                prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                frame_id=str(conditioning.artifact_id) if conditioning is not None else None,
                seed=None,
            )
            req = ShotRequest(
                job_id=job_id,
                shot_index=shot_index,
                attempt_no=1,
                prompt=prompt,
                conditioning_frame=conditioning,
                duration_s=DURATION_S,
                resolution=RESOLUTION,
                request_fingerprint=fingerprint,
                timeout_s=TIMEOUT_S,
            )
            print(f"--- shot {shot_index}: submitting (key index before = {key_before}) ---")  # noqa: T201
            result = await provider.generate(req, ctx=cast(Any, None))
            key_after = rotator.index if rotator is not None else 0
            rotated = " <-- ROTATED KEYS" if key_after != key_before else ""
            print(  # noqa: T201
                f"shot {shot_index}: cost_usd={result.cost_usd} "
                f"credits_charged={result.credits_charged} key_index_after={key_after}{rotated}"
            )
            print(f"shot {shot_index}: clip artifact_id={result.clip.artifact_id}")  # noqa: T201

            if shot_index < len(shot_prompts) - 1:
                clip_bytes = await artifacts.read(result.clip)
                with tempfile.TemporaryDirectory(prefix=f"real-trial-{job_id}-") as scratch:
                    clip_path = Path(scratch) / "clip.mp4"
                    clip_path.write_bytes(clip_bytes)
                    frame_path = Path(scratch) / "frame.png"
                    found = await asyncio.to_thread(
                        find_last_usable_frame, clip_path, frame_path
                    )
                    if not found:
                        print(f"shot {shot_index}: no usable frame; next shot is text-only")  # noqa: T201
                        conditioning = None
                        continue
                    png_bytes = await asyncio.to_thread(frame_path.read_bytes)
                conditioning = await artifacts.write(content_type="image/png", data=png_bytes)
                print(f"shot {shot_index}: extracted continuity frame {conditioning.artifact_id}")  # noqa: T201

        print("--- all shots complete ---")  # noqa: T201
    finally:
        await http_client.aclose()
        await object_transport.aclose()


if __name__ == "__main__":
    asyncio.run(main())
