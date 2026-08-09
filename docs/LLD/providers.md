---
doc: LLD
module: providers
title: Providers — video provider abstraction, capability negotiation, frame chaining
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

# LLD — `providers`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — BUILT.** **E2 — in the v1 build.** The Magic Hour adapter, capability validation and frame chaining all ship.
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

Turn a locked bible plus one beat into one 10-second clip, through an abstraction that
survives a provider changing its API.

> Generate shots sequentially — via **Higgsfield MCP behind a provider abstraction**. Each
> prompt = bible + beat action + camera move. `[PRD §How it works 3]`
> **Provider abstraction** — capability negotiation plus failover, so an API change is not an
> outage. `[PRD §Resilience]`

Four concerns:

1. the `VideoProvider` protocol and its registry;
2. **capability negotiation** — pick a provider that can actually do what the shot needs;
3. **failover** within the capability group, under the inherited failure policy;
4. **prompt composition** and the **frame chaining contract**.

> ### Provider substitution — Higgsfield → Magic Hour `[D-58]`
>
> The PRD names **Higgsfield MCP** `[PRD §How it works 3]`. **Higgsfield exposes no free or
> trial API tier and no credential was obtainable for this build**, so the v1 provider is
> **Magic Hour** (`https://api.magichour.ai`) instead.
>
> The substitution is defensible for exactly one reason: **Magic Hour satisfies
> `IMAGE_CONDITIONING`** — `POST /v1/image-to-video` accepts a start-frame image — so **frame
> chaining is preserved, and with it the product's core value proposition**. A provider
> without start-frame conditioning would not have been substitutable at any price, per
> `[D-31]`.
>
> The swap is absorbed **entirely by this module**. No other module names a provider, no
> caller changed, and the alias-only rule meant this was a config-level change — which is
> precisely the property `[D-06]` and `[CPS §Model routing]` were designed to buy.
>
> Deviations this forces on the PRD are recorded honestly rather than hidden: **no seed
> control** `[D-59]`, **cost in credits rather than USD** `[D-60]`, **model pinned for the 10s
> shot length** `[D-61]`, and **720p as the configured v1 target** `[D-63]`.
>
> The pinned model itself was later amended from `wan-2.2` to `ltx-2.3` `[D-61, amended]` —
> same measured cost, same 10s support, but faster, and render/queue latency was the actual
> problem the amendment fixed, not cost.

Magic Hour is the first adapter, *not* the interface. The word `magichour` appears in exactly
one adapter module and in config — never in a caller. `[CPS §Model routing]`, `[D-06]`
Magic Hour also publishes an MCP integration, so the PRD's intent of an MCP-based provider
remains reachable; the v1 adapter uses the documented REST API, which is the surface the
published contract covers.

## 2. Public interface

