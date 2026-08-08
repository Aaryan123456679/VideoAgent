"""Tests for `compute_request_fingerprint`. `providers.md` §2."""

from __future__ import annotations

from uuid import uuid4

from video_agent.providers.models import compute_request_fingerprint


def test_fingerprint_is_deterministic() -> None:
    job_id = uuid4()
    first = compute_request_fingerprint(
        job_id=job_id, shot_index=0, attempt_no=1, prompt_hash="abc123"
    )
    second = compute_request_fingerprint(
        job_id=job_id, shot_index=0, attempt_no=1, prompt_hash="abc123"
    )
    assert first == second


def test_fingerprint_changes_with_attempt_no() -> None:
    job_id = uuid4()
    first = compute_request_fingerprint(
        job_id=job_id, shot_index=0, attempt_no=1, prompt_hash="abc123"
    )
    second = compute_request_fingerprint(
        job_id=job_id, shot_index=0, attempt_no=2, prompt_hash="abc123"
    )
    assert first != second


def test_fingerprint_changes_with_frame_and_seed() -> None:
    job_id = uuid4()
    base = compute_request_fingerprint(
        job_id=job_id, shot_index=1, attempt_no=1, prompt_hash="abc123"
    )
    with_frame = compute_request_fingerprint(
        job_id=job_id, shot_index=1, attempt_no=1, prompt_hash="abc123", frame_id="frame-9"
    )
    with_seed = compute_request_fingerprint(
        job_id=job_id, shot_index=1, attempt_no=1, prompt_hash="abc123", seed=42
    )
    assert base != with_frame
    assert base != with_seed
    assert with_frame != with_seed
