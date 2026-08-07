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

## Contributing to the docs

1. **Do not hand-edit** `docs/HLD.md`, `docs/LLD/*.md`, `README.md` or `AGENT.md` — run the
   CDR documentation agent so `last_synced_commit` and the indexes stay truthful.
2. **Do not edit** `docs/specs/*` unless the source PDF changed. They are transcriptions.
3. A change to behaviour and a change to its document belong in the **same** pull request.
4. New design decisions get a `D-nn` tag in [HLD Appendix A](./docs/HLD.md#appendix-a--design-decision-register)
   and a line in `.cdr/index/decision.jsonl`.
