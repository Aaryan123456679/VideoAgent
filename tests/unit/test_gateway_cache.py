"""`S0.7.8` — the response cache, and the calls that must never be served from it.

The exclusion is the load-bearing test. `gateway.md` §4.4 says the bible must be freshly
derived, and the failure it prevents is not a slow one: a bible served from cache anchors a
whole run's continuity to a previous job's inputs, and every shot afterwards is consistent with
the wrong thing. So `test_planning_and_bible_prompts_never_touch_the_cache` asserts on *both*
directions — no read and no write — because a write-only leak would poison the next job rather
than this one, which is harder to notice and identical in effect.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from tests.gateway_doubles import (
    MODEL_A,
    HarnessOverrides,
    ScriptedTransport,
    StubPromptRegistry,
    a_request,
    build_harness,
    ok,
)
from video_agent.gateway import CallContext, DegradeReason
from video_agent.gateway.cache import (
    CACHE_TTL_SECONDS,
    NEVER_CACHED_PROMPTS,
    CachedResponse,
    InMemoryResponseCache,
    cache_key,
    is_cacheable,
)
from video_agent.gateway.models import PromptRef

ONE_HOUR_IN_SECONDS = 3600
SPEC_KEY_PREFIX = "cache:llm:"

SPEC_NEVER_CACHED = ("plan_story", "lock_bible", "story_plan", "continuity_bible")
"""The excluded prompt names, written out rather than read from `NEVER_CACHED_PROMPTS`.

Parametrising over the module's own constant produced a test that could not fail: emptying the
constant emptied the parameter list, the cases silently stopped being generated, and the suite
stayed green while the bible became cacheable. The names are literals here and
`test_the_exclusion_list_is_the_documented_one` ties the two together, so removing a name fails
in one obvious place.

Four names for two calls because `gateway.md` §4.4 and the delivery plan disagree on their
spelling — see the constant's own docstring.
"""
CALLS_WHEN_THE_CACHE_IS_BYPASSED = 2
"""Two identical calls that both went to the wire, which is the point of the exclusion."""


class RecordingCache:
    """A cache that records every interaction, so "never touched" is directly assertable."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}
        self.reads: list[str] = []
        self.writes: list[tuple[str, int]] = []

    async def get(self, key: str, /) -> str | None:
        """Record and serve."""
        self.reads.append(key)
        return self.entries.get(key)

    async def set(self, key: str, value: str, ttl_s: int, /) -> None:
        """Record and store."""
        self.writes.append((key, ttl_s))
        self.entries[key] = value


def test_the_cache_key_includes_the_prompt_version() -> None:
    """Acceptance 1: a version bump misses.

    Without it, a prompt edit would keep serving the old prompt's answer for an hour after the
    rollout, which makes a canary measurement a measurement of nothing.
    """
    first = cache_key(a_request(prompt_ref=PromptRef(name="qc_shot", version="v1")))
    second = cache_key(a_request(prompt_ref=PromptRef(name="qc_shot", version="v2")))
    assert first != second


def test_the_cache_key_includes_the_variables_and_the_untrusted_values() -> None:
    """Two QC re-scores differing only in the rationale are different calls."""
    base = a_request(prompt_ref=PromptRef(name="qc_shot", version="v1"))
    different_variables = a_request(
        prompt_ref=PromptRef(name="qc_shot", version="v1"), variables={"brief": "other"}
    )
    different_untrusted = a_request(
        prompt_ref=PromptRef(name="qc_shot", version="v1"), untrusted={"rationale": "muted"}
    )
    assert cache_key(base) != cache_key(different_variables)
    assert cache_key(base) != cache_key(different_untrusted)


def test_the_cache_key_is_stable_for_an_identical_call() -> None:
    """A key that varied per call would be a cache that never hits and always writes."""
    one = a_request(prompt_ref=PromptRef(name="qc_shot", version="v1"))
    two = a_request(prompt_ref=PromptRef(name="qc_shot", version="v1"))
    assert cache_key(one) == cache_key(two)
    assert cache_key(one).startswith(SPEC_KEY_PREFIX)


def test_a_schema_change_invalidates_via_the_prompt_version() -> None:
    """Acceptance 5: the schema and the prompt version move together.

    Keying on the schema name as well means a rename misses immediately; keying on the version
    means an in-place shape change misses when the version is bumped, which is the discipline
    the registry already enforces.
    """

    class Old(BaseModel):
        value: int

    class New(BaseModel):
        value: int
        extra: str

    reference = PromptRef(name="qc_shot", version="v1")
    assert cache_key(a_request(prompt_ref=reference, response_model=Old)) != cache_key(
        a_request(prompt_ref=reference, response_model=New)
    )


@pytest.mark.parametrize("name", SPEC_NEVER_CACHED)
def test_the_planning_and_bible_prompts_are_not_cacheable(name: str) -> None:
    """Acceptance 3: excluded by a constant, with no flag that could enable them."""
    assert is_cacheable(name) is False


def test_the_exclusion_list_is_the_documented_one() -> None:
    """The module's constant against the written-out names, in the one place they are compared."""
    assert frozenset(SPEC_NEVER_CACHED) == NEVER_CACHED_PROMPTS


def test_an_ordinary_prompt_is_cacheable() -> None:
    """The exclusion has to exclude something specific, not everything."""
    assert is_cacheable("qc_shot") is True


