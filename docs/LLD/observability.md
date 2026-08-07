---
doc: LLD
module: observability
title: Observability — Langfuse traces, logs, redaction, error taxonomy
status: canonical
implementation_status: deferred
version: 1
last_synced_commit: 438138573aa69c26727651b368e056262908bc69
generated_by: cdr-documentation
run_id: 2026-08-08/001-documentation
sources:
  - docs/specs/common-platform-spec.md
  - docs/specs/video-agent-prd.md
  - docs/HLD.md
---

# LLD — `observability`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — DEFERRED.** **E4 — DEFERRED, not cancelled.** Basic structured logging with `trace_id` ships in E0, but Langfuse traces/spans/generations/scores, the redaction tripwire and the CI gates are **not** in the v1 build. Read this document as a specification, not as a description of the running system.
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

> **Trace = one unit of work. Spans = graph nodes. Generations = LLM calls** with model,
> tokens, cost and prompt version. Logs are JSON with the Langfuse `trace_id`, so any log
> line joins to its trace. `[CPS §Observability]`

This module owns: the trace/span/generation/score model, the prompt registry integration,
structured logging, **redaction**, and the **stable error code taxonomy**. It is the module
that makes `[CPS §Failure behaviour]`'s promise real — *every error response carries a stable
code and the `trace_id`, so support opens the exact Langfuse trace instantly.*

It is a **cross-cutting dependency**: every other module uses it and it depends on none of
them.

## 2. Public interface

```python
class Obs(Protocol):
    """The only telemetry surface. No module imports the Langfuse SDK directly."""
    def trace(self, job_id: UUID, tenant_id: UUID, *, prompt_sha: str) -> TraceHandle: ...
    def span(self, node: str, **attrs: Any) -> AbstractAsyncContextManager[SpanHandle]: ...
    def generation(self, *, alias: str, model_used: str, prompt_name: str,
                   prompt_version: str, usage: Usage, latency_ms: int) -> None: ...
    def score(self, name: ScoreName, value: float, *,
              shot_index: int | None = None, comment: str | None = None) -> None: ...
    def event(self, code: str, **attrs: Any) -> None: ...

class ScoreName(StrEnum):
    CONTINUITY_SHOT = "continuity_shot"
    CONTINUITY_JOB  = "continuity_job"      # min across shots  [D-15]
    COHERENCE_HUMAN = "coherence_human"
    QC_CALIBRATION  = "qc_calibration"
    COST_PER_JOB    = "cost_per_job"

class ErrorCode(StrEnum):
    """Single source of truth for the taxonomy in section 6, the API error envelope
    and the CI check that every raised code is documented.  [D-55]"""
    VA_REQ_001 = "VA-REQ-001"
    # … one member per row of the table in section 6

def redact(payload: Mapping[str, Any]) -> dict[str, Any]: ...
    # deny-by-default: a field is dropped unless explicitly allow-listed  (section 5)

def get_prompt(name: str, *, job_id: UUID) -> PromptRef: ...
    # registry lookup with deterministic 10% canary assignment per job_id  [D-20]
```

`trace_id` is propagated through a context variable, so no module passes it by hand and no
log line can omit it.

### 2.1 The trace model

| Langfuse concept | Video Agent binding | Cardinality |
| --- | --- | --- |
| **Trace** | one `Job` | 1 per job |
| **Span** | one graph node execution | ~10–50 per job |
| **Nested span** | one provider call, one ffmpeg invocation, one DB unit of work | many |
| **Generation** | one LLM call through the gateway | 6–20 per job |
| **Score** | continuity scores, human ratings, calibration outcomes | several per job |
| **Event** | degradations, circuit trips, quarantined content, budget warnings | as they occur |

