"""The gateway: alias resolution, then retry, fallback, circuit break, degrade, fail honestly.

`gateway.md` §1 makes this the only path from application code to an LLM, and every rule the
module exists to enforce converges in `call()`. The order matters and is not arbitrary:

1. **Resolve, fail closed.** The alias group and every model in it are checked for the declared
   capabilities *before any HTTP call is made*. `gateway.md` §8: never guess a model. A
   resolution failure discovered after a request had already been sent would have paid for the
   discovery.
2. **Fetch the prompt by name and version**, with the job's canary assignment applied.
3. **Render**, separating trusted variables from quarantined untrusted values.
4. **Read the cache**, unless the prompt is one of the two that must be freshly derived.
5. **Walk the group.** Per model: consult the circuit, then up to three attempts with jittered
   backoff on retryable errors only. A non-retryable availability error moves to the next model
   immediately; a non-retryable request error raises immediately, because every model in the
   group would reject the same request; `402` raises immediately and escalates `[D-62]`.
6. **Flag every degrade** on both the response and the calling context, so it reaches
   `Job.degraded` rather than only whoever remembered to read the response.
7. **Fail honestly** when the group is exhausted: `VA-GW-001`, with what happened, what was
   preserved and what to do next, plus the `trace_id` the error captured at its raise site.

Two things this module deliberately does **not** do. It does not branch on `model_used` — the
concrete model is carried for observability and nothing reads it back. And it does not log the
rendered prompt: the log line carries the prompt name, the resolved version and a digest, which
is what `gateway.md` §6 permits and no more.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import uuid4

from pydantic import ValidationError

from video_agent.config.errors import AliasConfigError
from video_agent.gateway.breaker import Admission, CircuitState, dependency_key
from video_agent.gateway.cache import (
    CACHE_TTL_SECONDS,
    CachedResponse,
    cache_key,
    is_cacheable,
)
from video_agent.gateway.capabilities import missing_capabilities
from video_agent.gateway.classify import classify
from video_agent.gateway.clock import SystemClock, SystemJitter
from video_agent.gateway.errors import (
    NOTHING_PRESERVED,
    AliasGroupExhaustedError,
    AliasResolutionError,
    ContentPolicyError,
    ContextLengthExceededError,
    GatewayError,
    PaymentRequiredError,
    StructuredOutputError,
    UpstreamRequestError,
)
from video_agent.gateway.models import (
    AliasHealth,
    DegradeReason,
    LLMResponse,
    ModelHealth,
    Usage,
)
from video_agent.gateway.pricing import CostCalculator, cached_usage
from video_agent.gateway.prompts import is_canary
from video_agent.gateway.rendering import prompt_digest, render
from video_agent.gateway.retry import RetryPolicy
from video_agent.gateway.transport import TransportCall
from video_agent.observability.codes import ErrorCode
from video_agent.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pydantic import BaseModel

    from video_agent.config.aliases import Alias, AliasEntry, AliasTable
    from video_agent.gateway.breaker import CircuitBreaker
    from video_agent.gateway.cache import ResponseCacheStore
    from video_agent.gateway.capabilities import CapabilityRegistry
    from video_agent.gateway.classify import Classification
    from video_agent.gateway.clock import Clock, JitterSource
    from video_agent.gateway.models import CallContext, LLMRequest
    from video_agent.gateway.prompts import PromptRegistry
    from video_agent.gateway.rendering import RenderedPrompt
    from video_agent.gateway.transport import LLMTransport, TransportResult

__all__ = ["Gateway", "GatewayDeps", "LiteLLMGateway"]

_LOGGER = get_logger(__name__)

MILLISECONDS_PER_SECOND: Final = 1000

MODEL_CANARY_SCOPE: Final = "model:"
"""Namespace for the model canary's assignment key, so a model rollout and a prompt rollout of
the same name do not select the same 10% of jobs. Two changes riding one cohort cannot be
attributed to either."""

REFORMAT_DIRECTIVE: Final = (
    "Your previous reply was not valid JSON for the required schema. "
    "Reply with the JSON object only: no prose, no code fence, no commentary."
)
"""The single reformat attempt's added instruction. `gateway.md` §5.

A gateway mechanism, not a domain prompt, which is why it is a constant here rather than a
registry entry `[D-72]`. It carries no task content, changes only when the structured-output
mechanism changes, and putting it in the registry would make every structured call depend on a
second lookup that returns the same sentence every time.
"""

REFORMAT_IDEMPOTENCY_SUFFIX: Final = ":reformat"
"""The reformat call is a *different* logical request and gets a different idempotency key.

