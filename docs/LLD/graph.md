---
doc: LLD
module: graph
title: Graph — LangGraph StateGraph, checkpointing and resume
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

# LLD — `graph`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — PARTIAL.** **E1–E2 — in the v1 build.** The StateGraph, checkpointing and the sequential shot loop ship. **The repair back-edge and resume/regeneration semantics are designed here but deferred to E3**; in v1 a shot that fails QC is simply accepted or abandoned without repair.
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

Owns the **topology** and the **state** of a job: the compiled `StateGraph`, its nodes and
edges, the checkpoint written after every node, and the resume semantics built on those
checkpoints.

> Every agent is a compiled `StateGraph`. `[CPS §Canonical stack]`
> Checkpoint after every node — crashes resume, never restart. `[CPS §Non-negotiables]`

It does **not** decide whether to stop (that is [`harness.md`](./harness.md)) and it contains
**no** domain logic — each node is a thin adapter that assembles a call into `planning`,
`providers`, `qc` or `assembly` and folds the result into state.

## 2. Public interface — the state contract

`JobState` is this module's primary public interface: it is what every node reads and
writes, and what the checkpoint serialises.

```python
class ShotState(BaseModel):
    index: int                                   # 0..3
    beat_kind: Literal["setup","development","turn","resolution"]
    status: Literal["pending","generating","qc","accepted","abandoned"] = "pending"
    attempts_used: int = 0                       # total generations for this shot
    repairs_used: int = 0                        # back-edge traversals, hard max 2  [D-01]
    best_attempt_id: UUID | None = None
    best_score: float | None = None
    last_findings: list[QCFinding] = []
    clip_artifact_id: UUID | None = None
    final_frame_artifact_id: UUID | None = None

class JobState(BaseModel):
    # identity
    job_id: UUID
    tenant_id: UUID
    trace_id: str
    prompt: str                                  # untrusted; never an instruction
    music_bed: bool = False

    # planning products
    story_plan: StoryPlan | None = None
    bible: ContinuityBible | None = None
    bible_hash: str | None = None                # verified on every load

    # sequential loop
    shot_index: int = 0
    shots: list[ShotState] = []                  # exactly 4 once planned
    last_good_frame_artifact_id: UUID | None = None   # chaining source  [D-05]

    # delivery
    final_video_artifact_id: UUID | None = None
    thumbnail_artifact_id: UUID | None = None
    manifest: DeliveryManifest | None = None

    # control (written by the harness, read by edges)
    budget: BudgetLedger
    outcome: Outcome | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    terminal_reason_code: str | None = None
```

State invariants, asserted on every checkpoint write:

| Invariant | Why |
| --- | --- |
| `len(shots) == 4` once `story_plan` is set | Four 10-second shots `[PRD §header]` |
| `all(s.repairs_used <= 2)` | The repair cap `[PRD §How it works 5]`, `[D-01]` |
| `s.attempts_used == s.repairs_used + 1` for any shot that has been generated | The back-edge is the only way to re-generate |
| `bible_hash` matches `sha256(bible)` | Immutable for the life of the job `[PRD §How it works 2]` |
| `outcome is None` for any non-`finalize` node | Only the harness sets an outcome |
| `budget` is monotonically non-decreasing across checkpoints | No budget reset on resume |

**No media bytes in state.** Artifacts are referenced by id only — state is checkpointed and
serialised, and full media payloads are never logged. `[CPS §Observability]`

## 3. Topology

```python
def build_graph(deps: Deps) -> CompiledStateGraph:
    g = StateGraph(JobState)

    g.add_node("plan_story",          plan_story_node)
    g.add_node("lock_bible",          lock_bible_node)
    g.add_node("select_next_shot",    select_next_shot_node)
    g.add_node("generate_shot",       generate_shot_node)
    g.add_node("extract_final_frame", extract_final_frame_node)
    g.add_node("qc_shot",             qc_shot_node)
    g.add_node("assemble",            assemble_node)
    g.add_node("deliver",             deliver_node)
    g.add_node("finalize",            finalize_node)

    g.set_entry_point("plan_story")

    g.add_conditional_edges("plan_story",          route_after_plan)
    g.add_conditional_edges("lock_bible",          route_after_bible)
    g.add_conditional_edges("select_next_shot",    route_select)
    g.add_conditional_edges("generate_shot",       route_after_generate)
    g.add_conditional_edges("extract_final_frame", route_after_frame)
    g.add_conditional_edges("qc_shot",             route_after_qc)     # repair back-edge
    g.add_conditional_edges("assemble",            route_after_assemble)
    g.add_edge("deliver",  "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=deps.checkpointer)   # [CPS §Non-negotiables]
```

