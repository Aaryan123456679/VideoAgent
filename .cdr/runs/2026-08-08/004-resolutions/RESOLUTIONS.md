# OPEN_QUESTION resolutions — 2026-08-08

Resolutions for the ten `OPEN_QUESTIONS` raised by planning run `003-planner`.
These are binding inputs to implementation. Where a resolution contradicts an existing
canonical doc, **the doc is wrong and must be amended** — the amendment is named below.

Scope decision taken at the same time: **v1 overnight target is E0 (M0) + E1 (M1–M2) +
E2 (M3)** — foundation, job lifecycle, planning, continuity bible, Magic Hour adapter,
frame chaining, assembly and delivery. E3 (QC loop, partial results, resume) and E4
(observability, cost caps, load + chaos) are deferred, not cancelled.

---

## Q1 — `credits_per_usd` (blocking) → **RESOLVED**

Magic Hour bills in credits at a published, **tier-dependent** rate (per 1,000 credits):
Starter `$0.900` · Creator `$1.200` · Pro `$1.950` · Business `$2.500`. Volume discounts
step the rate down in brackets from 100,001 credits/month.

**Resolution.** Add `MAGICHOUR_USD_PER_1K_CREDITS` to the configuration contract, default
`0.90` (Starter). Derive `credits_per_usd` from it; never hard-code either.

**Volume discounts are deliberately ignored** when evaluating the USD cap. Applying them
would *lower* the computed cost of a job and let it run further before tripping the cap.
Using the undiscounted rate over-estimates spend, so the cap trips early rather than late.
A budget cap that errs toward under-spending is correct; one that errs toward over-spending
is not a cap. Record the discount as a reconciliation-time credit, never as a pre-flight
allowance.

`price_per_second` on `ProviderProfile` has the same defect and the same fix: it is derived
from the configured rate, not a literal.

New decision: **`D-65`**.

## Q2 — QC calibration set (blocking) → **DEFERRED, EXPLICITLY**

The ≥200-pair labelled set cannot be produced overnight and needs real credit spend.

**Resolution.** Build the calibration harness; do not run it. Ship the threshold
uncalibrated and say so in the docs — an uncalibrated threshold that is labelled as such is
honest; one presented as validated is not. Blocks nothing in the overnight scope because
E3 is deferred.

New decision: **`D-66`**.

## Q3 — Job queue transport (blocking) → **RESOLVED**

**Resolution.** Redis Streams with consumer groups. Redis 7 is already mandated by the
platform spec for locks, idempotency and progress, so this adds no dependency.

Delivery is **at-least-once**, which means a job step can be delivered twice. This is safe
only because `D-24` already requires it to be: the `shot_attempt.request_fingerprint`
uniqueness constraint plus `provider_project_id` reconciliation via
`GET /v1/video-projects/{id}` make a redelivered step re-read the existing provider job
rather than submit a new paid one. At-most-once was rejected — it drops work on a worker
crash, and a dropped shot on a paid, partially-billed job is worse than a duplicate that
the fingerprint check collapses.

New decision: **`D-67`**.

## Q4 — Authentication scheme (blocking) → **RESOLVED**

**Resolution.** Static per-tenant API keys presented as `Authorization: Bearer <key>`.
Keys are stored **hashed** (Argon2id) in `tenant_api_key`; the plaintext is shown once at
issuance and never again. `require_tenant` resolves the key to
`Principal{tenant_id, key_id}` and sets the Postgres RLS session variable from
`tenant_id` — never from anything in the request body or path.

OAuth/OIDC is out of scope for v1. It changes who issues identity, not how RLS consumes it,
so it can be added later behind the same `Principal` without touching any query.

New decision: **`D-68`**.

## Q5 — Music library (non-blocking) → **RESOLVED**

**Resolution.** v1 ships **no bundled music library**. The optional music bed accepts a
caller-supplied audio artifact reference; absent one, the bed is omitted and the field is
absent from the manifest. Licensing a library is a business decision, not an engineering
one, and shipping unlicensed audio is not an option.

New decision: **`D-69`**.

## Q6 — The `tenant` table (blocking) → **RESOLVED**

`tenant_id NOT NULL` appears on every table and `D-51` denormalises it so RLS never joins,
but no `tenant` table was ever defined — so the column referenced nothing and per-tenant
budget config had nowhere to live.

