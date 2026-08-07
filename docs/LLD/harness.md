---
doc: LLD
module: harness
title: Harness — loop engine, budgets and termination
status: canonical
implementation_status: partial
version: 1
last_synced_commit: 438138573aa69c26727651b368e056262908bc69
generated_by: cdr-documentation
run_id: 2026-08-08/001-documentation
sources:
  - docs/specs/common-platform-spec.md
  - docs/specs/video-agent-prd.md
  - docs/HLD.md
---

# LLD — `harness`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — PARTIAL.** **E0–E1 — in the v1 build.** The loop, context/tool ownership and termination outcomes ship. **Hard budget caps and no-progress detection are designed here but deferred to E4** (cost caps).
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

> The harness owns context, tools, budgets and termination. **The model is a component inside
> it, never the controller.** `[CPS §Agent harness & loop engine]`

This module is that harness. It owns four things and nothing else owns any of them:

| Owns | Meaning here |
| --- | --- |
| **Context** | Every node receives its context *from* the harness. A node never fetches its own inputs. This is also the sanitisation boundary for untrusted content. |
| **Tools** | The tool registry. A node may call a tool only if the harness granted it for that node. |
| **Budgets** | A single ledger per job for iterations, wall-clock, tokens and USD. Hard caps. `[CPS §Non-negotiables]` |
| **Termination** | `harness.decide()` is the **only** function in the system that may end a job. |

It does **not** own the topology (that is [`graph.md`](./graph.md)) or any domain logic.
The relationship is: the graph proposes the next node; the harness may veto.