```
TRACE  job:{job_id}   tags: tenant, outcome, degraded, budget_epoch
├─ SPAN plan_story
│  └─ GENERATION alias=reasoning-high  prompt=story_plan@v3  tokens=… cost=…
├─ SPAN lock_bible
│  └─ GENERATION alias=reasoning-high  prompt=bible@v2
├─ SPAN select_next_shot            (shot_index=0)
├─ SPAN generate_shot               (shot=0, attempt=1)
│  └─ SPAN provider.generate        (capabilities, provider_key, project_id, credits, cost)
├─ SPAN extract_final_frame         (shot=0)
│  └─ SPAN ffmpeg.extract_frame
├─ SPAN qc_shot                     (shot=0, attempt=1)
│  ├─ GENERATION alias=vision-default prompt=qc_continuity@v5
│  └─ SCORE continuity_shot = 0.81
│        … repeat per shot; a repair appears as a second generate_shot span …
├─ SPAN assemble  →  SPAN deliver  →  SPAN finalize
└─ SCORE continuity_job = min(shot scores)   [D-15]
```

A **repair is visible as structure**, not as a log message: a second `generate_shot` span
under the same shot index. Reading a trace tells you immediately which shot fought back.

### 2.2 Mandatory attributes

Every span: `job_id`, `tenant_id`, `node`, `shot_index` (when applicable), `attempt_no`,
`degraded`, `budget_epoch`.
Every generation: `alias`, `model_used`, `prompt_name`, `prompt_version`, input/output
tokens, `cost_usd`, `latency_ms`.
Every provider span: `provider_key`, `provider_model`, `provider_project_id`,
`capabilities_required`, `cost_usd`, `credits_charged`, `cost_is_final`,
`request_fingerprint`, and `seed` **only where the provider supports it** `[D-59]`.

`model_used` and `provider_key` exist **for observability only**. Application code may not
branch on them. `[CPS §Model routing]`

### 2.3 Scores

| Score | Source | Range | Used for |
| --- | --- | --- | --- |
| `continuity_shot` | `qc` per shot | 0–1 | Repair decisions, debugging |
| `continuity_job` | min across shots `[D-15]` | 0–1 | The `≥ 0.75 on ≥ 85% of jobs` metric `[PRD §Success metrics]` |
| `coherence_human` | Sampled human review | 1–5 | The `≥ 4.0` story-coherence metric |
| `qc_calibration` | Nightly labelled-set run | 0–1 | The QC-reliability gate `[PRD §Key risks]` |
| `cost_per_job` | Ledger at `finalize` | USD | Cost-regression gate `[CPS §Non-negotiables]` |

## 3. Prompt registry

**Prompts are authored in-repo under `prompts/` as versioned files, and those files are the
source of truth.** `[D-72]` On startup the application registers any prompt that is absent
from Langfuse.

This inverts the naive reading of `[CPS §Canonical stack]` for one deliberate reason: **a
fresh checkout with no Langfuse connection must still run.** Making Langfuse a hard runtime
dependency for prompt *retrieval* would mean a Langfuse outage stops all video generation —
an observability tool taking down the product it observes, which `[D-57]` already forbids in
the telemetry direction. Langfuse remains the **observability and version-tracking** surface:
every generation still records the prompt name and version it used.

A raw prompt string inline in application code remains a CI failure — prompts come from
`prompts/`, not from a string literal.

| Prompt | Consumer | Alias |
| --- | --- | --- |
| `story_plan` | `planning.plan_story` | `reasoning-high` |
| `continuity_bible` | `planning.lock_bible` | `reasoning-high` |
| `qc_continuity` | `qc.score_shot` | `vision-default` |
| `repair_delta` | `providers` (via `qc` findings) | `reasoning-fast` |

Each has a file under `prompts/<name>/<version>.md` with front-matter carrying its name,
version and target alias. Version is explicit in the filename, never implicit in git history.

Every generation records the exact version used, so a quality change is attributable to a
prompt version. Prompt changes go to **10% of traffic first** and are promoted only after
their Langfuse scores hold against the incumbent. `[CPS §Rollout]` Assignment is
deterministic per `job_id`, so one job never mixes prompt versions across its shots. `[D-20]`

## 4. Logging

JSON, one object per line, `trace_id` on **every** line. `[CPS §Observability]`

