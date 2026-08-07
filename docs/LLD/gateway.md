---
doc: LLD
module: gateway
title: Gateway — LiteLLM proxy, alias resolution and failure policy
status: canonical
implementation_status: built
version: 1
last_synced_commit: 438138573aa69c26727651b368e056262908bc69
generated_by: cdr-documentation
run_id: 2026-08-08/001-documentation
sources:
  - docs/specs/common-platform-spec.md
  - docs/specs/video-agent-prd.md
  - docs/HLD.md
---

# LLD — `gateway`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — BUILT.** **E0 — in the v1 build.** Alias resolution, retry/fallback/circuit-break and the prompt path all ship.
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

**LiteLLM proxy — single egress for every model call.** `[CPS §Canonical stack]`

This module is the only path from application code to an LLM. It:

- resolves a **logical alias** to a concrete model at call time, so code never names a
  provider and swapping models is a config change with zero code diff `[CPS §Model routing]`;
- applies the inherited failure policy — retry, fallback, circuit break, degrade, fail
  honestly `[CPS §Failure behaviour]`;
- reports model, tokens, cost and prompt version to the harness ledger and to Langfuse
  `[CPS §Observability]`.

**Scope boundary.** Video generation is *not* an LLM call and does not traverse LiteLLM; it
egresses through [`providers.md`](./providers.md), which implements the *same* alias-only and
failure policy. `[D-06]` The two egresses share this module's policy engine so the behaviour
cannot drift.

## 2. Public interface

```python
class Alias(StrEnum):                       # [CPS §Model routing] — the complete set
    REASONING_HIGH = "reasoning-high"
    REASONING_FAST = "reasoning-fast"
    REALTIME_VOICE = "realtime-voice"       # no consumer in v1 [D-13]
    EMBED_DEFAULT  = "embed-default"        # no consumer in v1 [D-13]
    VISION_DEFAULT = "vision-default"

class LLMRequest(BaseModel):
    alias: Alias                            # never a model name
    prompt_ref: PromptRef                   # registry name + version, not a raw string
    variables: dict[str, Any]               # trusted values
    untrusted: dict[str, str] = {}          # quarantined; rendered in a delimited block
    images: list[ArtifactRef] = []          # by reference; bytes fetched inside the gateway
    response_model: type[BaseModel] | None  # structured output; None means free text
    max_output_tokens: int
    temperature: float = 0.0
    timeout_s: float
    idempotency_hint: str | None            # stable across retries of the same logical call

class LLMResponse(BaseModel):
    parsed: BaseModel | None
    text: str | None
    model_used: str                         # concrete model, for observability ONLY
    alias: Alias
    prompt_version: str
    usage: Usage                            # input/output tokens, cost_usd
    latency_ms: int
    degraded: bool                          # true if served by fallback or from cache
    degrade_reason: str | None
    generation_id: str                      # Langfuse generation

class Gateway(Protocol):
    async def call(self, req: LLMRequest, *, ctx: NodeContext) -> LLMResponse: ...
    async def health(self, alias: Alias) -> AliasHealth: ...
```

`model_used` exists so a trace can answer "which model produced this". **Application code may
not branch on it.** A lint rule forbids comparing `model_used` to a literal.

## 3. Alias resolution

Aliases resolve **at the gateway**, from config, at call time. `[CPS §Model routing]`

```yaml
# config/aliases.yaml — the only place model names exist
aliases:
  reasoning-high:
    primary:  { model: "<vendor>/<model>", weight: 100 }
    fallbacks: [ { model: "<vendor-b>/<model>" }, { model: "<vendor-c>/<model>" } ]
    canary:   { model: "<vendor>/<model-next>", traffic_pct: 10 }   # [CPS §Rollout]
  vision-default:
    primary:  { model: "<vendor>/<vision-model>" }
    fallbacks: [ { model: "<vendor-b>/<vision-model>" } ]
    required_capabilities: [ "image_input", "structured_output" ]
```

