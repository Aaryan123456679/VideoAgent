---
doc: LLD
module: persistence
title: Persistence — PostgreSQL schema with RLS, Redis, artifact storage
status: canonical
implementation_status: built
version: 1
last_synced_commit: 438138573aa69c26727651b368e056262908bc69
generated_by: cdr-documentation
run_id: 2026-08-08/001-documentation
sources:
  - docs/specs/common-platform-spec.md
  - docs/specs/video-agent-prd.md
  - docs/HLD.md
---

# LLD — `persistence`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — BUILT.** **E0 — in the v1 build.** The schema, RLS, migrations, Redis usage and artifact storage all ship. Tables that only deferred modules write (QC columns on `shot_attempt`) are created but unused until E3.
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

Three stores, three distinct roles. `[CPS §Canonical stack]`

| Store | Role | Authoritative? |
| --- | --- | --- |
| **PostgreSQL 16** | System of record, **RLS per tenant**, LangGraph checkpoints | **Yes** |
| **Redis 7** | Cache, locks, rate limits, idempotency, progress | No — except idempotency, which is mirrored to Postgres |
| **Object store** | Artifact bytes; presigned URLs for delivery | Bytes yes, metadata no |

The single most important rule: **Postgres is the system of record.** Redis may be flushed at
any moment without losing a job, a shot or a dollar of recorded spend.

**pgvector / MongoDB Atlas** is declared by `[CPS §Canonical stack]` but has **no consumer in
Video Agent v1** — no retrieval surface exists. Declared, not built. `[D-13]`

## 2. Public interface — PostgreSQL schema

The DDL below **is** this module's public interface; migration files are generated from it.
Every other module reads and writes through these tables.

