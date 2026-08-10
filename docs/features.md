# What's implemented

← [Back to README](../README.md)

| Feature | What it does | Code |
| --- | --- | --- |
| Four-beat story planning | One LLM pass produces a 4-beat arc summing to exactly 40s | [`planning/service.py::plan_story`](../src/video_agent/planning/service.py) |
| Locked continuity bible | Character, wardrobe, location, lighting, palette, camera language — immutable for the job's life, enforced by a DB trigger | [`planning/service.py::lock_bible`](../src/video_agent/planning/service.py), [`planning/bible.py`](../src/video_agent/planning/bible.py) |
| Frame chaining | The last frame of shot *n* conditions shot *n+1*, so identity carries forward | [`graph/nodes.py::_resolve_conditioning`](../src/video_agent/graph/nodes.py), [`graph/frame_extraction.py`](../src/video_agent/graph/frame_extraction.py) |
| Capability negotiation + provider abstraction | A shot's requirements are matched against provider capabilities; a config change swaps providers with zero code diff | [`providers/negotiate.py`](../src/video_agent/providers/negotiate.py), [`providers/registry.py`](../src/video_agent/providers/registry.py) |
| Real video generation adapter | Magic Hour REST adapter — submit, poll, upload conditioning frames, download, full HTTP-error mapping | [`providers/magichour.py`](../src/video_agent/providers/magichour.py) |
| Multi-key rotation on insufficient credits | On a real `402`, rotates to a second configured account and retries; single-key deployments see no change | [`providers/magichour.py::RotatingApiKey`](../src/video_agent/providers/magichour.py) |
| Inbound webhook acceleration | A provider's webhook triggers an early re-poll instead of waiting for the next tick; payload is never trusted for status | [`providers/magichour.py::handle_webhook`](../src/video_agent/providers/magichour.py), [`api/webhooks.py`](../src/video_agent/api/webhooks.py) |
| Mock video provider | Real ffmpeg-rendered MP4s, zero network/cost/wait, for instant local trial runs | [`providers/mock.py`](../src/video_agent/providers/mock.py) |
| Idempotent job lifecycle | Every work-creating `POST` is idempotency-keyed; a retry replays, never double-creates or double-bills | [`api/idempotency.py`](../src/video_agent/api/idempotency.py) |
| Redelivery-safe graph nodes | Every node is safe to execute twice under at-least-once queue delivery | [`graph/nodes.py`](../src/video_agent/graph/nodes.py) |
| Manual repair-signal override | Exercises the real repair mechanism (back-edge, cap, continuity) without pretending QC scoring exists (that's still E3) | [`api/jobs.py::force_repair_shot`](../src/video_agent/api/jobs.py), [`graph/nodes.py::qc_shot_node`](../src/video_agent/graph/nodes.py) |
| Row-level tenant isolation | Postgres RLS on every table but two documented exemptions, enforced even against the table owner, audited by a static check | [`persistence/rls.py`](../src/video_agent/persistence/rls.py) |
| At-least-once job queue, crash recovery | Redis Streams consumer group; a stalled job is reclaimed via `XAUTOCLAIM` | [`persistence/queue.py`](../src/video_agent/persistence/queue.py), [`graph/worker.py`](../src/video_agent/graph/worker.py) |
| One-writer-per-job lock | Fencing-token Redis lock; a second worker on the same job declines rather than races | [`graph/lock.py`](../src/video_agent/graph/lock.py) |
| Agent harness — six-rule termination | Every step is evaluated against evaluator-satisfied, cancellation, fatal error, budget, no-progress, and default-continue, in that priority order | [`harness/decide.py`](../src/video_agent/harness/decide.py) |
| Hard budget caps | Iterations, wall-clock, tokens, USD — pre-flight veto and post-hoc breach detection | [`harness/budget.py`](../src/video_agent/harness/budget.py) |
| Failure-signature no-progress detection | The same failure twice at job scope stops the job; at shot scope, abandons just that shot | [`harness/signatures.py`](../src/video_agent/harness/signatures.py) |
| LLM gateway — single egress | Alias-based model routing (code never names a model), retry+backoff+jitter, per-dependency circuit breaker, response caching | [`gateway/gateway.py`](../src/video_agent/gateway/gateway.py), [`gateway/breaker.py`](../src/video_agent/gateway/breaker.py) |
| ffmpeg assembly pipeline | Per-clip normalize, stream-copy concat, thumbnail extraction, pinned-version startup assertion | [`assembly/media_toolchain.py`](../src/video_agent/assembly/media_toolchain.py) |
| Presigned, never-persisted delivery | Every artifact URL is minted fresh per request and never stored, cached, or logged | [`persistence/presign.py`](../src/video_agent/persistence/presign.py) |
| Structured logging + redaction tripwire | JSON logs with a propagated trace id; a runtime scanner refuses credentials, presigned URLs, and raw media bytes onto any log line | [`observability/logging.py`](../src/video_agent/observability/logging.py), [`observability/redaction.py`](../src/video_agent/observability/redaction.py) |
| Static leak/lint guards | Repo-wide checks: no provider name outside its adapter, no `print`, no hardcoded-secret-shaped names | [`tests/static_guards.py`](../tests/static_guards.py) |
| Trial UI + no-auth dev harness | A React front end plus a dev API server/worker pair for a full local end-to-end run in minutes | [`ui/`](../ui/), [`scripts/dev_server.py`](../scripts/dev_server.py), [`scripts/dev_worker.py`](../scripts/dev_worker.py) |

**Deferred, not missing by accident:** the QC vision-scoring/repair loop
([`docs/LLD/qc.md`](./LLD/qc.md), E3) and Langfuse tracing
([`docs/LLD/observability.md`](./LLD/observability.md), E4). Both are fully designed;
neither is wired into the running graph yet. See [module status](./module-status.md) for what's
real vs. stubbed.
