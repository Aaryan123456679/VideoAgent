"""Lightweight tests for `StoryPlan`/`ContinuityBible` validators. `planning.md` §6."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from video_agent.planning.models import TOTAL_DURATION_S, Beat, BeatKind, CameraMove, StoryPlan


def _beat(index: int, kind: BeatKind, duration: float = 10.0) -> Beat:
    return Beat(
        index=index,
        kind=kind,
        action="a" * 25,
        camera_move=CameraMove.STATIC,
        duration_s=duration,
    )


def _valid_beats() -> list[Beat]:
    return [
        _beat(0, BeatKind.SETUP),
        _beat(1, BeatKind.DEVELOPMENT),
        _beat(2, BeatKind.TURN),
        _beat(3, BeatKind.RESOLUTION),
    ]


def test_valid_plan_accepted() -> None:
    plan = StoryPlan(
        job_id=uuid4(),
        logline="A test logline",
        beats=_valid_beats(),
        model_alias="reasoning-high",
        prompt_version="v1",
        created_at=datetime.now(UTC),
    )
    assert plan.total_duration_s == TOTAL_DURATION_S


def test_wrong_order_rejected() -> None:
    beats = _valid_beats()
    beats[0], beats[1] = beats[1], beats[0]
    with pytest.raises(ValidationError, match=re.escape("indexed 0..3 in order")):
        StoryPlan(
            job_id=uuid4(),
            logline="x",
            beats=beats,
            model_alias="reasoning-high",
            prompt_version="v1",
            created_at=datetime.now(UTC),
        )


def test_duration_must_sum_to_exactly_40() -> None:
    beats = _valid_beats()
    with pytest.raises(ValidationError):
        beats[0] = _beat(0, BeatKind.SETUP, duration=9.9)


def test_wrong_beat_count_rejected() -> None:
    with pytest.raises(ValidationError):
        StoryPlan(
            job_id=uuid4(),
            logline="x",
            beats=_valid_beats()[:3],
            model_alias="reasoning-high",
            prompt_version="v1",
            created_at=datetime.now(UTC),
        )
