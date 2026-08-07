---
doc: LLD
module: qc
title: QC — continuity scoring, threshold and repair policy
status: canonical
implementation_status: deferred
version: 1
last_synced_commit: 438138573aa69c26727651b368e056262908bc69
generated_by: cdr-documentation
run_id: 2026-08-08/001-documentation
sources:
  - docs/specs/common-platform-spec.md
  - docs/specs/video-agent-prd.md
  - docs/HLD.md
---

# LLD — `qc`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — DEFERRED.** **E3 — DEFERRED, not cancelled.** None of this module runs in the v1 build. The design is complete and the calibration harness is built, but **the calibration is not run** `[D-66]` and the QC/repair loop is not wired into the graph. Read this document as a specification, not as a description of the running system.
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

> A vision model scores each shot against the bible; failures regenerate that shot only,
> **capped at 2 attempts**. `[PRD §How it works 5]`

This module is the *detective and corrective* half of the continuity thesis. It:

- scores a generated clip against the locked `ContinuityBible`, per dimension, with rationale;
- produces a single `continuity_score` and compares it to the **0.75** threshold;
- produces the **repair delta** that a regeneration will use;
- is **calibrated on a labelled set**, because an unreliable QC wastes spend
  `[PRD §Key risks]`.

It does **not** decide the next node. It emits a score; [`graph.md`](./graph.md) compares it
to a threshold this module owns, under the harness veto. The model never controls the loop.
`[CPS §Agent harness]`

## 2. Public interface

```python
# CONFIGURATION, not a compile-time constant.  [D-71] supersedes the former
# `CONTINUITY_THRESHOLD: Final[float] = 0.75`.
CONTINUITY_THRESHOLD: float = env.float("QC_ACCEPT_THRESHOLD", default=0.75)
MAX_REPAIRS_PER_SHOT: int   = env.int("QC_MAX_REPAIR_ATTEMPTS", default=2)  # [D-01]

class Dimension(StrEnum):
    """One per ContinuityBible dimension, plus two intra-shot integrity checks."""
    CHARACTER      = "character"
    WARDROBE       = "wardrobe"
    LOCATION       = "location"
    LIGHTING       = "lighting"
    PALETTE        = "palette"
    LENS_LANGUAGE  = "lens_language"
    BEAT_FIDELITY  = "beat_fidelity"      # does the described action actually happen? [D-35]
    INTEGRITY      = "integrity"          # no scene cut, no caption, no extra character [D-27]

class DimensionScore(BaseModel):
    dimension: Dimension
    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=400)   # untrusted model text; data, never instruction
    evidence_timestamps_s: list[float] = []

class QCFinding(BaseModel):
    dimension: Dimension
    severity: Literal["minor", "major", "blocking"]
    description: str
    corrective_hint: str

class QCReport(BaseModel):
    job_id: UUID; shot_index: int; attempt_id: UUID
    continuity_score: float                  # aggregate; see §3.3
    dimension_scores: list[DimensionScore]
    findings: list[QCFinding]
    passed: bool                             # continuity_score >= CONTINUITY_THRESHOLD
    hard_fail: bool                          # a blocking finding; see §3.4
    model_alias: str = "vision-default"
    prompt_version: str
    frames_sampled_s: list[float]
    cost_usd: Decimal
    degraded: bool                           # true if scored under a fallback/partial policy

async def score_shot(clip: ArtifactRef, bible: ContinuityBible, beat: Beat,
                     reference_frame: ArtifactRef | None, *,
                     ctx: NodeContext) -> QCReport: ...

async def build_repair_delta(report: QCReport, *, ctx: NodeContext) -> RepairDelta: ...

def failure_signature(report: QCReport) -> FailureSignature: ...   # [D-02], [D-18]
```

## 3. Scoring

