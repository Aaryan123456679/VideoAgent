"""Test doubles for the gateway: a controlled clock, a pinned jitter source, a scripted wire.

Not a test module — the filename does not match `python_files`, so pytest does not collect it.

Everything the gateway's failure policy does is a rule about *time* or about *what the wire
said*. Both are injected rather than patched, and the difference matters: a monkeypatched
`time.monotonic` is process-global, leaks across tests, and cannot express "this breaker is at
t=29 while that one is at t=31", which is precisely the assertion the sliding-window test needs
to make.

The model names here are deliberately fictional. Nothing in the gateway's behaviour depends on
which vendor is behind a name, so a test that used a real one would be asserting nothing extra
and would put a model name one copy-paste away from `src/`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from video_agent.config.aliases import Alias, AliasEntry, AliasTable, ModelPrice, ModelRef
from video_agent.gateway.breaker import CircuitBreaker, InMemoryCircuitStateStore
from video_agent.gateway.cache import ResponseCacheStore
from video_agent.gateway.capabilities import Capability, StaticCapabilityRegistry
from video_agent.gateway.gateway import GatewayDeps, LiteLLMGateway
from video_agent.gateway.models import LLMRequest, PromptRef
from video_agent.gateway.prompts import PromptTemplate
from video_agent.gateway.retry import RetryPolicy
from video_agent.gateway.transport import TransportCall, TransportResult

MODEL_A = "vendor-a/model-one"
MODEL_B = "vendor-b/model-two"
MODEL_C = "vendor-c/model-three"
VISION_MODEL = "vendor-a/vision-one"
UNPRICED_MODEL = "vendor-z/model-renamed"

DEFAULT_TEMPLATE = "Describe the scene.\n\nBrief: {{brief}}"
DEFAULT_PROMPT_NAME = "shot_prompt"
DEFAULT_PROMPT_VERSION = "v1"


class ManualClock:
    """A clock that only moves when a test moves it. `sleep` records and advances, never waits."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """The current instant."""
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Record the requested delay and jump the clock forward by it."""
        self.sleeps.append(seconds)
        self._now += seconds

    def advance(self, seconds: float) -> None:
        """Move time forward without anything having waited."""
        self._now += seconds


@dataclass
class FixedJitter:
    """A jitter source pinned to one multiplier, so a backoff schedule is exactly predictable."""

    multiplier: float = 1.0

    def uniform(self, low: float, high: float) -> float:
        """Return the pinned multiplier, clamped into `[low, high]` so a test cannot pin outside."""
        return min(max(self.multiplier, low), high)


@dataclass
class SequenceJitter:
    """Jitter drawn from a fixed sequence, cycling once it runs out."""

    values: Sequence[float]
    index: int = 0

    def uniform(self, low: float, high: float) -> float:
        """The next pinned value, clamped into `[low, high]`."""
        value = self.values[self.index % len(self.values)]
        self.index += 1
        return min(max(value, low), high)


class ScriptedTransport:
    """A wire that returns what the test told it to, and records everything it was asked.

    Outcomes are per model, because every fallback assertion is of the form "this model always
    fails and that one works". An exhausted script repeats its last entry, which is how "429
    forever" is expressed without writing an infinite list.
    """

    def __init__(
        self,
        outcomes: Mapping[str, Sequence[object]] | None = None,
        *,
        model_info: Sequence[Mapping[str, Any]] = (),
        fallback: Sequence[object] | None = None,
    ) -> None:
        self._outcomes = {model: list(script) for model, script in (outcomes or {}).items()}
        self._fallback = list(fallback) if fallback is not None else [ok()]
        self._model_info = list(model_info)
        self.calls: list[TransportCall] = []
        self.model_info_calls = 0

    @property
    def models_called(self) -> list[str]:
        """Every model this transport was asked to call, in order, with repeats."""
        return [call.model for call in self.calls]

    def calls_for(self, model: str) -> list[TransportCall]:
        """Every call made against one model."""
        return [call for call in self.calls if call.model == model]

    async def complete(self, call: TransportCall) -> TransportResult:
        """Return or raise the next scripted outcome for `call.model`."""
        self.calls.append(call)
        script = self._outcomes.get(call.model, self._fallback)
        index = len(self.calls_for(call.model)) - 1
        outcome = script[min(index, len(script) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, TransportResult):
            return outcome
        message = f"scripted outcome for {call.model!r} is neither a result nor an exception"
        raise TypeError(message)

    async def model_info(self) -> Sequence[Mapping[str, Any]]:
        """The scripted `/model/info` payload."""
        self.model_info_calls += 1
        return self._model_info


def ok(
    text: str = "an answer", *, input_tokens: int = 100, output_tokens: int = 20
) -> TransportResult:
    """A successful reply with round token counts, so an expected cost is easy to read."""
    return TransportResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)


@dataclass
class StubPromptRegistry:
    """A registry that returns one template, and counts how often it was asked."""

    body: str = DEFAULT_TEMPLATE
    name: str = DEFAULT_PROMPT_NAME
    version: str = DEFAULT_PROMPT_VERSION
    stale: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    def get_prompt(self, name: str, *, job_id: str) -> PromptTemplate:
        """The single configured template, under whatever name was asked for."""
        self.calls.append((name, job_id))
        return PromptTemplate(
            ref=PromptRef(name=name, version=self.version),
            body=self.body,
            stale=self.stale,
        )


