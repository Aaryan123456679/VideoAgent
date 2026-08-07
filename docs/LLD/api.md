---
doc: LLD
module: api
title: API — FastAPI async surface
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

# LLD — `api`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — PARTIAL.** **E1 — in the v1 build.** Job creation, status, artifacts and the SSE stream ship. **`POST /resume` and `POST /shots/{i}/regenerate` are designed here but deferred to E3**, along with everything they depend on.
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

The only network-facing surface of the Video Agent. It:

- accepts a prompt and creates a `Job`, **never** doing the work inline;
- enforces idempotency on every work-creating `POST` `[CPS §Non-negotiables]`;
- exposes job status, progress and the delivery manifest;
- exposes the two resilience affordances the PRD promises — **resume** and **shot-level
  regeneration** `[PRD §Resilience]`;
- renders every failure into one error envelope carrying a stable code and the `trace_id`
  `[CPS §Failure behaviour]`.

**Not** its responsibility: orchestration (`graph`), termination (`harness`), model calls
(`gateway`), any knowledge of a provider name.

Python 3.12, FastAPI, fully async. `[CPS §Canonical stack]` Every route handler is `async def`
and performs no blocking I/O; ffmpeg and provider calls never happen in a request path.

## 2. Public interface

### 2.1 Routes

| Method | Path | Idempotent | Purpose |
| --- | --- | --- | --- |
| `POST` | `/v1/jobs` | **key required** | Create a job. Returns `202` immediately. |
| `GET` | `/v1/jobs/{job_id}` | — | Job status, outcome, per-shot state, spend. |
| `GET` | `/v1/jobs/{job_id}/events` | — | SSE progress stream `[D-09]`. |
| `GET` | `/v1/jobs/{job_id}/artifacts` | — | Delivery manifest with presigned URLs. |
| `POST` | `/v1/jobs/{job_id}/resume` | **key required** | Resume from the last checkpoint. |
| `POST` | `/v1/jobs/{job_id}/shots/{shot_index}/regenerate` | **key required** | Regenerate one shot only. |
| `POST` | `/v1/jobs/{job_id}/cancel` | **key required** | Cooperative cancel → `FAILED`/`ESCALATED`. |
| `GET` | `/v1/jobs` | — | Cursor-paginated list, tenant-scoped. |
| `GET` | `/healthz` · `/readyz` | — | Liveness / readiness. |

`POST /v1/jobs` returns `202 Accepted`, never `200`. A 40-second video takes minutes
`[PRD §Success metrics]`; a synchronous create would be a lie.