```sql
CREATE TYPE job_status   AS ENUM ('queued','running','terminal');
CREATE TYPE job_outcome  AS ENUM ('SUCCESS','PARTIAL','FAILED_NO_PROGRESS','FAILED','ESCALATED');
CREATE TYPE shot_status  AS ENUM ('pending','generating','qc','accepted','abandoned');
CREATE TYPE beat_kind    AS ENUM ('setup','development','turn','resolution');
CREATE TYPE attempt_state AS ENUM ('in_flight','succeeded','failed','orphaned');
CREATE TYPE artifact_kind AS ENUM ('final_video','shot_clip','thumbnail','continuity_frame',
                                   'story_plan_json','bible_json');

-- ------------------------------------------------------------- tenant
-- The table RLS is defined in terms of. Every tenant_id in this schema is an FK to it. [D-70]
CREATE TABLE tenant (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT        NOT NULL,
  max_usd_per_job NUMERIC(10,4),          -- NULL = inherit the global cap  [D-08]
  retention_days  INTEGER     NOT NULL DEFAULT 30,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  disabled_at     TIMESTAMPTZ
);
-- DELIBERATELY NOT RLS-PROTECTED. Protecting the table that bootstraps the policy with the
-- policy it bootstraps is circular. Reachable only by the migration role and the admin
-- path, never by a tenant-scoped connection. [D-70]

-- --------------------------------------------------------- tenant_api_key
-- Static per-tenant API keys. Plaintext is shown ONCE at issuance and never stored. [D-68]
CREATE TABLE tenant_api_key (
  id            UUID PRIMARY KEY,
  tenant_id     UUID NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  key_hash      TEXT NOT NULL,            -- Argon2id. Never the plaintext, never reversible.
  key_prefix    TEXT NOT NULL,            -- short non-secret prefix, for lookup and support
  label         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at  TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ,
  UNIQUE (key_prefix)
);
CREATE INDEX tenant_api_key_tenant_idx ON tenant_api_key (tenant_id) WHERE revoked_at IS NULL;
-- Also not tenant-RLS-protected: it is read by the unauthenticated path that is trying to
-- establish which tenant is calling, i.e. before a tenant context exists. [D-68]

-- ---------------------------------------------------------------- job
CREATE TABLE job (
  id                UUID PRIMARY KEY,
  tenant_id         UUID NOT NULL REFERENCES tenant(id),
  idempotency_key   TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  prompt            TEXT NOT NULL,                 -- user content; see §6 redaction
  music_bed         BOOLEAN NOT NULL DEFAULT FALSE,
  status            job_status NOT NULL DEFAULT 'queued',
  outcome           job_outcome,
  degraded          BOOLEAN NOT NULL DEFAULT FALSE,
  degraded_reason   TEXT,
  terminal_reason_code TEXT,                       -- stable code [CPS §Failure behaviour]
  trace_id          TEXT NOT NULL,                 -- Langfuse trace = one job
  budget_caps       JSONB NOT NULL,
  budget_used       JSONB NOT NULL DEFAULT '{}',
  budget_epoch      INT  NOT NULL DEFAULT 0,       -- incremented on a resume grant [D-25]
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT job_idem_uq UNIQUE (tenant_id, idempotency_key)   -- [CPS §Non-negotiables]
);
CREATE INDEX job_tenant_created_idx ON job (tenant_id, created_at DESC);
CREATE INDEX job_status_idx ON job (status) WHERE status <> 'terminal';

-- ---------------------------------------------------------- story plan
CREATE TABLE story_plan (
  id            UUID PRIMARY KEY,
  job_id        UUID NOT NULL UNIQUE REFERENCES job(id) ON DELETE CASCADE,
  tenant_id     UUID NOT NULL REFERENCES tenant(id),
  logline       TEXT NOT NULL,
  total_duration_s NUMERIC(5,2) NOT NULL CHECK (total_duration_s = 40.00),  -- exactly 40s
  model_alias   TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE beat (
  id            UUID PRIMARY KEY,
  story_plan_id UUID NOT NULL REFERENCES story_plan(id) ON DELETE CASCADE,
  tenant_id     UUID NOT NULL REFERENCES tenant(id),
  idx           SMALLINT NOT NULL CHECK (idx BETWEEN 0 AND 3),
  kind          beat_kind NOT NULL,
  action        TEXT NOT NULL,
  camera_move   TEXT NOT NULL,
  duration_s    NUMERIC(4,2) NOT NULL CHECK (duration_s = 10.00),  -- v1 fixes 10s [D-03]
  continuity_note TEXT,
  UNIQUE (story_plan_id, idx)
);

-- ----------------------------------------------------- continuity bible
CREATE TABLE continuity_bible (
  id            UUID PRIMARY KEY,
  job_id        UUID NOT NULL UNIQUE REFERENCES job(id) ON DELETE CASCADE,
  tenant_id     UUID NOT NULL REFERENCES tenant(id),
  character     JSONB NOT NULL,
  wardrobe      JSONB NOT NULL,
  location      JSONB NOT NULL,
  lighting      JSONB NOT NULL,
  palette       JSONB NOT NULL,
  lens_language JSONB NOT NULL,
  negative_constraints JSONB NOT NULL DEFAULT '[]',
  content_hash  TEXT NOT NULL,
  locked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  model_alias   TEXT NOT NULL,
  prompt_version TEXT NOT NULL
);

-- IMMUTABLE FOR THE LIFE OF THE JOB [PRD §How it works 2] — enforced by the database,
-- not by application convention.
CREATE OR REPLACE FUNCTION reject_bible_update() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'VA-BIBLE-002: continuity_bible is immutable (job %)', OLD.job_id;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER continuity_bible_immutable
  BEFORE UPDATE ON continuity_bible
  FOR EACH ROW EXECUTE FUNCTION reject_bible_update();

-- ---------------------------------------------------------------- shot
CREATE TABLE shot (
  id            UUID PRIMARY KEY,
  job_id        UUID NOT NULL REFERENCES job(id) ON DELETE CASCADE,
  tenant_id     UUID NOT NULL REFERENCES tenant(id),
  beat_id       UUID NOT NULL REFERENCES beat(id),
  idx           SMALLINT NOT NULL CHECK (idx BETWEEN 0 AND 3),
  status        shot_status NOT NULL DEFAULT 'pending',
  attempts_used SMALLINT NOT NULL DEFAULT 0,
  repairs_used  SMALLINT NOT NULL DEFAULT 0 CHECK (repairs_used <= 2),   -- [D-01]
  best_attempt_id UUID,
  best_score    NUMERIC(4,3) CHECK (best_score BETWEEN 0 AND 1),
  UNIQUE (job_id, idx)
);

-- -------------------------------------------------------- shot attempt
CREATE TABLE shot_attempt (
  id            UUID PRIMARY KEY,
  shot_id       UUID NOT NULL REFERENCES shot(id) ON DELETE CASCADE,
  job_id        UUID NOT NULL REFERENCES job(id) ON DELETE CASCADE,
  tenant_id     UUID NOT NULL REFERENCES tenant(id),
  attempt_no    SMALLINT NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),   -- 1 + 2 repairs
  state         attempt_state NOT NULL DEFAULT 'in_flight',
  -- reproducibility contract [PRD §What's delivered]
  provider_key    TEXT,
  provider_model  TEXT,                     -- e.g. 'wan-2.2'
  provider_project_id TEXT,                 -- upstream render id; reproducibility handle [D-59]
  seed            BIGINT,                   -- NULL where the provider has no seed  [D-59]
  seed_supported  BOOLEAN NOT NULL DEFAULT FALSE,  -- explicit, so NULL is never ambiguous
  prompt_text     TEXT NOT NULL,
  prompt_hash     TEXT NOT NULL,
  bible_hash      TEXT NOT NULL,
  conditioning_frame_id UUID,
  request_fingerprint   TEXT NOT NULL,      -- crash reconciliation [D-24]
  cost_usd        NUMERIC(10,4) NOT NULL DEFAULT 0,   -- converted at credits_per_usd [D-60]
  credits_charged NUMERIC(12,4),            -- provisional until terminal  [D-60]
  cost_is_final   BOOLEAN NOT NULL DEFAULT FALSE,
  -- seed is nullable BY DESIGN. A NOT NULL column here would force a fabricated value
  -- and misrepresent [PRD §What's delivered]. See D-59.
  -- QC
  qc_score        NUMERIC(4,3) CHECK (qc_score BETWEEN 0 AND 1),
  qc_dimensions   JSONB,
  qc_findings     JSONB,
  qc_hard_fail    BOOLEAN NOT NULL DEFAULT FALSE,
  clip_artifact_id       UUID,
  final_frame_artifact_id UUID,
  error_code      TEXT,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at        TIMESTAMPTZ,
  UNIQUE (shot_id, attempt_no),
  UNIQUE (request_fingerprint)              -- never bill the same request twice
);

-- ------------------------------------------------------------ artifact
CREATE TABLE artifact (
  id            UUID PRIMARY KEY,
  job_id        UUID NOT NULL REFERENCES job(id) ON DELETE CASCADE,
  tenant_id     UUID NOT NULL REFERENCES tenant(id),
  kind          artifact_kind NOT NULL,
  shot_index    SMALLINT,
  storage_key   TEXT NOT NULL UNIQUE,       -- bytes live in the object store, never here
  content_type  TEXT NOT NULL,
  bytes         BIGINT NOT NULL,
  checksum_sha256 TEXT NOT NULL,            -- byte-identity assertion [PRD §Resilience]
  width INT, height INT, duration_s NUMERIC(6,2),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX artifact_job_kind_idx ON artifact (job_id, kind, shot_index);

-- ---------------------------------------------------------- checkpoint
-- Written after EVERY node [CPS §Non-negotiables], in the same transaction as that
-- node's domain writes [D-23].
CREATE TABLE checkpoint (
  id            BIGSERIAL PRIMARY KEY,
  thread_id     UUID NOT NULL,              -- = job.id
  tenant_id     UUID NOT NULL REFERENCES tenant(id),
  node          TEXT NOT NULL,
  seq           INT NOT NULL,
  state         JSONB NOT NULL,             -- JobState; NEVER media bytes
  budget_used   JSONB NOT NULL,
  failure_signatures JSONB NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (thread_id, seq)
);
CREATE INDEX checkpoint_thread_seq_idx ON checkpoint (thread_id, seq DESC);
```

