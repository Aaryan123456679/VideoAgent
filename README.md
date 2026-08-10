---
doc: README
title: Video Agent — navigation
status: canonical
version: 1
last_synced_commit: 438138573aa69c26727651b368e056262908bc69
generated_by: cdr-documentation
run_id: 2026-08-08/001-documentation
---

# Video Agent

One prompt becomes a continuous 40-second story — four 10-second shots with enforced
narrative and visual continuity. Text-to-video models already generate good clips in
isolation, but four clips from four prompts give you four unrelated clips: the protagonist
changes face, the room changes colour, the story never moves. This product is the continuity
engine around the generator — a planned 4-beat arc, a locked continuity bible, frame chaining
between shots, and a vision-model QC loop that repairs only the shot that broke.

Demo Video Link : https://drive.google.com/file/d/10H5C37x4SgAPLIEbYK9ATuuRK94uRzh-/view?uuspsharing

> **Status: design complete; implementation scoped to E0 + E1 + E2.** The documents here
> describe the **full** system design. The current build target is foundation, job lifecycle,
> planning, continuity bible, the Magic Hour adapter, frame chaining, assembly and delivery.
> **E3 (QC loop, partial results, resume) and E4 (observability, cost caps, load and chaos)
> are deferred, not cancelled.** Every LLD states its own `implementation_status`; read
> [`docs/LLD/qc.md`](./docs/LLD/qc.md) and
> [`docs/LLD/observability.md`](./docs/LLD/observability.md) as specifications, not as
> descriptions of running code. Scope table: [HLD §12](./docs/HLD.md#12-delivery-milestones).

> ### Note on the video generation provider
>
> The PRD specifies **Higgsfield MCP** as the video generation provider. **Higgsfield exposes
> no free or trial API tier, and no credential was obtainable for this build.** The v1
> provider is therefore **[Magic Hour](https://magichour.ai)**.
>
> This is a deliberate, recorded substitution — decision **`D-58`** — not an oversight. It is
> sound because Magic Hour accepts a **start-frame image**, which is the one capability the
> product cannot ship without: it is what makes frame chaining, and therefore continuity,
> possible.
>
> **The swap was a configuration change.** It touched one adapter module and `.env` — no other
> module names a provider, and no caller changed. That is exactly the property the provider
> abstraction and the alias-only model rule were designed to buy `[D-06]`.
>
> It does force honest deviations from the PRD, all recorded in
> [HLD Appendix A](./docs/HLD.md#appendix-a--design-decision-register): Magic Hour documents
> **no seed parameter**, so the PRD's "every job is reproducible" promise delivers full
> traceability but **not bit-exact re-rendering** (`D-59`); cost is billed in **credits**
> rather than USD (`D-60`); the model is pinned to `wan-2.2` because it is one of the few that
> permits a **10-second** clip (`D-61`); and 720p is the configured target, with 1080p as a
> ceiling rather than a floor (`D-63`).
>
> `docs/specs/*` is left saying "Higgsfield" on purpose — those files are verbatim
> transcriptions of the source PDFs, and a deviation is recorded in the design docs, never by
> editing the spec.

## Watch the pitch

Recorded demo — a real job run end to end against the live Magic Hour API, shown on the trial
dashboard: [watch on Google Drive](https://drive.google.com/file/d/10H5C37x4SgAPLIEbYK9ATuuRK94uRzh-/view?usp=sharing).

A ~10-minute walkthrough script, written to be read aloud over a screen recording of the
architecture diagrams below and the live trial UI: [`PITCH_SCRIPT.md`](./PITCH_SCRIPT.md).

## Architecture

**System view** — a prompt in, a delivered video out, and everything it passes through.

```mermaid
flowchart LR
    U["User prompt"] --> UI["Trial UI (ui/)\nor any client"]
    UI -->|"POST /v1/jobs"| API["FastAPI\nsrc/video_agent/api/"]
    API --> PG[("PostgreSQL 16\nRLS per tenant")]
    API -->|"idempotency claim,\nenqueue"| RD[("Redis 7\nqueue · locks · idempotency\ncircuit state · cancel signal")]
    RD -->|"jobs:stream,\nconsumer group"| W["JobWorker\ngraph/worker.py"]
    W --> G["Compiled LangGraph\ngraph/build.py + nodes.py"]
    G --> GW["LLM Gateway\ngateway/gateway.py"]
    GW --> LITELLM["LiteLLM proxy"] --> LLM["Gemini / OpenAI / Claude\n(alias-routed, never named in code)"]
    G --> PR["ProviderRegistry\nnegotiate → pin → fail over"]
    PR --> MH["MagicHourProvider\nreal render — D-58"]
    PR --> MOCK["MockVideoProvider\nffmpeg, trial/testing only"]
    MH --> S3[("S3 / MinIO\nchecksummed artifacts")]
    MOCK --> S3
    G --> S3
    S3 -->|"presigned URL,\nminted per request, never stored"| API
    API -->|"GET /v1/jobs/{id}/artifacts"| UI
```

**Job pipeline** — the LangGraph state machine every job actually runs through. Every arrow
into a node is guarded: the harness gets first say on every step, and can terminate the job
from any of them (omitted here for readability — see [`docs/LLD/graph.md`](./docs/LLD/graph.md) §3).

```mermaid
stateDiagram-v2
    [*] --> plan_story
    plan_story --> lock_bible : StoryPlan accepted
    lock_bible --> select_next_shot : ContinuityBible locked, immutable
    select_next_shot --> generate_shot : a shot is pending
    select_next_shot --> assemble : every shot done or abandoned
    generate_shot --> extract_final_frame
    extract_final_frame --> qc_shot
    qc_shot --> select_next_shot : accepted / abandoned
    qc_shot --> generate_shot : repair, capped at 2 attempts
    note right of qc_shot
        Real scoring is still E3 (deferred);
        qc_shot accepts every shot by default.
        The repair edge is reachable today only
        via a manual override (force-repair),
        never from QC's own judgment
    end note
    assemble --> deliver
    deliver --> finalize
    finalize --> [*]
```

Every one of those nine nodes checkpoints in the **same database transaction** as its own
domain write — a crash resumes from there, it never restarts from the top (except during v1's
simplified crash recovery, which re-enters at the graph's entry point rather than the last
checkpoint; see [`graph/worker.py`](./src/video_agent/graph/worker.py)'s own module docstring).

## What's implemented — features and the code behind them

| Feature | What it does | Code |
| --- | --- | --- |
| Four-beat story planning | One LLM pass produces a 4-beat arc summing to exactly 40s | [`planning/service.py::plan_story`](./src/video_agent/planning/service.py) |
| Locked continuity bible | Character, wardrobe, location, lighting, palette, camera language — immutable for the job's life, enforced by a DB trigger | [`planning/service.py::lock_bible`](./src/video_agent/planning/service.py), [`planning/bible.py`](./src/video_agent/planning/bible.py) |
| Frame chaining | The last frame of shot *n* conditions shot *n+1*, so identity carries forward | [`graph/nodes.py::_resolve_conditioning`](./src/video_agent/graph/nodes.py), [`graph/frame_extraction.py`](./src/video_agent/graph/frame_extraction.py) |
| Capability negotiation + provider abstraction | A shot's requirements are matched against provider capabilities; a config change swaps providers with zero code diff | [`providers/negotiate.py`](./src/video_agent/providers/negotiate.py), [`providers/registry.py`](./src/video_agent/providers/registry.py) |
| Real video generation adapter | Magic Hour REST adapter — submit, poll, upload conditioning frames, download, full HTTP-error mapping | [`providers/magichour.py`](./src/video_agent/providers/magichour.py) |
| Multi-key rotation on insufficient credits | On a real `402`, rotates to a second configured account and retries — a scoped exception to "402 is never retried," since a *different* balance can succeed where the first can't. Single-key deployments see no change | [`providers/magichour.py::RotatingApiKey`](./src/video_agent/providers/magichour.py) |
| Inbound webhook acceleration | A provider's webhook triggers an early re-poll instead of waiting for the next tick; payload is never trusted for status, only as a signal to re-read | [`providers/magichour.py::handle_webhook`](./src/video_agent/providers/magichour.py), [`api/webhooks.py`](./src/video_agent/api/webhooks.py) |
| Mock video provider | Real ffmpeg-rendered MP4s, zero network/cost/wait, for instant local trial runs | [`providers/mock.py`](./src/video_agent/providers/mock.py) |
| Idempotent job lifecycle | Every work-creating `POST` is idempotency-keyed; a retry replays, never double-creates or double-bills | [`api/idempotency.py`](./src/video_agent/api/idempotency.py) |
| Redelivery-safe graph nodes | Every node in the graph is safe to execute twice under at-least-once queue delivery | [`graph/nodes.py`](./src/video_agent/graph/nodes.py) (`plan_story_node`, `lock_bible_node`, `_claim_shot_attempt`) |
| Manual repair-signal override | Exercises the real repair mechanism (back-edge, cap, continuity) without pretending QC scoring exists — not QC itself, which remains E3 | [`api/jobs.py::force_repair_shot`](./src/video_agent/api/jobs.py), [`graph/nodes.py::qc_shot_node`](./src/video_agent/graph/nodes.py) |
| Row-level tenant isolation | Postgres RLS on every table but two documented exemptions, enforced even against the table owner, audited by a static check | [`persistence/rls.py`](./src/video_agent/persistence/rls.py), [`persistence/schema.py`](./src/video_agent/persistence/schema.py) |
| At-least-once job queue, crash recovery | Redis Streams consumer group; a stalled job is reclaimed via `XAUTOCLAIM` | [`persistence/queue.py`](./src/video_agent/persistence/queue.py), [`graph/worker.py`](./src/video_agent/graph/worker.py) |
| One-writer-per-job lock | Fencing-token Redis lock; a second worker on the same job declines rather than races | [`graph/lock.py`](./src/video_agent/graph/lock.py) |
| Agent harness — six-rule termination | Every step is evaluated against evaluator-satisfied, cancellation, fatal error, budget, no-progress, and default-continue, in that priority order | [`harness/decide.py`](./src/video_agent/harness/decide.py) |
| Hard budget caps | Iterations, wall-clock, tokens, USD — pre-flight veto and post-hoc breach detection | [`harness/budget.py`](./src/video_agent/harness/budget.py) |
| Failure-signature no-progress detection | The same failure twice at job scope stops the job; at shot scope, abandons just that shot | [`harness/signatures.py`](./src/video_agent/harness/signatures.py) |
| LLM gateway — single egress | Alias-based model routing (code never names a model), retry+backoff+jitter, per-dependency circuit breaker, response caching | [`gateway/gateway.py`](./src/video_agent/gateway/gateway.py), [`gateway/breaker.py`](./src/video_agent/gateway/breaker.py) |
| Structured-output schema compatibility fix | Rewrites Pydantic's `const` to `enum` before sending to providers (Gemini/Vertex) that reject `const` | [`gateway/gateway.py::_widen_schema_const_to_enum`](./src/video_agent/gateway/gateway.py) |
| ffmpeg assembly pipeline | Per-clip normalize (canonical codec/profile), stream-copy concat, thumbnail extraction, pinned-version startup assertion | [`assembly/media_toolchain.py`](./src/video_agent/assembly/media_toolchain.py) |
| Presigned, never-persisted delivery | Every artifact URL is minted fresh per request and never stored, cached, or logged — a presigned URL is a bearer credential | [`persistence/presign.py`](./src/video_agent/persistence/presign.py) |
| Structured logging + redaction tripwire | JSON logs with a propagated trace id; a runtime scanner refuses credentials, presigned URLs, and raw media bytes onto any log line | [`observability/logging.py`](./src/video_agent/observability/logging.py), [`observability/redaction.py`](./src/video_agent/observability/redaction.py) |
| Static leak/lint guards | Repo-wide checks: no provider name outside its adapter, no `print`, no hardcoded-secret-shaped names, every raised error code is registered | [`tests/static_guards.py`](./tests/static_guards.py) |
| Trial UI + no-auth dev harness | A React front end plus a dev API server/worker pair for a full local end-to-end run in minutes | [`ui/`](./ui/), [`scripts/dev_server.py`](./scripts/dev_server.py), [`scripts/dev_worker.py`](./scripts/dev_worker.py) |

**Deferred, not missing by accident** — the QC vision-scoring/repair loop
([`docs/LLD/qc.md`](./docs/LLD/qc.md), E3) and Langfuse tracing
([`docs/LLD/observability.md`](./docs/LLD/observability.md), E4). Both are fully designed;
neither is wired into the running graph yet. See the [function-level reference](#function-level-reference)
below for exactly what exists today versus what's still a stub.

## Repository layout

```
.
├── README.md                  ← you are here (navigation only)
├── AGENT.md                   ← operating procedures for AI agents working in this repo
├── docs/
│   ├── HLD.md                 ← the system design
│   ├── LLD/                   ← one document per module
│   │   ├── api.md             ├── graph.md        ├── qc.md
│   │   ├── harness.md         ├── planning.md     ├── assembly.md
│   │   ├── gateway.md         ├── providers.md    ├── persistence.md
│   │   └── observability.md
│   └── specs/                 ← source of truth; do not edit without a spec change
├── prompts/                   ← prompt text, versioned in-repo, source of truth [D-72]
├── .env.example               ← configuration contract
│       ├── common-platform-spec.md
│       └── video-agent-prd.md
├── Guidelines.pdf             ← origin PDF for common-platform-spec.md
├── Video-Agent.pdf            ← origin PDF for video-agent-prd.md
└── .cdr/                      ← CDR state: runs, indexes, memory, schemas
```

## Start here

| If you want to… | Read |
| --- | --- |
| Understand the product and the whole system | [`docs/HLD.md`](./docs/HLD.md) |
| Know what governs when a PRD is silent | [`docs/specs/common-platform-spec.md`](./docs/specs/common-platform-spec.md) |
| Know what the product must do | [`docs/specs/video-agent-prd.md`](./docs/specs/video-agent-prd.md) |
| Work as (or with) an AI agent in this repo | [`AGENT.md`](./AGENT.md) |
| Find every design decision and its rationale | [HLD Appendix A](./docs/HLD.md#appendix-a--design-decision-register) |

### Module documents

| Module | Responsibility | v1 |
| --- | --- | --- |
| [`api`](./docs/LLD/api.md) | FastAPI async surface, job lifecycle, idempotency keys, error envelope | E1 |
| [`harness`](./docs/LLD/harness.md) | Loop engine, context and tool ownership, budget caps, termination | E0–E1 *(caps E4)* |
| [`gateway`](./docs/LLD/gateway.md) | LiteLLM proxy as single LLM egress, alias resolution, retry/fallback/circuit break | E0 |
| [`graph`](./docs/LLD/graph.md) | LangGraph `StateGraph`, checkpoint after every node, resume semantics | E1–E2 *(resume E3)* |
| [`planning`](./docs/LLD/planning.md) | `StoryPlan` (4 beats, exactly 40s) and the locked, immutable `ContinuityBible` | E1 |
| [`providers`](./docs/LLD/providers.md) | Video provider abstraction, capability negotiation, Magic Hour adapter `[D-58]`, frame chaining | E2 |
| [`qc`](./docs/LLD/qc.md) | Vision scoring against the bible, 0.75 threshold, repair capped at 2 attempts, calibration | **E3 — deferred** |
| [`assembly`](./docs/LLD/assembly.md) | ffmpeg stitch and normalise, music bed, thumbnail, partial assembly | E2 *(partial E3)* |
| [`persistence`](./docs/LLD/persistence.md) | PostgreSQL 16 with RLS per tenant, Redis 7, artifact storage, presigned URLs | E0 |
| [`observability`](./docs/LLD/observability.md) | Langfuse traces/spans/generations/scores, JSON logs, redaction, error taxonomy | **E4 — deferred** |

## Document hierarchy

```
Guidelines.pdf ──▶ docs/specs/common-platform-spec.md ─┐
                                                        ├──▶ docs/HLD.md ──▶ docs/LLD/*.md
Video-Agent.pdf ─▶ docs/specs/video-agent-prd.md ──────┘
```

Precedence: **PDF → spec → HLD → LLD.** A lower document may add detail but may never
contradict a higher one. Where the PRD is silent, the Common Platform Specification governs.
Where both are silent, the HLD or an LLD makes an explicit, labelled decision — every such
decision carries a `D-nn` tag and appears in
[HLD Appendix A](./docs/HLD.md#appendix-a--design-decision-register).

## How to run and test (once code exists)

Not yet applicable — there is no source tree. When implementation begins, this section will
be regenerated by a CDR documentation `sync` run rather than hand-edited, and will cover:

| Concern | Planned shape |
| --- | --- |
| Runtime | Python 3.12, FastAPI (async) |
| Dependencies | PostgreSQL 16, Redis 7, an S3-compatible object store, LiteLLM proxy, Langfuse, ffmpeg |
| Local bring-up | `docker compose up` for the dependency set, then the API and worker processes |
| Configuration | `.env` from [`.env.example`](./.env.example) — the authoritative variable list, including `MAGICHOUR_*`, LiteLLM, Langfuse, Postgres, Redis, artifact storage and the budget caps. **Model aliases and provider keys live in config, never in code** |
| Migrations | Expand/contract, applied **before** deploy — see [`persistence.md`](./docs/LLD/persistence.md#4-migrations--expandcontract) |
| Prompts | Authored in-repo under `prompts/` and registered to Langfuse on startup if absent — a fresh checkout runs without a Langfuse connection `[D-72]` |
| Auth | Static per-tenant API keys, `Authorization: Bearer <key>`, Argon2id-hashed `[D-68]` |
| Queue | Redis Streams with consumer groups, at-least-once `[D-67]` |
| Tests | Unit and integration per module (each LLD's §"Test strategy"). The QC calibration suite is built but **not run** in v1 `[D-66]` |
| CI gates | Eval regression > 3% and cost regression > 20% block a merge — see [`observability.md`](./docs/LLD/observability.md#8-ci-gates) |

The per-module test strategies are already written and are the specification for the test
suite. Implementers should read the target module's LLD before writing a line.

## CDR workflow

Canonical documentation is maintained by CDR agents, whose state lives in `.cdr/`.

| Path | Contents |
| --- | --- |
| `.cdr/cdr.config.json` | Runtime configuration and macro registry |
| `.cdr/schemas/` | JSON Schemas for run metadata, index lines, handoffs, verification |
| `.cdr/runs/<date>/<NNN>-<agent>/` | One directory per agent run: `metadata.json`, reports, `handoff.json` |
| `.cdr/index/` | JSONL indexes: `feature`, `file`, `decision`, `regression`, `task` |
| `.cdr/memory/` | Durable cross-run memory: state, decisions, timeline, impact map, pending |

### Entry points

| Entry point | When | Effect |
| --- | --- | --- |
| **Documentation · `init`** | Greenfield, or docs do not exist | Creates HLD, LLDs, README and AGENT from a repo scan, stamping `last_synced_commit`. |
| **Documentation · `sync`** | After code changes | Builds a drift report from `.cdr/index/file.jsonl` and recent impact analyses, and regenerates **only** drifted sections. |

Conventions every run follows: read in the order `index/ → memory/ + handoffs → targeted LLD
→ touched files → source`; write `metadata.json` before doing work and update
`last_completed_step` after each step; never leave a canonical document half-written.

The most recent run is [`.cdr/runs/2026-08-08/001-documentation/`](./.cdr/runs/2026-08-08/001-documentation/),
which created every document listed above at commit `4381385`.

## Try it locally — the mock-provider trial UI

A full end-to-end run without touching a real video-generation account: real API, real
Postgres/Redis/S3, real graph, shots rendered by [`MockVideoProvider`](./src/video_agent/providers/mock.py)
(ffmpeg, no network, no cost). No auth is required for this path — see each script's own
docstring for exactly what it turns off and why it's never wired into a real deployment.

```bash
make compose-up                              # Postgres, Redis, MinIO, LiteLLM
uv run python scripts/dev_server.py          # terminal 1 — the real API, no-auth verifier
uv run python scripts/dev_worker.py          # terminal 2 — the real worker, mock shots
cd ui && npm install && npm run dev          # terminal 3 — http://localhost:5173
```

Open the UI, enter a prompt, click **Create video**, and watch `current_node`/`budget` update
live until the delivered video and all four shot clips play inline. For a one-shot run with no
UI at all: `uv run python scripts/mock_trial_run.py "your prompt here"`.

## Try it for real — a live Magic Hour run on the same dashboard

Same API, same graph, same UI as above — only the provider differs. This spends real credits
and produces real rendered clips instead of `MockVideoProvider`'s ffmpeg output.

**1. Get Magic Hour credentials.** Sign up at [magichour.ai](https://magichour.ai/settings/developer)
and grab a key (`mhk_live_...`). A 10-second `480p`/`720p` clip on the pinned model
(`ltx-2.3`, `[D-61, amended]`) costs ~240 credits, and one job renders 4 shots — budget **at
least ~1,000 credits** before starting a job, so a job doesn't die mid-run partway to assembly.
Split across two accounts is fine too (e.g. 500 + 500): `MagicHourProvider`'s key rotator
(`RotatingApiKey`, `providers/magichour.py`) automatically advances to the second key only when
the first is rejected for insufficient credits (`402`) — never for any other failure.

**2. Fill in `.env`** (copy from `.env.example` if you haven't already):

```bash
MAGICHOUR_API_KEY=mhk_live_...       # your primary key
MAGICHOUR_API_KEY_2=mhk_live_...     # optional — a second account, rotated onto on 402 only
MAGICHOUR_MODEL=ltx-2.3
MAGICHOUR_USD_PER_1K_CREDITS=0.90    # match YOUR account's billing tier — see the comment above
                                      # this line in .env.example for the tier table
```

`.env` is git-ignored and must never be committed. The rest of `.env.example`'s fields
(`DATABASE_URL`, `REDIS_URL`, `ARTIFACT_*`, `LITELLM_*`) already default to the local
`docker-compose.dev.yml` stack started in the next step, so nothing else needs to change for a
local run.

**3. Start everything** — four terminals:

```bash
make compose-up                              # terminal 1 — Postgres, Redis, MinIO, LiteLLM
uv run python scripts/dev_server.py          # terminal 2 — the real API, no-auth verifier
uv run python scripts/real_dev_worker.py     # terminal 3 — the real worker, REAL Magic Hour shots
cd ui && npm install && npm run dev          # terminal 4 — http://localhost:5173
```

Use `scripts/real_dev_worker.py` here, not `scripts/dev_worker.py` — the latter is hardcoded to
`MockVideoProvider` and will never call the real API no matter what's in `.env`.

`get_settings()` is cached once per process (`@lru_cache`), so if you edit `.env` — new keys,
a different model — **after** `dev_server.py`/`real_dev_worker.py` are already running, restart
both to pick the change up. Editing the file alone does nothing to an already-running process.

Open `http://localhost:5173` (not `127.0.0.1` — the Vite dev server binds the IPv6 loopback) and
submit a prompt exactly as in the mock walkthrough above. A real 4-shot job has taken anywhere
from ~6 minutes to well over 30 in testing, since it depends on Magic Hour's live queue depth —
that variance is expected, not a hang.

To drive it from a terminal instead of the browser (the same tenant sees both, since the
no-auth dev verifier resolves every request to one fixed trial tenant regardless of who calls):

```bash
curl -s -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Authorization: Bearer dev-no-auth-placeholder-token-000000" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"prompt": "a violinist plays on a rooftop at dawn as the city wakes below"}'
```

## Function-level reference

Every function and class below is grouped by module, with a one-line description and the
specific requirement, decision (`[D-nn]`), or spec section it implements — cited from the
matching [`docs/LLD/*.md`](./docs/LLD/) file, not asserted from memory. Where a doc's own
status header says a piece is deferred, that's stated plainly rather than glossed over; where
the running code has quietly gone further than, or diverged from, what a doc describes, that's
called out too. Click a module to expand it.

<details>
<summary><strong>harness/</strong> — the agent loop's termination engine (<a href="./docs/LLD/harness.md">harness.md</a>)</summary>

### `harness/decide.py`
- `FatalError.is_terminal` — reads terminality off the error's taxonomy code rather than the raiser — implements *harness.md §5 rule 2, "non-retryable error? → TERMINATE FAILED" (retryability sourced from the code, `[D-62]`)*
- `NoProgress.human_reason` — renders the repeated-signature explanation, noting cross-shot promotion — implements *harness.md §6, no-progress detection (`[D-02]`)*
- `EvaluatorState.satisfied` — conjuncts all four acceptance conditions (shots, assemble, deliver, non-empty manifest) — implements *harness.md §5, "Evaluator satisfied" definition; zero-deliverable guard `[D-73]`*
- `decide(state: LoopState, *, now: datetime) -> Decision` — evaluates the six termination rules in fixed order, first match wins — implements *harness.md §5, the six-rule priority order*

### `harness/budget.py`
- `BudgetBreach.code` — maps a breached axis to its `VA-BUDGET-*` code — implements *harness.md §4, one code per axis*
- `BudgetBreach.human_reason` — formats limit vs. projected for the error envelope — implements *harness.md §4, enforcement rules*
- `BudgetCaps.from_settings(settings, *, tenant_max_usd_per_job=None)` — builds the four hard caps from config, applying the tenant USD override — implements *harness.md §4, cap table row `max_usd` (`[D-08]`, `[D-70]`)*
- `BudgetLedger.usd_spent` — sums all charges, counting unsettled provisional amounts as spent, never free — implements *harness.md §8, "Provisional provider charge never settles" (`[D-60]`)*
- `BudgetLedger.tokens_used` — sums token charges across the ledger — implements *harness.md §4, `max_tokens` cap*
- `BudgetLedger.wall_clock_s(now)` — elapsed time from the persisted `started_at`, never a process-local clock — implements *harness.md §8, "Clock skew / paused container"*
- `BudgetLedger.view(now)` — derives the read-only `BudgetView` remainder for a node's context — implements *harness.md §3.1 `NodeContext.budget_remaining`*
- `BudgetLedger.exceeded(now)` — reports the first cap already breached, or `None` — implements *harness.md §5 rule 4, "budget exceeded? → TERMINATE PARTIAL"*
- `BudgetLedger.would_exceed(estimate, now)` — pre-flight veto: reports the first cap an estimated act would breach — implements *harness.md §4, "Pre-flight" enforcement rule*
- `BudgetLedger.count_superstep()` — increments `iterations_used` once per node — implements *harness.md §4, `max_iterations` cap*
- `BudgetLedger.apply(charge)` — records a charge idempotently by id, refusing a conflicting re-application — implements *harness.md §4, "Monotonic per finalised charge" (`[D-60]`)*
- `BudgetLedger.settle(charge_id, *, usd, tokens=None)` — corrects a provisional charge to its final amount, exactly once — implements *harness.md §4, "corrected exactly once" (`[D-60]`)*
- `BudgetLedger.refund(charge_id, *, refunded_usd=None)` — settles a charge downward for a failed render — implements *harness.md §8, "Provisional provider charge never settles" row (`[D-60]`)*
- `BudgetLedger.unsettled_ids()` — lists still-provisional charge ids for the settlement sweeper — implements *harness.md §8, sweeper reconciliation (`[D-60]`)*

### `harness/cancel.py`
- `CancelRequest.outcome` — maps actor to `FAILED` (client) or `ESCALATED` (operator) — implements *harness.md §5 rule 1, cancelled → ESCALATE/FAILED (`[D-12]`)*
- `CancelRequest.reason_code` — maps actor to `VA-REQ-006` or `VA-INT-001` — implements *harness.md §5 rule 1 (`[D-12]`)*
- `CancelRequest.human_reason` — names the actor without splicing the untrusted free-text reason — implements *harness.md §5 rule 1*
- **Gap, honestly disclosed:** this file defines only the cancel *data model* — no function anywhere implements `Harness.cancel(job_id, actor)` itself (the cooperative-completion wait in harness.md §8, "Cancel arrives mid-node"). The cancel *signal* is written by `api/jobs.py::cancel_job` and read by `graph/worker.py::_poll_cancel`; there is no single `Harness.cancel()` method.

### `harness/outcomes.py`
- `Decision.is_terminal` — whether the verdict is `TERMINATE`/`ESCALATE` — implements *harness.md §2, `Verdict`/`Decision` definitions*
- `Decision._check_outcome_matches_verdict` — enforces `outcome` set iff verdict is terminal — implements *harness.md §2*
- `Decision._check_reason_code` — enforces every failing terminal decision carries a taxonomy code — implements *harness.md §5, per-rule reason codes*

### `harness/signatures.py`
- `FailureSignature.digest()` — sha256 over scope, node, code, discriminator (excludes shot index) — implements *harness.md §6.1, signature construction*
- `FailureSignature.shot_field` — per-shot counter key — implements *harness.md §6.2, "Shot scope, seen twice" abandonment*
- `RepeatInfo.terminates_job` — true when repeat is at job scope — implements *harness.md §6.2, job-scope repeat → `FAILED_NO_PROGRESS`*
- `RepeatInfo.abandons_shot` — true when repeat is at shot scope — implements *harness.md §6.2, shot abandonment*
- `score_band(score, width=SCORE_BAND_WIDTH)` — buckets a QC score into a 0.05-wide `Decimal` band — implements *harness.md §6.1, `[D-18]`*
- `qc_discriminator(*, failing_dimensions, score)` — builds `dims=...;band=...` — implements *harness.md §6.1*
- `SignatureLedger.record(signature)` — increments digest/per-shot counters; detects same-shot repeat and cross-shot promotion — implements *harness.md §6.2*
- `SignatureLedger.count_of(signature)` — total times a digest has been recorded — implements *harness.md §6, Redis `sig:{job_id}` hash*
- `SignatureLedger.snapshot()` / `.restore(snapshot)` — checkpoint mirroring so a resumed job doesn't forget what already failed — implements *harness.md §6.2*

### `harness/context.py`
- `NodeContext.for_node(...)` — verifies the bible, looks up the node's tool grant, assembles context — implements *harness.md §3.1 rules 1, 2, 4; §3.2 tool registry*
- `NodeContext.require_tool(tool)` — raises unless `tool` is granted to this node — implements *harness.md §3.1 rule 4*

### `harness/errors.py` and `harness/grants.py`
- `HarnessError`, `UngrantedToolError`, `UnknownToolError`, `BibleHashMismatchError`, `LedgerWriteError`, `ChargeConflictError`, `SettlementError` — the module's error taxonomy — implement *harness.md §3.1, §3.2, §4, §8, `[D-19]`, `[D-60]`*
- `GRANTS: dict[str, frozenset[str]]` — the node → allowed-tool-names table, transcribed verbatim — implements *harness.md §3.2, `[D-06]`, `[D-58]` (capability names, never providers)*

**Status:** harness.md's own header claims budget caps and no-progress detection are designed but "deferred to E4." **The code disagrees with its own doc** — `budget.py`'s full pre-flight/settle/refund ledger and `signatures.py`'s full promotion logic are built and wired into `decide.py`'s rules 3 and 4 today, ahead of the doc's stated schedule.
</details>

<details>
<summary><strong>graph/</strong> — the LangGraph state machine and its worker (<a href="./docs/LLD/graph.md">graph.md</a>)</summary>

### `graph/state.py`
- `ShotState` / `JobState` (`BaseModel`) — the per-shot and per-job checkpointed state — implements *graph.md §2's state contract, including "No media bytes in state" and `last_good_frame_artifact_id` (`[D-05]`)*
- `GraphInvariantError` / `ShotCountInvariantError` — invariant-violation errors — implement *graph.md §8, `VA-PLAN-003`*
- `assert_invariants(state, *, node, previous=None)` — checks all six checkpoint-time invariants in the doc's own table order (shot count, repair cap `[D-01]`, attempts=repairs+1, bible-hash match, outcome-only-at-finalize, monotonic budget) — implements *graph.md §2's invariant table*

### `graph/guard.py`
- `JobHarness.decide(state, node, *, now)` — builds the superstep's `LoopState` from live cancel/error/no-progress facts and calls the harness's `decide()` — implements *graph.md §3.1*
- `guard(state, node, *, harness, now)` — the harness veto every router applies first; terminal decision writes `outcome`/`degraded`/`terminal_reason_code` and routes to `finalize` — implements *graph.md §3.1's `_guard` pseudocode*

### `graph/deps.py` and `graph/build.py`
- `GraphDeps` — everything a compiled job graph needs (`engine`, `gateway`, `checkpointer`, `harness`, `now`, `providers`, `artifacts`) — implements *graph.md §3, §4 (`[D-23]`)*
- `build_graph(deps)` — wires all nine nodes, one entry point, seven conditional routers plus two direct edges, compiled with the checkpointer — implements *graph.md §3's exact topology*

### `graph/lock.py`
- `JobLock.acquire(job_id)` — claims `job:{job_id}` via `SET NX EX`, `None` if already held — implements *graph.md §6.2, `[D-10]`*
- `JobLock.heartbeat(token)` — read-then-write TTL renewal, raises `JobLockLostError` if lost — implements *graph.md §6.2, `[D-10]`*
- `JobLock.release(token)` — deletes the key only if this token still owns it — implements *graph.md §6.2, one-writer-per-job*

### `graph/frame_extraction.py`
- `extract_last_frame` — writes a clip's last decodable frame as a lossless PNG via `ffmpeg -sseof` — supports *graph.md §7's assembly.md dependency for `extract_final_frame`*
- `frame_variance` / `is_uniform_frame` — grayscale variance and the uniform-frame rejection check — same dependency; graph.md itself names no variance floor
- `find_last_usable_frame` — steps back up to 3s from end-of-stream for a non-uniform frame, else no anchor — feeds *graph.md §3.3's chaining rule, `[D-05]`*'s degraded-fallback path

### `graph/worker.py`
- `JobWorker.run_forever(*, poll_block_ms=5000)` — polls `claim_stalled`, then `read_own_pending`, then `read_new`, forever — implements *graph.md §6.1, `[D-67]`; §8's orphan-sweep row*
- `JobWorker.handle_one(delivery)` — acquires the lock, runs the job under a heartbeat, releases, `XACK`s — implements *graph.md §6.2, `[D-10]`; §8's two-workers-one-job row*
- `JobWorker._heartbeat_loop(token)` — renews the lock; an uncaught `JobLockLostError` must propagate — implements *graph.md §6.2, `[D-10]`*
- `JobWorker._run_job(tenant_id, job_id)` — loads the job, builds fresh state/harness/deps, invokes the graph with `thread_id=str(job_id)` — implements *graph.md §4's checkpoint-table thread-id row*
- `JobWorker._poll_cancel(harness, job_id)` — polls the cancel-signal key into the harness — implements *graph.md §3.1's harness-veto mechanism*
- `_fresh_state(job)` / `_fallback_caps()` — initial `JobState` construction; the fallback-caps policy is a `worker.py`-local decision, genuinely uncited in graph.md

### `graph/nodes.py` — the nine node bodies and their routers
- `plan_story_node` / `route_after_plan` — one LLM planning pass, persisted once, redelivery-safe (re-derives from disk rather than double-writing) — implements *graph.md §3, §6.1's "every node safe to execute twice" (`[D-67]`)*
- `lock_bible_node` / `route_after_bible` — one LLM pass, persisted once, never updated, redelivery-safe — implements *graph.md §2's `bible_hash` invariant, §6.1*
- `select_next_shot_node` / `route_select` — advances to the lowest-index pending shot from checkpointed state only — implements *graph.md §5's "second guard" description (in-code noted as a v1 gap: intended to also check Postgres directly, `[D-11]`)*
- `generate_shot_node` — the three-phase write sequence: claim attempt in-flight and commit → call provider, persist `provider_project_id` → settle cost, catalogue clip, checkpoint in one transaction — implements *graph.md §4's three numbered phases, `[D-23]`, `[D-24]`, `[D-58]`*
- `_resolve_conditioning` — shot 0 is text-only; a later shot chains the last accepted frame or, absent one, generates text-only with `degraded=True` — implements *graph.md §3.3, `[D-05]`*
- `_claim_shot_attempt` / `_settle_shot_and_checkpoint` — phase (1) and phase (3) of the write sequence above — implement *graph.md §4, `[D-24]`, `[D-23]`*
- `extract_final_frame_node` / `route_after_frame` — extracts and catalogues the continuity frame, or flags `degraded` with no anchor — implements *graph.md §7's assembly.md dependency and the `[D-05]` fallback*
- `qc_shot_node` / `route_after_qc` — **confirmed stub**: unconditionally accepts every shot with `best_score=1.0`, never scores, never repairs — implements *graph.md's own status header ("in v1 a shot that fails QC is simply accepted or abandoned without repair"); QC scoring/repair is E3 scope*. The repair back-edge is structurally present in `build.py`, and — as of the manual override below — reachable, just never from QC's own judgment, since that judgment doesn't exist yet.
- **Addition, not QC:** `qc_shot_node` also checks `deps.harness.force_repair_shots` — a signal manually injected via `POST /v1/jobs/{job_id}/shots/{shot_index}/force-repair` (`persistence.keys.shot_repair_signal_key`), relayed into the harness by `worker.py`'s `_poll_repair_signals`. If the flagged shot hasn't hit the repair cap, it's sent back to `PENDING` with `repairs_used` incremented instead of being accepted — exercising the real repair mechanism (the back-edge, the cap, continuity held across the regenerated shot) without pretending the scoring itself exists. Nothing here evaluates the shot against the bible.
- `assemble_node` / `route_after_assemble` — normalizes/concatenates every accepted shot, picks a thumbnail source, raises on zero accepted shots — implements *graph.md §7's assembly.md dependency; §8's "stop rather than paper over it" philosophy*
- `deliver_node` — builds the `DeliveryManifest` from already-catalogued artifacts — implements *graph.md §3's direct `deliver → finalize` edge*
- `finalize_node` — the only node permitted to write a job's terminal outcome — implements *graph.md §2's invariant "outcome is None for any non-finalize node"*

**Status:** graph.md's header states **E1 + E2 ship in v1**; **the repair back-edge and full checkpoint-precise resume (§5) are deferred to E3.** Confirmed against the code: `qc_shot_node` is a genuine unconditional-accept stub, and `worker.py`'s crash recovery re-runs a job from the graph's entry point via lock-TTL expiry, not from its last checkpoint — `plan_story`/`lock_bible` can re-spend one LLM call on a crash before `generate_shot`'s fingerprint idempotency takes over. `select_next_shot_node` also documents a v1 gap against §5's "second guard."
</details>

<details>
<summary><strong>planning/</strong> and <strong>qc/</strong> — story planning, the continuity bible, and QC (<a href="./docs/LLD/planning.md">planning.md</a>, <a href="./docs/LLD/qc.md">qc.md</a>)</summary>

### `planning/models.py`
- `BeatKind` / `CameraMove` (`StrEnum`) — the fixed four-beat arc and nine-value camera-movement vocabularies — implement *planning.md §2.1, `[D-26]`*
- `Beat` — one frozen 10s beat, index bounded 0–3, duration pinned to exactly 10.0 — implements *planning.md §2.1, `[D-03]`*
- `StoryPlan._validate` — asserts beat indices/kinds/order and that durations sum to 40.0s within `1e-6` — implements *planning.md §2.1's validator block, PRD "How it works" step 1*
- `CharacterSpec` / `WardrobeSpec` / `LocationSpec` / `LightingSpec` / `PaletteSpec` / `LensLanguageSpec` — the six bible dimension schemas, frozen, `extra="forbid"` — implement *planning.md §2.2, `[D-63]`*
- `ContinuityBible` — the six dimensions plus `negative_constraints`, `content_hash`, `locked_at` — implements *planning.md §2.2, `[D-27]`, `[D-07]`*

### `planning/bible.py`
- `compute_content_hash(bible)` — sha256 over canonical JSON, excluding `content_hash` and `locked_at` (server-assigned write-time metadata) — implements *planning.md §3.2*
- `verify_bible(bible)` — recomputes the hash, raises `BibleHashMismatchError` (`VA-BIBLE-002`) on mismatch — implements *planning.md §3.2, §5*
- `render_bible_block(bible)` — the one deterministic renderer consumed by both generation prompts and QC scoring — implements *planning.md §3.4, "one renderer, two consumers"*

### `planning/service.py`
- `plan_story(prompt, *, ctx, gateway)` — one `reasoning-high` structured-output pass, up to one re-ask on validation failure — implements *planning.md §3.1, `[D-28]`*
- `lock_bible(plan, prompt, *, ctx, gateway)` — one `reasoning-high` pass, retried once on a specificity-gate failure — implements *planning.md §3.2, `[D-07]`, `[D-29]`*
- `_specificity_violations(draft)` — flags empty strings, hedge words, and under-specified character detail — implements *planning.md §3.2's "specificity gate," `[D-29]`*

### `planning/errors.py`
- `PlanUnparseableError` (`VA-PLAN-001`), `PlanInvalidError` (`VA-PLAN-002`/`003`), `BibleTooVagueError` (`VA-BIBLE-001`) — implement *planning.md §5's failure-mode table*

**Status — planning: implemented.** planning.md's header states E1 ships in v1; the code matches its public interface function-for-function.

### `qc/models.py`
- `Dimension` (`StrEnum`) — **diverges from the doc**: 7 members here (`character_consistency`, `wardrobe_consistency`, `location_consistency`, `lighting_consistency`, `palette_consistency`, `negative_constraints`, `motion_quality`) versus qc.md §2's 8 (`character`, `wardrobe`, `location`, `lighting`, `palette`, `lens_language`, `beat_fidelity`, `integrity`)
- `QCFinding` — a stub (`dimension`, `score`, `rationale`) matching neither qc.md's `QCFinding` (severity/description/corrective_hint, no score) nor its `DimensionScore` exactly — exists only so `NodeContext.prior_findings` type-checks

**Not implemented** (qc.md §2–§6, absent from `src/video_agent/qc/`): `DimensionScore`, `QCReport`, `score_shot()`, `build_repair_delta()`, `failure_signature()`, the `WEIGHTS` aggregation formula, hard-fail clamping, and the calibration harness (labelled-set precision/recall/false-pass/false-fail). No vision-model call exists anywhere in the repo.

**Status — qc: deferred (E3), by design.** qc.md's own header says "design is complete... QC/repair loop is not wired into the graph." The code matches that exactly: `qc/` holds only the `Dimension`/`QCFinding` stub, and `graph/nodes.py::qc_shot_node` never calls into it.
</details>

<details>
<summary><strong>providers/</strong> — capability negotiation, failover, and the two adapters (<a href="./docs/LLD/providers.md">providers.md</a>)</summary>

### `providers/models.py`
- `Capability` (`StrEnum`), `ProviderProfile`, `ShotRequest`, `ShotResult`, `ProviderHealth` — the closed capability vocabulary and the request/result/profile shapes — implement *providers.md §2, `[D-59]`, `[D-60]`, `[D-61]`*
- `VideoProvider` / `ProviderRegistry` (`Protocol`) — the adapter and registry contracts — implement *providers.md §2*
- `VideoProvider.handle_webhook` / `ProviderRegistry.handle_webhook` — **an extension beyond the doc's §2 protocol block**, added on top of the base spec, justified by *providers.md §7.3* ("webhooks are... preferred over polling")
- `compute_request_fingerprint(...)` — deterministic sha256 over an attempt's identity so retries reuse the same fingerprint — implements *providers.md §2*

### `providers/negotiate.py`
- `required_for(shot)` — builds the required capability set (resolution ceiling, `IMAGE_CONDITIONING` when chaining, `NEGATIVE_PROMPT` when requested) — implements *providers.md §3*
- `select_providers(required, providers)` — ranks capability-superset providers by (superset size, config order, price, latency) — implements *providers.md §3 rule 3*
- `negotiate(shot, providers)` — never waives `IMAGE_CONDITIONING`; only waives `NEGATIVE_PROMPT` when unavailable, else raises — implements *providers.md §3 rules 1–2, `[D-31]`*

### `providers/registry.py`
- `PinnedProviderRegistry.generate(req, *, ctx)` — negotiates, pins shot 0's provider, retries/falls over/circuit-breaks across candidates — implements *providers.md §4, `[D-32]`*
- `PinnedProviderRegistry._attempt` — per-provider retry with exponential backoff+jitter, re-raises `402` immediately without retry — implements *providers.md §4, `[D-62]`*
- `PinnedProviderRegistry._finalize` — flags `degraded=True, reason="provider_switch_mid_job"` on a mid-job failover — implements *providers.md §4, `[D-32]`*
- `PinnedProviderRegistry.handle_webhook` — tries each provider's own verification sequentially — implements *providers.md §7.3* (same extension-over-base-spec note as `models.py`)

### `providers/compose.py`
- `compose_prompt(bible, beat, *, repair_delta=None, max_chars)` — assembles the fixed six-section prompt, truncating [4] then [3] then compressing [2], never [1]/[6] — implements *providers.md §5, `[D-33]`*

### `providers/errors.py`
- `NoProviderSatisfiesCapabilitiesError`, `ProviderGroupExhaustedError`, `PromptExceedsLimitError`, `ProviderUnavailableError`, `ProviderTimeoutError`, `ProviderPaymentRequiredError`, `ProviderRequestRejectedError`, `ProviderCredentialRejectedError`, `ProviderProjectNotFoundError`, `ProviderUnprocessableEntityError`, `ProviderRenderFailedError`, `ProviderRenderCanceledError` — the module's full status/error taxonomy — implement *providers.md §3 rule 1, §4, §5, §7.4, `[D-31]`, `[D-33]`, `[D-62]`*

### `providers/artifact_store.py`
- `S3ArtifactStore.read` / `.write` — the concrete `ArtifactStore` over `persistence.objects.ObjectTransport` — implements *providers.md §6's Transport row*. `.write`'s unscoped `_provider/{id}.{ext}` key is a disclosed, out-of-scope gap against `persistence.md` §6's tenant-prefixed layout, not a providers.md deviation.

### `providers/magichour.py` — the real adapter
- `MagicHourClient.submit_text_to_video` / `.submit_image_to_video` — `POST /v1/text-to-video` (shot 0, always, including repairs) and `POST /v1/image-to-video` (shots 1–3, no end-frame conditioning) — implement *providers.md §7.1*
- `MagicHourClient.get_video_project` — `GET /v1/video-projects/{id}`, warns on undocumented `draft` status — implements *providers.md §7.3*
- `MagicHourClient.create_upload_url` / `.upload_bytes` — the upload-URL frame-conditioning flow, auth kept intact on the URL — implement *providers.md §7.2, `[D-52]`, `[D-64]`*
- `MagicHourClient.download` — fetches completed render bytes, never caches the link — implements *providers.md §7.3, `[D-52]`*
- `_raise_for_status` — the full HTTP-status → error-code table (400/401/402/404/422/429/5xx) — implements *providers.md §7.4*
- `MagicHourProvider.generate` — routes shot 0 to text-to-video, shots 1–3/repairs to image-to-video, polls to terminal, converts credits to USD — implements *providers.md §7.1, `[D-61]`, `[D-60]`*
- `MagicHourProvider._poll_until_terminal` / `._notified` — polls every interval until terminal, skipping the sleep when a webhook already flagged the id — implements *providers.md §7.3's "webhooks are preferred... polling remains the fallback"*
- `MagicHourProvider.handle_webhook` — verifies an HMAC-SHA256 signature, extracts the project id, flags it — never trusts the payload for status/cost — implements *providers.md §7.3*; **an extension beyond the §2 protocol block**, same note as `models.py`
- `_build_profile(settings)` — validates the pinned model against duration constraints, builds capabilities, derives `price_per_second` — implements *providers.md §7.4, `[D-34, amended]`, `[D-61]`, `[D-63]`*
- `RotatingApiKey` — cycles forward through `Settings.magichour_api_keys()`, advancing only on a `402` — implements *providers.md §7.4's scoped exception to `[D-62]`*: a second, independent account can succeed where the first's balance cannot; single-key deployments are unaffected
- `MagicHourProvider._submit_with_rotation` — submits once, retries on `402` if `key_rotator` has a next key, propagates every other failure immediately — safe to retry unconditionally since a `402` is a rejection, never a charge
- `build_magichour_provider(settings, *, artifacts)` — the real adapter, wired from settings, rotator included automatically once a second key is configured — genuinely new: nothing built a real `MagicHourProvider` from settings before this function existed, mirroring `api.clients.build_gateway`'s same gap-filling role for the LLM gateway

### `providers/mock.py` — **not part of the original spec**
providers.md documents exactly one concrete adapter (Magic Hour, §7). `MockVideoProvider` is a
trial/testing tool added later — it structurally satisfies the `VideoProvider` protocol but is
not an implementation of any documented requirement.
- `MockVideoProvider.generate` — renders and stores a real MP4 via ffmpeg in a thread, turning the conditioning frame into the clip's background when present
- `MockVideoProvider.handle_webhook` — returns `False` unconditionally, correctly implementing the protocol's documented "no webhook support" behaviour

**Status — implemented:** the full negotiation/pinning/failover machinery, the fixed prompt composer, the Magic Hour adapter end-to-end (submit/poll/upload/download/error-mapping/cost accounting), and inbound webhook handling as a polling accelerant. **Deferred:** whatever E3/E4 scope the doc's milestone table reserves.

**Provider-substitution decisions:** per **`[D-58]`**, the PRD's specified "Higgsfield MCP" is replaced by **Magic Hour**, because Higgsfield offered no obtainable trial credential. This forces four disclosed deviations: **no seed parameter** (`[D-59]`), **credit-based billing** converted to USD (`[D-60]`), a **pinned model** (`wan-2.2`) validated against the fixed 10s shot length (`[D-61]`), and a **720p v1 target** with 1080p only as a ceiling (`[D-63]`).
</details>

<details>
<summary><strong>gateway/</strong> — the single LLM egress (<a href="./docs/LLD/gateway.md">gateway.md</a>)</summary>

### `gateway/gateway.py`
- `LiteLLMGateway.call(req, *, ctx)` — orchestrates plan → cache-read → serve — implements *gateway.md §1*
- `LiteLLMGateway._candidates` — checks required capabilities against **every** model in the alias group, fails closed on any deficiency — implements *§3 rule 3*
- `LiteLLMGateway._failover_order` — deterministic per-`job_id` canary head selection, never crossing alias groups — implements *§3 rules 2 and 4, `[D-20]`*
- `LiteLLMGateway._serve` — walks candidates respecting circuit admission, flags `FALLBACK`/`STALE_PROMPT` degrades, raises on exhaustion — implements *§4.2, §4.3, §4.5*
- `LiteLLMGateway._attempt_model` — up to three attempts with jittered backoff, retryable errors only — implements *§4.1, §4.3*
- `LiteLLMGateway._validate` — exactly one reformat attempt on invalid structured output, else `VA-GW-004` — implements *§5, §8*
- `_widen_schema_const_to_enum` / `_widen_node` — recursively rewrites `{"const": x}` to `{"enum": [x]}` before a structured-output schema is sent — **a compatibility fix, not a doc-specified rule.** gateway.md §5 never anticipates a provider (Gemini/Vertex) rejecting Pydantic's `const` keyword; this exists purely because a real call returned `400 INVALID_ARGUMENT: Unknown name "const"`.
- `LiteLLMGateway._as_error` — maps a classified failure to the typed error gateway.md §8 names (context-length, content-policy, `402`, else `VA-INT-001`) — implements *§8, `[D-62]`*
- `LiteLLMGateway._record_quarantine` — logs one `VA-SEC-001` warning per escaped span, never the matched text — implements *§5, §6's redaction rule*

### `gateway/transport.py`
- `HttpxLiteLLMTransport.complete` — one POST to `LITELLM_BASE_URL` with an `Idempotency-Key` header — implements *§4.1, §1*
- `HttpxLiteLLMTransport.model_info` — feeds the capability registry from `/model/info` — implements *§3 rule 3*
- `build_payload` — builds the chat-completions body, switching to `response_format: json_schema` for structured output — implements *§5*

### `gateway/breaker.py`
- `CircuitConfig` — 5 failures/30s window, 30s initial open, 5-min cap, 2x backoff, 30s probe lock — implements *§4.3's state-machine numbers verbatim*
- `CircuitBreaker.allows` / `.record_success` / `.record_failure` — admission decisions and the CLOSED↔OPEN↔HALF_OPEN transitions — implement *§4.3's state diagram*
- `ResilientCircuitStateStore` — degrades to an in-memory fallback on a Redis outage, alarming rather than failing — implements *`[D-22]`, §8's "Redis down (circuit state)" row*

### `gateway/capabilities.py`, `gateway/prompts.py`, `gateway/retry.py`, `gateway/pricing.py`, `gateway/classify.py`, `gateway/rendering.py`, `gateway/cache.py`, `gateway/clock.py`, `gateway/errors.py`, `gateway/models.py`
- `ProxyCapabilityRegistry` — discovers and caches model capabilities from the proxy, fails closed on a fetch failure — implements *§3 rule 3*
- `is_canary` / `canary_bucket` — SHA-256-derived, deterministic-per-`job_id` canary bucketing — implement *§3 rule 4, `[D-20]`*
- `CachingPromptRegistry.get_prompt` — serves last-known-good on registry failure, marked stale, never falls back to an inline string — implements *§4.4, `[D-72]`*
- `RetryPolicy.delay` — `min(base * 2**(n-1), cap) * uniform(0.5, 1.5)`, exactly 3 attempts total — implements *§4.1's formula verbatim*
- `CostCalculator.usage_for` — exact `Decimal` cost from the alias table's price entry; an unpriced model charges a pessimistic ceiling and alarms — implements *§6, `[D-21]`*
- `classify(exc)` / `_classify_status` / `_classify_body` — the full retryable/non-retryable table, checking the error body before the status code — implement *§4.1, §8*
- `render` / `escape_untrusted` — separates trusted instruction text from a delimited, escaped untrusted block; untrusted content never issues instructions — implement *§5, `[CPS §Non-negotiables]`*
- `is_cacheable(prompt_name)` — excludes `plan_story`/`lock_bible` unconditionally, no config flag — implements *§4.4's cache-exclusion row*
- `SystemClock` / `SystemJitter` — injected time and CSPRNG-based jitter for retry/circuit timing — underlie *§4.1, §4.3*
- `GatewayError` — the three-fact failure shape (what happened / preserved / next) plus a stable code and `trace_id` — implements *§4.5*

**Status:** gateway.md's header states **E0 + E1 + E2 ship** (alias resolution, retry/fallback/circuit-break, the prompt path, caching, cost accounting) — all implemented above. **E3/E4 are deferred**, and gateway.md itself doesn't itemize what they contain.
</details>

<details>
<summary><strong>persistence/</strong> — Postgres RLS, migrations, Redis, and artifact storage (<a href="./docs/LLD/persistence.md">persistence.md</a>)</summary>

### `persistence/schema.py`, `persistence/enums.py`
- `metadata` + ten `Table` objects — the single machine-checkable schema definition, `tenant_id` denormalised onto every child table — implements *persistence.md §2 whole; `[D-51]`, `[D-01]`, `[D-03]`, `[D-59]`, `[D-24]`, `[D-60]`, `[D-16]`, `[D-23]`*
- `enum_labels` / `pg_enum` — one member list shared by migration, ORM column, and drift test — implements *§2*

### `persistence/rls.py`
- `RLS_PROTECTED_TABLES` / `RLS_EXEMPT_TABLES` — every table protected except two, each with a mandatory documented reason — implement *§3, `[D-70]`, `[D-68]`*
- `enable_rls_statements(table_name)` — `ENABLE` + `FORCE ROW LEVEL SECURITY` + a policy with both `USING` and `WITH CHECK` — implements *§3 rules 2 and 4*
- `audit_rls(facts)` — the CI gate: flags any table missing `ENABLE`, `FORCE`, a policy, or a correct predicate — implements *§10, "a CI check fails the build if any table lacks RLS"*

### `persistence/session.py`
- `tenant_session(engine, tenant_id)` — opens one transaction, runs `SELECT set_config('app.tenant_id', ...)` as a bound, `is_local=true` parameter, yields a `TenantSession` — implements *§3 rule 3 verbatim*; this is the one function in the codebase that issues that statement
- `TenantSession.require_open()` — raises `TenantContextMissingError` and alarms rather than silently querying with no tenant set — hardens *§9's "RLS setting absent → zero rows, plus an alarm"* into a loud application-level error

### `persistence/keys.py`, `persistence/redis_client.py`, `persistence/queue.py`
- `KEY_REGISTRY` — every Redis key pattern, type, and TTL policy transcribed from §5's table in one place — implements *§5 whole*. Includes `shot_repair_signal_key` — the manual QC-repair override the api/ and graph/ sections below describe, added on top of the original spec, following this exact registered-key discipline rather than a one-off literal
- `RedisStore.set_if_absent` — `SET NX EX`, one atomic command, never read-then-write — implements *§5*
- `JobQueue.read_new` / `.read_own_pending` / `.claim_stalled` / `.ack` — the full at-least-once consumer-group protocol (new entries, own-pending redelivery, stalled reclaim, and ack-only-after-durable) — implement *§5.1, §9's "worker dies holding a pending entry" row*

### `persistence/objects.py`, `persistence/presign.py`
- `storage_key(location)` — renders `{tenant_id}/{job_id}/{kind}/{shot_index|"job"}/{artifact_id}.{ext}`, validated against path escape — implements *§6*
- `ArtifactStore.upload` — checksum-first, retry-backed `PUT`, then read-back verification, only then deletes the local scratch file — implements *§6, §9*
- **`[D-52]` enforcement — persist.py's whole reason to exist:** `mint_artifact_url` signs a `GET` on demand and never returns anything logged/cached/stored; `mint_or_null` alarms on presign failure instead of dropping the artifact or logging the failure detail; there is no `url` column anywhere in `schema.py` — implement *§6, "never stored, never cached, never logged"*

### `persistence/repositories.py`
- `_Repository.tenant_id` — the **only** source of tenant id for any write; no method in this file accepts one as an argument — implements *§3 rule 3, `[D-68]`*
- `JobRepository.create` — `INSERT ... ON CONFLICT DO NOTHING` + fallback read; a fingerprint mismatch on the winning row raises rather than double-billing — implements *`[D-16]`*
- `ShotAttemptRepository.claim` — writes the `in_flight` attempt row **before** calling the provider, so a crash mid-render leaves a fingerprinted row, not an untraceable charge — implements *`[D-24]`, `[D-67]`, `[D-59]`*
- `ShotAttemptRepository.settle_cost` — the only place `cost_is_final` becomes `true` — implements *`[D-60]`*
- `ContinuityBibleRepository` — deliberately has no `update` method; a database trigger enforces immutability — implements *PRD "How it works" step 2, `VA-BIBLE-002`*
- `CheckpointRepository.write` — appends a `(thread_id, seq)`-unique row in the same transaction as the node's own domain writes — implements *§2, `[D-23]`*

**Status:** persistence.md's header states **implementation BUILT, E0**. Everything above is implemented. The one declared-but-unbuilt piece is out of scope for this module's code: pgvector/MongoDB Atlas has no consumer in Video Agent v1 (`[D-13]`).
</details>

<details>
<summary><strong>api/</strong> — the FastAPI surface (<a href="./docs/LLD/api.md">api.md</a>)</summary>

### `api/app.py`, `api/jobs.py`, `api/artifacts.py`
- `create_app(...)` — builds the wired application: lifespan-managed resources, middleware, exception handlers, routers — implements *api.md §1, §7*
- `create_job` — the full idempotency algorithm (claim → create-or-adopt → enqueue once → finish), returns `202`, never `200` — implements *api.md §2.1, §3*
- `get_job` / `list_jobs` — status/outcome/degraded/budget view; cursor-paginated tenant-scoped listing — implement *api.md §2.1, §2.2*
- `cancel_job` — idempotency-keyed cooperative cancel, `200` no-op if already terminal — implements *api.md §2.1, §8*
- `force_repair_shot` — **not in any spec, and not `regenerate`.** `POST /v1/jobs/{job_id}/shots/{shot_index}/force-repair` manually injects the repair signal QC's own scoring (`qc.md`, E3) would eventually send — it never evaluates anything. Same idempotency-keyed, no-op-if-terminal shape as `cancel_job`; writes `persistence.keys.shot_repair_signal_key` for the worker to relay into `graph.nodes.qc_shot_node`
- `stream_job` — polls the checkpoint repository and emits SSE-shaped events — implements the doc's progress-stream intent, but **deviates**: it serves `/stream`, not §2.1's `/events` route, and polls rather than reading the richer Redis `progress:{job_id}` channel §5 describes
- `list_job_artifacts` — tenant-scoped, freshly presigns every artifact per request, `url: None` rather than dropping one on a presign failure — implements *api.md §2.1, §2.2, §8*; response shape is narrower than §2.2's full `DeliveryManifest`

### `api/webhooks.py` — **not in api.md's route table**
- `receive_provider_webhook` — `POST /v1/webhooks/{provider_key}`, verifies via `ProviderRegistry.handle_webhook`, answers `200`/`401`/`503` — an addition beyond the original spec, implementing the inbound-webhook design *providers.md §7.3* describes, distinct from the outbound `webhook_url` field api.md's `[D-74]` removed

### `api/principal.py`, `api/idempotency.py`
- `require_tenant` — resolves `Authorization: Bearer <key>`, one `401` for every failure shape — implements *api.md §6*
- `assert_tenant_owns` — `404`, never confirming existence, for a cross-tenant read — implements *api.md §4, §6*
- `begin_idempotent` / `finish_idempotent` — the full claim/replay/conflict algorithm — implement *api.md §3 steps 1–5*
- `RedisIdempotencyStore` — `SET ... NX EX`, `503` on Redis failure — implements *api.md §3, §8, `[D-17]`*

### `api/errors.py`, `api/handlers.py`, `api/health.py`
- `ErrorEnvelope` — the one response shape for every non-2xx (`code`, `message`, `retryable`, `trace_id`, `job_id`, `preserved`, `next_steps`, `details`) — implements *api.md §4 verbatim*
- `register_exception_handlers` — installs handlers for `ApiError`, `VideoAgentError`, framework `HTTPException`, and validation errors, all rendering the same envelope — implements *api.md §4*
- `readyz` — probes Postgres and Redis, `503 VA-STORE-003` if either is unreachable — implements *api.md §2.1, §8*

**Status:** api.md's header states **E1 ships**; `resume`/`shots/{i}/regenerate` are deferred to E3 and genuinely absent from `jobs.py`. Two things ship *differently* than documented rather than being deferred outright — the progress stream and the artifacts response shape, both noted above — and one thing ships beyond the doc entirely: the inbound webhooks route.
</details>

<details>
<summary><strong>assembly/</strong> and <strong>observability/</strong> — ffmpeg stitching, logging, and redaction (<a href="./docs/LLD/assembly.md">assembly.md</a>, <a href="./docs/LLD/observability.md">observability.md</a>)</summary>

### `assembly/media_toolchain.py`
- `assert_media_toolchain()` — verifies ffmpeg/ffprobe are present, at the pinned version, from the same release — implements *assembly.md §7–8, "ffmpeg version drift → Refuse to start"*
- `resolve_binary(binary)` — resolves to an absolute path, rejecting non-absolute/non-executable overrides — implements *§6, "every binary path comes from `resolve_binary`"*
- `normalize_clip` — scale/pad, retime to 24fps CFR, H.264 High `yuv420p` BT.709, strip audio, `+faststart` — implements *§4.1, `[D-46]`*
- `concat_clips` — stream-copy concat via the ffmpeg concat demuxer, no re-encode — implements *§4.1, §4.2, `[D-47]`*
- `build_thumbnail` — re-encodes an extracted frame into the canonical 1280×720 JPEG — implements *§4.4, `[D-49]`*

### `assembly/models.py`
- `DeliveryManifest` — the finished job's manifest, requiring at least one entry — implements *§5, `[D-73]`, "an empty manifest is a zero-deliverable job"*

**Status — assembly:** frame extraction and the ffmpeg primitives (E1/E2) are built; **partial assembly's full mechanism and music-bed mixing (E3/E4) are designed but their orchestration lives in `graph/nodes.py`, not this package** — `assembly/` itself holds only the low-level ffmpeg wrappers and the manifest model.

### `observability/logging.py`, `observability/redaction.py`
- `configure_logging(settings)` — installs JSON logging on the root logger with trace/tripwire/sampling filters — implements *observability.md §4–5*
- `TripwireFilter` — runs the redaction scan outside `Handler.emit`'s exception-swallowing try, so a hit can actually raise and fail a build — implements *§5*
- **`scan_payload` / `redact` — this is the `[D-52]` tripwire mechanism.** It catches credentials (by key-name pattern and value shape — entropy, known issuer prefixes like `sk-`/`AKIA`/`ghp_`, `Authorization` schemes), presigned/credentialed URLs (signature/token query parameters, userinfo auth — explicitly citing `[D-52]`/`[D-64]` in its own hit detail), raw or base64-encoded media magic bytes and `data:` URIs, and `SecretStr`-style wrapper objects — recursively, over every log record and every would-be Langfuse payload.
- `summarise_prompt(prompt)` — the only representation of a user prompt allowed to leave the database: a sha256 plus a scanned 64-character preview — implements *§5, "Raw PII"*

### `observability/codes.py`, `observability/registry.py`
- `ErrorCode` — every `VA-<DOMAIN>-<NNN>` code with its meaning and retryability — implements *§6 whole, `[D-55]`*
- `check_registry` — cross-checks live codes against an append-only register for unregistered/removed/reissued/repointed/retryability-changed codes — implements *§10's "undocumented error code → CI check → build blocked"*

**Status — observability:** structured JSON logging, the redaction tripwire, and the error-code taxonomy (E0) are fully implemented. **Langfuse tracing itself — `trace()`/`span()`/`generation()`/`score()` — is not wired up anywhere in the code**; no file imports the Langfuse SDK, and `observability/__init__.py`'s own docstring says spans/generations/scores "are deliberately not here." This confirms the doc's own header: **E4 is deferred**, E0 is built.
</details>

<details>
<summary><strong>scripts/ and ui/</strong> — the mock-provider trial harness (not part of any LLD spec)</summary>

Added after the original scope, purely so the pipeline can be exercised end to end without a
real, slow, account-limited video render. None of this is auto-wired into a real deployment —
each piece is opt-in and says so in its own docstring.

- [`scripts/dev_server.py`](./scripts/dev_server.py) — serves the real API against the real local dev stack, with an `AllowAllApiKeyVerifier` (defined here, never in `src/`) and CORS open for the trial UI's dev server
- [`scripts/dev_worker.py`](./scripts/dev_worker.py) — the continuous `JobWorker` loop, rendering shots with `MockVideoProvider` instead of a real provider
- [`scripts/mock_trial_run.py`](./scripts/mock_trial_run.py) — a one-shot version of the above: create one job, drive it through the compiled graph directly, print the result
- [`ui/`](./ui/) — a small React (Vite) single-page app: create a job, watch its live status/`current_node`/budget, list all jobs, and play the delivered video plus its shot clips once terminal
- [`api/app.py::create_app`](./src/video_agent/api/app.py)'s `cors_origins` parameter — the one production-code change this required: an opt-in CORS middleware, `None` by default, added for exactly this local-browser-client use case
</details>

## Contributing to the docs

1. **Do not hand-edit** `docs/HLD.md`, `docs/LLD/*.md`, `README.md` or `AGENT.md` — run the
   CDR documentation agent so `last_synced_commit` and the indexes stay truthful.
2. **Do not edit** `docs/specs/*` unless the source PDF changed. They are transcriptions.
3. A change to behaviour and a change to its document belong in the **same** pull request.
4. New design decisions get a `D-nn` tag in [HLD Appendix A](./docs/HLD.md#appendix-a--design-decision-register)
   and a line in `.cdr/index/decision.jsonl`.
