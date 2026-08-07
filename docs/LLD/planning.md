---
doc: LLD
module: planning
title: Planning — StoryPlan generation and ContinuityBible lock
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

# LLD — `planning`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — BUILT.** **E1 — in the v1 build.** `StoryPlan` generation and the `ContinuityBible` lock both ship.
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

The two nodes that run **before any pixel is generated**, and whose outputs every later stage
depends on:

1. **Plan the story** — one LLM pass produces a 4-beat arc (setup, development, turn,
   resolution) summing to exactly 40s. `[PRD §How it works 1]`
2. **Lock a continuity bible** — canonical character, wardrobe, location, lighting, palette
   and lens language. **Immutable for the life of the job.** `[PRD §How it works 2]`

Both artifacts are delivered to the user as machine-readable JSON. `[PRD §What's delivered]`
They are therefore a **public contract**, not internal scratch: a schema change is a breaking
API change.

This module does not generate video, does not score, and never sees a provider.

## 2. Public interface

### 2.1 StoryPlan

```python
class BeatKind(StrEnum):
    SETUP = "setup"; DEVELOPMENT = "development"; TURN = "turn"; RESOLUTION = "resolution"

class CameraMove(StrEnum):
    STATIC = "static"; PAN_LEFT = "pan_left"; PAN_RIGHT = "pan_right"
    PUSH_IN = "push_in"; PULL_OUT = "pull_out"; TILT_UP = "tilt_up"
    TILT_DOWN = "tilt_down"; TRACKING = "tracking"; ORBIT = "orbit"
    # Closed vocabulary [D-26]: providers accept a bounded set, and a bounded set is
    # QC-checkable. Free-text camera direction is not.

class Beat(BaseModel):
    index: int = Field(ge=0, le=3)
    kind: BeatKind
    action: str = Field(min_length=20, max_length=400)   # what physically happens
    camera_move: CameraMove
    duration_s: float = Field(default=10.0, ge=10.0, le=10.0)   # v1 fixes 10s  [D-03]
    continuity_note: str | None = None   # what must visibly carry over from the previous beat

class StoryPlan(BaseModel):
    job_id: UUID
    logline: str = Field(max_length=200)
    beats: list[Beat] = Field(min_length=4, max_length=4)
    total_duration_s: float = 40.0
    model_alias: str            # "reasoning-high"
    prompt_version: str
    created_at: datetime

    @model_validator(mode="after")
    def _validate(self) -> "StoryPlan":
        assert [b.index for b in self.beats] == [0, 1, 2, 3]
        assert [b.kind for b in self.beats] == [BeatKind.SETUP, BeatKind.DEVELOPMENT,
                                                BeatKind.TURN, BeatKind.RESOLUTION]
        assert abs(sum(b.duration_s for b in self.beats) - 40.0) < 1e-6   # exactly 40s
        return self
```

The beat **order is fixed** to setup → development → turn → resolution. `[PRD §How it works 1]`
names them in that order as an arc; a permutation would not be that arc.

### 2.2 ContinuityBible

Exactly the six dimensions the PRD names, no more and no fewer. Adding a seventh dimension is
a spec change, not an implementation detail.

```python
class CharacterSpec(BaseModel):
    name: str
    age_appearance: str
    build: str
    skin_tone: str
    hair: str
    facial_features: str          # the identity anchor QC scores hardest against
    distinguishing_marks: str | None

class WardrobeSpec(BaseModel):
    garments: list[str]
    colours: list[str]
    materials: list[str]
    condition: str                # pristine / worn / damp — drift here is highly visible

class LocationSpec(BaseModel):
    setting: str
    time_of_day: str
    architecture_or_terrain: str
    key_props: list[str]
    weather: str | None

class LightingSpec(BaseModel):
    key_light: str
    direction: str
    quality: str                  # hard / soft / diffused
    colour_temperature: str
    contrast_ratio: str

class PaletteSpec(BaseModel):
    dominant: list[str] = Field(min_length=2, max_length=5)
    accent: list[str] = Field(max_length=3)
    saturation: str
    grade: str                    # e.g. teal-orange, bleach bypass, natural

class LensLanguageSpec(BaseModel):
    focal_length: str
    aperture_feel: str            # depth-of-field character
    framing: str
    movement_style: str
    aspect_ratio: Literal["16:9"] = "16:9"
    resolution_ceiling: Literal["1080p"] = "1080p"    # [PRD §Out of scope] above 1080p.
    # A CEILING, not a delivery target: v1 renders at MAGICHOUR_RESOLUTION (720p)  [D-63]

class ContinuityBible(BaseModel):
    model_config = ConfigDict(frozen=True)            # immutable in memory
    job_id: UUID
    character: CharacterSpec
    wardrobe: WardrobeSpec
    location: LocationSpec
    lighting: LightingSpec
    palette: PaletteSpec
    lens_language: LensLanguageSpec
    negative_constraints: list[str]      # what must NEVER appear  [D-27]
    content_hash: str                    # sha256 of canonical JSON, excluding this field
    locked_at: datetime
    model_alias: str                     # "reasoning-high"  [D-07]
    prompt_version: str
```

