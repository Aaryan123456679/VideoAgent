"""The prompt registry client, and deterministic per-job canary assignment.

Two rules meet here.

**A prompt has a name and a version, and never an inline string.** `gateway.md` §5 and
`[D-72]`: prompts are authored in-repo under `prompts/` as versioned files, that is the source
of truth, and Langfuse is the observability and version-tracking surface rather than a hard
runtime dependency — making it one would mean a Langfuse outage stops all video generation. So
this client reads the in-repo files, and there is no inline fallback anywhere in it. A prompt
name the registry does not hold raises; it does not default. A default would be a prompt no
trace can name, which is the same failure as an inline string wearing a version number.

**Canary assignment is a pure function of `(job_id, prompt_name)`.** `[CPS §Rollout]` puts model
and prompt changes in front of 10% of traffic first, and `[D-20]` adds the constraint that makes
the implementation non-obvious: *a single job never mixes models or prompt versions across its
shots*. A per-call random draw would satisfy the 10% and violate that — a forty-shot job would
land four shots on the new prompt and thirty-six on the old, and the continuity between them
would be the thing being measured. So assignment is a SHA-256 of the pair, not `random`, and not
`hash()`: `hash()` is salted per process, so the same job would be assigned differently after a
worker restart, and a resumed job would silently change prompt version mid-run.

**Registry unavailable degrades to last-known-good, flagged.** `gateway.md` §4.4's rule that a
degrade is always flagged applies to a stale prompt exactly as it does to a cached answer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import yaml

from video_agent.gateway.errors import PromptRegistryError
from video_agent.gateway.models import PromptRef
from video_agent.observability.alarms import AlarmCounter

__all__ = [
    "PROMPT_REGISTRY_UNAVAILABLE_ALARM",
    "CachingPromptRegistry",
    "FilePromptRegistry",
    "PromptRegistry",
    "PromptTemplate",
    "canary_bucket",
    "is_canary",
]

REGISTRY_FILE_NAME: Final = "registry.yaml"
PROMPT_BODY_SUFFIX: Final = ".md"
BUCKET_RESOLUTION: Final = 10_000
"""Assignment granularity: one basis point. Finer than any rollout percentage worth setting,
and a power-of-ten so a `traffic_pct` maps onto it without rounding."""

PERCENT_TO_BUCKETS: Final = BUCKET_RESOLUTION // 100

PROMPT_REGISTRY_UNAVAILABLE_ALARM: Final[AlarmCounter] = AlarmCounter("prompt_registry_unavailable")
"""Counts resolutions served from the last-known-good cache because the registry could not be
read. Non-zero means prompt versions in traces may lag what the repository says."""


def canary_bucket(job_id: str, prompt_name: str) -> int:
    """A stable bucket in `[0, 10000)` for one job and one prompt.

    SHA-256 rather than `hash()`, and the pair rather than the job alone: two prompts rolling
    out at once should not put the same 10% of jobs on both, or the canary cohort becomes a
    single population carrying every change and no change can be attributed.
    """
    digest = hashlib.sha256(f"{prompt_name}\x00{job_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % BUCKET_RESOLUTION


def is_canary(job_id: str, prompt_name: str, traffic_pct: int) -> bool:
    """Whether this job is in the canary cohort for this prompt. Pure, and process-independent."""
    if traffic_pct <= 0:
        return False
    return canary_bucket(job_id, prompt_name) < traffic_pct * PERCENT_TO_BUCKETS


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A prompt body with the reference that identifies it, and whether it is stale."""

    ref: PromptRef
    body: str
    stale: bool = False


class PromptRegistry(Protocol):
    """Resolve a prompt name to a versioned template for one job."""

    def get_prompt(self, name: str, *, job_id: str) -> PromptTemplate: ...


@dataclass(frozen=True, slots=True)
class _Entry:
    production: str
    canary_version: str | None
    canary_traffic_pct: int