```json
{
  "ts": "2026-08-08T10:14:02.113Z",
  "level": "info",
  "msg": "shot accepted",
  "trace_id": "lf_9f2c…",
  "span_id": "sp_41a…",
  "job_id": "…",
  "tenant_id": "…",
  "node": "qc_shot",
  "shot_index": 1,
  "attempt_no": 2,
  "continuity_score": 0.83,
  "code": null,
  "degraded": false
}
```

Rules: no `print`, no unstructured strings · `trace_id` injected from context, never passed by
hand · levels are meaningful (`error` = someone should look; a below-threshold QC score is
`info`, because it is the system working) · one log line per state transition, not per loop
iteration · sampling is allowed for `debug`, never for `error`.

## 5. Redaction

> **Never logged:** credentials, raw PII, full media payloads, row-level query results.
> `[CPS §Observability]`

Implemented as a **deny-by-default serialiser** applied to every log record and every
Langfuse payload. A field must be explicitly allow-listed to be emitted.

| Category | Rule |
| --- | --- |
| Credentials | API keys, MCP tokens, DB URLs, signing keys — dropped by key-name pattern **and** by value shape (high-entropy strings). Never `****`-masked in a way that reveals length |
| **Presigned URLs** | Dropped entirely. A presigned URL is a bearer credential `[D-52]`. **This covers the provider's `upload_url` and `downloads[].url` too** — both carry auth in the query string `[D-58]`, `[D-64]` |
| Raw PII | The user prompt may contain it. Logged as `prompt_sha256` plus the first 64 characters, never in full. The full prompt lives only in the RLS-protected `job` row |
| Media payloads | No bytes, no base64, no data URIs — in logs, spans, generations or checkpoints. Media is referenced by `artifact_id` / `storage_key` only |
| Row-level query results | Log the statement identity and the row **count**. Never the rows |
| Model outputs | Plans, bibles and QC rationales are stored in Postgres and referenced from the trace; QC rationale is attached to the trace (it is operationally essential) but truncated and never re-injected as instruction |

Enforcement is not by convention:

1. A serialiser unit test asserts each category is dropped.
2. A runtime tripwire scans outgoing payloads for base64-media signatures, URL signature
   parameters and known key prefixes; a hit raises in dev/CI and drops-plus-alarms in
   production.
3. A CI test replays a full synthetic job and greps all captured logs and trace payloads for
   planted canary secrets, canary PII and media magic bytes. Any hit fails the build. `[D-54]`

## 6. Error taxonomy

Stable codes. **A code's meaning never changes and a retired code is never reused** —
`[CPS §Failure behaviour]` requires that support can act on a code, which is only true if it
is stable. Format: `VA-<DOMAIN>-<NNN>`.