### 2.3 Functions

```python
async def plan_story(prompt: str, *, ctx: NodeContext) -> StoryPlan: ...
async def lock_bible(plan: StoryPlan, prompt: str, *, ctx: NodeContext) -> ContinuityBible: ...
def render_bible_block(bible: ContinuityBible) -> str: ...   # canonical prompt fragment
def verify_bible(bible: ContinuityBible) -> None: ...        # raises VA-BIBLE-002 on mismatch
```

## 3. Behaviour

### 3.1 `plan_story`

- **One LLM pass.** `[PRD §How it works 1]` Alias `reasoning-high` `[CPS §Model routing]`.
  Structured output against `StoryPlan`. The prompt is `prompts/story_plan/<version>.md`,
  authored in-repo and the source of truth `[D-72]`; Langfuse tracks the version it ran under
  but is not required to retrieve it.
- The user's prompt enters as **untrusted content** in a delimited block. It supplies subject
  matter; it never supplies instructions. `[CPS §Non-negotiables]`
- Validation is deterministic code, not a second model call: four beats, correct kinds in
  order, durations summing to exactly 40s, non-empty actions, camera moves in the closed
  vocabulary.
- On validation failure: **one** structured re-ask carrying the specific violation. A second
  identical violation is the same failure signature twice → `FAILED_NO_PROGRESS`
  `[CPS §Agent harness]`, `[D-02]`. The planner is cheap; a planner that cannot produce a
  valid plan twice will not produce one on the third try, and every downstream cost depends
  on it.
- **"One LLM pass" is a cost and latency claim, not a prohibition on validation.** The re-ask
  is a repair of a malformed response, not a second planning strategy. `[D-28]`

### 3.2 `lock_bible`

- Input: the user prompt **and** the accepted `StoryPlan`, so the bible is consistent with
  the arc it must serve.
- Alias `reasoning-high` `[D-07]`: the bible is immutable and every subsequent prompt embeds
  it, so an error here is unrecoverable and is paid for four times over.
- **Specificity gate.** Each dimension is checked for concreteness: no empty strings, no
  hedging vocabulary ("some", "perhaps", "various", "or"), palette colours resolvable to
  named or hex values, at least three distinguishing details on `character`. A vague bible
  cannot enforce continuity and cannot be QC-scored against. Failure → one re-ask, then
  `VA-BIBLE-001`. `[D-29]`
- **Locking.** On acceptance: compute `content_hash` over canonical JSON, set `locked_at`,
  insert. The row is protected by a database trigger that rejects `UPDATE` on any content
  column — immutability is enforced by the database, not by `frozen=True` alone, because
  in-process immutability does not survive a second process. See
  [`persistence.md`](./persistence.md).
- Every later read calls `verify_bible()`. A hash mismatch is `VA-BIBLE-002` and terminates
  the job: it means every remaining shot would be generated against a different bible.

### 3.3 `negative_constraints` `[D-27]`

Not named by the PRD, but the six positive dimensions cannot express "no other characters
enter frame", "no scene cut inside the shot", "no text or captions", "no aspect-ratio
change". These are the drifts that most often break a 4-shot sequence, they are cheap to
state and cheap to check, and they are derived by the same pass that writes the bible. They
are rendered into every generation prompt and read by QC as fail-fast checks.

### 3.4 `render_bible_block`