```python
class Capability(StrEnum):
    IMAGE_CONDITIONING   = "image_conditioning"   # required for frame chaining
    END_FRAME_CONDITIONING = "end_frame_conditioning"  # optional; not offered by wan-2.2
    SEED_CONTROL         = "seed_control"         # NOT offered by Magic Hour  [D-59]
    NEGATIVE_PROMPT      = "negative_prompt"
    CAMERA_DIRECTIVE     = "camera_directive"
    ASPECT_16_9          = "aspect_16_9"
    RES_720P             = "res_720p"
    RES_1080P            = "res_1080p"            # ceiling, not a floor  [D-63]
    DURATION_10S         = "duration_10s"
    ASYNC_POLL           = "async_poll"
    WEBHOOK_CALLBACK     = "webhook_callback"

class ProviderProfile(BaseModel):
    provider_key: str                 # opaque registry key, e.g. "video-a"
    capabilities: frozenset[Capability]
    min_duration_s: float; max_duration_s: float
    allowed_durations_s: frozenset[float] | None   # None = continuous range  [D-61]
    max_resolution: Literal["480p", "720p", "1080p"]   # 1080p ceiling [PRD §Out of scope]
    cost_unit: Literal["usd", "credits"]           # Magic Hour bills credits  [D-60]
    price_per_second: Decimal                      # in cost_unit; DERIVED, never a literal [D-65]
    credits_per_usd: Decimal | None                # derived from MAGICHOUR_USD_PER_1K_CREDITS [D-65]
    typical_latency_s: float
    max_prompt_chars: int

class ShotRequest(BaseModel):
    job_id: UUID; shot_index: int; attempt_no: int
    prompt: str                       # composed by compose_prompt()
    negative_prompt: str | None
    conditioning_frame: ArtifactRef | None    # final frame of shot n-1  [PRD §How it works 4]
    duration_s: float = 10.0
    aspect_ratio: Literal["16:9"] = "16:9"
    resolution: Literal["720p", "1080p"] = "720p"    # MAGICHOUR_RESOLUTION  [D-63]
    seed: int | None = None           # None when the provider offers no seed control [D-59]
    request_fingerprint: str          # sha256(job, shot, attempt, prompt_hash, frame_id, seed?)
    timeout_s: float

class ShotResult(BaseModel):
    clip: ArtifactRef
    provider_key: str                 # observability only; callers must not branch on it
    provider_model: str               # e.g. "wan-2.2"
    provider_project_id: str          # Magic Hour video-project id; the reproducibility handle
    seed_used: int | None             # None => unsupported by provider, NOT "unknown" [D-59]
    duration_s: float
    resolution: str
    fps: int | None
    width: int | None; height: int | None
    cost_usd: Decimal                 # converted from credits at the configured rate [D-60]
    credits_charged: Decimal | None   # provisional until terminal status  [D-60]
    cost_is_final: bool               # False while the render is non-terminal
    latency_ms: int
    degraded: bool
    degrade_reason: str | None

class VideoProvider(Protocol):
    profile: ProviderProfile
    async def generate(self, req: ShotRequest, *, ctx: NodeContext) -> ShotResult: ...
    async def lookup(self, request_fingerprint: str) -> ShotResult | None: ...   # [D-24]
    async def health(self) -> ProviderHealth: ...

class ProviderRegistry(Protocol):
    def select(self, required: frozenset[Capability]) -> list[VideoProvider]: ...
    async def generate(self, req: ShotRequest, *, ctx: NodeContext) -> ShotResult: ...
```

`lookup()` is mandatory on the protocol. Without it, resume cannot tell a paid-for clip from
an unmade one, and "completed shots are never re-billed" `[PRD §Resilience]` cannot be
guaranteed across a crash. An adapter whose upstream offers no lookup implements it against a
local request-fingerprint table and declares the weaker guarantee. `[D-24]`

## 3. Capability negotiation

The registry never picks "the default provider". It picks a provider that satisfies the
shot's **required** capability set, ranked by preference.

```python
# SEED_CONTROL is deliberately NOT required: no available provider offers it. [D-59]
REQUIRED_ALWAYS = frozenset({Capability.DURATION_10S, Capability.ASPECT_16_9})

def required_for(shot: ShotRequest) -> frozenset[Capability]:
    req = set(REQUIRED_ALWAYS)
    req.add(Capability.RES_1080P if shot.resolution == "1080p"
            else Capability.RES_720P)               # ceiling, not floor  [D-63]
    if shot.conditioning_frame is not None:
        req.add(Capability.IMAGE_CONDITIONING)      # non-negotiable when chaining
    if shot.negative_prompt:
        req.add(Capability.NEGATIVE_PROMPT)
    return frozenset(req)
```

### 3.1 What Magic Hour declares

