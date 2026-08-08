"""The gateway's public data types: what a caller may ask for, and what it gets back.

`gateway.md` §2 fixes this interface, and one field of it carries most of the weight:
`LLMRequest.alias` is an `Alias` enum member and there is no model-name string anywhere on the
request. That is what makes "swapping a model is a config change with zero code diff"
`[CPS §Model routing]` true as a property of the type rather than as a convention — a call site
that wanted to name a model would have nowhere to put the name.

`LLMResponse.model_used` is the one place a concrete model surfaces, and it exists for
observability only. `tests/static_guards.py` forbids comparing it to a literal, so a trace can
answer "which model produced this" without code being able to act on the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from video_agent.config.aliases import Alias

__all__ = [
    "Alias",
    "AliasHealth",
    "ArtifactRef",
    "CallContext",
    "DegradeReason",
    "LLMRequest",
    "LLMResponse",
    "ModelHealth",
    "PromptRef",
    "Usage",
]


class DegradeReason(StrEnum):
    """Why a response is flagged degraded. A closed set, because it is read by alerting.

    `gateway.md` §4.4 requires every degrade to be flagged and propagated; a free-text reason
    would be flagged but not groupable, and "how often are we serving from cache" is exactly
    the question the flag exists to answer.
    """

    FALLBACK = "fallback"
    CACHE = "cache"
    STALE_PROMPT = "stale_prompt"


class PromptRef(BaseModel):
    """A prompt by registry name and version — never a raw string. `[D-72]`, `[CPS §Rollout]`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    is_canary: bool = False


class ArtifactRef(BaseModel):
    """An image input by reference. Bytes are fetched inside the gateway, never carried here.

    `AGENT.md` §3 forbids media payloads and base64 in logs, span attributes and error
    messages. A request that could hold the bytes is a request that will end up in one of the
    three, so it holds a key instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str = Field(min_length=1)
    storage_key: str = Field(min_length=1)


class Usage(BaseModel):
    """Tokens and cost for one call, in `Decimal` so a job total is exact rather than close.

    `cost_is_ceiling` records that the price came from the configured pessimistic ceiling
    rather than the price table `[D-21]`. A budget cap must be able to tell a real cost from a
    defensive guess; collapsing the two would let "we do not know what this cost" read as
    "this cost that much".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    cost_is_ceiling: bool = False


class LLMRequest(BaseModel):
    """One logical LLM call. Carries an alias, never a model.

    `untrusted` is separate from `variables` because the two are rendered differently and the
    difference is a `[CPS §Non-negotiables]` rule: `variables` reach the instruction section,
    `untrusted` values reach only a delimited block with instruction-shaped content escaped.
    Merging them into one mapping would make the rule unenforceable, since nothing downstream
    could tell which half was which.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    alias: Alias
    prompt_ref: PromptRef
    variables: dict[str, Any] = Field(default_factory=dict)
    untrusted: dict[str, str] = Field(default_factory=dict)
    images: tuple[ArtifactRef, ...] = ()
    response_model: type[BaseModel] | None = None
    max_output_tokens: int = Field(gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_s: float = Field(gt=0)
    idempotency_hint: str | None = None


class LLMResponse(BaseModel):
    """One completed call. `degraded` is never silently false. `gateway.md` §4.4."""

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    parsed: BaseModel | None = None
    text: str | None = None
    model_used: str = Field(min_length=1)
    alias: Alias
    prompt_version: str = Field(min_length=1)
    usage: Usage
    latency_ms: int = Field(ge=0)
    degraded: bool = False
    degrade_reason: DegradeReason | None = None
    generation_id: str = Field(min_length=1)


class ModelHealth(BaseModel):
    """One concrete model's circuit state inside a group."""

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    model: str = Field(min_length=1)
    state: str = Field(min_length=1)
    admits_traffic: bool


class AliasHealth(BaseModel):
    """Whether an alias group can serve at all, and which of its members are open.

    `healthy` is false only when *every* member is open, because that — and not one sick
    member — is the condition `gateway.md` §4.3 maps to `VA-GW-001`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    alias: Alias
    healthy: bool
    models: tuple[ModelHealth, ...]


@dataclass(slots=True)
class CallContext:
    """The calling node's identity, and the degrade flag the gateway raises on it.

    Mutable, and deliberately so. `gateway.md` §4.4 requires a degrade to propagate to
    `Job.degraded`, which means the fact has to leave the call rather than only ride on the
    response — a caller that forgot to inspect `LLMResponse.degraded` would otherwise present a
    fallback-served answer as a clean one, which is the exact outcome the rule forbids. The
    harness persists `degraded` and `degrade_reason` onto the job row; the gateway does not
    write to the database itself.
    """

    job_id: str
    node: str
    degraded: bool = False
    degrade_reasons: list[DegradeReason] = field(default_factory=list)

    def mark_degraded(self, reason: DegradeReason) -> None:
        """Record a degrade. Idempotent per reason so a retried node does not double-count."""
        self.degraded = True
        if reason not in self.degrade_reasons:
            self.degrade_reasons.append(reason)