## 3. Row-level security

**RLS per tenant.** `[CPS §Canonical stack]` Applied to **every** table above, without
exception, including `checkpoint` and `artifact`.

```sql
ALTER TABLE job ENABLE ROW LEVEL SECURITY;
ALTER TABLE job FORCE ROW LEVEL SECURITY;          -- applies to the table owner too
CREATE POLICY job_tenant_isolation ON job
  USING      (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
-- repeated for every table
```

**Two tables are deliberately exempt** `[D-70]`, `[D-68]`:

| Exempt table | Why |
| --- | --- |
| `tenant` | It is the table the policy is defined *in terms of*. Protecting it with the policy it bootstraps is circular. Reachable only by the migration role and the admin path, never by a tenant-scoped connection. |
| `tenant_api_key` | It is read by the **unauthenticated** path that is trying to establish *which* tenant is calling — i.e. before a tenant context exists. Lookup is by non-secret `key_prefix`; the row yields the `tenant_id` that then sets the session variable. |

Every other table is RLS-protected without exception.

Rules:

1. `tenant_id` is **denormalised onto every table**, including children, so a policy is a
   single-column predicate and never a join. A join-based policy is a policy that can be
   accidentally bypassed. `[D-51]` **The `REFERENCES tenant(id)` foreign keys added by
   `[D-70]` do not change this** — the FK buys referential integrity; RLS still reads the
   local `tenant_id` column and never joins.