| Code | Meaning | Retryable | Typical outcome |
| --- | --- | --- | --- |
| `VA-REQ-001` | Invalid prompt | no | 400 |
| `VA-REQ-002` | Idempotency key missing | no | 400 |
| `VA-REQ-003` | Idempotency key reused with a different body | no | 409 |
| `VA-REQ-004` | Duplicate request in flight | yes | 409 + `Retry-After` |
| `VA-REQ-005` | Job not found (also returned cross-tenant) | no | 404 |
| `VA-REQ-006` | Job not resumable | no | 409 |
| `VA-REQ-007` | Request schema invalid | no | 422 |
| `VA-AUTH-001` | Unauthenticated | no | 401 |
| `VA-AUTH-002` | Tenant forbidden | no | 403 (surfaced as 404) |
| `VA-PLAN-001` | Plan unparseable | no | `FAILED` |
| `VA-PLAN-002` | Beats do not sum to exactly 40s | no | `FAILED` |
| `VA-PLAN-003` | Wrong beat count/kind/order | no | `FAILED` |
| `VA-BIBLE-001` | Bible incomplete or too vague | no | `FAILED` |
| `VA-BIBLE-002` | Bible mutation attempted / hash mismatch | no | `FAILED` |
| `VA-PROV-001` | Provider unavailable | yes | retry → fallback |
| `VA-PROV-002` | No provider satisfies required capabilities | no | shot fails |
| `VA-PROV-003` | Provider timeout | yes | retry → fallback |
| `VA-PROV-004` | Content policy rejection | no | shot abandoned, no repair `[D-42]` |
| `VA-PROV-005` | All providers in the group exhausted | no | `PARTIAL` or `FAILED` |
| `VA-PROV-006` | Prompt exceeds provider limit even after policy truncation | no | shot fails |
| `VA-PROV-007` | Provider rejected the request (`400`) | no | config/programming fault, alarm |
| `VA-PROV-008` | Provider credential rejected (`401`) | no | `ESCALATED` |
| `VA-PROV-009` | **Provider payment required (`402`) — credits exhausted** | **no** | **`FAILED`/`ESCALATED`; never retried** `[D-62]` |
| `VA-PROV-010` | Provider project not found (`404`) | no | attempt marked orphaned |
| `VA-PROV-011` | Provider unprocessable entity (`422`) | no | shot fails; no repair if content-related |
| `VA-PROV-012` | Render reached terminal `error` | no | failed attempt, repairable |
| `VA-PROV-013` | Render reached terminal `canceled` | no | failed attempt |
| `VA-QC-001` | QC model unavailable | yes | provisional accept, degraded `[D-43]` |
| `VA-QC-002` | Score below threshold *(internal signal, never an HTTP error)* | n/a | repair or abandon |
| `VA-QC-003` | QC response unparseable | no | treated as `VA-QC-001` |
| `VA-ASM-001` | ffmpeg failed / timed out | yes (once) | degraded or `FAILED` |
| `VA-ASM-002` | No usable clips to assemble | no | `FAILED`, zero deliverable |
| `VA-ASM-003` | Output duration mismatch | yes (once) | re-encode path |
| `VA-ASM-004` | Disk exhausted | yes | operational alarm |
| `VA-STORE-001` | Artifact write failed | yes | retry |
| `VA-STORE-002` | Presign failed | yes | manifest with null URL |
| `VA-STORE-003` | Database unavailable | yes | 503 |
| `VA-STORE-004` | Artifact checksum mismatch | no | exclude, degrade |
| `VA-BUDGET-001` | USD cap exhausted | no | `PARTIAL` |
| `VA-BUDGET-002` | Wall-clock cap exhausted | no | `PARTIAL` |
| `VA-BUDGET-003` | Token cap exhausted | no | `PARTIAL` |
| `VA-BUDGET-004` | Iteration cap exhausted | no | `PARTIAL` |
| `VA-GW-001` | Circuit open / alias group down | yes | 503 |
| `VA-GW-002` | Alias unresolvable | no | config alarm |
| `VA-GW-003` | Rate limited | yes | 429 |
| `VA-GW-004` | Structured output unparseable | no | node-specific |
| `VA-GW-005` | Context length exceeded | no | node-specific |
| `VA-GW-006` | Content policy rejection at the LLM | no | honest failure |
| `VA-SEC-001` | Instruction-shaped content quarantined | n/a | event only, never fatal |
| `VA-INT-001` | Internal error | no | 500, generic message |
| `VA-INT-002` | No progress — repeated failure signature | no | `FAILED_NO_PROGRESS` |
| `VA-INT-003` | Checkpoint schema drift | no | non-resumable, deliver partial |

Codes are declared in **one enum**, which is the single source for this table, the API error
envelope and the CI check that every raised code is documented. `[D-55]`

## 7. Metric instrumentation

Each `[PRD §Success metrics]` target, bound to a concrete measurement `[D-14]`:

| Metric | Target | Query |
| --- | --- | --- |
| Story coherence (human, 1–5) | ≥ 4.0 | mean(`coherence_human`) over the review window |
| Jobs with continuity score ≥ 0.75 | ≥ 85% | count(`continuity_job` ≥ 0.75) / count(terminal jobs) |
| p90 end-to-end latency | ≤ 8 min | p90(trace duration, accept → `deliver`) |
| Jobs failing with zero deliverable | < 1% | count(outcome ∈ {`FAILED`,`FAILED_NO_PROGRESS`} ∧ artifacts = 0) / count(terminal) |

Operational signals beyond the PRD's four `[D-56]`: repair rate per shot index (which beat is
hardest), abandonment rate, degrade rate by reason, circuit trips by dependency, cost per job
by percentile, budget-cap hit rate, and mean attempts per accepted shot.

