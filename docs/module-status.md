# Module status

← [Back to README](../README.md)

What each module's LLD promises versus what's actually running. Full function-level detail
lives in the LLDs themselves — this is the summary.

| Module | Status | Notable |
| --- | --- | --- |
| [`harness`](./LLD/harness.md) | Built, ahead of schedule | Budget ledger and no-progress detection are fully wired even though the doc marks them E4 |
| [`graph`](./LLD/graph.md) | E1–E2 built | `qc_shot_node` is a genuine stub (always accepts); crash recovery restarts at the graph's entry point, not the last checkpoint (E3) |
| [`planning`](./LLD/planning.md) | Built, matches doc | 4-beat arc and immutable continuity bible, function-for-function |
| [`qc`](./LLD/qc.md) | Deferred (E3), by design | Only a `Dimension`/`QCFinding` stub exists; no vision-model call anywhere; scoring dimensions differ from the doc's list |
| [`providers`](./LLD/providers.md) | Built | Negotiation, failover, the Magic Hour adapter (`D-58`), key rotation on `402`, inbound webhooks. `MockVideoProvider` is a trial addition, not in the spec |
| [`gateway`](./LLD/gateway.md) | E0–E2 built | Alias routing, retry/circuit-break, caching, cost accounting; one added fix for providers that reject Pydantic's `const` keyword |
| [`persistence`](./LLD/persistence.md) | Built (E0) | RLS on every table but two (documented), at-least-once queue, presigned URLs never stored |
| [`api`](./LLD/api.md) | E1 built | `resume` / `shots/{i}/regenerate` deferred to E3; the progress stream and artifacts response shape diverge slightly from the doc; inbound webhooks route added beyond spec |
| [`assembly`](./LLD/assembly.md) | Partial | ffmpeg primitives (normalize/concat/thumbnail) built; partial-assembly orchestration lives in `graph/nodes.py`, not this package |
| [`observability`](./LLD/observability.md) | E0 built | Structured logging, redaction tripwire, and error-code taxonomy; Langfuse tracing (E4) isn't wired anywhere |
| `scripts/`, `ui/` | Trial harness | Mock-provider dev server/worker plus a React UI for local end-to-end runs; not part of any LLD spec |
