---
doc: PITCH_SCRIPT
title: Video Agent — pitch video script (~10 minutes)
status: draft — for recording
---

# Video Agent — pitch script

A spoken-word script for a ~10-minute walkthrough video. Timestamps are targets, not
handcuffs — pace to how it actually feels once you're talking. Stage directions are in
*italics*; everything else is read aloud. Screen-share cues point at what's already built:
[`README.md`](./README.md)'s architecture diagrams, and the live UI at `http://localhost:5173`
once `scripts/dev_server.py` + `scripts/dev_worker.py` are running.

---

## 0:00 – 0:50 — The problem

*Screen: blank, or the four unrelated stock-footage-style clips side by side, if you have them.*

Text-to-video models are good now. Give one a prompt, and in ten seconds you get a genuinely
nice clip. The problem shows up the moment you need more than ten seconds.

Ask for four clips to tell one story — a violinist on a rooftop at dawn, say — and generate
them from four separate prompts, and you get four unrelated clips. The violinist's face
changes. The rooftop changes color. The light shifts from dawn to noon and back. Each
individual clip is fine. Together, they're not a story — they're four random videos that
happen to share a subject.

Generation is solved. **Continuity is not.** That's the actual problem this project answers.

## 0:50 – 1:45 — What we built

*Screen: README architecture diagram 1 (system overview).*

Video Agent turns one prompt into a continuous 40-second story: four chained 10-second shots
that hold together — same face, same wardrobe, same room, same light — end to end.

The mechanism is deliberately simple to describe, even though making it reliable took real
engineering. One LLM call plans a four-beat story arc — setup, development, turn, resolution
— exactly 40 seconds. A second LLM call locks a **continuity bible**: the character, the
wardrobe, the location, the lighting, the color palette, the camera language — and that bible
is then immutable for the rest of the job, enforced by a database trigger, not just a
convention. Every shot's prompt is built from that same bible plus its own beat, so shot two
isn't improvising a new room — it's describing the *same* room the bible already locked.

And the part that actually buys the continuity: the last frame of every shot becomes the
first frame of the next one. The model isn't asked to imagine "the same person" from a text
description each time — it's handed the literal pixels of where the last shot left off. That
single design choice is why four independently-generated clips turn into one story instead of
four videos of a stranger.

## 1:45 – 3:15 — Architecture, and why it's not a toy

*Screen: README architecture diagram 2 (the graph state machine) plus a scroll through
`docs/HLD.md` / `docs/LLD/*.md`.*

Under the hood this is a compiled LangGraph state machine — nine nodes, from `plan_story`
through `lock_bible`, shot generation, frame extraction, QC, assembly, and delivery — with a
hard rule behind every one of them: **checkpoint after every node, so a crash resumes, it
never restarts.** That's not a slogan I'm reading off a slide — it's an actual invariant a
test suite checks: every node writes its checkpoint in the *same* database transaction as its
own domain data, so there's no window where the two disagree.

A few things I want to call out specifically, because they're the difference between a demo
and something you'd actually run:

Every job is protected by **row-level security in Postgres** — not an application-level `if
tenant_id matches`, but a database policy that's enforced even for the table owner, audited by
a static check that fails the build if any table is missing it.

Every write that creates something — a new job, a cancel — goes through an **idempotency
key**, so a retried request can never create two jobs or bill twice. Redis holds a queue with
consumer groups and at-least-once delivery, and every single graph node is written to be safe
to run twice, because "at least once" means it sometimes will.

Every LLM call and every video-provider call goes through a real **circuit breaker** and
**retry-with-backoff-and-jitter** policy, shared across workers in Redis — not a try/except
with a hopeful comment.

And there's an actual **agent harness** underneath the whole thing — a six-rule termination
engine that decides, every single step, whether to continue, succeed, degrade to partial,
detect it's stuck repeating the same failure and stop, fail outright, or escalate to a human.
Hard caps on iterations, wall-clock time, tokens, and dollars. That's what keeps a repair loop
from quietly burning your budget.

## 3:15 – 4:15 — The provider swap that proves the abstraction works

*Screen: README section on the provider substitution / `providers.md`.*

Here's a detail I'm genuinely proud of. The original spec named a specific video-generation
provider. That provider turned out to have no accessible trial tier — no credential I could
actually get. So I swapped in a different provider, Magic Hour, instead.

The reason that's worth mentioning in a pitch and not just a footnote: the swap touched **one
adapter file and a config value.** Nothing else in the codebase names a provider — there's
actually a static check in the test suite that scans the entire source tree and fails the
build if the word "Magic Hour" — or the original provider's name — shows up anywhere outside
that one adapter and its config. That's the capability-negotiation abstraction doing exactly
what it was designed to do: an API change, or even a total provider change, is a config
change, not an outage.

