"""The 1h response cache, and the two prompts that must never be served from it.

`gateway.md` §4.4 permits a cached identical call — same prompt version plus variables hash,
TTL 1h — as a degrade for re-scoring an unchanged artifact, and forbids it outright for the
planning and bible calls, because *the bible must be freshly derived*. That exclusion is not a
performance trade-off: a bible served from cache is a bible derived from a previous job's
inputs, and continuity for the whole run is then anchored to the wrong thing.

**The exclusion has no configuration flag**, deliberately. `S0.7.8` acceptance 3 asks that those
prompts bypass the cache on both read and write with no way to enable it, so the check is a
constant set consulted unconditionally. A flag would eventually be turned on by someone chasing
latency, and the resulting continuity failure would be untraceable.

**The prompt version is part of the key.** A prompt edit that did not change the key would serve
the old prompt's answer for an hour after the rollout, which makes a canary measurement
meaningless. It is also what invalidates a structured-output cache when the schema changes: the
schema and the prompt version move together, so a shape change misses rather than deserialising
yesterday's shape into today's model.

**A hit is flagged.** `degraded=true`, reason `cache`. `gateway.md` §4.4 again: *a degraded
result is never presented as a clean one.*
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from video_agent.gateway.models import LLMRequest

__all__ = [
    "CACHE_KEY_PREFIX",
    "CACHE_TTL_SECONDS",
    "NEVER_CACHED_PROMPTS",
    "CachedResponse",
    "InMemoryResponseCache",
    "RedisResponseCache",
    "ResponseCacheStore",
    "cache_key",
    "is_cacheable",
]

CACHE_KEY_PREFIX: Final = "cache:llm:"
"""`persistence.md` §5's key registry namespace for this cache. One prefix, so an operator can
scope a flush to it without touching idempotency records, which may never be flushed."""

CACHE_TTL_SECONDS: Final = 3600
"""One hour `[gateway.md §4.4]`."""

NEVER_CACHED_PROMPTS: Final[frozenset[str]] = frozenset(
    {"plan_story", "lock_bible", "story_plan", "continuity_bible"}
)
"""The prompts that must be freshly derived, on both read and write.

Four names for two calls, and that is deliberate rather than sloppy. `gateway.md` §4.4 names
them `plan_story` and `lock_bible`; the delivery plan's `S0.7.8` names the same two calls
`story_plan` and `continuity_bible`. The documents disagree and the disagreement is unresolved,
so both spellings are excluded: excluding a name that turns out not to exist costs one cache
miss that never happens, while omitting the spelling that turns out to be real would cache the
bible. The asymmetry decides it. This should be reconciled to one pair of names.
"""


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """What is stored: the text and the accounting that identifies which call produced it.

    `model_used` is stored so a cache hit can still report which model's answer is being served.
    A hit that reported the model it *would* have called would put a model in the trace that
    never ran.
    """

    text: str
    model_used: str
    prompt_version: str
    input_tokens: int
    output_tokens: int

    def to_json(self) -> str:
        """Serialise for the store."""
        return json.dumps(
            {
                "text": self.text,
                "model_used": self.model_used,
                "prompt_version": self.prompt_version,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> CachedResponse | None:
        """Parse a stored entry, treating anything unreadable as a miss.

        A miss rather than an error: a schema-drifted entry means the call is made again, which
        is correct and costs one call. Raising would fail a job over the contents of a cache.
        """
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        text = payload.get("text")
        model_used = payload.get("model_used")
        prompt_version = payload.get("prompt_version")
        if not isinstance(text, str) or not isinstance(model_used, str):
            return None
        if not isinstance(prompt_version, str):
            return None
        return cls(
            text=text,
            model_used=model_used,
            prompt_version=prompt_version,
            input_tokens=int(payload.get("input_tokens", 0)),
            output_tokens=int(payload.get("output_tokens", 0)),
        )


class ResponseCacheStore(Protocol):
    """A string store with a TTL. Redis in production; process-local in tests."""

    async def get(self, key: str, /) -> str | None: ...

    async def set(self, key: str, value: str, ttl_s: int, /) -> None: ...


class InMemoryResponseCache:
    """A process-local cache. No expiry, because nothing that uses it outlives a test."""

    def __init__(self) -> None:
        self._entries: dict[str, str] = {}

    async def get(self, key: str, /) -> str | None:
        """The stored value, or `None`."""
        return self._entries.get(key)

    async def set(self, key: str, value: str, _ttl_s: int, /) -> None:
        """Store the value. The TTL is part of the protocol and is not honoured here."""
        self._entries[key] = value


class CacheRedis(Protocol):
    """The two Redis commands this cache needs. Structural, for the reason `breaker` gives."""

    async def get(self, name: str) -> object: ...

    async def set(self, name: str, value: str, *, ex: int | None = None) -> object: ...


class RedisResponseCache:
    """The shared cache. Every failure is a miss, never an error.

    A cache outage must not fail a call: the whole point of this layer is that the answer is
    obtainable without it. A read failure is a miss and a write failure is dropped, both
    silently as far as the caller is concerned — the circuit-state store is where an outage is
    alarmed, because there the consequence is a lost safety property rather than a lost
    optimisation.
    """

    def __init__(self, client: CacheRedis) -> None:
        self._client = client

    async def get(self, key: str, /) -> str | None:
        """The stored value, or `None` on a miss or any failure."""
        try:
            raw = await self._client.get(key)
        except Exception:
            return None
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes | bytearray) else str(raw)

    async def set(self, key: str, value: str, ttl_s: int, /) -> None:
        """Store with a TTL, ignoring failure."""
        try:
            await self._client.set(key, value, ex=ttl_s)
        except Exception:
            return


def is_cacheable(prompt_name: str) -> bool:
    """Whether this prompt may touch the cache at all, on read or on write."""
    return prompt_name not in NEVER_CACHED_PROMPTS


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def variables_digest(variables: Mapping[str, Any], untrusted: Mapping[str, str]) -> str:
    """A stable hash of everything that varies between two calls of the same prompt.

    Untrusted values are included. Two QC re-scores that differ only in the rationale they were
    given are different calls, and a key that ignored the untrusted half would serve the first
    one's answer for the second.
    """
    digest = hashlib.sha256()
    digest.update(_canonical(dict(variables)).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(_canonical(dict(untrusted)).encode("utf-8"))
    return digest.hexdigest()


def cache_key(request: LLMRequest) -> str:
    """The cache key for one request: prompt identity, inputs, and the shape asked for.

    `max_output_tokens` and `temperature` are in the key because both change the answer, and a
    key that ignored them would serve a 200-token answer to a caller that asked for 2,000.
    """
    schema_name = request.response_model.__name__ if request.response_model is not None else ""
    material = "|".join(
        (
            request.alias.value,
            request.prompt_ref.name,
            request.prompt_ref.version,
            schema_name,
            str(request.max_output_tokens),
            f"{request.temperature:.6f}",
            variables_digest(request.variables, request.untrusted),
        )
    )
    return CACHE_KEY_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()