**Resolution.** Define it:

```sql
CREATE TABLE tenant (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT        NOT NULL,
    -- per-tenant budget overrides; NULL means "inherit the global cap"
    max_usd_per_job NUMERIC(10,4),
    retention_days  INTEGER     NOT NULL DEFAULT 30,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at     TIMESTAMPTZ
);
```

Every `tenant_id` column becomes `REFERENCES tenant(id)`. `tenant` itself is **not**
RLS-protected — it is the table RLS is defined *in terms of*, and protecting it with the
policy it bootstraps is circular. It is reachable only by the migration role and the
admin path, never by a tenant-scoped connection.

`D-51`'s denormalisation is unaffected: the FK is for referential integrity, RLS still
reads the local `tenant_id` column and never joins.

New decision: **`D-70`**.

## Q7 — Is `0.75` a constant or configuration? (blocking) → **RESOLVED: configuration**

`qc.md` declared `CONTINUITY_THRESHOLD: Final[float] = 0.75` and stated it is "a product
commitment, not a tunable". `.env.example` exposes `QC_ACCEPT_THRESHOLD`.

**Resolution.** It is **configuration**, sourced from `QC_ACCEPT_THRESHOLD`, default
`0.75`. `qc.md` must be amended to drop the "not a tunable" claim.

Rationale: `Q2` defers calibration, and a threshold that has never been calibrated against
a labelled set cannot honestly be frozen as a compile-time commitment. Making it
configurable is what *allows* it to be corrected once calibration runs, without a code
change and a redeploy. `0.75` remains the default and the number the product commits to;
configurability is how it gets validated, not a licence to quietly lower it. Any value
other than the default must be logged at startup and surfaced on the job manifest so a
loosened gate can never be invisible.

New decision: **`D-71`**, superseding the `qc.md` constant.

## Q8 — Prompt authorship and bootstrap (non-blocking) → **RESOLVED**

**Resolution.** The four registry prompts are authored **in-repo** under `prompts/` as
versioned files and are the source of truth. On startup the application registers any
prompt absent from Langfuse. A fresh checkout with no Langfuse connection therefore still
runs, reading the in-repo copies directly. Langfuse is the observability and
version-tracking surface, not a hard runtime dependency for prompt retrieval — making it
one would mean a Langfuse outage stops all video generation.

New decision: **`D-72`**.

## Q9 — The "zero deliverable" metric (non-blocking) → **RESOLVED**

HLD §11 defined it as FAILED/FAILED_NO_PROGRESS **and zero artifacts**, but `assembly.md`
§5 returns plan and bible JSON even in the total-failure case — which are artifacts. Read
literally the metric always reads 0% and the `< 1%` target can never be exceeded.

**Resolution.** "Zero deliverable" means **no playable video artifact** — no stitched MP4
and no individual shot clip. Plan and bible JSON explicitly do **not** count. A metric that
cannot fail measures nothing, and this one is a headline PRD commitment.

New decision: **`D-73`**, amending HLD §11.

## Q10 — `CreateJobRequest.webhook_url` (non-blocking) → **RESOLVED: removed**

Declared in `api.md` §2.2 with no payload shape, retry policy, signing or failure
behaviour, and no subtask implements it.

**Resolution.** **Remove the field from v1.** A declared-but-inert field in a public API
contract is worse than an absent one: callers integrate against it and silently never
receive callbacks. Job completion is observable via `GET /v1/jobs/{id}`. If outbound
webhooks are wanted later they need their own design — signing, retry, replay protection —
and that is a feature, not a field.

Note this is *our* outbound webhook to the caller. It is unrelated to Magic Hour's inbound
`video.completed` webhook, which stays available as an alternative to polling.

New decision: **`D-74`**.

---

## Two resolutions the planner made in-plan, confirmed here

- **`D-34` MCP capability discovery at startup** — the v1 Magic Hour adapter is REST and has
  no discovery endpoint. Static capability profile plus a startup validation that the
  configured model actually permits the configured shot duration. Confirmed.
- **Repair of shot 0 has no anchor frame** — `providers.md` §7.1's `text-to-video` vs
  `image-to-video` routing is ambiguous for exactly that case. Pinned: shot 0 always routes
  to `text-to-video`, including on repair, because it has no predecessor frame by
  definition. `S2.2.3`'s test spec asserts it.