2. The application connects as a **non-superuser, non-owner** role. `FORCE ROW LEVEL
   SECURITY` closes the owner-bypass hole.
3. `SET LOCAL app.tenant_id` runs at the start of every transaction, sourced from
   `Principal.tenant_id` **only** — resolved from the API key `[D-68]`, never from a request
   body, path parameter, query string or header.
4. `WITH CHECK` is present on every policy, so a cross-tenant **write** is as impossible as a
   cross-tenant read.
5. Tests run against RLS **enabled**, as a normal role. A test suite that runs as superuser
   proves nothing about isolation.

## 4. Migrations — expand/contract

**Migrations are expand/contract and applied before deploy.** `[CPS §Rollout]`

| Phase | Action | Deployable with old code? |
| --- | --- | --- |
| **Expand** | Add nullable columns / new tables / new enum values; backfill in batches; dual-write | Yes |
| **Migrate** | New code reads and writes the new shape; old shape still populated | Yes |
| **Contract** | Drop the old column/constraint — a **separate deploy**, after the new code is fully rolled out | N/A |

Hard rules: never add a `NOT NULL` column without a default in one step · never rename in
place (add, dual-write, backfill, drop) · never drop in the same release that stops writing ·
every migration has a tested rollback · long-lived locks are avoided (`CREATE INDEX
CONCURRENTLY`, `NOT VALID` then `VALIDATE`) · **the `JobState` checkpoint schema follows the
same discipline**, since an in-flight job's checkpoint must deserialise under the new code or
resume breaks `[D-23]`.

## 5. Redis 7

Cache, locks, rate limits, idempotency, progress. `[CPS §Canonical stack]`

| Key | Type | TTL | Purpose |
| --- | --- | --- | --- |
| `idem:{tenant}:{route}:{key}` | hash | 24h | Idempotency record; mirrored by the `job_idem_uq` constraint `[D-16]` |
| `job:{job_id}` | string (fencing token) | 60s, heartbeat | One writer per job `[D-10]` |
| `jobs:stream` | **stream + consumer group** | — | **Job queue** `[D-67]`. At-least-once delivery; see below |
| `progress:{job_id}` | stream | 1h | SSE progress events `[D-09]` |
| `sig:{job_id}` | hash | job TTL | Failure-signature counts; mirrored into the checkpoint `[D-02]` |
| `rl:{tenant}:{window}` | string | window | Rate limit token bucket |
| `cb:{dependency}` | hash | 5m | Circuit-breaker state, shared across workers |
| `cache:llm:{hash}` | string | 1h | Gateway response cache (never for planning/bible) |
| `job:{job_id}:cancel` | string | 24h | Cooperative cancel signal for the harness loop `[D-12]` |

