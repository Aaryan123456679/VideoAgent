# Module documents

← [Back to README](../README.md)

| Module | Responsibility | v1 |
| --- | --- | --- |
| [`api`](./LLD/api.md) | FastAPI async surface, job lifecycle, idempotency keys, error envelope | E1 |
| [`harness`](./LLD/harness.md) | Loop engine, context and tool ownership, budget caps, termination | E0–E1 *(caps E4)* |
| [`gateway`](./LLD/gateway.md) | LiteLLM proxy as single LLM egress, alias resolution, retry/fallback/circuit break | E0 |
| [`graph`](./LLD/graph.md) | LangGraph `StateGraph`, checkpoint after every node, resume semantics | E1–E2 *(resume E3)* |
| [`planning`](./LLD/planning.md) | `StoryPlan` (4 beats, exactly 40s) and the locked, immutable `ContinuityBible` | E1 |
| [`providers`](./LLD/providers.md) | Video provider abstraction, capability negotiation, Magic Hour adapter `[D-58]`, frame chaining | E2 |
| [`qc`](./LLD/qc.md) | Vision scoring against the bible, 0.75 threshold, repair capped at 2 attempts, calibration | **E3 — deferred** |
| [`assembly`](./LLD/assembly.md) | ffmpeg stitch and normalise, music bed, thumbnail, partial assembly | E2 *(partial E3)* |
| [`persistence`](./LLD/persistence.md) | PostgreSQL 16 with RLS per tenant, Redis 7, artifact storage, presigned URLs | E0 |
| [`observability`](./LLD/observability.md) | Langfuse traces/spans/generations/scores, JSON logs, redaction, error taxonomy | **E4 — deferred** |