| Capability | Declared | Note |
| --- | --- | --- |
| `IMAGE_CONDITIONING` | **yes** | `POST /v1/image-to-video` with `assets.image_file_path`. **This is the capability the substitution turned on** `[D-58]`, and it is never waivable `[D-31]` |
| `DURATION_10S` | **yes**, model-dependent | The pinned `ltx-2.3` and the earlier-pinned `wan-2.2` both allow 10s. `sora-2` allows only 4, 8, 12, 24, 36, 48, 60 — it **cannot** produce 10s `[D-61, amended]` |
| `ASPECT_16_9` | yes | |
| `RES_720P` / `RES_1080P` | yes / yes | v1 configures 720p (`MAGICHOUR_RESOLUTION`) `[D-63]` |
| `ASYNC_POLL` | yes | `GET /v1/video-projects/{id}` |
| `WEBHOOK_CALLBACK` | yes | `video.started` / `video.completed` / `video.errored`; preferred over polling |
| `SEED_CONTROL` | **no** | No seed parameter is documented. The PRD's reproducibility promise is partially unmet — recorded honestly, not faked `[D-59]` |
| `END_FRAME_CONDITIONING` | **no** on `wan-2.2` | `assets.end_image_file_path` exists on the endpoint but is unsupported by `wan-2.2` and `sora-2`. Unused in v1 |
| `NEGATIVE_PROMPT` | **no** | No negative-prompt field. Constraints fold into the positive prompt and the result is flagged `degraded`, per rule 2 below |

Rules:

1. **`IMAGE_CONDITIONING` is never waived when a conditioning frame exists.** Silently
   dropping it produces a clip that looks fine in isolation and breaks the product's only
   value proposition. If no provider in the group offers it, the shot fails with
   `VA-PROV-002` rather than generating an unchained clip. `[D-31]`
2. `NEGATIVE_PROMPT` **may** be waived: the constraints are folded into the positive prompt
   and the result is flagged `degraded`. Always flagged. `[CPS §Failure behaviour]`
3. Ranking is by (capability superset, then configured preference, then price, then latency).
   Deterministic — two workers pick the same provider for the same shot.
4. Selection is recorded on the `ShotAttempt` and as a span attribute, so a continuity
   regression can be attributed to a provider swap.
5. **`SEED_CONTROL` is requested but never required.** No available provider offers it, so
   requiring it would fail every shot. Where it is absent, `seed_used` is `None` and the
   `ShotAttempt` records `seed_supported = false` — an explicit statement that the provider
   has no seed, **not** a null that could be mistaken for "we forgot to record it". `[D-59]`
6. **Duration is validated against `allowed_durations_s`, not just the min/max range.** A
   model can advertise 1–60s and still refuse 10s — `sora-2` does exactly that. Validation
   happens at **startup** against the configured `MAGICHOUR_MODEL`, so a bad model choice
   fails the deploy rather than every job. `[D-61]`

## 4. Failover

Within the capability group, under the inherited policy `[CPS §Failure behaviour]`, shared
with [`gateway.md`](./gateway.md) so the two egresses cannot drift:

- **Retry** — exponential backoff + jitter, retryable errors only, **max 3**, reusing
  `request_fingerprint` so a deduplicating upstream does not double-bill.
- **Fallback** — the next provider in the ranked list, each with its own retry budget.
- **Circuit break** — per provider, **5 failures in 30s**; state in Redis, shared across
  workers.
- **Degrade** — a waivable capability dropped, or 720p accepted when 1080p is unavailable —
  **always flagged**, never silent.
- **Fail honestly** — `VA-PROV-005` when the group is exhausted, naming what was preserved
  (earlier accepted shots) and what to do next (`resume`).

A provider switch **mid-job** is itself a continuity risk: two providers rarely render the
same face. The registry therefore pins the provider chosen for shot 0 for the whole job and
fails over only when that provider cannot serve the shot at all. A failover that changes
provider mid-job sets `degraded=true` with reason `provider_switch_mid_job`, and QC will
usually catch the resulting drift. `[D-32]`

## 5. Prompt composition

> Each prompt = **bible + beat action + camera move**. `[PRD §How it works 3]`

```python
def compose_prompt(bible: ContinuityBible, beat: Beat, *,
                   repair_delta: str | None = None,
                   max_chars: int) -> ComposedPrompt: ...
```

Fixed section order — deterministic, so `prompt_hash` is reproducible `[PRD §What's delivered]`:

```
[1] CONTINUITY BIBLE   render_bible_block(bible)      ← identical bytes in every shot
[2] BEAT ACTION        beat.action
[3] CAMERA             beat.camera_move (+ lens_language.movement_style)
[4] CONTINUITY NOTE    beat.continuity_note           ← what must carry over
[5] REPAIR DELTA       corrective guidance, repairs only          [D-07]
[6] NEGATIVE           bible.negative_constraints (+ repair negatives)
```