def price(input_rate: str, output_rate: str) -> ModelPrice:
    """A price entry from two decimal strings, so a golden expectation stays exact."""
    return ModelPrice(
        input_usd_per_1k_tokens=Decimal(input_rate),
        output_usd_per_1k_tokens=Decimal(output_rate),
    )


def alias_table(
    *,
    aliases: Mapping[Alias, AliasEntry] | None = None,
    prices: Mapping[str, ModelPrice] | None = None,
) -> AliasTable:
    """A small alias table with fictional models, built directly rather than parsed from YAML.

    Built rather than parsed on purpose: a test that read `config/aliases.yaml` would fail
    whenever a model was swapped, which is exactly the change the whole design is meant to make
    free.
    """
    default_aliases: dict[Alias, AliasEntry] = {
        Alias.REASONING_HIGH: AliasEntry(
            primary=ModelRef(model=MODEL_A),
            fallbacks=(ModelRef(model=MODEL_B), ModelRef(model=MODEL_C)),
            required_capabilities=("structured_output",),
        ),
        Alias.VISION_DEFAULT: AliasEntry(
            primary=ModelRef(model=VISION_MODEL),
            required_capabilities=("image_input", "structured_output"),
        ),
    }
    default_prices: dict[str, ModelPrice] = {
        MODEL_A: price("0.00100", "0.01000"),
        MODEL_B: price("0.00200", "0.02000"),
        MODEL_C: price("0.00400", "0.04000"),
        VISION_MODEL: price("0.00100", "0.01000"),
    }
    return AliasTable(
        aliases=MappingProxyType(dict(aliases if aliases is not None else default_aliases)),
        prices=MappingProxyType(dict(prices if prices is not None else default_prices)),
        unpriced_ceiling=price("0.05000", "0.15000"),
    )


ALL_CAPABILITIES: frozenset[Capability] = frozenset(Capability)
"""Everything, for the tests whose subject is not the capability check."""


def capability_table(*models: str) -> dict[str, frozenset[Capability]]:
    """A capability mapping crediting each named model with everything."""
    return dict.fromkeys(models, ALL_CAPABILITIES)


def a_request(**overrides: object) -> LLMRequest:
    """A minimal valid request; the overrides are what a given test is actually varying."""
    fields: dict[str, object] = {
        "alias": Alias.REASONING_HIGH,
        "prompt_ref": PromptRef(name=DEFAULT_PROMPT_NAME, version=DEFAULT_PROMPT_VERSION),
        "variables": {"brief": "a lighthouse at dawn"},
        "max_output_tokens": 256,
        "timeout_s": 30.0,
    }
    fields.update(overrides)
    return LLMRequest.model_validate(fields)


@dataclass
class Harness:
    """A wired gateway plus the collaborators a test needs to inspect or drive.

    Returned as one object so a test can advance the clock, read the transport's call log and
    assert on the alarm counters without re-deriving how the gateway was assembled.
    """

    gateway: LiteLLMGateway
    transport: ScriptedTransport
    clock: ManualClock
    jitter: FixedJitter | SequenceJitter
    breaker: CircuitBreaker
    prompts: StubPromptRegistry
    cache: ResponseCacheStore | None


@dataclass(frozen=True, slots=True)
class HarnessOverrides:
    """The collaborators a test wants to control, as one object rather than nine keywords.

    A parameter object because the set is long and every entry is optional; nine keyword
    arguments is a signature whose call sites eventually pass one positionally by mistake.
    """

    capabilities: Mapping[str, frozenset[Capability]] | None = None
    table: AliasTable | None = None
    prompts: StubPromptRegistry | None = None
    cache: ResponseCacheStore | None = None
    clock: ManualClock | None = None
    jitter: FixedJitter | SequenceJitter | None = None
    retry: RetryPolicy | None = None
    breaker: CircuitBreaker | None = None


def build_harness(
    transport: ScriptedTransport | None = None,
    overrides: HarnessOverrides | None = None,
) -> Harness:
    """A gateway wired entirely to in-process collaborators, with time under the test's control."""
    chosen = overrides if overrides is not None else HarnessOverrides()
    wire = transport if transport is not None else ScriptedTransport()
    tick = chosen.clock if chosen.clock is not None else ManualClock()
    shake = chosen.jitter if chosen.jitter is not None else FixedJitter(1.0)
    registry = chosen.prompts if chosen.prompts is not None else StubPromptRegistry()
    circuit = (
        chosen.breaker
        if chosen.breaker is not None
        else CircuitBreaker(store=InMemoryCircuitStateStore(), clock=tick)
    )
    gateway = LiteLLMGateway(
        GatewayDeps(
            table=chosen.table if chosen.table is not None else alias_table(),
            transport=wire,
            capabilities=StaticCapabilityRegistry(
                chosen.capabilities
                if chosen.capabilities is not None
                else capability_table(MODEL_A, MODEL_B, MODEL_C, VISION_MODEL, UNPRICED_MODEL)
            ),
            prompts=registry,
            breaker=circuit,
            cache=chosen.cache,
            clock=tick,
            jitter=shake,
            retry=chosen.retry if chosen.retry is not None else RetryPolicy(),
        )
    )
    return Harness(
        gateway=gateway,
        transport=wire,
        clock=tick,
        jitter=shake,
        breaker=circuit,
        prompts=registry,
        cache=chosen.cache,
    )