Retries of one logical call reuse the hint so a deduplicating upstream does not double-bill
`[gateway.md §4.1]`. The reformat is the opposite case: it asks for a different answer, and
reusing the key would let a deduplicating upstream return the same malformed reply it just
returned — turning the one corrective attempt into a guaranteed `VA-GW-004`.
"""


@dataclass(frozen=True, slots=True)
class GatewayDeps:
    """Everything the gateway needs, injected. One object, so wiring is one call site.

    A parameter object rather than nine constructor arguments: the set grows as the failure
    policy does, and a nine-argument constructor is one whose call sites drift into passing
    positionally.
    """

    table: AliasTable
    transport: LLMTransport
    capabilities: CapabilityRegistry
    prompts: PromptRegistry
    breaker: CircuitBreaker
    cache: ResponseCacheStore | None = None
    clock: Clock = field(default_factory=SystemClock)
    jitter: JitterSource = field(default_factory=SystemJitter)
    retry: RetryPolicy = field(default_factory=RetryPolicy)


class Gateway(Protocol):
    """`gateway.md` §2's interface. Callers depend on this, never on the implementation."""

    async def call(self, req: LLMRequest, *, ctx: CallContext) -> LLMResponse: ...

    async def health(self, alias: Alias) -> AliasHealth: ...


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One model's outcome: what it returned, plus tokens spent on a discarded first reply."""

    result: TransportResult
    extra_input_tokens: int = 0
    extra_output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class _CallPlan:
    """Everything decided before the first byte goes out, carried through the group walk.

    Assembled once so the walk cannot re-resolve, re-render or re-hash per attempt. Re-rendering
    per attempt would be the subtle way to break `idempotency_hint` stability: the same logical
    call would produce two different payloads and a deduplicating upstream would bill twice.
    """

    req: LLMRequest
    ctx: CallContext
    rendered: RenderedPrompt
    digest: str
    candidates: tuple[str, ...]
    started: float


type _JSONNode = str | int | float | bool | list["_JSONNode"] | dict[str, "_JSONNode"] | None