## 4:15 – 7:00 — Live demo

*Screen: the running UI at `http://localhost:5173`. Have `scripts/dev_server.py` and
`scripts/dev_worker.py` already running in two terminals before you start recording.*

Let me actually show you this working. This is a small React front end I built on top of the
real API — no shortcuts in the backend, the only thing turned off here is authentication, so
we can watch this live without a credential dance.

*Type a prompt into the textarea. Something with visual specificity — e.g. "a violinist plays
on a rooftop at dawn as the city wakes below." Click Create video.*

That just hit `POST /v1/jobs` for real — it created a row in Postgres, claimed an idempotency
key, and published a message onto a Redis stream. Watch the job list — it shows up immediately
as `queued`.

*Click the job in the list.*

Now we're watching its live state — this is polling `GET /v1/jobs/{id}` every three seconds,
same call the doc's client contract describes. You can see `current_node` moving through the
graph in real time: `plan_story` right now — that's a real call to an LLM through the gateway,
planning the four-beat arc. Budget's tracked live too — iterations, tokens, dollars, wall
clock, all against hard caps.

*Wait for it to progress — narrate as it moves.*

Now it's on `lock_bible` — locking the character, wardrobe, location and lighting that every
shot has to match. Now `generate_shot` — and this is worth being honest about on camera: a
real render from Magic Hour can take anywhere from under a minute to, in my testing, over
forty minutes, because free-tier queue behavior varies a lot. So for this demo, the shots
you're about to see are rendered by a **mock provider** I built — real ffmpeg, real files,
zero network call, zero cost, zero wait. It takes the previous shot's final frame and turns
it into the next clip's actual background, with the job id burned into the frame as text —
so you can literally *see* the chaining happening, shot to shot.

*Wait for terminal state.*

And there it is — `outcome: SUCCESS`. Delivered video, right here, playing in the browser,
plus every individual shot clip below it. That's a presigned URL straight out of the object
store, minted fresh on this exact request — the system never stores or logs that link, by
design, because a presigned URL is a bearer credential.

## 7:00 – 8:15 — What's honestly not done yet, and why

*Screen: README's status/milestone table.*

I want to be straight about scope, because a pitch that hides the gaps is a worse pitch than
one that names them. Two things are explicitly deferred, not missing by accident:

The **QC repair loop** — a vision model scoring each shot against the bible and regenerating
only the shot that broke, capped at two attempts — is fully designed in the spec, but in this
build it's an intentional stub: every shot is accepted unconditionally. The repair path exists
structurally in the graph — the edge is there — it's just not wired live yet. That's the next
thing I'd build.

**Observability** — Langfuse tracing, the generation-level cost and score dashboards — is
also deferred. What *is* built is the entire structural half: JSON logs on every line with a
propagated trace id, and a redaction tripwire that scans every outgoing log line and refuses
to let a credential, a presigned URL, or raw media bytes through — enforced at test time, not
by convention.

And one finding I got from actually running this against the real provider, not just testing
against fakes: real render queue times on a free-tier account varied wildly — sometimes under
a minute, once over forty minutes. That's outside what I can control from the code, but it's
exactly the kind of thing you only find by running the real integration, which is why I did.

## 8:15 – 9:15 — What this demonstrates, beyond the demo

*Screen: back to the architecture diagram, or just camera.*

I want to name what I think this project actually proves, past "it makes a video."

It proves I can take a spec seriously enough to encode its invariants as tests, not just
prose — checkpoint-safety, tenant isolation, idempotency, budget caps are all things a test
suite fails on if violated, not things a code reviewer has to remember to check.

It proves I debug against reality, not fakes — the mock provider, the real Magic Hour
integration, the real Postgres-and-Redis dev stack all surfaced real bugs that unit tests
alone never would have: a missing required field the real API rejected, a resolution the
account tier couldn't serve, a redelivery path that would have silently double-billed a
render.

And it proves the abstraction boundaries actually hold under a real swap — provider,
resolution tier, even the entire rendering backend — without touching the graph, the harness,
or the API surface that everything else depends on.

## 9:15 – 10:00 — Close

*Screen: the delivered video, playing, or the README.*

One prompt in. A continuous, coherent 40-second story out — with the engineering underneath it
built to survive a crash, a retry, a provider outage, or a runaway loop, and to say so
honestly when something's still a stub instead of pretending it's finished.

That's Video Agent. Thanks for watching.