The loop is `observe → think → act → evaluate → repeat | terminate | escalate`
`[CPS §Agent harness]`, mapped one-to-one onto a LangGraph superstep — see
[HLD §4](../HLD.md#4-where-the-harness-sits).

## 2. Public interface

```python
class Phase(StrEnum):
    OBSERVE = "observe"; THINK = "think"; ACT = "act"; EVALUATE = "evaluate"

class Outcome(StrEnum):                       # [CPS §Agent harness]
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED_NO_PROGRESS = "FAILED_NO_PROGRESS"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"

class Verdict(StrEnum):
    CONTINUE = "continue"; TERMINATE = "terminate"; ESCALATE = "escalate"

class Decision(BaseModel):
    verdict: Verdict
    outcome: Outcome | None          # set iff verdict != CONTINUE
    reason_code: str                 # stable error/termination code
    human_reason: str                # "what happened", for the error envelope
    degraded: bool

class BudgetCaps(BaseModel):
    """Hard caps. [CPS §Non-negotiables]. Values are config; see §4."""
    max_iterations: int
    max_wall_clock_s: float
    max_tokens: int
    max_usd: Decimal

class BudgetLedger(BaseModel):
    caps: BudgetCaps
    iterations_used: int = 0
    started_at: datetime
    tokens_used: int = 0
    usd_spent: Decimal = Decimal("0")

    def wall_clock_s(self, now: datetime) -> float: ...
    def exceeded(self, now: datetime) -> BudgetBreach | None: ...
    def would_exceed(self, est: CostEstimate, now: datetime) -> BudgetBreach | None: ...

class FailureSignature(BaseModel):
    scope: Literal["shot", "job"]                 # [D-02]
    node: str
    code: str
    discriminator: str        # e.g. sorted failing QC dimensions, or provider error class
    def digest(self) -> str:  # sha256 over the fields above
        ...

class Harness(Protocol):
    async def observe(self, job_id: UUID, node: str) -> NodeContext: ...
    async def charge(self, job_id: UUID, usage: Usage) -> None: ...
    async def record_failure(self, job_id: UUID, sig: FailureSignature) -> RepeatInfo: ...
    async def decide(self, state: JobState, node: str) -> Decision: ...
    async def cancel(self, job_id: UUID, actor: str) -> None: ...
```

`decide()` is called by **every** conditional edge in the graph, before any node-local
routing condition is evaluated. That ordering is what makes budget exhaustion and no-progress
able to pre-empt the repair back-edge.

## 3. Context and tool ownership

### 3.1 Context assembly

```python
class NodeContext(BaseModel):
    job_id: UUID
    node: str
    trace_id: str
    bible: ContinuityBible | None      # frozen; hash-verified on load
    beat: Beat | None
    chained_frame_ref: ArtifactRef | None
    prior_findings: list[QCFinding]    # sanitised
    budget_remaining: BudgetView
    tools: frozenset[str]              # exactly what this node may call
```

Rules the implementation must uphold:

1. **Nodes are pure with respect to input.** A node reads `NodeContext` and nothing else. No
   node opens a DB session or reads Redis directly.
2. **The bible is verified, not trusted.** Its content hash is checked on every load. A
   mismatch is `VA-BIBLE-002` and terminates the job — a mutated bible invalidates every
   downstream shot.
3. **Untrusted content is quarantined.** Provider responses, MCP tool output, QC model
   rationale and the user's own prompt are *data*. They enter prompts inside a delimited,
   labelled block and are never concatenated into the instruction section.
   `[CPS §Non-negotiables]` The harness strips or escapes instruction-shaped content
   (role markers, "ignore previous", tool-call syntax) before it reaches a prompt, and
   records a `VA-SEC-001` observation when it does. A model's output can therefore change
   *content*, never *control flow*.
4. **Tool grants are per node.** `plan_story` gets no video tool; `qc_shot` gets read-only
   artifact access; only `generate_shot` may call a provider. A call to an ungranted tool is
   a programming error and raises, rather than being silently allowed.

### 3.2 Tool registry

```python
class ToolSpec(BaseModel):
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    cost_estimator: Callable[[BaseModel], CostEstimate]
    retryable_errors: frozenset[type[Exception]]

GRANTS: dict[str, frozenset[str]] = {
    "plan_story":         frozenset({"llm.reasoning_high"}),
    "lock_bible":         frozenset({"llm.reasoning_high"}),
    "select_next_shot":   frozenset(),
    "generate_shot":      frozenset({"llm.reasoning_fast", "video.generate", "artifact.write"}),
    "extract_final_frame":frozenset({"ffmpeg.extract_frame", "artifact.write"}),
    "qc_shot":            frozenset({"llm.vision_default", "artifact.read"}),
    "assemble":           frozenset({"ffmpeg.concat", "ffmpeg.thumbnail", "artifact.write"}),
    "deliver":            frozenset({"artifact.presign"}),
    "finalize":           frozenset(),
}
```

Tool names are **capabilities, never providers**. There is no `magichour.generate` and no
`higgsfield.generate`. This is what allowed the v1 provider to change without touching a
single tool grant `[D-58]`. `[CPS §Model routing]`, `[D-06]`

## 4. Budget caps

Hard caps on iterations, wall-clock, tokens and dollars. `[CPS §Non-negotiables]` `[CPS]` sets
no numbers, so these are `[D-08]` and live in config, not code:

| Cap | Default | Reasoning |
| --- | --- | --- |
| `max_iterations` | 40 supersteps | Ceiling path is 2 (plan, bible) + 4 shots × (select + generate + extract + qc) × up to 3 attempts + assemble + deliver + finalize ≈ 4 + 48… so 40 deliberately bites *before* the theoretical worst case, forcing `PARTIAL` rather than a maximal-cost run. |
| `max_wall_clock_s` | 1200 (20 min) | 2.5× the `≤ 8 min` p90 target `[PRD §Success metrics]`. A safety net, not the common path. |
| `max_tokens` | 250 000 | Planning + bible + 4×3 QC passes with generous headroom. |
| `max_usd` | `tenant.max_usd_per_job`, falling back to `BUDGET_MAX_USD_PER_JOB` when NULL `[D-70]` | The PRD's named mitigation for "repair loops blow the budget" is a **hard USD cap** `[PRD §Key risks]`. Video generation dominates cost, so this is the cap that actually protects spend. |

Enforcement rules:

- **Pre-flight.** Before an expensive act (`video.generate` above all), the harness calls
  `would_exceed(estimate)`. If the estimate would breach a cap, the call is **not made** and
  the job terminates `PARTIAL`. Discovering exhaustion after paying for the clip defeats the
  cap.
- **Post-charge.** Actual usage is charged from the gateway/provider response, so estimation
  error self-corrects.
- **Costed in USD from a configured rate.** The provider bills credits; the ledger converts
  using a rate derived from `MAGICHOUR_USD_PER_1K_CREDITS` `[D-65]`. **Volume discounts are
  never applied pre-flight** — an undiscounted rate over-estimates spend, so the cap trips
  early rather than late. A cap that errs toward under-spending is correct; one that errs
  toward over-spending is not a cap. Discounts are reconciliation-time credits only.
- **Monotonic per finalised charge.** The ledger only increases once a charge is *final*.
  The v1 provider bills in **credits** and reports a **provisional** amount that is settled —
  and refunded on a failed render — only at terminal status, so a provisional charge may be
  corrected **exactly once** when it settles `[D-60]`. It may never be revised twice, and
  pre-flight checks always use the estimate, so an under-estimate cannot authorise a call the
  cap would have refused.
- **Persisted per node.** The ledger is written into the checkpoint after every node
  `[CPS §Non-negotiables]`, so a resumed job cannot reset its own budget. This is the
  mechanism behind "completed shots are never re-billed" `[PRD §Resilience]`.
- **Breach is always `PARTIAL`, never `FAILED`** `[CPS §Agent harness]` — best-so-far,
  flagged `degraded`. If zero shots succeeded, `assemble` finds nothing and the outcome
  degrades to `FAILED` at `finalize`, which is the zero-deliverable case.

## 5. Termination decisions

`decide()` evaluates in this fixed order. First match wins; the order encodes precedence.

```
1. cancelled?               → ESCALATE / FAILED   (operator or client cancel)   [D-12]
2. non-retryable error?     → TERMINATE FAILED                     VA-*-*
3. job-scope signature x2?  → TERMINATE FAILED_NO_PROGRESS         VA-INT-002   [D-02]
4. budget exceeded?         → TERMINATE PARTIAL (degraded=true)    VA-BUDGET-*
5. evaluator satisfied?     → TERMINATE SUCCESS
6. otherwise                → CONTINUE
```

Rule 3 sits **above** rule 4 because `[CPS]` says a repeated failure signature stops
*immediately* — burning the remaining budget on a known-dead path is exactly the failure the
rule exists to prevent.

"Evaluator satisfied" for a video job means: all four shots `accepted`, `assemble` and
`deliver` completed, manifest non-empty.

Mapping to the four outcomes is in [HLD §5](../HLD.md#5-termination-outcomes).

## 6. No-progress detection

`[CPS §Agent harness]`: *same failure signature twice → `FAILED_NO_PROGRESS`, stop
immediately.* The PRD's repair loop deliberately retries the same shot, so signatures are
**scoped** `[D-02]`.

### 6.1 Signature construction

| Field | Shot-scope example | Job-scope example |
| --- | --- | --- |
| `node` | `qc_shot` | `plan_story` |
| `code` | `VA-QC-002` | `VA-PLAN-002` |
| `discriminator` | `shot=2;dims=character,lighting;band=0.55-0.60` | `beats_sum!=40` |

The QC discriminator includes a **score band** (0.05-wide bucket), so a repair that improves
the score by at least 0.05 produces a *different* signature and counts as progress. A repair
that lands in the same band with the same failing dimensions is no progress. `[D-18]`

### 6.2 Scope effects

- **Shot scope, seen twice** → the shot is abandoned immediately, even if `repairs_used < 2`.
  Spending the second repair on a provably stuck shot is the wasted spend `[PRD §Key risks]`
  warns about. Its best attempt is retained for partial assembly. The **job continues**, which
  is what keeps "never returns nothing" true `[PRD §Resilience]`.
- **Job scope, seen twice** → `FAILED_NO_PROGRESS`, stop immediately.
- A shot-scope signature that recurs on a **different shot index** is promoted to job scope:
  the same defect reproducing across shots is a systemic fault, not a bad roll.

Signatures live in Redis (`sig:{job_id}` hash → count) with the job's TTL and are mirrored
into the checkpoint, so a resumed job does not forget what already failed.

## 7. Dependencies

| Depends on | For |
| --- | --- |
| [`persistence.md`](./persistence.md) | ledger and signature durability, checkpoint co-write, job status transitions |
| [`observability.md`](./observability.md) | span per phase, generation cost ingestion, reason codes |
| [`gateway.md`](./gateway.md) | token/USD usage reporting for LLM calls |
| [`providers.md`](./providers.md) | cost estimates and actual charges for video calls |
| [`graph.md`](./graph.md) | *is a consumer of this module* — the dependency points inward |

The harness must not import `planning`, `qc`, `assembly` or any domain module. If it needs to
know what "good" means, that knowledge arrives as a score in state, not as an import.

## 8. Failure modes

| Failure | Detection | What the harness does |
| --- | --- | --- |
| Budget cap hit mid-loop | `exceeded()` at `evaluate` | Terminate `PARTIAL`, `degraded=true`, reason `VA-BUDGET-*`. Preserve every accepted shot; route to `assemble`. |
| Estimate would breach cap | `would_exceed()` pre-flight | Skip the call entirely; terminate `PARTIAL`. Never pay for a call whose result cannot be used. |
| Repeated shot-scope signature | `record_failure` returns `repeat=True, scope=shot` | Abandon the shot; continue the job. |
| Repeated job-scope signature | `record_failure` returns `repeat=True, scope=job` | Terminate `FAILED_NO_PROGRESS` immediately. |
| Non-retryable error from any tool | Error classification at the gateway/provider boundary | Terminate `FAILED` if nothing is preserved, else `PARTIAL`. |
| Ledger write fails | Persistence error | **Terminate the job.** An unrecorded charge is an unbounded budget; the cap is a non-negotiable and cannot be degraded. `[D-19]` |
| Provisional provider charge never settles | `cost_is_final` still false at terminal status | Sweeper reconciles from the upstream project; alarm if unreachable. An un-settled estimate is treated as **charged at the estimate**, never as free `[D-60]`. |
| Clock skew / paused container | `wall_clock_s` computed from a monotonic source plus persisted start | Wall-clock uses stored `started_at`; a resumed job continues accruing rather than resetting. |
| Cancel arrives mid-node | Cancel flag in Redis, checked at each `decide()` | Cooperative: the current node completes and checkpoints, then the job terminates. Never a hard kill mid-write. |
| Model emits a control instruction | Output schema validation + quarantine | Rejected as `VA-SEC-001`. The model never selects an edge. `[CPS §Agent harness]` |
| Harness process dies | Job lock TTL expires | The job is reclaimed and resumed from the last checkpoint — crashes resume, never restart. `[CPS §Non-negotiables]` |

## 9. Test strategy

| Level | Tests |
| --- | --- |
| Decision table | Exhaustive parametrised test over the six `decide()` rules, asserting precedence — in particular that no-progress pre-empts budget, and budget pre-empts "evaluator satisfied". |
| Budget | Property test: for any sequence of charges, terminal `usd_spent` never exceeds `max_usd` **and** no provider call is issued after the cap is reachable. A fuzz test asserts the ledger is monotonic per finalised charge, that a provisional charge settles **exactly once**, and that a refund on a failed render is returned to the ledger `[D-60]`. Assert `tenant.max_usd_per_job` overrides the global cap and that NULL inherits it `[D-70]`; assert a volume discount never raises the pre-flight allowance `[D-65]`. |
| Signatures | Table test over shot-scope vs job-scope; the score-band rule (`+0.04` is no progress, `+0.06` is progress); promotion to job scope on a second shot index. |
| Resume | Kill the process at every node boundary (parametrised) and assert on resume: no accepted shot is regenerated, the ledger continues rather than resets, and total spend equals the uninterrupted run. |
| Injection | Adversarial corpus of provider/QC outputs containing instruction-shaped text; assert none changes routing, all are quarantined, and `VA-SEC-001` fires. |
| Tool grants | For every node, assert calling an ungranted tool raises. Assert no tool name contains a provider name (static check). |
| Chaos (M5) | Kill Redis, kill Postgres, stall a provider past the wall-clock cap; assert the outcome is always one of the four, always with a preserved-set and next-steps. `[PRD §Delivery M5]` |