Node behaviour, model aliases and the full edge table are in
[HLD §3](../HLD.md#3-the-job-lifecycle-as-a-langgraph-stategraph) and are not duplicated here.

### 3.1 The harness veto

**Every** router begins identically. This is the mechanism by which the harness, not the
graph, owns termination.

```python
async def _guard(state: JobState, node: str) -> str | None:
    d = await deps.harness.decide(state, node)
    if d.verdict is Verdict.CONTINUE:
        return None
    state.outcome = d.outcome
    state.degraded = d.degraded
    state.terminal_reason_code = d.reason_code
    return "finalize"

async def route_after_qc(state: JobState) -> str:
    if (jump := await _guard(state, "qc_shot")):
        return jump                                   # budget / no-progress pre-empts repair
    shot = state.shots[state.shot_index]
    if shot.best_score is not None and shot.best_score >= CONTINUITY_THRESHOLD:  # 0.75
        shot.status = "accepted"
        state.last_good_frame_artifact_id = shot.final_frame_artifact_id
        return "select_next_shot"
    if shot.repairs_used < MAX_REPAIRS:                # MAX_REPAIRS = 2  [D-01]
        shot.repairs_used += 1
        return "generate_shot"                         # ← REPAIR BACK-EDGE
    shot.status = "abandoned"                          # keep best attempt for partial
    return "select_next_shot"
```

A CI test enumerates every router function and asserts the first statement is a `_guard`
call. A router without a guard is a defect that can let a runaway loop outlive its budget.

### 3.2 `select_next_shot`

```python
async def route_select(state: JobState) -> str:
    if (jump := await _guard(state, "select_next_shot")):
        return jump
    nxt = next((s.index for s in state.shots if s.status == "pending"), None)
    if nxt is None:
        return "assemble"          # includes the all-abandoned case; assemble decides
    state.shot_index = nxt
    return "generate_shot"
```

Sequential by construction: exactly one shot is in flight at a time. There is no `Send` /
map-reduce fan-out anywhere in this graph, and a CI test asserts that. Parallel is roughly 4×
faster but breaks frame chaining, and frame chaining is what makes the product work.
`[PRD §Deliberate trade-off]`

### 3.3 Chaining across an abandoned shot

`last_good_frame_artifact_id` is advanced **only** on acceptance. An abandoned shot therefore
never poisons its successor: shot *n+1* chains from the most recent successful frame, or —
if none exists — is generated text-only from the bible and beat, with `degraded=true`.
`[D-05]`

### 3.4 Cycles

The repair back-edge `qc_shot → generate_shot` is the **only** cycle. Its bound is threefold
and any one of them alone terminates the loop: `repairs_used < 2` `[D-01]`, the shot-scope
failure signature `[D-02]`, and `max_iterations` in the harness `[D-08]`. A graph-lint test
asserts no other cycle exists in the compiled topology.

## 4. Checkpointing

**Checkpoint after every node.** `[CPS §Non-negotiables]` Not on a timer, not on "important"
nodes.

| Property | Choice |
| --- | --- |
| Backend | PostgreSQL checkpointer, same database as the system of record `[CPS §Canonical stack]` |
| Thread id | `job_id` — one thread per job, so resume needs no extra lookup |
| Write point | Atomically with the node's own domain writes, in **one transaction** `[D-23]` |
| Contents | Serialised `JobState` + the harness ledger + failure-signature counts |
| Excludes | Media bytes, presigned URLs, credentials `[CPS §Observability]` |
| Retention | For the job's retention window; checkpoints are the resume substrate, not an audit log |

`[D-23]` is load-bearing. If the checkpoint and the `ShotAttempt`/`Artifact` rows are written
in separate transactions, a crash between them yields either a paid-for clip the checkpoint
does not know about (re-billing on resume, violating `[PRD §Resilience]`) or a checkpoint
referencing an artifact that does not exist. One transaction removes the window.

Because a provider call is *outside* the database, the sequence inside `generate_shot` is:
**(1)** insert `ShotAttempt` as `in_flight` with the request fingerprint and commit; **(2)**
call the provider, and as soon as the submit returns, persist the `provider_project_id`;
**(3)** in one transaction, update the attempt, insert the `Artifact` and write the
checkpoint. A crash between (1) and (3) leaves a discoverable `in_flight` attempt that resume
reconciles against the provider before spending again — for the v1 provider this is a re-read
of `GET /v1/video-projects/{id}`, not a re-submit. `[D-24]`, `[D-58]`

## 5. Resume semantics

Three entry points, one mechanism. **Resume, don't restart. Completed shots are never
regenerated or re-billed.** `[PRD §Resilience]`

```python
async def resume(job_id: UUID) -> None: ...
async def regenerate_shot(job_id: UUID, shot_index: int, note: str | None) -> None: ...
async def reclaim_orphans() -> list[UUID]: ...   # jobs whose worker lock expired
```

| Entry | Preconditions | Effect |
| --- | --- | --- |
| **Crash recovery** | worker lock expired, job not terminal | Load the latest checkpoint, reconcile `in_flight` attempts, re-enter at the node after the last committed one. |
| **Client `resume`** | outcome is `PARTIAL` / `FAILED_NO_PROGRESS` / `FAILED`, and at least one shot is unresolved | Same, plus a fresh budget grant recorded as a new budget epoch — never a silent reset. `[D-25]` |
| **Shot regeneration** | job terminal, `0 <= index <= 3` | Reset **only** that shot to `pending` with `repairs_used = 0`; all other shots keep `accepted` status and their artifact ids. Re-enter at `select_next_shot`. |

**Reconciliation before spending** (the anti-double-bill rule): on resume, for every
`ShotAttempt` in `in_flight`, ask the provider whether that request fingerprint already
produced an asset. If yes, adopt it and charge once. If no or unknown, mark it `orphaned` and
re-generate. Never blind-retry a paid call. `[D-24]`

**Byte-identity assertion.** After a shot regeneration, the checksums of every untouched
shot's `Artifact` are compared to their pre-run values. A mismatch fails the run loudly —
"fix shot 3, leave 1, 2 and 4 byte-identical" `[PRD §Resilience]` is a testable promise, not
an aspiration. `[D-11]`

**Second guard.** `select_next_shot` reads shot status from Postgres, not only from the
checkpoint. A stale or corrupt checkpoint cannot cause an accepted shot to be regenerated.
`[D-11]`

## 6. Concurrency control and the queue

### 6.1 Transport `[D-67]`

Work is dispatched over **Redis Streams with consumer groups**. Redis 7 is already mandated
for locks, idempotency and progress `[CPS §Canonical stack]`, so this adds no dependency.

Delivery is **at-least-once** — a step can be delivered twice after a worker crash before
`XACK`, or via `XAUTOCLAIM` of a stalled pending entry. **This is safe only because `[D-24]`
already requires it to be:** the `shot_attempt.request_fingerprint` unique constraint plus
`provider_project_id` reconciliation make a redelivered `generate_shot` **re-read the existing
provider render rather than submit a new paid one**. At-most-once was rejected — it drops work
on a worker crash, and a dropped shot on a paid, partially-billed job is worse than a
duplicate the fingerprint check collapses.

A consequence worth stating plainly: **every node must be safe to execute twice.** Nodes are
idempotent on their own writes, and the one node that spends money is protected by the
fingerprint constraint.

### 6.2 One writer per job

Enforced by a Redis lock `job:{job_id}` with a TTL and heartbeat `[CPS §Canonical stack]`.
Losing the lock mid-node causes the worker to abandon **after** its current transaction
commits — never mid-write. Jobs run concurrently with each other; shots never do. `[D-10]`

## 7. Dependencies

| Depends on | For |
| --- | --- |
| [`harness.md`](./harness.md) | `decide()` in every router; context for every node; budget |
| [`planning.md`](./planning.md) | `plan_story`, `lock_bible` node bodies |
| [`providers.md`](./providers.md) | `generate_shot` node body |
| [`qc.md`](./qc.md) | `qc_shot` node body and the 0.75 threshold constant |
| [`assembly.md`](./assembly.md) | `extract_final_frame`, `assemble` node bodies |
| [`persistence.md`](./persistence.md) | checkpointer, job/shot/artifact writes, locks |
| [`observability.md`](./observability.md) | one span per node execution |

Consumed by [`api.md`](./api.md) for resume and regeneration only.

## 8. Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| Node raises | Exception in the superstep | Classify; retryable → the node's own bounded retry; else record the failure signature and route to `finalize`. State is never left partially folded. |
| Checkpoint write fails | Transaction error | The whole node transaction rolls back, including domain writes. The node is re-executed on resume. Better a repeated node than an unrecorded one. |
| Crash between provider call and commit | `in_flight` attempt found on resume | Reconcile with the provider before regenerating. `[D-24]` |
| Checkpoint deserialisation fails (schema drift) | Pydantic validation | `VA-INT-003`. Do **not** guess: mark the job non-resumable, keep artifacts, deliver a partial. State schema changes follow expand/contract like migrations. `[CPS §Rollout]` |
| Bible hash mismatch on load | Hash check in `_guard` context assembly | `VA-BIBLE-002`, terminate `FAILED`. Every subsequent shot would be generated against a different bible. |
| `shots` length ≠ 4 | Invariant assertion | `VA-PLAN-003`, terminate `FAILED` before spending on generation. |
| Repair cap exceeded | Invariant assertion | Programming error; assert rather than tolerate. Silently allowing a third repair is unbounded spend. |
| Two workers on one job | Redis lock | Second worker declines. Fencing token on writes so a stale worker's write is rejected. |
| Queue step redelivered | At-least-once delivery `[D-67]` | Re-execute the node; the `request_fingerprint` constraint and `provider_project_id` reconciliation collapse any paid call to one `[D-24]`. |
| Orphaned job (worker died) | Lock TTL expiry sweep | `reclaim_orphans()` resumes from the last checkpoint. |
| Infinite loop | `max_iterations` in the harness | Terminate `PARTIAL`. |

## 9. Test strategy

| Level | Tests |
| --- | --- |
| Topology | Snapshot the compiled node and edge sets; a change requires a doc update in the same PR (CDR drift gate). Assert exactly one cycle, and that it is `qc_shot → generate_shot`. |
| Guard coverage | Reflectively enumerate all router functions; assert each calls `_guard` first. |
| Sequentiality | Assert no fan-out primitive is used; a fake provider records call timestamps and asserts non-overlap. |
| Repair cap | Force QC below threshold forever; assert exactly 3 generations per shot, then `abandoned`, then the job continues. |
| Checkpoint | For every node boundary, kill the process and assert resume produces the same terminal state and the same total spend as an uninterrupted run. |
| Atomicity | Fault-inject a failure between domain write and checkpoint write; assert both roll back. |
| Reconciliation | Simulate a crash after the provider billed but before commit; assert exactly one charge on resume. |
| At-least-once | Deliver every node twice, in order and interleaved; assert terminal state and total spend are identical to a single-delivery run `[D-67]`. |
| Byte identity | Regenerate shot 3; assert checksums of shots 1, 2, 4 are unchanged and no provider call was made for them. |
| Chaining | Abandon shot 1; assert shot 2 chains from no frame and is flagged degraded. Abandon shot 2; assert shot 3 chains from shot 1's frame. |
| Invariants | Property test over random state mutations; assert every invariant in §2 either holds or raises. |