### 5.1 The job queue `[D-67]`

**Redis Streams with consumer groups.** Redis 7 is already mandated for locks, idempotency
and progress `[CPS §Canonical stack]`, so this adds no dependency.

Delivery is **at-least-once**: a job step can be delivered twice (worker crash before `XACK`,
or a `XAUTOCLAIM` of a stalled pending entry). **This is safe only because `[D-24]` already
requires it to be** — the `shot_attempt.request_fingerprint` unique constraint plus
`provider_project_id` reconciliation via `GET /v1/video-projects/{id}` make a redelivered step
**re-read the existing provider render rather than submit a new paid one**.

At-most-once was rejected: it drops work on a worker crash, and a dropped shot on a paid,
partially-billed job is worse than a duplicate that the fingerprint check collapses.

**Redis is never the only copy of anything that costs money or creates a job.** Idempotency
is mirrored to Postgres; the budget ledger and failure signatures are mirrored into the
checkpoint; queue entries are recoverable from job status in Postgres, which remains the
system of record. A Redis flush degrades performance and loses in-flight progress events. It
must never cause a duplicate job or a duplicate charge.

## 6. Artifact storage and presigned URLs

| Property | Choice |
| --- | --- |
| Layout | `{tenant_id}/{job_id}/{kind}/{shot_index}/{artifact_id}.{ext}` — tenant-prefixed so bucket policy is a second isolation layer after RLS |
| Bytes in Postgres | **Never.** Metadata and key only |
| Checksum | SHA-256 computed on write, stored, and verified on read. This is what makes byte-identity assertable `[PRD §Resilience]` |
| Upload | Server-side from the worker; retried with backoff; the local file is retained until the checksum is confirmed |
| Delivery | **Presigned URLs** `[PRD §How it works 6]`, default 1h expiry, `GET` only, minted on demand at `GET /v1/jobs/{id}/artifacts` — never stored, never cached, never logged (a presigned URL is a bearer credential) `[D-52]` |
| Encryption | At rest, plus TLS in transit |
| Lifecycle | Retained per tenant policy; expiry deletes bytes and marks the artifact row `expired` rather than deleting it, so the reproducibility record survives the media `[D-53]` |

## 7. Data handling

**Never logged: credentials, raw PII, full media payloads, row-level query results.**
`[CPS §Observability]`

- The user's `prompt` is stored (it is needed for resume and reproducibility) but is
  **redacted in logs and traces** — hash plus a short truncation only.
- Query results are never logged. Log the statement identity and row *count*, never rows.
- Credentials come from the secret store and never appear in a row, a log or a checkpoint.
- Checkpoint `state` is asserted at write time to contain no key matching a media-bytes or
  URL pattern.

## 8. Dependencies

Depends on PostgreSQL 16, Redis 7 and the object store. Depends on no other module in this
repo — it is the bottom layer. Everything else depends on it.