## 8. CI gates

**CI gates on eval regression > 3% and cost regression > 20%.** `[CPS §Non-negotiables]`

| Gate | Measured | Threshold | Action |
| --- | --- | --- | --- |
| Eval regression | Continuity + coherence on the fixed eval set, vs the baseline on `main` | > 3% worse | block merge |
| Cost regression | Mean `cost_per_job` on the eval set vs baseline | > 20% worse | block merge |
| QC calibration | False-pass / false-fail on the labelled set | > 3% worse | block merge |
| Redaction canary | Planted secrets/PII/media in a synthetic job | any leak | block merge |

Baselines are stored per commit so a gate compares like with like. Post-merge, the same
comparison runs against the **10% canary** and auto-rolls-back on a score regression.
`[CPS §Rollout]`

## 9. Dependencies

Depends on Langfuse (SDK), the logging runtime and config. **Depends on no other module in
this repo.** Every other module depends on it. A dependency from `observability` into a
domain module would create a cycle and is a CI failure.

## 10. Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| Langfuse unavailable | SDK error / queue full | **Never fail a job for telemetry.** Buffer locally, drop oldest on overflow, log a counter, alarm. Jobs continue. `[D-57]` |
| Trace export backlog | Queue depth | Sample `debug`/`info` spans; never sample errors or scores. |
| `trace_id` missing on a log line | CI lint + runtime assertion | Test failure in CI; in production, synthesise and alarm — a log line that cannot join its trace defeats the model. |
| Redaction rule misses a new field | Deny-by-default allow-list | New fields are dropped until allow-listed. Fail-safe, not fail-open. |
| Canary secret found in output | CI redaction test | Build blocked. |
| Undocumented error code raised | CI enum/table cross-check | Build blocked. |
| Score written to the wrong trace | Trace-id assertion at write | Rejected and alarmed; a misattributed score corrupts the metrics. |
| Langfuse prompt registry unavailable | SDK error | **Read the in-repo `prompts/` file** — the source of truth `[D-72]`. Generation proceeds normally; only version *tracking* degrades. Alarm, but do not flag the job degraded. Never fall back to an inline string. |
| In-repo prompt missing or malformed | Startup validation | **Fail to start.** A missing prompt is a broken build, not a runtime degradation. |
| Registry and repo disagree on a version's content | Startup diff | The repo wins and re-registers. Alarm on the divergence — someone edited a prompt in the Langfuse UI, which is not the authoring path. |
| Clock skew across workers | NTP monitoring | Timestamps are server-generated; span ordering uses sequence numbers, not wall clock. |

## 11. Test strategy

| Level | Tests |
| --- | --- |
| Trace shape | Run a synthetic job end to end against a Langfuse test sink; assert the exact span tree, one generation per LLM call, and one score per shot. Assert a repair appears as a second `generate_shot` span. |
| Attributes | Assert every mandatory attribute is present on every span/generation; a missing one fails CI. |
| Redaction (highest priority) | The canary test of §5.3 — planted secrets, PII, presigned URLs and media magic bytes across every emission path. Any leak fails the build. |
| Codes | Assert every code in the enum appears in the taxonomy table and in at least one test; assert no code is reused or renumbered across releases (a committed historical registry is compared). |
| Log format | Every emitted line parses as JSON and carries `trace_id`. Assert no `print` and no unstructured logger exists in the tree. |
| Cost accounting | Sum of generation + provider costs on a trace equals `Job.budget_used.usd` exactly, **after** provisional credit charges are reconciled to terminal values `[D-60]`. |
| Gates | Deliberately regress quality by 4% and cost by 25% in a fixture run; assert both gates block. |
| Degradation | Langfuse down → the job still completes; assert zero job failures attributable to telemetry. |
| Prompt versions | Assert one job never mixes prompt versions across its shots. |
| Offline bootstrap | With Langfuse unreachable, assert a fresh checkout starts, resolves all four prompts from `prompts/`, and completes a job `[D-72]`. |
| Registration | Assert startup registers only prompts absent from Langfuse, and is idempotent across restarts. |
