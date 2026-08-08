"""`S0.7.7` — the prompt registry client and deterministic per-job canary assignment.

The assignment tests are the interesting ones. `[D-20]` requires that a single job never mixes
prompt versions across its shots, which rules out a per-call random draw even though a per-call
draw would hit the 10% share perfectly. It also rules out `hash()`, which is salted per process:
the same job would be assigned differently after a worker restart, so a resumed job would change
prompt version halfway through. `test_assignment_is_identical_in_a_separate_process` is what
distinguishes the two implementations, and it is the only assertion that can.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.static_guards import find_inline_prompts
from video_agent.gateway.errors import PromptRegistryError
from video_agent.gateway.prompts import (
    PROMPT_REGISTRY_UNAVAILABLE_ALARM,
    CachingPromptRegistry,
    FilePromptRegistry,
    canary_bucket,
    is_canary,
)

SAMPLE_SIZE = 10_000
CANARY_PCT = 10
TOLERANCE_PCT = 1
SHOTS_PER_JOB = 4
ATTEMPTS_PER_SHOT = 3


def write_registry(root: Path, *, canary: bool = False) -> Path:
    """A small on-disk registry: a manifest plus one body file per version."""
    root.mkdir(parents=True, exist_ok=True)
    canary_block = "\n    canary: { version: v2, traffic_pct: 10 }" if canary else ""
    (root / "registry.yaml").write_text(
        f"version: 1\nprompts:\n  shot_prompt:\n    production: v1{canary_block}\n",
        encoding="utf-8",
    )
    (root / "shot_prompt").mkdir(exist_ok=True)
    (root / "shot_prompt" / "v1.md").write_text("Describe {{brief}}.", encoding="utf-8")
    (root / "shot_prompt" / "v2.md").write_text("Describe {{brief}}, vividly.", encoding="utf-8")
    return root


def test_get_prompt_returns_a_ref_carrying_name_and_resolved_version(tmp_path: Path) -> None:
    """Acceptance 1: a name and a version, so a trace can say which prompt produced an output."""
    registry = FilePromptRegistry(write_registry(tmp_path / "prompts"))
    template = registry.get_prompt("shot_prompt", job_id="job-1")
    assert template.ref.name == "shot_prompt"
    assert template.ref.version == "v1"
    assert template.body == "Describe {{brief}}."


def test_an_unknown_prompt_name_raises_rather_than_defaulting(tmp_path: Path) -> None:
    """Acceptance 5. A default would be a prompt no trace can name — an inline string with a
    version number stuck on it."""
    registry = FilePromptRegistry(write_registry(tmp_path / "prompts"))
    with pytest.raises(PromptRegistryError, match="not in the registry"):
        registry.get_prompt("no_such_prompt", job_id="job-1")


def test_a_registered_version_with_no_body_file_raises(tmp_path: Path) -> None:
    """A manifest pointing at a missing body is a broken registry, not an empty prompt."""
    root = write_registry(tmp_path / "prompts")
    (root / "shot_prompt" / "v1.md").unlink()
    with pytest.raises(PromptRegistryError, match="no body file"):
        FilePromptRegistry(root).get_prompt("shot_prompt", job_id="job-1")


def test_assignment_is_a_pure_function_of_job_and_prompt() -> None:
    """Acceptance 2: same inputs, same bucket, every time."""
    first = canary_bucket("job-abc", "shot_prompt")
    assert all(canary_bucket("job-abc", "shot_prompt") == first for _ in range(100))
    others = {canary_bucket(f"job-{index}", "shot_prompt") for index in range(200)}
    assert len(others) > 1, "every job landing in one bucket would make the share meaningless"
    assert 0 <= first < SAMPLE_SIZE


def test_two_prompts_do_not_share_one_canary_cohort() -> None:
    """Two rollouts riding the same 10% cannot be attributed to either."""
    jobs = [f"job-{index}" for index in range(2000)]
    one = {job for job in jobs if is_canary(job, "shot_prompt", CANARY_PCT)}
    two = {job for job in jobs if is_canary(job, "qc_shot", CANARY_PCT)}
    assert one != two


def test_assignment_is_identical_in_a_separate_process() -> None:
    """Acceptance 2, the half that rules out `hash()`.

    `hash()` on a `str` is salted per process, so an implementation built on it would pass every
    in-process determinism test and reassign every job on restart. This is the only assertion
    that tells the two apart.
    """
    in_process = [canary_bucket(f"job-{index}", "shot_prompt") for index in range(20)]
    script = textwrap.dedent(
        """
        from video_agent.gateway.prompts import canary_bucket
        print(",".join(str(canary_bucket(f"job-{i}", "shot_prompt")) for i in range(20)))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert [int(value) for value in completed.stdout.strip().split(",")] == in_process


