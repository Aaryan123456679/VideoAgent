---
doc: LLD
module: assembly
title: Assembly — frame extraction, ffmpeg stitch, normalise, partial delivery
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

# LLD — `assembly`

> Tags: `[CPS §…]` = Common Platform Spec · `[PRD §…]` = Video Agent PRD · `[D-nn]` = design
> decision registered in [HLD Appendix A](../HLD.md#appendix-a--design-decision-register).

> **Implementation status — PARTIAL.** **E2 — in the v1 build.** Frame extraction, ffmpeg stitch/normalise, thumbnail and delivery ship. **Partial assembly (§5) is designed here but deferred to E3**, since it exists to serve the QC/abandonment path.
> Scope: v1 builds **E0 + E1 + E2**; **E3 and E4 are deferred**. See
> [HLD §12](../HLD.md#12-delivery-milestones).

## 1. Responsibility

All media manipulation. Nothing else in the system shells out to ffmpeg.

> **Assemble and deliver** — ffmpeg stitch, normalise, optional music bed, presigned URLs.
> `[PRD §How it works 6]`
> **Never returns nothing** — if one shot succeeded, a stitched partial is delivered with a
> working resume. `[PRD §Resilience]`

Three jobs:

1. **`extract_final_frame`** — produce the anchor frame that chains shot *n* to shot *n+1*
   `[PRD §How it works 4]`;
2. **`assemble`** — stitch and normalise whatever succeeded, optional music bed, thumbnail;
3. **partial assembly** — the mechanism behind "never returns nothing".

Presigned URL minting itself belongs to [`persistence.md`](./persistence.md); this module
builds the manifest that lists them.

## 2. Public interface

```python
class FrameExtraction(BaseModel):
    frame: ArtifactRef          # PNG, native resolution, unmodified
    source_timestamp_s: float
    width: int; height: int
    checksum_sha256: str

async def extract_final_frame(clip: ArtifactRef, *, ctx: NodeContext) -> FrameExtraction: ...

class AssemblyRequest(BaseModel):
    job_id: UUID
    clips: list[ClipRef]        # ordered by shot index; ONLY shots with a usable attempt
    music_bed: bool = False
    expected_shot_count: int = 4

class AssemblyResult(BaseModel):
    final_video: ArtifactRef
    thumbnail: ArtifactRef
    duration_s: float           # 40.0 when complete; less when partial
    partial: bool
    missing_shot_indices: list[int]
    normalisation_applied: list[str]
    degraded: bool
    degrade_reason: str | None

async def assemble(req: AssemblyRequest, *, ctx: NodeContext) -> AssemblyResult: ...
async def build_manifest(job_id: UUID, *, ctx: NodeContext) -> DeliveryManifest: ...
```

## 3. Frame extraction

The chaining contract's producer side — see
[`providers.md` §6](./providers.md#6-frame-chaining-contract).

| Property | Choice |
| --- | --- |
| Which frame | The **last decodable** frame, not a fixed timestamp. A truncated tail must not yield a black anchor. |
| Format | PNG, lossless. JPEG artefacts on the identity anchor would propagate into every subsequent shot. `[D-44]` |
| Geometry | Native resolution, no resize, no crop, no colour transform. Any transform is an unauthorised continuity change. |
| When | Immediately after `generate_shot`, before QC — QC uses the extracted frames too. |
| Advance | `last_good_frame` advances **only** on QC acceptance. `[D-05]` |
| Validation | Reject an all-black or all-uniform frame (variance below a floor): it is unusable as an anchor. Fall back to the last frame that passes, else no anchor and `degraded=true`. `[D-45]` |
| Delivered | Yes — extracted continuity frames are part of the deliverable. `[PRD §What's delivered]` |

```bash
# shape only; real invocation is parameterised and never string-interpolated from user input
ffmpeg -sseof -1 -i <clip> -vsync 0 -q:v 1 -frames:v 1 -f image2 <frame.png>
```

## 4. Stitch and normalise

### 4.1 Normalise first, then concatenate
Clips come from a generator and may vary in frame rate, pixel format, colour range, SAR or
container timebase — even from one provider. Concatenating heterogeneous clips produces
stutter and colour steps at every boundary, which reads to a viewer as exactly the continuity
failure the product exists to prevent. So: **normalise every clip to one canonical profile,
then concatenate.** `[D-46]`

| Parameter | Canonical value |
| --- | --- |
| Container / codec | MP4 / H.264 High, `yuv420p` |
| Resolution | The **configured target**, `MAGICHOUR_RESOLUTION` (v1: 720p → 1280×720). 1080p is a **ceiling, not a floor** — `[PRD §Out of scope]` forbids *above* 1080p, it does not require it `[D-63]`. All clips in one job share one resolution |
| Frame rate | 24 fps, CFR |
| SAR / DAR | 1:1 / 16:9 |
| Colour | BT.709, limited range, tagged |
| Audio | none unless a music bed is requested; then AAC 128 kbps 48 kHz stereo |
| `faststart` | yes — the MP4 must start playing before it finishes downloading |

Concatenation uses the demuxer path on normalised inputs, so it is a stream copy — no
re-encode of already-conformant video, no generational quality loss, and fast.

### 4.2 Cuts, not transitions
Shot boundaries are **hard cuts**. No crossfades. `[D-47]` A crossfade would blend two shots
and cosmetically mask exactly the identity drift the QC loop is built to detect, and the PRD
authorises "stitch, normalise, optional music bed" and nothing more.

### 4.3 Music bed
Optional, off by default `[PRD §How it works 6]`.

**v1 ships no bundled music library** `[D-69]`. The bed accepts a **caller-supplied audio
artifact** (`CreateJobRequest.music_bed_artifact_id`); absent one, the bed is simply omitted
and the field is **absent from the manifest** rather than present-and-null. Licensing a
library is a business decision, not an engineering one, and shipping unlicensed audio is not
an option. Audio is **never generated and never fetched from the open internet**.

When supplied: mixed at −18 LUFS, faded in/out 0.5s, trimmed to the video's actual duration
(which is shorter than 40s for a partial). Failure to attach the bed is **non-fatal**:
deliver silent video, flag `degraded`. `[D-48]`

### 4.4 Thumbnail
`[PRD §What's delivered]` — a 1280×720 JPEG. Selected as the highest-scoring accepted shot's
mid-point frame, falling back to shot 0's first frame. Using the best-scored shot means the
thumbnail represents the product at its best rather than at an arbitrary index. `[D-49]`

## 5. Partial assembly

The mechanism behind *"never returns nothing"* `[PRD §Resilience]`.

```python
usable = [c for c in clips if c.best_attempt_id is not None]   # accepted OR abandoned-with-a-clip
if not usable:
    # "Zero deliverable" = no PLAYABLE VIDEO artifact: no stitched MP4 and no shot clip.
    # Plan and bible JSON are still returned but do NOT count against it.  [D-73]
    raise NoDeliverable(code="VA-ASM-002")     # the < 1% case  [PRD §Success metrics]
result = stitch(sorted(usable, key=lambda c: c.shot_index))
result.partial = len(usable) < req.expected_shot_count
```

| Rule | Rationale |
| --- | --- |
| **Abandoned shots with a clip are included**, using their best attempt | A below-threshold shot is a worse product than a good one, but a far better product than a gap. Included, flagged, and the score is reported. |
| **Missing shots are gaps, not placeholders** | No black slate, no "shot unavailable" card. The video is shorter and the manifest names `missing_shot_indices`. Inserting filler would be dishonest output. `[D-50]` |
| **Order is always by shot index** | Never by completion order. |
| **Partial always sets `partial: true` and `degraded: true`** | Always flagged. `[CPS §Failure behaviour]` |
| **Every individual clip is delivered separately regardless** | Each 10-second clip is a deliverable in its own right `[PRD §What's delivered]`, so a user gets value even from a badly broken job. |
| **A partial is resumable** | The manifest carries the `resume` affordance `[PRD §Resilience]`; see [`api.md`](./api.md). |
| **Zero usable clips → `VA-ASM-002`** | Honest failure with what was preserved (the plan and bible JSON, which are still delivered) and what to do next. **This is the "zero deliverable" case for the PRD metric**: it is defined as *no playable video artifact*, and the plan and bible JSON deliberately do **not** rescue it `[D-73]`. |

## 6. Execution safety

- ffmpeg runs as a **subprocess with an argv list**, never a shell string. No user or model
  text is ever interpolated into an argument. Filenames are internally generated UUID paths.
- Hard `timeout` per invocation; on expiry the process group is killed and temp files removed.
- Runs in a worker, never in a request path — the API is async and non-blocking
  `[CPS §Canonical stack]`. Invocations are `await`ed via an executor.
- Temp files live in a per-job scratch directory removed in a `finally`, including on crash
  cleanup at worker start.
- Resource caps: `-threads` bounded so one job cannot starve the box.
- **ffmpeg stderr is captured for diagnosis and truncated in logs; media bytes are never
  logged.** `[CPS §Observability]`
- Output is probed (`ffprobe`) before being accepted as an artifact: duration within
  tolerance, expected stream count, non-zero bitrate. An unprobed output is not a deliverable.

## 7. Dependencies

| Depends on | For |
| --- | --- |
| ffmpeg / ffprobe (system) | all media work; version pinned in the image and asserted at startup |
| [`persistence.md`](./persistence.md) | artifact read/write, checksums, presigned URLs |
| [`planning.md`](./planning.md) | `StoryPlan` / `ContinuityBible` JSON exports in the manifest |
| [`harness.md`](./harness.md) | `NodeContext`, tool grants, budget (wall-clock) |
| [`observability.md`](./observability.md) | spans per invocation, error codes |

Consumed by [`graph.md`](./graph.md) (`extract_final_frame`, `assemble`) and
[`api.md`](./api.md) (manifest read). No dependency on `providers` or `qc` — assembly stitches
what it is given and does not judge it.

## 8. Failure modes

| Failure | Detection | Response |
| --- | --- | --- |
| Clip unreadable / zero bytes | `ffprobe` pre-check | Exclude from the stitch, mark the shot unusable, continue with the rest. One bad file must not lose the whole job. |
| Clips at mixed resolutions within a job | Probe before concat | Normalise all to the job's configured target `[D-63]`; record the upscale. Never let one shot change the output geometry mid-video. |
| Extracted frame is black or uniform | Variance check | Step back frame by frame to the last usable one; if none, no anchor, `degraded=true`. `[D-45]` |
| Frame extraction fails entirely | ffmpeg non-zero exit | Retry once, then continue without an anchor and flag degraded. Never block the pipeline on a chaining aid. |
| Clips differ in fps or pixel format | Probe before concat | Expected — normalisation exists for this. Record what was applied. |
| Concat produces a mismatched duration | `ffprobe` on output | `VA-ASM-003`; retry once with the re-encode path instead of stream copy, then fail honestly. |
| ffmpeg timeout | Watchdog | Kill the process group, clean temp files, `VA-ASM-001`. Retryable once. |
| Disk full | `ENOSPC` | `VA-ASM-004`, retryable after scratch cleanup. Alarm — this is an operational fault, not a job fault. |
| No music bed supplied | `music_bed_artifact_id` absent | Not a failure. Deliver silent, omit the field from the manifest, do **not** flag `degraded` — this is the default path in v1 `[D-69]`. |
| Supplied music bed missing or corrupt | Artifact fetch / probe | Deliver silent, `degraded=true`. Never fail a job over optional audio. `[D-48]` |
| Thumbnail generation fails | ffmpeg exit | Deliver without a thumbnail, flag degraded. Non-fatal. |
| Zero usable clips | `usable == []` | `VA-ASM-002` → outcome `FAILED`, zero-deliverable. Still return plan + bible JSON and an honest envelope. |
| Artifact upload fails | Object-store error | Retry with backoff; on exhaustion `VA-STORE-001`. The local file is retained for the job's lifetime so resume can re-upload rather than re-encode. |
| ffmpeg version drift | Startup assertion | Refuse to start. A silent encoder change is an unlogged output change. |

## 9. Test strategy

| Level | Tests |
| --- | --- |
| Golden media | Committed tiny synthetic clips (a few KB, generated by a fixture script, not real media) with known properties; assert byte-stable outputs for a fixed input set. |
| Normalisation | Inputs deliberately varying in fps, pixel format, SAR, colour range **and resolution**; assert every output matches the configured canonical profile exactly, and that the profile follows `MAGICHOUR_RESOLUTION` rather than a hard-coded 1080p `[D-63]`. |
| Partial matrix | All 16 combinations of 4 shots present/absent. Assert: 0 usable → `VA-ASM-002`; 1–3 → partial with correct duration, `missing_shot_indices` and `degraded`; 4 → 40.0s ± tolerance and `partial=false`. |
| Ordering | Shuffle completion order; assert output order is always by shot index. |
| Frame extraction | Clips whose tail is truncated or black; assert the last *decodable, non-uniform* frame is chosen and that the PNG is byte-identical to the source frame. |
| Safety | Filenames containing shell metacharacters and prompt text; assert argv invocation and no shell expansion. Assert timeouts kill the process group and clean up. |
| Concurrency | N assemblies in parallel; assert scratch isolation and no cross-job file leakage. |
| Resource | Assert temp directories are removed on success, failure and simulated crash. |
| Manifest | Assert every delivered artifact class from `[PRD §What's delivered]` is present: 40s MP4, each 10s clip, thumbnail, continuity frames, `StoryPlan` and `ContinuityBible` JSON, and the per-shot cost/model/prompt/provider-project-id record, with the seed caveat where the provider supports none `[D-59]`. |
| No-audio default | Assert the default output has zero audio streams and that supplying a `music_bed_artifact_id` adds exactly one. Assert no audio file ships in the repo or image `[D-69]`. |
| Zero deliverable | Assert the total-failure case still returns plan and bible JSON **and** that the metric counts it as zero-deliverable regardless, because no playable video artifact exists `[D-73]`. |