A single deterministic function producing the canonical bible fragment used by
[`providers.md`](./providers.md) for prompt composition and by [`qc.md`](./qc.md) as the
scoring reference. **One renderer, two consumers** — if generation and QC described the bible
differently, QC would be scoring against a different target than the one the generator was
given. Its output is stable-ordered and hashed into `ShotAttempt.prompt_hash` for
reproducibility. `[PRD §What's delivered]`

## 4. Dependencies

| Depends on | For |
| --- | --- |
| [`gateway.md`](./gateway.md) | `reasoning-high` calls, prompt resolution, structured output |
| `prompts/` (in-repo) | `story_plan` and `continuity_bible` prompt text — the source of truth `[D-72]` |
| [`harness.md`](./harness.md) | `NodeContext`, untrusted-content quarantine, budget charging |
| [`persistence.md`](./persistence.md) | `StoryPlan` / `Beat` / `ContinuityBible` rows and the immutability trigger |
| [`observability.md`](./observability.md) | spans, generations, prompt versions |

Consumers: [`graph.md`](./graph.md) (node bodies), [`providers.md`](./providers.md)
(`render_bible_block`), [`qc.md`](./qc.md) (scoring reference),
[`assembly.md`](./assembly.md) (JSON exports in the manifest).

## 5. Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| Beats do not sum to 40s | `StoryPlan` validator | One re-ask with the arithmetic stated; then `VA-PLAN-002`. |
| Wrong beat count or order | Validator | One re-ask; then `VA-PLAN-003`. |
| Camera move outside the vocabulary | Enum coercion | Map to the nearest member if unambiguous, else re-ask. Record the coercion. |
| Model returns unparseable JSON | Gateway schema validation | Gateway's single reformat attempt, then `VA-PLAN-001`. |
| Same invalid plan twice | Job-scope failure signature | `FAILED_NO_PROGRESS`, stop immediately. `[CPS §Agent harness]` |
| Vague bible | Specificity gate | One re-ask naming the weak dimensions; then `VA-BIBLE-001`, `FAILED`. Do not proceed — four shots against a vague bible waste the whole budget. |
| Bible mutation attempted | DB trigger + hash check | `VA-BIBLE-002`, terminate `FAILED`. Loud, never silent. |
| Prompt requests out-of-scope content (dialogue, lip-sync, voiceover, a different duration) | Planner instruction + deterministic post-check | Plan the visual story regardless; record a `scope_note` returned to the client. Never attempt the out-of-scope feature. `[PRD §Out of scope]` |
| Prompt is prompt-injection shaped | Harness quarantine | Treated as subject matter only; `VA-SEC-001` observation. |
| Content policy rejection | Gateway | `VA-GW-006` surfaced honestly, naming the planning stage. |
| `reasoning-high` group fully unavailable | Gateway circuit | `VA-GW-001`. Terminate `FAILED` — with no plan there is nothing to preserve. This is a legitimate zero-deliverable case. |

## 6. Test strategy

| Level | Tests |
| --- | --- |
| Schema | Golden JSON fixtures for `StoryPlan` and `ContinuityBible`; any field change fails CI, since both are delivered artifacts `[PRD §What's delivered]`. |
| Validators | Property test: durations summing to 39.9 or 40.1 are rejected; beat permutations are rejected; exactly `[0,1,2,3]` passes. |
| Re-ask | Assert exactly **one** re-ask on a malformed response, and that a second identical failure raises a job-scope signature. |
| Immutability | Attempt an `UPDATE` on a locked bible via SQL; assert the trigger rejects it. Mutate the in-memory object; assert `frozen=True` raises. Corrupt a stored bible; assert `verify_bible` raises `VA-BIBLE-002`. |
| Renderer | `render_bible_block` is deterministic and stable-ordered across runs and processes; its hash is reproducible. The QC reference and the generation prompt fragment are byte-identical. |
| Specificity gate | Labelled corpus of vague vs specific bibles; assert precision/recall targets, so the gate neither blocks good bibles nor passes useless ones. |
| Eval | A fixed prompt set is planned nightly and human/LLM-scored for arc quality; feeds the `> 3%` eval-regression CI gate `[CPS §Non-negotiables]` and the `≥ 4.0` story-coherence metric `[PRD §Success metrics]`. |
| Injection | Prompts containing "ignore the bible", role markers and tool syntax; assert no effect on control flow and that the bible is still locked. |
| Cost | Assert the planning stage makes at most 2 `reasoning-high` calls per node (initial + one re-ask) and never a third. |