## 9. Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| Postgres unavailable | Connection error | `VA-STORE-003`, retryable. The API returns 503; workers back off and retry. No job is lost — nothing was committed. |
| Redis unavailable | Connection error | Degrade for cache/progress; **reject** for idempotency `[D-17]`; treat circuits as closed and alarm `[D-22]`. |
| Unique violation on `job_idem_uq` | Constraint error | The idempotent replay path. Return the existing job, never a duplicate. |
| Unique violation on `request_fingerprint` | Constraint error | A provider call was already recorded. Adopt the existing attempt; never bill twice `[D-24]`. |
| Provisional cost never reconciled | `cost_is_final = false` on a terminal attempt | Sweeper re-reads the upstream project and settles the ledger; alarm if it cannot `[D-60]`. |
| Bible `UPDATE` attempted | Trigger raises | `VA-BIBLE-002`. Terminate the job loudly. |
| `repairs_used > 2` | CHECK constraint | Programming error; the transaction fails. The database is the last line of defence for the cap. |
| RLS setting absent | Policy evaluates false | Zero rows, not an error, plus an alarm. Silent full-table access is impossible by construction. |
| Queue entry redelivered | Duplicate consumer-group delivery | Safe by construction: the `request_fingerprint` constraint and `provider_project_id` reconciliation collapse it to one paid render `[D-67]`, `[D-24]`. |
| Worker dies holding a pending entry | `XPENDING` idle time exceeded | `XAUTOCLAIM` reassigns it; the job resumes from its last checkpoint. |
| API key presented for a disabled tenant | `tenant.disabled_at IS NOT NULL` | `VA-AUTH-002`. Checked at key resolution, so a disabled tenant cannot create work `[D-70]`. |
| Revoked API key presented | `revoked_at IS NOT NULL` | `VA-AUTH-001`, constant-time; never distinguish revoked from unknown. |
| Checkpoint deserialisation fails | Pydantic validation | Job marked non-resumable; artifacts preserved; deliver the partial. Never guess at a state shape. |
| Object store unavailable | S3-compatible error | Retry with backoff; on exhaustion `VA-STORE-001`. Keep the local file so resume re-uploads instead of re-encoding — an artifact already paid for is never regenerated. |
| Checksum mismatch on read | Verification | `VA-STORE-004`. Treat the artifact as lost, exclude it from assembly, flag degraded. |
| Presign fails | Store error | `VA-STORE-002`; the manifest still lists the artifact with a null URL. |
| Long-running migration locks a table | Deploy monitoring | Expand/contract plus `CONCURRENTLY` avoids it; a migration exceeding its lock budget is aborted. |
| Disk/quota exhaustion | Store error | Alarm; jobs fail honestly with what was preserved. |

## 10. Test strategy

| Level | Tests |
| --- | --- |
| RLS (highest priority) | For **every** table except the two documented exemptions: as tenant B, assert `SELECT`, `UPDATE`, `INSERT` and `DELETE` against tenant A's rows all yield zero rows or a policy error. Run as a non-owner role with `FORCE RLS`. A CI check fails the build if any table lacks RLS or lacks a `WITH CHECK` **and is not on the explicit exemption list** `[D-70]`, `[D-68]` — the list is asserted, so a new table cannot be exempted silently. |
| Auth | Argon2id verification round-trip; assert the plaintext key is never persisted or logged; assert unknown, revoked and disabled-tenant keys are indistinguishable in timing and response `[D-68]`. |
| Queue | Force a redelivery and assert exactly one provider render and one charge result `[D-67]`. Assert `XAUTOCLAIM` recovers a stalled entry. |
| Constraints | Assert each CHECK rejects its violation: `total_duration_s != 40`, `duration_s != 10`, `repairs_used = 3`, `attempt_no = 4`, scores outside `[0,1]`. |
| Seed honesty | Assert `seed` accepts NULL with `seed_supported = false`, and that the delivered reproducibility record reports the limitation rather than a fabricated value `[D-59]`. |
| Cost reconciliation | Assert a provisional `credits_charged` is settled exactly once at terminal status, that a refund decreases `cost_usd`, and that `cost_is_final` ends true `[D-60]`. |
| Immutability | `UPDATE continuity_bible` raises; `DELETE` cascades only with the job. |
| Idempotency | Concurrent inserts with one key → exactly one row. Flush Redis mid-flight → still exactly one row (Postgres constraint holds). |
| Reconciliation | Duplicate `request_fingerprint` insert → constraint violation → adoption path → exactly one charge. |
| Migrations | Every migration is applied and rolled back in CI against a seeded database. An **old-code / new-schema** compatibility test runs the previous release's code against the new schema, which is what expand/contract actually promises `[CPS §Rollout]`. |
| Checkpoint round-trip | `JobState` serialise/deserialise fidelity, including across a schema version bump; assert no media bytes and no URL-shaped strings are present. |
| Artifacts | Checksum verified end to end; corrupt an object and assert `VA-STORE-004`; assert storage keys are tenant-prefixed. |
| Presign | Assert URLs are never persisted or logged; assert expiry is enforced. |
| Redaction | Assert no log line or trace attribute contains the raw prompt, a credential, a presigned URL or media bytes. |