def test_the_canary_share_is_ten_percent_within_one_point() -> None:
    """Acceptance 3: 10% ± 1% across 10,000 synthetic job ids."""
    hits = sum(
        1 for index in range(SAMPLE_SIZE) if is_canary(f"job-{index}", "shot_prompt", CANARY_PCT)
    )
    share = 100 * hits / SAMPLE_SIZE
    assert CANARY_PCT - TOLERANCE_PCT <= share <= CANARY_PCT + TOLERANCE_PCT


def test_a_zero_percent_canary_assigns_nobody() -> None:
    """Rollback to 0% must actually stop the canary, not merely reduce it."""
    assert not any(is_canary(f"job-{index}", "shot_prompt", 0) for index in range(1000))


def test_one_job_resolves_one_version_across_every_shot_and_attempt(tmp_path: Path) -> None:
    """`[D-20]`: twelve resolutions for one job — four shots, three attempts — one version."""
    registry = FilePromptRegistry(write_registry(tmp_path / "prompts", canary=True))
    versions = {
        registry.get_prompt("shot_prompt", job_id="job-42").ref.version
        for _ in range(SHOTS_PER_JOB * ATTEMPTS_PER_SHOT)
    }
    assert len(versions) == 1


def test_a_canary_assigned_job_actually_gets_the_canary_version(tmp_path: Path) -> None:
    """The mechanism has to select somebody, or "deterministic" would be trivially satisfied."""
    registry = FilePromptRegistry(write_registry(tmp_path / "prompts", canary=True))
    assigned = next(
        job
        for job in (f"job-{index}" for index in range(500))
        if is_canary(job, "shot_prompt", CANARY_PCT)
    )
    template = registry.get_prompt("shot_prompt", job_id=assigned)
    assert template.ref.version == "v2"
    assert template.ref.is_canary is True


def test_registry_unavailable_serves_the_last_known_good_and_flags_it(tmp_path: Path) -> None:
    """Acceptance 4: cached version, `stale=True`, alarm — and never an inline string."""
    PROMPT_REGISTRY_UNAVAILABLE_ALARM.reset()
    root = write_registry(tmp_path / "prompts")
    registry = CachingPromptRegistry(FilePromptRegistry(root))
    first = registry.get_prompt("shot_prompt", job_id="job-1")
    assert first.stale is False
    (root / "registry.yaml").unlink()
    second = registry.get_prompt("shot_prompt", job_id="job-1")
    assert second.stale is True
    assert second.ref.version == first.ref.version
    assert second.body == first.body
    assert PROMPT_REGISTRY_UNAVAILABLE_ALARM.count == 1


def test_an_unavailable_registry_with_nothing_cached_still_raises(tmp_path: Path) -> None:
    """There is no honest last-known-good for a prompt that never resolved once."""
    root = write_registry(tmp_path / "prompts")
    registry = CachingPromptRegistry(FilePromptRegistry(root))
    (root / "registry.yaml").unlink()
    with pytest.raises(PromptRegistryError):
        registry.get_prompt("shot_prompt", job_id="job-1")


def test_the_module_contains_no_inline_prompt_literal() -> None:
    """Acceptance 4's tail: *no code path returns an inline string*, checked structurally.

    An AST scan rather than a grep, and over the whole gateway package rather than one module:
    a fallback prompt added to a neighbouring file would satisfy a per-module check and still be
    an unversioned prompt in code.
    """
    repo_root = Path(__file__).resolve().parents[2]
    offenders = [
        violation
        for violation in find_inline_prompts(repo_root)
        if violation.path.startswith("src/video_agent/gateway/")
    ]
    assert offenders == []