### 3.1 What is scored
Frames sampled from the clip, plus the **reference frame** — the conditioning frame the shot
was chained from — so identity drift is measured against the actual anchor rather than
against a text description of it. Sampling: first frame, last frame, and evenly spaced
interior frames (default 5 total). First and last are always included because drift usually
accumulates toward the end of a clip, and the last frame is the one that becomes the next
shot's anchor. `[D-36]`

### 3.2 How
One `vision-default` call `[CPS §Model routing]` with structured output against
`QCReport`. The reference text is `render_bible_block(bible)` — the **same renderer** the
generation prompt used, defined in [`planning.md`](./planning.md) — so QC scores against
exactly the target the generator was given. `temperature = 0`.

Clip bytes never enter state, logs or traces; frames are passed by artifact reference and
fetched inside the gateway. `[CPS §Observability]`

### 3.3 Aggregation

```python
WEIGHTS = {
    Dimension.CHARACTER:     0.30,   # identity is the product claim
    Dimension.WARDROBE:      0.15,
    Dimension.LOCATION:      0.15,
    Dimension.LIGHTING:      0.10,
    Dimension.PALETTE:       0.10,
    Dimension.LENS_LANGUAGE: 0.05,
    Dimension.BEAT_FIDELITY: 0.10,
    Dimension.INTEGRITY:     0.05,
}
continuity_score = sum(WEIGHTS[d] * score[d] for d in Dimension)
```

Weights are `[D-37]` — the PRD names a single continuity score but not its composition.
`CHARACTER` dominates because "the protagonist changes face" is the failure the PRD opens
with `[PRD §The problem]`. Weights live in config and are re-fitted during calibration (§5).

### 3.4 Hard fails
Some defects are not weighable. If any of these is present, `hard_fail = True` and
`continuity_score` is clamped to `min(score, 0.50)` so it cannot pass on the strength of
other dimensions: a scene cut inside the shot · a second character where the bible allows one
· burned-in text or captions · an aspect-ratio or resolution change · a black or corrupt
clip. `[D-27]`, `[D-38]`

## 4. The 0.75 threshold

`[PRD §Success metrics]` states the target *"Jobs with continuity score ≥ 0.75: ≥ 85%"*.
A metric target is not, by itself, a gate — but the PRD's QC step requires a pass/fail
decision and names no other number. Resolution `[D-39]`:

- **0.75 is the acceptance gate.** `score >= 0.75` → shot accepted; below → repair.
- **A shot that exhausts its repairs below 0.75 is still delivered**, marked `abandoned`,
  with the job flagged `degraded`. This is why the fleet target is 85% and not 100%: the
  remaining ~15% are jobs that ship a below-threshold shot rather than shipping nothing,
  which is exactly "never returns nothing" `[PRD §Resilience]`.
- **Job-level continuity score is the minimum across shots** `[D-15]`, so one broken shot
  cannot be averaged away.

Had the gate been set above the metric, the metric would be unmeasurable; below, the gate
would be weaker than the published promise. 0.75 is the only self-consistent choice.

### 4.1 The threshold is configuration `[D-71]`

An earlier version of this document declared `CONTINUITY_THRESHOLD` a `Final` constant and
called it "a product commitment, not a tunable". **That was wrong and is superseded.** The
threshold is read from `QC_ACCEPT_THRESHOLD`, default `0.75`.

The reason is `[D-66]`: calibration is deferred, and **a threshold that has never been
validated against a labelled set cannot honestly be frozen at compile time**. Configurability
is what allows it to be corrected once calibration runs, without a code change and a
redeploy.

Configurable is not the same as negotiable. `0.75` remains the default and the number the
product commits to `[PRD §Success metrics]`, and two guards keep a loosened gate visible:

1. **Any non-default value is logged at startup**, at `warn`, naming the configured value and
   the default it departs from.
2. **Any non-default value is surfaced on the job manifest**, so a consumer of a delivered
   video can see the gate it was accepted under.

A gate can therefore be lowered — deliberately, and never invisibly.

## 5. Calibration

