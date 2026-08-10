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
narrative and visual continuity. Text-to-video models generate good clips in isolation, but
four clips from four prompts drift: the protagonist's face changes, the room's color changes,
the story never moves. This is the continuity engine around the generator — a planned 4-beat
arc, a locked continuity bible, frame chaining between shots, and a vision-model QC loop that
repairs only the shot that broke.

> **Status: design complete, build scoped to E0–E2** — foundation, job lifecycle, planning,
> continuity bible, the Magic Hour adapter, frame chaining, assembly, delivery. **E3 (QC loop,
> resume) and E4 (observability, cost caps, chaos)** are deferred, not cancelled. Every LLD
> states its own `implementation_status`. Scope table: [HLD §12](./docs/HLD.md#12-delivery-milestones).

> **Provider note:** the PRD specifies **Higgsfield MCP**, which has no free/trial API tier —
> no credential was obtainable. v1 uses **[Magic Hour](https://magichour.ai)** instead
> (decision `D-58`), a config-only swap (one adapter module + `.env`) enabled by the provider
> abstraction `[D-06]`. Disclosed deviations: no seed → no bit-exact replay (`D-59`),
> credit-based billing (`D-60`), pinned `wan-2.2` model for 10s clips (`D-61`), 720p target /
> 1080p ceiling (`D-63`) — full list in [HLD Appendix A](./docs/HLD.md#appendix-a--design-decision-register).
> `docs/specs/*` still say "Higgsfield" on purpose — they're verbatim source transcriptions.

## Watch the pitch

Recorded demo — a real job run end to end against the live Magic Hour API:
[watch on Google Drive](https://drive.google.com/file/d/10H5C37x4SgAPLIEbYK9ATuuRK94uRzh-/view?usp=sharing).
Walkthrough script: [`PITCH_SCRIPT.md`](./PITCH_SCRIPT.md).

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

**Job pipeline** — the LangGraph state machine every job runs through. Every arrow into a node
is guarded by the harness, which can terminate the job from any step (omitted for readability —
see [`docs/LLD/graph.md`](./docs/LLD/graph.md) §3).

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

Every node checkpoints in the **same database transaction** as its own domain write — a crash
resumes from there, not from the top (v1's simplified crash recovery re-enters at the graph's
entry point rather than the last checkpoint; see [`graph/worker.py`](./src/video_agent/graph/worker.py)).

## What's implemented

| Feature | What it does | Code |
| --- | --- | --- |
| Four-beat story planning | One LLM pass produces a 4-beat arc summing to exactly 40s | [`planning/service.py::plan_story`](./src/video_agent/planning/service.py) |
| Locked continuity bible | Character, wardrobe, location, lighting, palette, camera language — immutable for the job's life, enforced by a DB trigger | [`planning/service.py::lock_bible`](./src/video_agent/planning/service.py), [`planning/bible.py`](./src/video_agent/planning/bible.py) |
| Frame chaining | The last frame of shot *n* conditions shot *n+1*, so identity carries forward | [`graph/nodes.py::_resolve_conditioning`](./src/video_agent/graph/nodes.py), [`graph/frame_extraction.py`](./src/video_agent/graph/frame_extraction.py) |
| Capability negotiation + provider abstraction | A shot's requirements are matched against provider capabilities; a config change swaps providers with zero code diff | [`providers/negotiate.py`](./src/video_agent/providers/negotiate.py), [`providers/registry.py`](./src/video_agent/providers/registry.py) |
| Real video generation adapter | Magic Hour REST adapter — submit, poll, upload conditioning frames, download, full HTTP-error mapping | [`providers/magichour.py`](./src/video_agent/providers/magichour.py) |
| Multi-key rotation on insufficient credits | On a real `402`, rotates to a second configured account and retries; single-key deployments see no change | [`providers/magichour.py::RotatingApiKey`](./src/video_agent/providers/magichour.py) |
| Inbound webhook acceleration | A provider's webhook triggers an early re-poll instead of waiting for the next tick; payload is never trusted for status | [`providers/magichour.py::handle_webhook`](./src/video_agent/providers/magichour.py), [`api/webhooks.py`](./src/video_agent/api/webhooks.py) |
| Mock video provider | Real ffmpeg-rendered MP4s, zero network/cost/wait, for instant local trial runs | [`providers/mock.py`](./src/video_agent/providers/mock.py) |
| Idempotent job lifecycle | Every work-creating `POST` is idempotency-keyed; a retry replays, never double-creates or double-bills | [`api/idempotency.py`](./src/video_agent/api/idempotency.py) |
| Redelivery-safe graph nodes | Every node is safe to execute twice under at-least-once queue delivery | [`graph/nodes.py`](./src/video_agent/graph/nodes.py) |
| Manual repair-signal override | Exercises the real repair mechanism (back-edge, cap, continuity) without pretending QC scoring exists (that's still E3) | [`api/jobs.py::force_repair_shot`](./src/video_agent/api/jobs.py), [`graph/nodes.py::qc_shot_node`](./src/video_agent/graph/nodes.py) |
| Row-level tenant isolation | Postgres RLS on every table but two documented exemptions, enforced even against the table owner, audited by a static check | [`persistence/rls.py`](./src/video_agent/persistence/rls.py) |
| At-least-once job queue, crash recovery | Redis Streams consumer group; a stalled job is reclaimed via `XAUTOCLAIM` | [`persistence/queue.py`](./src/video_agent/persistence/queue.py), [`graph/worker.py`](./src/video_agent/graph/worker.py) |
| One-writer-per-job lock | Fencing-token Redis lock; a second worker on the same job declines rather than races | [`graph/lock.py`](./src/video_agent/graph/lock.py) |
| Agent harness — six-rule termination | Every step is evaluated against evaluator-satisfied, cancellation, fatal error, budget, no-progress, and default-continue, in that priority order | [`harness/decide.py`](./src/video_agent/harness/decide.py) |
| Hard budget caps | Iterations, wall-clock, tokens, USD — pre-flight veto and post-hoc breach detection | [`harness/budget.py`](./src/video_agent/harness/budget.py) |
| Failure-signature no-progress detection | The same failure twice at job scope stops the job; at shot scope, abandons just that shot | [`harness/signatures.py`](./src/video_agent/harness/signatures.py) |
| LLM gateway — single egress | Alias-based model routing (code never names a model), retry+backoff+jitter, per-dependency circuit breaker, response caching | [`gateway/gateway.py`](./src/video_agent/gateway/gateway.py), [`gateway/breaker.py`](./src/video_agent/gateway/breaker.py) |
| ffmpeg assembly pipeline | Per-clip normalize, stream-copy concat, thumbnail extraction, pinned-version startup assertion | [`assembly/media_toolchain.py`](./src/video_agent/assembly/media_toolchain.py) |
| Presigned, never-persisted delivery | Every artifact URL is minted fresh per request and never stored, cached, or logged | [`persistence/presign.py`](./src/video_agent/persistence/presign.py) |
| Structured logging + redaction tripwire | JSON logs with a propagated trace id; a runtime scanner refuses credentials, presigned URLs, and raw media bytes onto any log line | [`observability/logging.py`](./src/video_agent/observability/logging.py), [`observability/redaction.py`](./src/video_agent/observability/redaction.py) |
| Static leak/lint guards | Repo-wide checks: no provider name outside its adapter, no `print`, no hardcoded-secret-shaped names | [`tests/static_guards.py`](./tests/static_guards.py) |
| Trial UI + no-auth dev harness | A React front end plus a dev API server/worker pair for a full local end-to-end run in minutes | [`ui/`](./ui/), [`scripts/dev_server.py`](./scripts/dev_server.py), [`scripts/dev_worker.py`](./scripts/dev_worker.py) |

**Deferred, not missing by accident:** the QC vision-scoring/repair loop
([`docs/LLD/qc.md`](./docs/LLD/qc.md), E3) and Langfuse tracing
([`docs/LLD/observability.md`](./docs/LLD/observability.md), E4). Both are fully designed;
neither is wired into the running graph yet. See [Module status](#module-status) for what's real
vs. stubbed.

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

Precedence: **PDF → spec → HLD → LLD** — a lower document may add detail but never contradict a
higher one. Every decision not settled by a document above it carries a `D-nn` tag in
[HLD Appendix A](./docs/HLD.md#appendix-a--design-decision-register).

## Try it

**Stack:** Python 3.12 (FastAPI, async), PostgreSQL 16, Redis 7, an S3-compatible store,
LiteLLM proxy, ffmpeg. Config lives in `.env` (see [`.env.example`](./.env.example)) — model
aliases and provider keys are never hardcoded in code.

### Mock provider — no account needed

Full pipeline (real API, Postgres/Redis/S3, real graph), shots rendered by
[`MockVideoProvider`](./src/video_agent/providers/mock.py) via ffmpeg — no network, no cost, no
auth required.

```bash
make compose-up                              # Postgres, Redis, MinIO, LiteLLM
uv run python scripts/dev_server.py          # terminal 1 — the API
uv run python scripts/dev_worker.py          # terminal 2 — worker, mock shots
cd ui && npm install && npm run dev          # terminal 3 — http://localhost:5173
```

Open the UI, enter a prompt, click **Create video**, and watch `current_node`/`budget` update
live. Headless one-shot: `uv run python scripts/mock_trial_run.py "your prompt here"`.

### Real Magic Hour run

Same API, graph, and UI — only the provider differs, and this spends real credits.

1. Get a key at [magichour.ai](https://magichour.ai/settings/developer) (`mhk_live_...`). One
   job renders 4 shots at ~240 credits each on the pinned model (`ltx-2.3`) — budget **at least
   ~1,000 credits**. A second key rotates in automatically on `402` (insufficient credits) only.
2. Fill in `.env`:
   ```bash
   MAGICHOUR_API_KEY=mhk_live_...
   MAGICHOUR_API_KEY_2=mhk_live_...     # optional — second account, rotated onto on 402 only
   MAGICHOUR_MODEL=ltx-2.3
   MAGICHOUR_USD_PER_1K_CREDITS=0.90    # match your account's billing tier
   ```
   `.env` is git-ignored and must never be committed.
3. Same four terminals as above, but use `scripts/real_dev_worker.py` in place of
   `dev_worker.py` (the latter is hardcoded to `MockVideoProvider`).

Gotchas: `get_settings()` is `@lru_cache`d — restart the server/worker after editing `.env`.
Use `http://localhost:5173`, not `127.0.0.1` (the Vite dev server binds IPv6 loopback). A real
4-shot job takes anywhere from ~6 to 30+ minutes depending on Magic Hour's queue depth.

Terminal equivalent of the UI's "Create video":

```bash
curl -s -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Authorization: Bearer dev-no-auth-placeholder-token-000000" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"prompt": "a violinist plays on a rooftop at dawn as the city wakes below"}'
```

## Module status

What each module's LLD promises versus what's actually running. Full function-level detail
lives in the LLDs themselves — this is the summary.

| Module | Status | Notable |
| --- | --- | --- |
| [`harness`](./docs/LLD/harness.md) | Built, ahead of schedule | Budget ledger and no-progress detection are fully wired even though the doc marks them E4 |
| [`graph`](./docs/LLD/graph.md) | E1–E2 built | `qc_shot_node` is a genuine stub (always accepts); crash recovery restarts at the graph's entry point, not the last checkpoint (E3) |
| [`planning`](./docs/LLD/planning.md) | Built, matches doc | 4-beat arc and immutable continuity bible, function-for-function |
| [`qc`](./docs/LLD/qc.md) | Deferred (E3), by design | Only a `Dimension`/`QCFinding` stub exists; no vision-model call anywhere; scoring dimensions differ from the doc's list |
| [`providers`](./docs/LLD/providers.md) | Built | Negotiation, failover, the Magic Hour adapter (`D-58`), key rotation on `402`, inbound webhooks. `MockVideoProvider` is a trial addition, not in the spec |
| [`gateway`](./docs/LLD/gateway.md) | E0–E2 built | Alias routing, retry/circuit-break, caching, cost accounting; one added fix for providers that reject Pydantic's `const` keyword |
| [`persistence`](./docs/LLD/persistence.md) | Built (E0) | RLS on every table but two (documented), at-least-once queue, presigned URLs never stored |
| [`api`](./docs/LLD/api.md) | E1 built | `resume` / `shots/{i}/regenerate` deferred to E3; the progress stream and artifacts response shape diverge slightly from the doc; inbound webhooks route added beyond spec |
| [`assembly`](./docs/LLD/assembly.md) | Partial | ffmpeg primitives (normalize/concat/thumbnail) built; partial-assembly orchestration lives in `graph/nodes.py`, not this package |
| [`observability`](./docs/LLD/observability.md) | E0 built | Structured logging, redaction tripwire, and error-code taxonomy; Langfuse tracing (E4) isn't wired anywhere |
| `scripts/`, `ui/` | Trial harness | Mock-provider dev server/worker plus a React UI for local end-to-end runs; not part of any LLD spec |

## CDR workflow

Canonical docs are maintained by CDR agents; their state lives in `.cdr/` (`runs/`, `index/`,
`memory/`, `schemas/`). Two entry points: **`init`** bootstraps HLD/LLDs/README from a repo
scan; **`sync`** regenerates only drifted sections after code changes, using
`.cdr/index/file.jsonl`.