def _parse_entry(name: str, raw: object) -> _Entry:
    if not isinstance(raw, dict):
        message = f"prompt {name!r} must be a mapping in {REGISTRY_FILE_NAME}"
        raise PromptRegistryError(
            what_happened=message,
            what_to_do_next=f"fix the {name!r} entry in {REGISTRY_FILE_NAME}",
        )
    production = raw.get("production")
    if not isinstance(production, str) or not production:
        message = f"prompt {name!r} declares no production version"
        raise PromptRegistryError(
            what_happened=message,
            what_to_do_next=f"add `production:` to the {name!r} entry in {REGISTRY_FILE_NAME}",
        )
    canary = raw.get("canary")
    if isinstance(canary, dict):
        version = canary.get("version")
        pct = canary.get("traffic_pct", 0)
        return _Entry(
            production=production,
            canary_version=version if isinstance(version, str) and version else None,
            canary_traffic_pct=int(pct) if isinstance(pct, int) else 0,
        )
    return _Entry(production=production, canary_version=None, canary_traffic_pct=0)


class FilePromptRegistry:
    """Prompts from `prompts/registry.yaml` plus `prompts/<name>/<version>.md`. `[D-72]`.

    The manifest holds versions and rollout percentages; the bodies are plain files, one per
    version, never edited in place. A version whose file changes is a version whose trace lies,
    so a change is a new file and a manifest bump.

    Reads from disk on every resolution rather than caching, so an operator editing the
    manifest does not have to restart every worker to promote a canary. `CachingPromptRegistry`
    wraps this when a last-known-good copy is wanted.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """Where the registry is rooted. Logged at startup, like the alias table's path."""
        return self._root

    def get_prompt(self, name: str, *, job_id: str) -> PromptTemplate:
        """The version this job should use, and its body. Raises if the name is unknown."""
        entry = self._entry(name)
        version = entry.production
        canary = False
        if entry.canary_version is not None and is_canary(job_id, name, entry.canary_traffic_pct):
            version = entry.canary_version
            canary = True
        return PromptTemplate(
            ref=PromptRef(name=name, version=version, is_canary=canary),
            body=self._body(name, version),
        )

    def _manifest(self) -> dict[str, Any]:
        path = self._root / REGISTRY_FILE_NAME
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PromptRegistryError(
                what_happened=f"the prompt registry manifest could not be read: {exc}",
                what_to_do_next=(
                    f"check that {REGISTRY_FILE_NAME} exists under the configured prompt root "
                    f"and is valid YAML"
                ),
            ) from exc
        prompts = raw.get("prompts") if isinstance(raw, dict) else None
        if not isinstance(prompts, dict):
            raise PromptRegistryError(
                what_happened="the prompt registry manifest has no `prompts:` mapping",
                what_to_do_next=f"add a `prompts:` mapping to {REGISTRY_FILE_NAME}",
            )
        return prompts

    def _entry(self, name: str) -> _Entry:
        prompts = self._manifest()
        if name not in prompts:
            raise PromptRegistryError(
                what_happened=f"prompt {name!r} is not in the registry",
                what_to_do_next=(
                    f"register {name!r} with a version before calling it; the gateway never "
                    f"falls back to an inline prompt string"
                ),
            )
        return _parse_entry(name, prompts[name])

    def _body(self, name: str, version: str) -> str:
        path = self._root / name / f"{version}{PROMPT_BODY_SUFFIX}"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PromptRegistryError(
                what_happened=f"prompt {name!r} version {version!r} has no body file",
                what_to_do_next=(
                    f"add the body for {name!r} {version!r} under the prompt root, or point the "
                    f"manifest at a version that exists"
                ),
            ) from exc


class CachingPromptRegistry:
    """Last-known-good in front of another registry, flagging what it serves as stale.

    `S0.7.7` acceptance 4: registry unavailable → last-known-good cached version, `degraded=true`
    and an alarm; *no code path returns an inline string*. So the cache serves only what it has
    previously served successfully. A name that has never resolved raises, because there is no
    honest last-known-good for it and inventing one is the inline-string failure by another
    route.
    """

    def __init__(self, inner: PromptRegistry) -> None:
        self._inner = inner
        self._cache: dict[tuple[str, str], PromptTemplate] = {}

    def get_prompt(self, name: str, *, job_id: str) -> PromptTemplate:
        """Resolve through the inner registry, falling back to the last good copy."""
        key = (name, job_id)
        try:
            template = self._inner.get_prompt(name, job_id=job_id)
        except PromptRegistryError:
            cached = self._cache.get(key)
            if cached is None:
                raise
            PROMPT_REGISTRY_UNAVAILABLE_ALARM.increment()
            return PromptTemplate(ref=cached.ref, body=cached.body, stale=True)
        self._cache[key] = template
        return template
