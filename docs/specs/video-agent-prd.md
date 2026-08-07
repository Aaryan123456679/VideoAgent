# Video Agent — Product Requirements Document

> **Source of truth.** Transcribed verbatim in substance from `Video-Agent.pdf`
> (Entermind · PRD · Version 1.0 · 02 August 2026).
> Inherits everything in [`common-platform-spec.md`](./common-platform-spec.md).

One prompt becomes a continuous 40-second story — four 10-second shots with enforced
narrative and visual continuity.

## The problem

Text-to-video models generate clips of 5–10 seconds in isolation. Generate four clips
from four prompts and you get four unrelated clips: the protagonist changes face, the
room changes colour, the story never moves. **Generation is solved; continuity is not.**

## How it works

1. **Plan the story** — one LLM pass produces a 4-beat arc (setup, development, turn,
   resolution) summing to exactly 40s.
2. **Lock a continuity bible** — canonical character, wardrobe, location, lighting,
   palette and lens language. Immutable for the life of the job.
3. **Generate shots sequentially** — via Higgsfield MCP behind a provider abstraction.
   Each prompt = bible + beat action + camera move.
4. **Chain the frames** — the final frame of shot *n* conditions shot *n+1*, so
   identity carries forward.
5. **QC and repair** — a vision model scores each shot against the bible; failures
   regenerate that shot only, capped at 2 attempts.
6. **Assemble and deliver** — ffmpeg stitch, normalise, optional music bed,
   presigned URLs.

## Deliberate trade-off

Shots run **sequentially, not in parallel**. Parallel is roughly 4× faster but breaks
frame chaining, and frame chaining is what makes the product work. Latency was traded
for the core value proposition.

## Resilience

- **Never returns nothing** — if one shot succeeded, a stitched partial is delivered
  with a working resume
- **Resume, don't restart** — completed shots are never regenerated or re-billed
- **Shot-level regeneration** — fix shot 3, leave 1, 2 and 4 byte-identical
- **Provider abstraction** — capability negotiation plus failover, so an API change is
  not an outage

## Delivery milestones

| Milestone | Scope |
| --- | --- |
| M1–M2 | Job lifecycle, planning, continuity bible |
| M3 | Higgsfield MCP, frame chaining, assembly |
| M4 | QC loop, partial results, resume |
| M5 | Observability, cost caps, load + chaos |

## Success metrics

| Metric | V1 target |
| --- | --- |
| Story coherence (human, 1–5) | ≥ 4.0 |
| Jobs with continuity score ≥ 0.75 | ≥ 85% |
| p90 end-to-end job latency | ≤ 8 min |
| Jobs failing with zero deliverable | < 1% |

## Key risks

| Risk | Mitigation |
| --- | --- |
| Provider can't hold identity across clips | Frame chaining + locked bible + QC loop |
| QC itself unreliable → wasted spend | Calibrate on labelled set; cap attempts |
| Repair loops blow the budget | Hard USD cap; no-progress detection |

## What's delivered

- The stitched 40-second MP4, plus each 10-second clip separately
- Thumbnail and the extracted continuity frames
- `StoryPlan` and `ContinuityBible` as machine-readable JSON
- Per-shot cost, model, seed and prompt — every job is reproducible

## Out of scope (v1)

Dialogue and lip-sync · durations other than 40s · user-supplied reference characters ·
voiceover · editing timeline · above 1080p.