- Section [1] is **byte-identical across all four shots**, produced by the single renderer in
  [`planning.md`](./planning.md). Identical bytes are what make the bible a constant rather
  than a paraphrase that drifts shot to shot.
- **Truncation policy** when `max_prompt_chars` binds: drop from section [4], then [3], then
  compress [2]. **Never** truncate [1] or [6]. A truncated bible is a broken bible; if [1]
  alone exceeds the limit the shot fails `VA-PROV-006` rather than generating against a
  partial bible. `[D-33]`
- The **repair delta** is produced by `reasoning-fast` from the QC findings `[D-07]` and is
  additive corrective guidance ("the jacket must be the same olive canvas as the reference
  frame"), never a rewrite of the bible. The bible is immutable. `[PRD §How it works 2]`
- The composed prompt and its hash are stored on the `ShotAttempt`, together with the model,
  the cost and the `provider_project_id`. The PRD asks for "per-shot cost, model, **seed** and
  prompt — every job is reproducible" `[PRD §What's delivered]`; **Magic Hour documents no seed
  parameter**, so the seed leg of that promise is unmet and is recorded as
  `seed_supported = false` rather than faked. What we can still guarantee is *traceability*
  (exactly which model, prompt and cost produced this clip, and which upstream project id) —
  not *bit-exact re-rendering*. `[D-59]`

## 6. Frame chaining contract

> The final frame of shot *n* conditions shot *n+1*, so identity carries forward.
> `[PRD §How it works 4]`

| Term | Definition |
| --- | --- |
| Producer | [`assembly.md`](./assembly.md)'s `extract_final_frame`, which extracts the **last decodable** frame of an **accepted** clip as PNG at native resolution |
| Consumer | `generate_shot` for shot *n+1*, passing it as `conditioning_frame` |
| Advance rule | `last_good_frame` advances **only** on QC acceptance `[D-05]` |
| Shot 0 | No conditioning frame. Text-only from the bible; this is expected, not degraded |
| After abandonment | Chain from the most recent accepted frame; if none, text-only **and** `degraded=true` `[D-05]` |
| Repairs | A repair of shot *n* re-uses the **same** conditioning frame as the failed attempt. Changing the anchor mid-repair confounds QC — the repair would be testing two variables |
| Capability | `IMAGE_CONDITIONING` is required whenever a frame is present and is never waived `[D-31]` |
| Transport | By `ArtifactRef`. The adapter fetches bytes at the boundary and uploads them via `POST /v1/files/upload-urls`, passing the returned `file_path` as `assets.image_file_path` `[D-64]`. Frame bytes never enter state, logs or traces `[CPS §Observability]` |
| Shot 0 | Uses `POST /v1/text-to-video`; every other shot and every repair uses `POST /v1/image-to-video` |
| Fidelity | The frame is passed unmodified — no re-encode, no resize, no colour transform. Any transform is a continuity change the bible did not authorise |

## 7. Magic Hour adapter

The first concrete adapter, substituted for the PRD's Higgsfield MCP `[D-58]`. Only the facts
below are contractual; **nothing outside this section may be assumed about the upstream API.**

- **Base URL** `https://api.magichour.ai` (`MAGICHOUR_BASE_URL`).
- **Auth** `Authorization: Bearer <MAGICHOUR_API_KEY>`, keys formatted `mhk_live_...`. From
  the secret store, never in code, never logged. `[CPS §Observability]`
- Configuration comes from [`.env.example`](../../.env.example): `MAGICHOUR_API_KEY`,
  `MAGICHOUR_BASE_URL`, `MAGICHOUR_MODEL`, `MAGICHOUR_RESOLUTION`, `MAGICHOUR_WEBHOOK_SECRET`,
  `MAGICHOUR_USD_PER_1K_CREDITS` `[D-65]`.

### 7.1 Endpoints used

| Endpoint | Used for |
| --- | --- |
| `POST /v1/text-to-video` | **Shot 0 only — always, including on repair.** Shot 0 has no predecessor frame *by definition*, so a repair of it still has no anchor and must not be routed to `image-to-video` |
| `POST /v1/image-to-video` | **Shots 1–3, and repairs of those shots** — start-frame conditioned generation |
| `POST /v1/files/upload-urls` | Uploading the extracted continuity frame `[D-64]` |
| `GET /v1/video-projects/{id}` | Polling to a terminal status; source of `downloads[]` |
| webhooks | `video.started` / `video.completed` / `video.errored` — preferred over polling |

**`POST /v1/image-to-video`**

| Field | Required | Notes |
| --- | --- | --- |
| `end_seconds` | **yes** | float, min 1, max 60, **further constrained per model**. `ltx-2.3` (pinned): 3–30. `wan-2.2`: 3–10 and 15. `sora-2`: only 4, 8, 12, 24, 36, 48, 60 `[D-61, amended]` |
| `assets.image_file_path` | **yes** | `minLength 1`. Accepts **either a direct public URL or a `file_path` returned by the upload-URLs endpoint**, e.g. `api-assets/id/1234.png` |
| `assets.end_image_file_path` | no | Not supported by `wan-2.2` or `sora-2`. Unused in v1 |
| `model` | no | Enum below; v1 pins `ltx-2.3` (amended from `wan-2.2`) `[D-61, amended]` |
| `resolution` | no | v1 sends `MAGICHOUR_RESOLUTION` (720p) `[D-63]` |
| `style.prompt` | no | The composed prompt from §5 |
| `name` | no | Set to `job_id:shot_index:attempt_no` for support traceability |

`model` enum, verbatim: `default`, `ltx-2`, `ltx-2.3`, `wan-2.2`, `seedance-1.5`,
`seedance-2.0`, `seedance-2.0-mini`, `kling-2.5`, `kling-3.0`, `veo3.1`, `veo3.1-lite`,
`sora-2`, `kling-1.6`, `seedance`, `kling-2.5-audio`, `veo3.1-audio`.

**200 response:** `{ id, credits_charged }`. `id` is the video-project id, persisted as
`provider_project_id`. **`credits_charged` is an estimate until the render reaches a terminal
state** `[D-60]`.

### 7.2 Frame upload flow `[D-64]`

`image_file_path` would accept one of our own presigned artifact URLs, but we do not use that
path: a presigned URL is a bearer credential `[D-52]` that we would be handing to a third
party, and its TTL can expire mid-render. Instead:

```
POST /v1/files/upload-urls   {"items":[{"type":"image","extension":"png"}]}
  → {"items":[{upload_url, expires_at, file_path}]}      order matches the request
PUT <upload_url>  <raw PNG bytes>        keep the auth query params on the URL intact
POST /v1/image-to-video   assets.image_file_path = <file_path>
```

`type` ∈ `video|audio|image`; image extensions include png, jpg, jpeg, webp, heic, avif,
tiff, bmp. We always send `png`, because the anchor frame is lossless PNG `[D-44]`.
**Re-request the upload URL if `expires_at` has passed** — never retry a `PUT` against a
stale URL. Both `upload_url` and `downloads[].url` carry auth in the query string and are
therefore bearer credentials, covered by the never-logged rule `[D-52]`.

### 7.3 Polling and terminal states

`GET /v1/video-projects/{id}` returns `status` ∈ `draft`, `queued`, `rendering`, `complete`,
`error`, `canceled`. **Terminal:** `complete`, `error`, `canceled`. (`draft` is documented as
unused; the adapter treats it as non-terminal and alarms if it ever appears.)

On `complete` the response carries `downloads[]` of `{url, expires_at}`, plus
`credits_charged`, `fps`, `width`, `height`, `start_seconds`, `end_seconds`, and a nullable
`error` of `{code, message}`. `downloads[]` is **populated only after a successful render and
has no fixed TTL**, so the adapter reads `expires_at`, downloads immediately into our own
artifact store, and **never caches or persists the link** `[D-52]`.

Webhooks are preferred over polling where `MAGICHOUR_WEBHOOK_SECRET` is configured; polling
with backoff remains the fallback and the reconciliation path. Webhook payloads are
**untrusted content** — they are used only to trigger a re-read of
`GET /v1/video-projects/{id}`, never as the source of truth for status or cost.
`[CPS §Non-negotiables]`

The submit response's `id` is persisted with the `in_flight` `ShotAttempt` **before** polling
begins, so a crash mid-render is recoverable: `lookup()` re-reads the project by id rather
than re-submitting. `[D-24]`

### 7.4 Error mapping

Upstream errors return `{message}`. Mapped into the shared taxonomy at the boundary, so no
caller ever sees a provider-shaped error:

| Upstream | Code | Retryable | Behaviour |
| --- | --- | --- | --- |
| `400` invalid request | `VA-PROV-007` | no | Programming or config error; fail the shot and alarm |
| `401` unauthorized | `VA-PROV-008` | no | Credential fault → `ESCALATED`, not a job failure |
| **`402` payment required** | **`VA-PROV-009`** | **no** | **Account credits exhausted. Never retried — a retry cannot succeed and every attempt costs latency. Terminates `FAILED`/`ESCALATED`, preserving accepted shots** `[D-62]` |
| `404` not found | `VA-PROV-010` | no | Unknown project id; treat the attempt as orphaned |
| `422` unprocessable entity | `VA-PROV-011` | no | Usually a content or duration violation; no repair if content-related `[D-42]` |
| `429`, `5xx`, timeout | `VA-PROV-001` / `-003` | yes | Retry ≤3 with jitter → fallback → circuit break |
| terminal `status: error` | `VA-PROV-012` | no | Carries upstream `error.code` / `error.message`; a failed attempt, eligible for repair |
| terminal `status: canceled` | `VA-PROV-013` | no | Treated as a failed attempt |

- **All upstream response text is untrusted content.** The adapter validates against
  `ShotResult` and discards everything unmodelled; an upstream `message` field cannot
  influence the next prompt. `[CPS §Non-negotiables]`
- **Startup capability validation** `[D-34, amended]`. The v1 Magic Hour adapter is REST and
  exposes **no discovery endpoint**, so the `ProviderProfile` is a **static, in-repo profile**
  rather than a discovered one. What runs at startup is a **validation**: the configured
  `MAGICHOUR_MODEL` must appear in the model enum and its `allowed_durations_s` must permit
  the configured shot duration, and the profile must satisfy the required capability set. A
  model that cannot do 10s therefore fails the deploy rather than every job `[D-61]`. The
  original `[D-34]` wording assumed MCP tool discovery; that mechanism does not exist on this
  transport.
- **Multi-key rotation is a scoped exception to `[D-62]`, not a repeal of it.** `[D-62]` says a
  `402` is never retried because a retry against the *same* account cannot succeed — that
  reasoning is unchanged. `providers.magichour.RotatingApiKey` (wired in only when
  `Settings.magichour_api_keys()` configures a second credential, e.g. for demo/trial capacity
  across two accounts) advances to a *different* account specifically because a different
  balance can succeed where the first one's cannot. Single-key deployments see no behaviour
  change: `MagicHourProvider.key_rotator` defaults to `None`, and a `402` still terminates
  exactly as this section describes.

### 7.5 Cost accounting `[D-60]`

Magic Hour bills **credits**; `[CPS §Non-negotiables]` mandates a hard **USD** cap. The
adapter therefore:

1. converts using a rate **derived from `MAGICHOUR_USD_PER_1K_CREDITS`** (default `0.90`, the
   Starter tier) — never a guessed or hard-coded one. `price_per_second` is derived from the
   same value; a literal in either place is a defect `[D-65]`;
2. charges the ledger the **estimated** `credits_charged` from the submit response, marked
   `cost_is_final = false`;
3. **reconciles** against the terminal `credits_charged`, adjusting the ledger up or down;
4. **credits refunded on a failed render are reconciled back**, so an `error` or `canceled`
   render does not permanently consume budget it never used.

**Volume discounts are deliberately excluded from cap evaluation** `[D-65]`. Magic Hour steps
the per-1,000-credit rate down in brackets above 100,001 credits/month. Applying a discount
would *lower* a job's computed cost and let it run further before tripping the USD cap. The
undiscounted configured rate **over-estimates** spend, so the cap trips early rather than
late — a cap that errs toward under-spending is correct; one that errs toward over-spending
is not a cap. A discount is recorded as a **reconciliation-time credit**, never as a
pre-flight allowance.

This makes the ledger a **reconciling** ledger, not a purely accumulating one, which is a
change from the pure monotonic rule in [`harness.md` §4](./harness.md#4-budget-caps): the
ledger is monotonic **per finalised charge**, and a provisional charge may be corrected
exactly once when it becomes terminal. Pre-flight budget checks use the estimate, so an
under-estimate can never authorise a call the cap would have refused.

## 8. Dependencies

| Depends on | For |
| --- | --- |
| [`planning.md`](./planning.md) | `ContinuityBible`, `Beat`, `render_bible_block` |
| [`gateway.md`](./gateway.md) | `reasoning-fast` for the repair delta; the shared failure-policy engine |
| [`persistence.md`](./persistence.md) | `ShotAttempt` rows, artifact write, Redis circuit state |
| [`harness.md`](./harness.md) | budget pre-flight veto, cost charging, untrusted quarantine |
| [`observability.md`](./observability.md) | provider spans, cost, error taxonomy |

Consumed by [`graph.md`](./graph.md) only.

## 9. Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| No provider satisfies required capabilities | `select()` returns empty | `VA-PROV-002`, non-retryable. Fail rather than generate an unchained clip. `[D-31]` |
| Provider 5xx / timeout | HTTP or MCP error | Retry ≤3 with jitter, same fingerprint → fallback → circuit break. |
| Provider returns a clip of the wrong duration | Post-fetch probe | Accept within ±0.3s and let assembly normalise; outside that, treat as a failed attempt and let QC/repair handle it. |
| Provider returns a lower resolution than requested | Probe | Accept if at or above the configured target, else flag `degraded`. Below 480p, reject. v1 requests 720p `[D-63]`. |
| `402 Payment Required` | HTTP status | **Never retried** — the account is out of credits and a retry cannot succeed. `VA-PROV-009` → `FAILED`/`ESCALATED`, preserving every accepted shot `[D-62]`. |
| Upload URL expired before `PUT` | `expires_at` elapsed / upload rejected | Re-request a fresh upload URL; never retry a `PUT` against a stale URL `[D-64]`. |
| `credits_charged` differs from the estimate at terminal status | Reconciliation | Adjust the ledger; if the cap is now exceeded, terminate `PARTIAL` before the next shot `[D-60]`. |
| Render terminal as `error` / `canceled` | Poll or webhook | Failed attempt eligible for repair; reconcile any refunded credits back to the ledger `[D-60]`. |
| Configured model cannot produce 10s | Startup validation against `allowed_durations_s` | **Fail the deploy**, not the job. `sora-2` is the concrete trap `[D-61]`. |
| Webhook payload claims a status the API does not | Re-read `GET /v1/video-projects/{id}` | The webhook is untrusted; it only triggers a re-read and is never the source of truth. |
| Provider ignores the conditioning frame | QC catches the identity drift | Not detectable here; this is precisely why the QC loop exists. `[PRD §Key risks]` |
| Content policy rejection | Provider error | Non-retryable `VA-PROV-004`. No repair — a repair would repeat the rejection and burn budget. Abandon the shot and continue. |
| Group exhausted | All circuits open | `VA-PROV-005`. Preserve accepted shots; harness terminates `PARTIAL`, assembly delivers a partial. |
| Crash after submit, before commit | `in_flight` attempt on resume | `lookup(request_fingerprint)`; adopt the asset if it exists, charge once. Never blind-retry a paid call. `[D-24]` |
| Provider drops a capability after a deploy | Startup discovery diff | Alarm; jobs needing it fail over. Never silently degrade `IMAGE_CONDITIONING`. |
| Prompt exceeds `max_prompt_chars` | Length check | Truncate by policy §5; if section [1] alone exceeds, `VA-PROV-006`. |
| Provider offers no seed control | `SEED_CONTROL` absent from the profile | Record `seed_supported = false` and `seed_used = None`. Do **not** flag every shot `degraded` — this is a known, documented v1 limitation `[D-59]`, not a per-job degradation. It is disclosed once, on the job's reproducibility record. |
| Cost above the estimate | Post-charge reconciliation | Charge actual; if the ledger now exceeds the cap, terminate `PARTIAL` before the next shot. |

## 10. Test strategy

| Level | Tests |
| --- | --- |
| Protocol conformance | One shared suite every adapter must pass: capability truthfulness, `lookup()` idempotency, error-code mapping, `ShotResult` validity. A new provider is one adapter plus a green suite. |
| Static | CI grep: no provider name (`magichour`, `higgsfield`, any model from the enum) outside the adapter module and `config/`; no caller branches on `provider_key`. |
| Negotiation | Table test over capability sets; assert `IMAGE_CONDITIONING` is never waived and that `NEGATIVE_PROMPT` waiver always sets `degraded`. |
| Failover | Fake providers with scripted failures; assert retry counts, per-provider circuit isolation, and provider pinning within a job. |
| Idempotency | Simulate a crash after submit; assert `lookup()` adoption yields exactly one charge and one artifact. |
| Composition | Golden prompt fixtures; assert section order is fixed, section [1] is byte-identical across all four shots, and truncation never touches [1] or [6]. |
| Chaining | Assert the conditioning frame passed for shot *n+1* is exactly the artifact produced from the accepted shot *n*, unmodified (checksum equality). Assert a repair re-uses the same anchor frame. |
| Injection | MCP tool responses containing instruction-shaped text; assert they are discarded and never reach the next prompt. |
| Contract (recorded) | Recorded Magic Hour HTTP transcripts (`upload-urls`, `image-to-video`, `text-to-video`, `video-projects` across every `status` value) replayed in CI, so an upstream API change fails a test rather than a production job — the "an API change is not an outage" promise, made testable `[PRD §Resilience]`. |
| Cost | Assert credits→USD conversion derives from `MAGICHOUR_USD_PER_1K_CREDITS` and that no rate literal exists in the tree `[D-65]`; assert a provisional charge is reconciled **exactly once** at terminal status; assert a refunded `error`/`canceled` render returns its credits to the ledger `[D-60]`; assert a volume discount never reduces a **pre-flight** estimate `[D-65]`. |
| Duration guard | Startup validation test: configuring `sora-2` (which cannot do 10s) **fails the deploy**; `ltx-2.3` (pinned) and `wan-2.2` both pass `[D-61, amended]`. The profile is static and asserted against the documented enum — there is no discovery call to mock `[D-34, amended]`. |
| `402` handling | Assert `402` produces zero retries, maps to `VA-PROV-009`, terminates `FAILED`/`ESCALATED`, and preserves every accepted shot `[D-62]`. |
| Upload flow | Assert the `upload-urls` response order matches the request order; assert an expired `expires_at` triggers a fresh URL rather than a retry of the stale `PUT`; assert `file_path` (not our own presigned URL) is what reaches `image_file_path` `[D-64]`. |
| Credential leakage | Assert `upload_url` and `downloads[].url` never appear in a log line, span attribute or persisted row — they carry auth in the query string `[D-52]`. |
| Shot 0 routing | Assert shot 0 uses `text-to-video` **on the initial attempt and on every repair**, and that shots 1–3 and their repairs use `image-to-video`. |
| Seed honesty | Assert `seed_supported = false` is recorded and that the delivered reproducibility record states the limitation rather than emitting a null or a fabricated seed `[D-59]`. |
| Webhooks | Assert a webhook only triggers a re-read of `GET /v1/video-projects/{id}` and is never trusted as the source of status or cost; assert signature verification against `MAGICHOUR_WEBHOOK_SECRET`. |
