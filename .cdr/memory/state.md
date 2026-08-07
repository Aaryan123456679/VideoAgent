# State

- **Phase:** documentation complete, implementation not started.
- **Repo:** greenfield. No application source, no `pyproject.toml`, no tests.
- **Canonical docs:** `docs/HLD.md`, `docs/LLD/{api,harness,gateway,graph,planning,providers,qc,assembly,persistence,observability}.md`, `README.md`, `AGENT.md` — all stamped `last_synced_commit: 438138573aa69c26727651b368e056262908bc69`.
- **Source of truth:** `docs/specs/common-platform-spec.md` (governs where the PRD is silent) and `docs/specs/video-agent-prd.md`. Both verified faithful to their origin PDFs on 2026-08-08.
- **Module set is fixed** at the ten LLDs above. Downstream planning must align to it.
- **Indexes seeded:** `feature.jsonl` (68), `file.jsonl` (15), `decision.jsonl` (57). `task.jsonl` and `regression.jsonl` still empty — owned by planning/verification.
- **Video provider is Magic Hour, not Higgsfield** (`D-58`). Higgsfield had no free/trial API tier and no credential was obtainable. `docs/specs/*` still says Higgsfield on purpose — it is a verbatim PDF transcription. Config lives in `.env.example` (`MAGICHOUR_*`).
- **Known PRD deviation:** no seed control, so "every job is reproducible" delivers traceability but not bit-exact re-rendering (`D-59`). Also credits-not-USD billing (`D-60`), model pinned `wan-2.2` for 10s (`D-61`), 720p target (`D-63`).
- **Build scope is E0 + E1 + E2** (foundation, job lifecycle, planning, bible, Magic Hour adapter, frame chaining, assembly, delivery). **E3** (QC loop, partial results, resume) and **E4** (observability, cost caps, load+chaos) are **deferred, not cancelled**. Every LLD carries `implementation_status`; `qc.md` and `observability.md` are wholly deferred and must be read as specs, not as running code.
- **Now configuration, not literals:** `QC_ACCEPT_THRESHOLD` (`D-71`, was a `Final` constant), `MAGICHOUR_USD_PER_1K_CREDITS` (`D-65`), `tenant.max_usd_per_job` (`D-70`).
- **Settled infrastructure:** Redis Streams job queue, at-least-once (`D-67`); static per-tenant API keys, Argon2id (`D-68`); `tenant` table exists and is RLS-exempt along with `tenant_api_key` (`D-70`); prompts authored in-repo under `prompts/` (`D-72`).
- **Next:** product confirmation on A1/A2/A4 (see run 001's `drift-report.json`) and on the `D-59` reproducibility deviation, then implementation planning against the M1–M5 milestone/module map in `docs/HLD.md#12-delivery-milestones`.