*"QC itself unreliable → wasted spend. Mitigation: calibrate on a labelled set; cap
attempts."* `[PRD §Key risks]` — a first-class deliverable, not a nice-to-have.

> **Implementation status: the harness is built; the calibration has not been run.** `[D-66]`
> The ≥200-pair labelled set cannot be produced in the v1 build window and requires real
> credit spend. **The shipped threshold is therefore uncalibrated, and this document says so
> rather than implying it was validated.** An uncalibrated threshold labelled as such is
> honest; one presented as validated is not. Nothing in the v1 scope is blocked, because this
> whole module is deferred to **E3**. The targets below are the acceptance criteria for the
> calibration run *when it happens*, not claims about the shipped system.

| Element | Definition |
| --- | --- |
| Labelled set | ≥ 200 shot pairs (clip + its anchor frame + bible), each human-labelled per dimension and overall pass/fail, spanning obvious passes, obvious failures and borderline cases |
| Stored | Versioned in the eval repository, referenced by hash from the QC prompt version |
| Metrics | Precision and recall of `passed` against human labels; Spearman correlation of `continuity_score` with human score; **false-pass rate** and **false-fail rate** |
| Targets `[D-40]` | false-pass ≤ 0.10 (a bad shot shipped), false-fail ≤ 0.20 (a good shot repaired). Asymmetric on purpose: a false pass breaks the product; a false fail costs one regeneration |
| Cadence | On every QC prompt change, every `vision-default` alias change, and nightly in CI — **once the set exists** `[D-66]` |
| Gate | A calibration regression `> 3%` blocks the merge `[CPS §Non-negotiables]` |
| Fitting | Weights (§3.3) and the hard-fail list are re-fitted on the labelled set. The **threshold may also be corrected** from its `0.75` default as a result — that is the point of making it configuration `[D-71]`, and any departure from the default is logged and surfaced per §4.1 |

Human ratings and QC scores are both written as Langfuse scores on the job trace, so drift
between them is visible without a separate pipeline. `[CPS §Observability]`

## 6. Repair policy

**Capped at 2 repair attempts per shot** — at most 3 generations. `[PRD §How it works 5]`, `[D-01]`

```
attempt 1 (initial)  → qc → pass? accept : repair 1
repair 1             → qc → pass? accept : repair 2
repair 2             → qc → pass? accept : ABANDON (keep the best attempt)
```

Rules:

1. **Best-attempt retention.** The highest-scoring attempt is kept even when abandoned, and
   is what partial assembly uses. Never discard a paid-for clip.
2. **The bible is never modified.** A repair changes the *prompt delta*, never the bible —
   it is immutable for the life of the job. `[PRD §How it works 2]`
3. **Same anchor frame.** A repair re-uses the failed attempt's conditioning frame, so the
   only changed variable is the delta.
4. **A repair must change something the provider actually reads.** The v1 provider offers
   **no seed control** `[D-59]`, so the changed variable is the **prompt delta**; upstream
   non-determinism supplies the rest. A repair that would send a byte-identical request is
   never issued — it is spend with zero expected value. `[D-41]` *(This rule originally said
   "different seed"; it was amended when the provider changed.)*
5. **Delta targets only failing dimensions**, ordered by `weight × (1 − score)`, produced by
   `reasoning-fast` `[D-07]` from the findings. Additive corrective guidance, never a rewrite.