Rules:

1. **No provider string in application code, ever.** Enforced by a CI check that greps the
   source tree for vendor names outside `config/` and the gateway's own adapter layer. This
   is restated as an agent hard rule in [`AGENT.md`](../../AGENT.md).
2. **An alias group is a failover unit.** Fallback is to an *alternate model within the alias
   group* `[CPS §Failure behaviour]` — never across groups. A `vision-default` failure never
   falls back to `reasoning-high`.
3. **Capability-checked.** If a resolved model lacks a `required_capability`, resolution
   fails closed with `VA-GW-002` rather than silently degrading quality.
4. **Canary at 10%.** Model and prompt changes go to 10% of traffic first and are promoted
   only after their Langfuse scores hold against the incumbent. `[CPS §Rollout]` Assignment
   is deterministic per `job_id`, so a single job never mixes models across its shots — which
   would itself be a continuity hazard. `[D-20]`

## 4. Failure policy

Inherited verbatim in substance from `[CPS §Failure behaviour]`.

### 4.1 Retry
Exponential backoff **with jitter**, **retryable errors only**, **max 3**.

```
attempt n delay = min(base * 2**(n-1), cap) * uniform(0.5, 1.5)     base=0.5s, cap=8s
```

| Retryable | Not retryable |
| --- | --- |
| 408, 429, 500, 502, 503, 504 | 400, 401, 403, 404, 422 |
| Connection reset, DNS, read timeout | Content-policy rejection |
| Provider "overloaded"/"capacity" | Context-length exceeded |
| | Schema validation failure after 1 reformat attempt |

Retries reuse `idempotency_hint` so a provider that deduplicates does not double-bill.
"Max 3" is 3 **attempts total**, not 3 retries after the first.

### 4.2 Fallback
On exhausted retries, or immediately on a non-retryable *availability* error, try the next
model **within the alias group**. Each fallback gets its own retry budget. A response served
by a fallback is returned with `degraded=true` and the reason — always flagged.
`[CPS §Failure behaviour]`

### 4.3 Circuit break
**Per dependency, 5 failures in 30s.** `[CPS §Failure behaviour]` The dependency key is
`(alias, concrete_model)`, so one sick model does not open the circuit for its healthy
siblings in the same group.

```
CLOSED --5 failures in a 30s sliding window--> OPEN (30s)
OPEN   --cooldown elapsed--> HALF_OPEN (1 probe)
HALF_OPEN --probe ok--> CLOSED     |     --probe fails--> OPEN (doubled, cap 5 min)
```

State lives in Redis so all workers share one view. `[CPS §Canonical stack]` An open circuit
skips straight to fallback; all circuits in a group open → `VA-GW-001`.

### 4.4 Degrade
A cached, stale or partial result, **always flagged**. `[CPS §Failure behaviour]` What this
module may degrade to, in order:

| Degrade | Allowed for | Not allowed for |
| --- | --- | --- |
| Fallback model in the group | all aliases | — |
| Cached identical call (same prompt version + variables hash), TTL 1h | `qc_shot` re-scores of an unchanged artifact | `plan_story`, `lock_bible` — the bible must be freshly derived |
| Reduced `max_output_tokens` | free-text calls | structured-output calls |
| Returning `None` and letting the node fail | never silently | — |

Every degrade sets `degraded=true` on the response, propagates to `Job.degraded`, and is
recorded as a Langfuse event. A degraded result is never presented as a clean one.

### 4.5 Fail honestly
When the policy is exhausted, the gateway raises a typed error carrying **what happened**,
**what was preserved** and **what to do next**, plus a stable code and the `trace_id`
`[CPS §Failure behaviour]`. It never returns an empty or fabricated response.

## 5. Prompts and untrusted content

- Prompts come from the **Langfuse prompt registry** by name and version; a raw prompt string
  in application code is a CI failure. `[CPS §Observability]`