def _widen_schema_const_to_enum(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite every `{"const": x}` in a JSON schema to `{"enum": [x]}`, recursively.

    Pydantic emits `const` for a single-value `Literal` field (`aspect_ratio: Literal["16:9"]`),
    which is valid JSON Schema but outside one alias-group member's reduced structured-output
    dialect — a real request with such a field anywhere in the schema comes back
    `400 INVALID_ARGUMENT: Unknown name "const"`. `enum` with one member accepts exactly the same
    values `const` does, so this is a strict widening, never a validation change, and is applied
    unconditionally rather than gated by model — a schema every provider accepts is not a schema
    any provider need be protected from.
    """
    widened = _widen_node(schema)
    assert isinstance(widened, dict)  # a dict in, a dict out; recursion preserves shape
    return widened


def _widen_node(node: _JSONNode) -> _JSONNode:
    if isinstance(node, dict):
        widened = {key: _widen_node(value) for key, value in node.items()}
        if "const" in widened:
            widened["enum"] = [widened.pop("const")]
        return widened
    if isinstance(node, list):
        return [_widen_node(item) for item in node]
    return node


class LiteLLMGateway:
    """The single egress. Constructed once per process and shared."""

    def __init__(self, deps: GatewayDeps) -> None:
        self._deps = deps
        self._cost = CostCalculator(deps.table)

    # --- Public interface -----------------------------------------------------------------

    async def call(self, req: LLMRequest, *, ctx: CallContext) -> LLMResponse:
        """Serve one logical LLM call, or fail with a code, a trace and three sentences."""
        plan = await self._plan(req, ctx)
        cached = await self._read_cache(plan.req)
        if cached is not None:
            return self._from_cache(plan, cached)
        return await self._serve(plan)

    async def health(self, alias: Alias) -> AliasHealth:
        """Per-model circuit state for one group. `healthy` only if something admits traffic."""
        entry = self._resolve(alias)
        members: list[ModelHealth] = []
        for model in entry.models:
            state = await self._deps.breaker.state(dependency_key(alias, model))
            members.append(
                ModelHealth(
                    model=model,
                    state=state.value,
                    admits_traffic=state is not CircuitState.OPEN,
                )
            )
        return AliasHealth(
            alias=alias,
            healthy=any(member.admits_traffic for member in members),
            models=tuple(members),
        )

    # --- Planning -------------------------------------------------------------------------

    async def _plan(self, req: LLMRequest, ctx: CallContext) -> _CallPlan:
        started = self._deps.clock.monotonic()
        entry = self._resolve(req.alias)
        candidates = await self._candidates(req.alias, entry, ctx.job_id)
        template = self._deps.prompts.get_prompt(req.prompt_ref.name, job_id=ctx.job_id)
        if template.stale:
            ctx.mark_degraded(DegradeReason.STALE_PROMPT)
        resolved = req.model_copy(update={"prompt_ref": template.ref})
        rendered = render(
            template=template.body,
            variables=resolved.variables,
            untrusted=resolved.untrusted,
        )
        self._record_quarantine(rendered, resolved)
        return _CallPlan(
            req=resolved,
            ctx=ctx,
            rendered=rendered,
            digest=prompt_digest(rendered),
            candidates=candidates,
            started=started,
        )

    def _resolve(self, alias: Alias) -> AliasEntry:
        try:
            return self._deps.table.resolve(alias)
        except AliasConfigError as exc:
            raise AliasResolutionError(
                what_happened=f"alias {alias.value!r} is not in the alias table.",
                what_to_do_next=(
                    f"add a {alias.value!r} group to config/aliases.yaml and restart. The "
                    f"gateway never guesses a model."
                ),
            ) from exc

    async def _candidates(self, alias: Alias, entry: AliasEntry, job_id: str) -> tuple[str, ...]:
        """The group's models in failover order, after every one has passed the capability check.

        The whole group is checked, not only the head. Checking lazily would let a
        capability-deficient fallback sit undetected until the day the primary failed — which
        is the one day nobody wants to discover that the group's second choice cannot see an
        image. `gateway.md` §3 rule 3 says resolution fails closed; this is resolution.
        """
        ordered = self._failover_order(entry, alias, job_id)
        deficient = {
            model: missing
            for model in ordered
            if (
                missing := missing_capabilities(
                    entry.required_capabilities,
                    await self._deps.capabilities.capabilities(model),
                )
            )
        }
        if deficient:
            detail = "; ".join(
                f"{model} lacks {', '.join(missing)}"
                for model, missing in sorted(deficient.items())
            )
            raise AliasResolutionError(
                what_happened=(
                    f"alias {alias.value!r} requires "
                    f"{', '.join(entry.required_capabilities)} and {detail}."
                ),
                what_to_do_next=(
                    "correct the group in config/aliases.yaml, or check that the proxy serves "
                    "and declares these models. A capability-deficient model is never "
                    "substituted silently."
                ),
            )
        return ordered

    @staticmethod
    def _failover_order(entry: AliasEntry, alias: Alias, job_id: str) -> tuple[str, ...]:
        """Primary first, unless this job is in the canary cohort, then every other member.

        Canary assignment is deterministic per `job_id` `[D-20]`, so every shot of one job
        resolves to the same head model. A per-call draw would satisfy the 10% and still mix
        two models inside a single job, and the continuity between shots is exactly what the
        canary is being measured on.

        The order never leaves the group. `gateway.md` §3 rule 2: a `vision-default` failure
        that fell back to `reasoning-high` would answer a question about an image with a model
        that never saw it.
        """
        head = entry.primary.model
        canary = entry.canary
        if canary is not None and is_canary(
            job_id, f"{MODEL_CANARY_SCOPE}{alias.value}", canary.traffic_pct
        ):
            head = canary.model
        return (head, *[model for model in entry.models if model != head])

    # --- Serving --------------------------------------------------------------------------

    async def _serve(self, plan: _CallPlan) -> LLMResponse:
        refused = 0
        for index, model in enumerate(plan.candidates):
            key = dependency_key(plan.req.alias, model)
            if await self._deps.breaker.allows(key) is Admission.REFUSE:
                refused += 1
                continue
            attempt = await self._attempt_model(plan, model=model, key=key)
            if attempt is None:
                continue
            reason = DegradeReason.FALLBACK if index > 0 else self._stale_reason(plan)
            if reason is not None:
                plan.ctx.mark_degraded(reason)
            return await self._respond(plan, attempt, model=model, reason=reason)
        raise AliasGroupExhaustedError(
            what_happened=(
                f"every model in alias group {plan.req.alias.value!r} failed or is "
                f"circuit-open ({len(plan.candidates)} tried, {refused} refused by an open "
                f"circuit)."
            ),
            what_was_preserved=NOTHING_PRESERVED,
            what_to_do_next=(
                "check upstream status for this group and retry the job; completed work is "
                "checkpointed and is not repeated on resume."
            ),
        )

    @staticmethod
    def _stale_reason(plan: _CallPlan) -> DegradeReason | None:
        if DegradeReason.STALE_PROMPT in plan.ctx.degrade_reasons:
            return DegradeReason.STALE_PROMPT
        return None

    async def _attempt_model(self, plan: _CallPlan, *, model: str, key: str) -> _Attempt | None:
        """Up to three attempts against one model. `None` means "try the next model".

        `None` rather than an exception for the fall-over case, because "this model did not
        work" is ordinary control flow inside a failover group, while every exception raised
        from here ends the call for the whole group at once.
        """
        policy = self._deps.retry
        for attempt in policy.attempt_numbers():
            try:
                call = self._build_call(plan, model)
                result = await self._deps.transport.complete(call)
            except Exception as exc:
                classification = classify(exc)
                if not classification.retryable and not classification.may_fall_back:
                    raise self._as_error(classification, model=model) from exc
                await self._deps.breaker.record_failure(key)
                if not classification.retryable or policy.is_last(attempt):
                    return None
                await self._deps.clock.sleep(policy.delay(attempt, self._deps.jitter))
                continue
            await self._deps.breaker.record_success(key)
            return await self._validate(plan, model=model, result=result)
        return None

    async def _validate(self, plan: _CallPlan, *, model: str, result: TransportResult) -> _Attempt:
        """Validate structured output, with exactly one reformat attempt. `gateway.md` §5."""
        response_model = plan.req.response_model
        if response_model is None or self._parse(response_model, result.text) is not None:
            return _Attempt(result=result)
        second = await self._deps.transport.complete(self._build_call(plan, model, reformat=True))
        if self._parse(response_model, second.text) is None:
            raise StructuredOutputError(
                what_happened=(
                    f"structured output did not validate against {response_model.__name__} "
                    f"after one reformat attempt."
                ),
                what_was_preserved=NOTHING_PRESERVED,
                what_to_do_next=(
                    "review the prompt for this call: the model was asked twice and did not "
                    "produce the required shape. This is not retried further."
                ),
            )
        return _Attempt(
            result=second,
            extra_input_tokens=result.input_tokens,
            extra_output_tokens=result.output_tokens,
        )

    @staticmethod
    def _parse(model_type: type[BaseModel] | None, text: str | None) -> BaseModel | None:
        """Validate `text` against the schema, returning `None` if it does not comply."""
        if model_type is None or not text:
            return None
        try:
            payload: Any = json.loads(text)
        except ValueError:
            return None
        try:
            return model_type.model_validate(payload)
        except ValidationError:
            return None

    def _build_call(self, plan: _CallPlan, model: str, *, reformat: bool = False) -> TransportCall:
        req = plan.req
        instruction = plan.rendered.instruction
        idempotency = req.idempotency_hint
        if reformat:
            instruction = f"{instruction}\n\n{REFORMAT_DIRECTIVE}"
            if idempotency is not None:
                idempotency = f"{idempotency}{REFORMAT_IDEMPOTENCY_SUFFIX}"
        schema = (
            _widen_schema_const_to_enum(req.response_model.model_json_schema())
            if req.response_model is not None
            else None
        )
        return TransportCall(
            model=model,
            instruction=instruction,
            untrusted_block=plan.rendered.untrusted_block,
            max_output_tokens=req.max_output_tokens,
            temperature=req.temperature,
            timeout_s=req.timeout_s,
            idempotency_key=idempotency,
            response_schema=schema,
            image_keys=tuple(image.storage_key for image in req.images),
        )

    # --- Responses ------------------------------------------------------------------------

    async def _respond(
        self, plan: _CallPlan, attempt: _Attempt, *, model: str, reason: DegradeReason | None
    ) -> LLMResponse:
        result = attempt.result
        usage = self._cost.usage_for(
            model=model,
            input_tokens=result.input_tokens + attempt.extra_input_tokens,
            output_tokens=result.output_tokens + attempt.extra_output_tokens,
        )
        response = self._build_response(
            plan, model=model, text=result.text, usage=usage, reason=reason
        )
        await self._write_cache(plan.req, response)
        self._log(plan, response)
        return response

    def _from_cache(self, plan: _CallPlan, cached: CachedResponse) -> LLMResponse:
        plan.ctx.mark_degraded(DegradeReason.CACHE)
        response = self._build_response(
            plan,
            model=cached.model_used,
            text=cached.text,
            usage=cached_usage(),
            reason=DegradeReason.CACHE,
        )
        self._log(plan, response)
        return response

    def _build_response(
        self,
        plan: _CallPlan,
        *,
        model: str,
        text: str | None,
        usage: Usage,
        reason: DegradeReason | None,
    ) -> LLMResponse:
        elapsed = self._deps.clock.monotonic() - plan.started
        return LLMResponse(
            parsed=self._parse(plan.req.response_model, text),
            text=text,
            model_used=model,
            alias=plan.req.alias,
            prompt_version=plan.req.prompt_ref.version,
            usage=usage,
            latency_ms=max(0, int(elapsed * MILLISECONDS_PER_SECOND)),
            degraded=reason is not None,
            degrade_reason=reason,
            generation_id=uuid4().hex,
        )

    # --- Cache ----------------------------------------------------------------------------

    async def _read_cache(self, req: LLMRequest) -> CachedResponse | None:
        if self._deps.cache is None or not is_cacheable(req.prompt_ref.name):
            return None
        raw = await self._deps.cache.get(cache_key(req))
        return CachedResponse.from_json(raw) if raw is not None else None

    async def _write_cache(self, req: LLMRequest, response: LLMResponse) -> None:
        if self._deps.cache is None or not is_cacheable(req.prompt_ref.name):
            return
        if response.text is None:
            return
        entry = CachedResponse(
            text=response.text,
            model_used=response.model_used,
            prompt_version=response.prompt_version,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        await self._deps.cache.set(cache_key(req), entry.to_json(), CACHE_TTL_SECONDS)

    # --- Errors and observability ---------------------------------------------------------

    @staticmethod
    def _as_error(classification: Classification, *, model: str) -> GatewayError:
        """Turn a classified upstream failure into the typed error `gateway.md` §8 names."""
        if classification.code is ErrorCode.VA_GW_005:
            return ContextLengthExceededError(
                what_happened="the rendered prompt exceeded the model's context window.",
                what_was_preserved=NOTHING_PRESERVED,
                what_to_do_next=(
                    "shorten the inputs to this call. Nothing was truncated automatically: a "
                    "silently truncated continuity bible breaks every shot after it."
                ),
            )
        if classification.code is ErrorCode.VA_GW_006:
            return ContentPolicyError(
                what_happened="the upstream refused this request on content policy.",
                what_was_preserved=NOTHING_PRESERVED,
                what_to_do_next="revise the prompt for this stage and resubmit the job.",
            )
        if classification.code is ErrorCode.VA_PROV_009:
            return PaymentRequiredError(
                what_happened="the upstream reported that credits are exhausted (402).",
                what_was_preserved=NOTHING_PRESERVED,
                what_to_do_next=(
                    "top up the upstream account. This is neither retried nor failed over: "
                    "retrying cannot succeed and would only delay the escalation."
                ),
            )
        return UpstreamRequestError(
            what_happened=f"the proxy rejected the request for a model in this group ({model}).",
            what_was_preserved=NOTHING_PRESERVED,
            what_to_do_next=(
                "check the gateway's own configuration and credential; the request itself was "
                "refused, so no other model in the group would accept it either."
            ),
        )

    @staticmethod
    def _record_quarantine(rendered: RenderedPrompt, req: LLMRequest) -> None:
        """Emit one `VA-SEC-001` line per escaped span. `AGENT.md` §1.4.

        The field name and the kind, never the matched text: the matched text is the
        attacker-controlled string, and logging it is how instruction-shaped content reaches an
        operator's terminal.
        """
        for event in rendered.events:
            _LOGGER.warning(
                "instruction-shaped content quarantined in untrusted input",
                extra={
                    "event": "untrusted_content_quarantined",
                    "code": event.code.value,
                    "reason": f"{event.kind} in field {event.field}",
                    "prompt_name": req.prompt_ref.name,
                    "prompt_version": req.prompt_ref.version,
                },
            )

    @staticmethod
    def _log(plan: _CallPlan, response: LLMResponse) -> None:
        """One line per completed call. The prompt is a reference and a digest, never text."""
        _LOGGER.info(
            "llm call completed",
            extra={
                "event": "llm_call",
                "alias": response.alias.value,
                "model_used": response.model_used,
                "prompt_name": plan.req.prompt_ref.name,
                "prompt_version": response.prompt_version,
                "prompt_sha256": plan.digest,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cost_usd": response.usage.cost_usd,
                "latency_ms": response.latency_ms,
                "degraded": response.degraded,
            },
        )