6. **Blocking findings skip straight to a targeted delta** (e.g. "single continuous take, no
   cut") rather than a general nudge.
7. **No repair after a content-policy rejection.** It will be rejected again. Abandon and
   continue. `[D-42]`
8. **Early abandonment on no progress.** If a repair yields the same failing dimension set
   within the same 0.05 score band, the shot is abandoned immediately even with a repair
   remaining — that is a shot-scope repeated failure signature `[D-02]`, `[D-18]`, and the
   PRD's own named mitigation for wasted spend.
9. **The budget can pre-empt any repair.** `harness.decide()` runs before the back-edge is
   taken. `[CPS §Non-negotiables]`

## 7. Dependencies

| Depends on | For |
| --- | --- |
| [`gateway.md`](./gateway.md) | `vision-default` and `reasoning-fast` calls, prompt registry |
| [`planning.md`](./planning.md) | `ContinuityBible`, `Beat`, `render_bible_block` |
| [`persistence.md`](./persistence.md) | QC reports and dimension scores on `ShotAttempt`; artifact reads |
| [`harness.md`](./harness.md) | failure-signature registration, budget, untrusted quarantine |
| [`observability.md`](./observability.md) | Langfuse scores, spans, cost |

Consumed by [`graph.md`](./graph.md). Must not import `providers` — QC judges output without
knowing who produced it, so a provider swap cannot bias the score.

## 8. Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| `vision-default` group unavailable | Gateway circuit `VA-GW-001` | **Do not auto-pass and do not auto-fail.** Accept the shot provisionally with `degraded=true`, reason `qc_unavailable`, and score `null`. The job continues and the user is told QC did not run. Auto-passing hides breakage; auto-failing burns the budget on unverifiable repairs. `[D-43]` |
| QC response unparseable | Schema validation | Gateway's single reformat, then `VA-QC-003`; treat as the `qc_unavailable` path above. |
| Model returns out-of-range scores | Validator | Clamp to `[0,1]`, record an anomaly; if repeated, a calibration alarm. |
| Clip unreadable / zero bytes | ffprobe before scoring | `hard_fail`, score 0, no vision call — do not pay to look at a broken file. |
| QC disagrees with human labels beyond target | Nightly calibration | Alarm and block promotion of the QC prompt/model. `[CPS §Rollout]` |
| Repair loop with no improvement | Score-band signature | Abandon the shot early. `[D-18]` |
| All four shots below threshold | Job state at `assemble` | Deliver the partial with the best attempts, `PARTIAL` + `degraded`. Still not zero deliverable. |
| QC rationale contains instructions | Harness quarantine | Rationale is data. It is rendered into the repair delta inside a delimited block and never as instruction. `[CPS §Non-negotiables]` |
| QC cost creeping | Per-job QC cost span | QC is bounded at 12 calls per job (4 shots × 3 attempts) and is pre-flight budget-checked like any other call. |

## 9. Test strategy

| Level | Tests |
| --- | --- |
| Calibration (primary, **deferred — E3**) | The labelled set is the main test asset. Assert false-pass ≤ 0.10, false-fail ≤ 0.20, Spearman ≥ target. CI fails on a `> 3%` regression `[CPS §Non-negotiables]`. **Not run in v1** `[D-66]`: the harness and fixtures are built and the suite is skipped with an explicit reason, never silently absent. |
| Golden | Fixed clip/bible pairs with expected score bands, so prompt edits show their effect immediately. |
| Threshold | Boundary tests at 0.7499 / 0.7500 / 0.7501 against the **configured** value; assert it is read from `QC_ACCEPT_THRESHOLD` in exactly one place and that no `0.75` literal is used as a gate elsewhere. Assert a non-default value is logged at startup **and** appears on the job manifest `[D-71]`. |
| Repair cap | Force perpetual failure; assert exactly 2 repairs, 3 generations, then abandonment, then the job continues to the next shot. |
| No-progress | Assert `+0.04` in the same band abandons early and `+0.06` continues repairing. |
| Determinism | Same clip scored twice at `temperature=0` varies by ≤ 0.05; larger variance is a calibration alarm. |
| Hard fails | Synthetic clips with a mid-clip cut, a caption overlay, an extra character, a black frame; assert `hard_fail` and the 0.50 clamp. |
| Degradation | Vision alias down → provisional acceptance, `degraded=true`, score `null`, job continues, user informed. |
| Adversarial | QC rationales containing instruction-shaped text; assert no control-flow effect. |
| Independence | Assert `qc` imports nothing from `providers`; assert the score for identical inputs is unchanged when `provider_key` differs. |