> **No outbound webhooks in v1** `[D-74]`. `CreateJobRequest.webhook_url` was declared in an
> earlier draft with no payload shape, retry policy, signing or failure behaviour, and nothing
> implemented it. **A declared-but-inert field in a public API contract is worse than an
> absent one** — callers integrate against it and silently never receive callbacks. It is
> removed. Job completion is observable via `GET /v1/jobs/{job_id}` and the SSE stream in §5.
> Outbound webhooks, if wanted later, need their own design (signing, retry, replay
> protection) and are a feature, not a field.
>
> This is unrelated to **Magic Hour's inbound `video.completed` webhook**, which is an
> upstream callback *to us* and remains available as an alternative to polling — see
> [`providers.md` §7.3](./providers.md#73-polling-and-terminal-states).

### 2.2 Models

```python
# --- request ------------------------------------------------------------
class CreateJobRequest(BaseModel):
    prompt: str = Field(min_length=8, max_length=2_000)
    music_bed_artifact_id: UUID | None = None   # caller-supplied audio; v1 ships no
                                                # bundled music library  [D-69]
    metadata: dict[str, str] = Field(default_factory=dict, max_length=20)
    # No duration field: 40s is fixed. [PRD §Out of scope]
    # No reference_image field: user-supplied reference characters are out of scope.
    # No webhook_url field: REMOVED from v1. [D-74] — see the note below.

class RegenerateShotRequest(BaseModel):
    reason: str | None = None
    note_to_planner: str | None = None   # advisory only; never overrides the locked bible

# --- response -----------------------------------------------------------
class JobAccepted(BaseModel):
    job_id: UUID
    status: Literal["queued"]
    trace_id: str
    created_at: datetime

class ShotView(BaseModel):
    index: int                                    # 0..3
    beat_kind: Literal["setup", "development", "turn", "resolution"]
    status: Literal["pending", "generating", "qc", "accepted", "abandoned"]
    attempts_used: int
    repairs_used: int                             # <= 2  [D-01]
    continuity_score: float | None                # 0.0..1.0
    duration_s: float | None

class BudgetView(BaseModel):
    iterations_used: int; iterations_cap: int
    wall_clock_s: float;  wall_clock_cap_s: float
    tokens_used: int;     tokens_cap: int
    usd_spent: Decimal;   usd_cap: Decimal

class JobView(BaseModel):
    job_id: UUID
    status: Literal["queued", "running", "terminal"]
    outcome: Literal["SUCCESS", "PARTIAL", "FAILED_NO_PROGRESS",
                     "FAILED", "ESCALATED"] | None      # [CPS §Agent harness]
    degraded: bool                                       # [CPS §Failure behaviour]
    degraded_reason: str | None
    shots: list[ShotView]
    budget: BudgetView
    trace_id: str
    resumable: bool                                      # [PRD §Resilience]
    created_at: datetime
    updated_at: datetime

class ArtifactView(BaseModel):
    kind: Literal["final_video", "shot_clip", "thumbnail",
                  "continuity_frame", "story_plan_json", "bible_json"]
    shot_index: int | None
    url: HttpUrl            # presigned  [PRD §How it works 6]
    expires_at: datetime
    bytes: int
    checksum_sha256: str

class DeliveryManifest(BaseModel):
    job_id: UUID
    outcome: str
    partial: bool                       # true when fewer than 4 shots are present
    qc_threshold_used: float | None     # set ONLY when it differs from the 0.75 default,
                                        # so a loosened gate is never invisible  [D-71]
    artifacts: list[ArtifactView]
    reproducibility: list[ShotReproRecord]   # per-shot cost, model, prompt, provider
                                             # project id; seed where supported.
                                             # [PRD §What's delivered], [D-59]
    reproducibility_caveat: str | None       # set when a promise is unmet, e.g. the v1
                                             # provider offers no seed control [D-59]
```

### 2.3 Handler signatures

```python
@router.post("/v1/jobs", status_code=202, response_model=JobAccepted)
async def create_job(
    body: CreateJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=255)],
    principal: Annotated[Principal, Depends(require_tenant)],
) -> JobAccepted: ...

@router.get("/v1/jobs/{job_id}", response_model=JobView)
async def get_job(job_id: UUID, principal: ... ) -> JobView: ...

@router.get("/v1/jobs/{job_id}/events")
async def stream_events(job_id: UUID, principal: ...) -> EventSourceResponse: ...

@router.get("/v1/jobs/{job_id}/artifacts", response_model=DeliveryManifest)
async def get_artifacts(job_id: UUID, principal: ...) -> DeliveryManifest: ...

@router.post("/v1/jobs/{job_id}/resume", status_code=202, response_model=JobAccepted)
async def resume_job(job_id: UUID, idempotency_key: ..., principal: ...) -> JobAccepted: ...

@router.post("/v1/jobs/{job_id}/shots/{shot_index}/regenerate",
             status_code=202, response_model=JobAccepted)
async def regenerate_shot(job_id: UUID, shot_index: Annotated[int, Path(ge=0, le=3)],
                          body: RegenerateShotRequest,
                          idempotency_key: ..., principal: ...) -> JobAccepted: ...
```

## 3. Idempotency

`Idempotency-Key` is **required** on every work-creating `POST`. `[CPS §Non-negotiables]`
Missing key → `400 VA-REQ-002`. No exceptions, no "optional in dev".

Algorithm, executed inside a Redis lock keyed on `(tenant_id, route, key)`:

1. Compute `request_fingerprint = sha256(tenant_id | route | canonical_json(body))`.
2. `SET idem:{tenant}:{route}:{key} -> {state: in_flight, fingerprint} NX EX 86400`.
   TTL is 24h. `[D-16]`
3. `NX` succeeded → this is the first call. Create the job, then overwrite the entry with
   `{state: done, job_id, response_body}`.
4. `NX` failed and the stored fingerprint **matches** → replay: return the stored response
   with the original status code and header `Idempotency-Replayed: true`. If still
   `in_flight`, return `409 VA-REQ-004` with `Retry-After`.
5. `NX` failed and the fingerprint **differs** → `409 VA-REQ-003` (key reused with a
   different body). Never silently create a second job.

`[D-16]`: 24h TTL and fingerprint-mismatch-is-an-error are choices; `[CPS]` mandates the keys
but not their semantics. Redis holds idempotency by `[CPS §Canonical stack]`, but the
`job_id ↔ idempotency_key` pair is **also** written to Postgres with a unique constraint, so
a Redis flush cannot cause a duplicate job. See [`persistence.md`](./persistence.md).

## 4. Error envelope

One shape for every non-2xx. `[CPS §Failure behaviour]` — *fail honestly: what happened, what
was preserved, what to do next.*

```python
class ErrorEnvelope(BaseModel):
    class Error(BaseModel):
        code: str          # stable, e.g. "VA-PROV-005"  — never renumbered, never reused
        message: str       # what happened, human-readable, no PII, no stack traces
        retryable: bool
        trace_id: str      # [CPS §Failure behaviour] support opens the exact Langfuse trace
        job_id: UUID | None
        preserved: dict[str, Any]   # what survived: shots accepted, artifacts already stored
        next_steps: str             # what to do next: "POST /v1/jobs/{id}/resume"
        details: dict[str, Any]     # machine-readable specifics; redacted
    error: Error
```

The full code taxonomy is owned by [`observability.md`](./observability.md); this module only
renders it. Codes are **stable** — a code's meaning may never change, and a retired code is
never re-used.

| HTTP | Typical codes |
| --- | --- |
| 400 | `VA-REQ-001` invalid prompt · `VA-REQ-002` idempotency key missing |
| 401 / 403 | `VA-AUTH-001` unauthenticated · `VA-AUTH-002` tenant forbidden |
| 404 | `VA-REQ-005` job not found *(also returned for cross-tenant reads — never confirm existence)* |
| 409 | `VA-REQ-003` idempotency conflict · `VA-REQ-004` request in flight · `VA-REQ-006` job not resumable |
| 422 | `VA-REQ-007` request schema invalid |
| 429 | `VA-GW-003` rate limited (Redis token bucket) |
| 503 | `VA-GW-001` circuit open · `VA-PROV-001` provider unavailable |
| 500 | `VA-INT-001` internal error — message is always generic |

A **terminal job is never an HTTP error.** `GET /v1/jobs/{id}` returns `200` with
`outcome: FAILED`. HTTP status describes the API call; `outcome` describes the job.

## 5. Progress stream

`GET /v1/jobs/{job_id}/events` is Server-Sent Events, reading the Redis progress channel
`progress:{job_id}` `[D-09]`. Event types: `node_entered`, `shot_started`, `shot_scored`,
`shot_accepted`, `shot_abandoned`, `assembling`, `terminal`. Each event carries `job_id`,
`trace_id`, monotonic `seq` and a UTC timestamp. Heartbeat every 15s. On reconnect the client
sends `Last-Event-ID`; the last 200 events are retained in a Redis stream for replay.

No media bytes and no presigned URLs travel on this channel.

## 6. Authentication, authorisation and tenancy

**Static per-tenant API keys** `[D-68]`, presented as `Authorization: Bearer <key>`.

```python
class Principal(BaseModel):
    tenant_id: UUID
    key_id: UUID

async def require_tenant(
    authorization: Annotated[str, Header()],
) -> Principal: ...
```

| Property | Choice |
| --- | --- |
| Storage | **Argon2id hash** in `tenant_api_key`. The plaintext is shown **once at issuance and never again** — it is not recoverable, by us or by anyone |
| Lookup | By the non-secret `key_prefix`, then a constant-time Argon2id verification of the remainder |
| Resolution | Key → `Principal{tenant_id, key_id}` |
| RLS binding | `SET LOCAL app.tenant_id` from `Principal.tenant_id` **only** — never from a request body, path parameter, query string or header |
| Rejection | Unknown, revoked and disabled-tenant keys are indistinguishable in response and timing |
| Rotation | Multiple live keys per tenant; revoke by setting `revoked_at`, no downtime |

OAuth/OIDC is **out of scope for v1**. It changes *who issues identity*, not *how RLS consumes
it*, so it can be added later behind the same `Principal` without touching a single query.

The tenant id is pushed into the request-scoped DB session as the RLS setting before any query
runs `[CPS §Canonical stack]`; the API issues **no** query outside that session. Cross-tenant
access therefore fails at the database, not at a hand-written `WHERE` clause. Attempts are
logged as `VA-AUTH-002` and are surfaced to clients as `404`.

**The API key is a credential and is never logged** — not the plaintext, not the hash. Logs
carry `key_id` only. `[CPS §Observability]`, `[D-52]`

Rate limits are per tenant, enforced in Redis. `[CPS §Canonical stack]`

## 7. Dependencies

| Depends on | For |
| --- | --- |
| [`persistence.md`](./persistence.md) | job/shot/artifact reads, idempotency record, presigned URL minting |
| [`harness.md`](./harness.md) | job submission, cancel signal, budget view |
| [`graph.md`](./graph.md) | resume and shot-regeneration entry points |
| [`observability.md`](./observability.md) | trace creation, error code taxonomy, redaction |

Depends on **nothing** in `providers`, `qc`, `assembly`, `planning` or `gateway`. If a route
handler needs to import one of those, the layering is wrong.

## 8. Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| Missing idempotency key | Header validation | `400 VA-REQ-002`. Reject; never invent a key. |
| Duplicate key, same body | Redis fingerprint match | Replay the stored `202`. Exactly one job exists. |
| Duplicate key, different body | Fingerprint mismatch | `409 VA-REQ-003`. |
| Concurrent duplicate in flight | Redis `NX` + `in_flight` state | `409 VA-REQ-004` + `Retry-After`. |
| Redis unavailable | Connection error on the idempotency write | **Reject with `503`.** Idempotency is a non-negotiable; degrading it is not permitted, unlike a cache. `[D-17]` |
| Postgres unavailable | Connection error | `503 VA-STORE-003`, `retryable: true`. |
| Resume on a `SUCCESS` job | `resumable == false` | `409 VA-REQ-006`; the manifest is already complete. |
| Regenerate a shot on a running job | Job status `running` | `409 VA-REQ-004`; one writer per job, enforced by a Redis job lock. |
| Regenerate index outside 0–3 | Path validation | `422`. |
| Client disconnects mid-SSE | Stream write failure | Close the stream. Job execution is unaffected — it does not live in the request. |
| Presign fails | Object-store error | `503 VA-STORE-002`; the manifest lists the artifact with `url: null` rather than omitting it, so the client learns it exists. |
| Unhandled exception | Global exception handler | `500 VA-INT-001`, generic message, real detail only in the trace. Never leak stack traces. `[CPS §Observability]` |

## 9. Test strategy

| Level | Tests |
| --- | --- |
| Contract | OpenAPI schema snapshot; a breaking change to any response model fails CI. Error envelope shape asserted for every code path. |
| Idempotency | Property test: N concurrent identical `POST /v1/jobs` create **exactly one** job row. Same key + different body → 409. Replay returns byte-identical body. Key survives a process restart (Postgres unique constraint holds when Redis is flushed). |
| Tenancy | For every route, a request with tenant B's key against tenant A's job returns 404 and produces zero rows. Run with RLS **enabled** in the test database — never with a superuser role. |
| Auth | Assert the RLS session variable is set from `Principal.tenant_id` and can never be influenced by body, path, query or header. Assert unknown / revoked / disabled-tenant keys are indistinguishable. Assert no plaintext key or hash reaches a log line `[D-68]`. |
| Removed fields | Assert `webhook_url` is rejected as an unknown field rather than silently ignored, so a caller integrating against the old draft fails loudly `[D-74]`. |
| Async hygiene | A lint/test gate asserting no blocking call (`subprocess`, sync driver, `requests`) is reachable from a route handler. |
| Failure injection | Redis down → 503, not a duplicate job. Postgres down → 503. Object store down → manifest with null URLs. |
| SSE | Ordering by `seq`, heartbeat cadence, `Last-Event-ID` replay, and an assertion that no event body ever contains a URL or base64 payload. |
| Latency | The create path is asserted to do O(1) work: one Redis round trip, two inserts. No provider or model call is reachable from it. |