- `variables` are trusted; `untrusted` values are rendered inside a delimited, labelled block
  with instruction-shaped content escaped. Untrusted content never issues instructions.
  `[CPS §Non-negotiables]` The gateway is the last enforcement point before the wire, after
  the harness's own quarantine.
- Structured output is requested via the provider's native schema mode where available. On a
  parse failure the gateway makes **one** reformat attempt, then classifies the error as
  non-retryable.

## 6. Usage, cost and observability

Every call emits a Langfuse **generation** with model, tokens, cost and prompt version
`[CPS §Observability]`, nested under the calling node's span, and returns `Usage` to the
harness ledger. Cost is computed from a per-model price table in the same config file as the
aliases; an unknown model prices at a configured pessimistic ceiling rather than zero, so an
unpriced model can never look free to a budget cap. `[D-21]`

**Never logged:** credentials, raw PII, full media payloads. `[CPS §Observability]` The
gateway logs the prompt **reference and variable hashes**, not the rendered prompt, and image
inputs by artifact key, never as base64.

## 7. Dependencies

| Depends on | For |
| --- | --- |
| LiteLLM proxy (infrastructure) | the actual egress |
| [`observability.md`](./observability.md) | generation records, error codes, redaction |
| [`persistence.md`](./persistence.md) | Redis circuit state, response cache |
| [`harness.md`](./harness.md) | receives `Usage`; obeys pre-flight budget veto |

Depends on no domain module. `planning`, `qc` and `providers` depend on it.

## 8. Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| Alias not in config | Resolution | `VA-GW-002`, non-retryable, fail closed. Never guess a model. |
| Primary model 429/5xx | HTTP status | Retry ≤3 with jitter → fallback → `degraded=true`. |
| Whole alias group down | All circuits open | `VA-GW-001`, `503` upstream; harness terminates `PARTIAL` if anything is preserved. |
| Structured output unparseable | Schema validation | One reformat attempt, then `VA-GW-004` non-retryable. |
| Context length exceeded | Provider error | Non-retryable `VA-GW-005`. Do not silently truncate the bible — a truncated bible breaks continuity. |
| Content policy rejection | Provider error | Non-retryable `VA-GW-006`, surfaced honestly to the user with the offending stage named. |
| Unpriced model | Missing price entry | Charge the pessimistic ceiling and raise a config alarm. Never charge zero. |
| Redis down (circuit state) | Connection error | Fail **closed** on circuit state: treat as CLOSED but disable cross-worker sharing, and alarm. Retry/fallback still apply. `[D-22]` |
| Canary model scores worse | Langfuse score comparison | Automatic rollback of the canary to 0% and an alarm. `[CPS §Rollout]` |
| Cost regression > 20% | CI eval gate | Merge blocked. `[CPS §Non-negotiables]` |

## 9. Test strategy

| Level | Tests |
| --- | --- |
| Static | CI grep: no vendor/provider name outside `config/` and the adapter layer; no raw prompt strings; no branch on `model_used`. |
| Retry | Deterministic-jitter test asserting exactly 3 attempts, monotonic backoff, and **zero** retries for each non-retryable class. |
| Fallback | Group exhaustion order; assert failover never crosses alias groups; assert `degraded=true` propagates to `Job.degraded`. |
| Circuit | Time-controlled test of the 5-in-30s threshold, OPEN duration, single HALF_OPEN probe, and per-`(alias, model)` isolation. |
| Cache | Assert `plan_story` and `lock_bible` are never served from cache; assert cache keys include the prompt version. |
| Cost | Golden test of the price table; assert an unknown model prices at the ceiling; assert ledger totals equal the sum of generation costs. |
| Injection | Untrusted block containing role markers and tool syntax; assert escaping and that no instruction reaches the instruction section. |
| Canary | Deterministic per-`job_id` assignment; assert all shots of one job use one model. |
| Contract | Recorded-response fixtures per model family so a provider API change is caught in CI, not in production. |