@pytest.mark.asyncio
async def test_a_cache_hit_is_served_and_flagged_degraded_with_reason_cache() -> None:
    """Acceptance 2. An unflagged hit is a stale answer presented as a fresh one."""
    cache = InMemoryResponseCache()
    transport = ScriptedTransport({MODEL_A: [ok("fresh")]})
    prompts = StubPromptRegistry(name="qc_shot")
    ctx = CallContext(job_id="j", node="qc")
    harness = build_harness(transport, HarnessOverrides(prompts=prompts, cache=cache))
    request = a_request(prompt_ref=PromptRef(name="qc_shot", version="v1"))

    first = await harness.gateway.call(request, ctx=ctx)
    assert first.degraded is False

    second_ctx = CallContext(job_id="j", node="qc")
    second = await harness.gateway.call(request, ctx=second_ctx)
    assert second.degraded is True
    assert second.degrade_reason is DegradeReason.CACHE
    assert second.text == "fresh"
    assert second_ctx.degraded is True
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_a_cache_hit_reports_zero_incremental_tokens() -> None:
    """`S0.7.6` acceptance 5, from the cache side: recorded, but not billed twice."""
    cache = InMemoryResponseCache()
    transport = ScriptedTransport({MODEL_A: [ok("fresh", input_tokens=500, output_tokens=500)]})
    harness = build_harness(
        transport, HarnessOverrides(prompts=StubPromptRegistry(name="qc_shot"), cache=cache)
    )
    request = a_request(prompt_ref=PromptRef(name="qc_shot", version="v1"))
    await harness.gateway.call(request, ctx=CallContext(job_id="j", node="qc"))
    hit = await harness.gateway.call(request, ctx=CallContext(job_id="j", node="qc"))
    assert hit.usage.input_tokens == 0
    assert hit.usage.output_tokens == 0
    assert hit.model_used == MODEL_A


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SPEC_NEVER_CACHED)
async def test_planning_and_bible_prompts_never_touch_the_cache(name: str) -> None:
    """Acceptance 3, both directions: zero reads and zero writes, twice over.

    Asserting the write as well as the read matters: a write-only leak would not affect this
    job at all, and would serve the bible to the *next* one.
    """
    cache = RecordingCache()
    transport = ScriptedTransport({MODEL_A: [ok()]})
    harness = build_harness(
        transport, HarnessOverrides(prompts=StubPromptRegistry(name=name), cache=cache)
    )
    request = a_request(prompt_ref=PromptRef(name=name, version="v1"))
    await harness.gateway.call(request, ctx=CallContext(job_id="j", node="plan"))
    await harness.gateway.call(request, ctx=CallContext(job_id="j", node="plan"))
    assert cache.reads == []
    assert cache.writes == []
    assert len(transport.calls) == CALLS_WHEN_THE_CACHE_IS_BYPASSED


@pytest.mark.asyncio
async def test_the_ttl_written_is_one_hour_under_the_documented_key_prefix() -> None:
    """Acceptance 4: `cache:llm:` and 1h, which is what the key registry documents."""
    cache = RecordingCache()
    transport = ScriptedTransport({MODEL_A: [ok()]})
    harness = build_harness(
        transport, HarnessOverrides(prompts=StubPromptRegistry(name="qc_shot"), cache=cache)
    )
    await harness.gateway.call(
        a_request(prompt_ref=PromptRef(name="qc_shot", version="v1")),
        ctx=CallContext(job_id="j", node="qc"),
    )
    assert len(cache.writes) == 1
    key, ttl = cache.writes[0]
    assert key.startswith(SPEC_KEY_PREFIX)
    assert ttl == ONE_HOUR_IN_SECONDS
    assert CACHE_TTL_SECONDS == ONE_HOUR_IN_SECONDS


@pytest.mark.asyncio
async def test_a_structured_call_is_cached_and_returns_a_parsed_object_on_the_hit() -> None:
    """Acceptance 5: what comes back from the cache is the parsed object, not raw text."""

    class Verdict(BaseModel):
        score: float

    cache = InMemoryResponseCache()
    transport = ScriptedTransport({MODEL_A: [ok('{"score": 0.8}')]})
    harness = build_harness(
        transport, HarnessOverrides(prompts=StubPromptRegistry(name="qc_shot"), cache=cache)
    )
    request = a_request(prompt_ref=PromptRef(name="qc_shot", version="v1"), response_model=Verdict)
    await harness.gateway.call(request, ctx=CallContext(job_id="j", node="qc"))
    hit = await harness.gateway.call(request, ctx=CallContext(job_id="j", node="qc"))
    assert isinstance(hit.parsed, Verdict)
    assert hit.parsed.score == pytest.approx(0.8)
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_a_bumped_prompt_version_misses_the_cache_end_to_end() -> None:
    """Acceptance 1, through the gateway rather than through the key function alone."""
    cache = InMemoryResponseCache()
    transport = ScriptedTransport({MODEL_A: [ok("first"), ok("second")]})
    prompts = StubPromptRegistry(name="qc_shot", version="v1")
    harness = build_harness(transport, HarnessOverrides(prompts=prompts, cache=cache))
    request = a_request(prompt_ref=PromptRef(name="qc_shot", version="v1"))
    await harness.gateway.call(request, ctx=CallContext(job_id="j", node="qc"))
    prompts.version = "v2"
    second = await harness.gateway.call(request, ctx=CallContext(job_id="j", node="qc"))
    assert second.text == "second"
    assert second.degraded is False
    assert len(transport.calls) == CALLS_WHEN_THE_CACHE_IS_BYPASSED


def test_an_unreadable_cache_entry_is_a_miss_rather_than_an_error() -> None:
    """A schema-drifted entry costs one call. Raising would fail a job over a cache's contents."""
    assert CachedResponse.from_json("not json") is None
    assert CachedResponse.from_json(json.dumps({"text": 1})) is None
    assert CachedResponse.from_json(json.dumps({"text": "a", "model_used": "m"})) is None
