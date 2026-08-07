# Impact Map

Doc-level impact for the documentation agent: which docs must be revisited when a thing changes.
Module dependency edges are authoritative in each LLD's "Dependencies" section.

| If this changes | Revisit |
| --- | --- |
| `docs/specs/*` (a spec or its PDF) | **Everything.** Specs are the root of the precedence chain. |
| Graph node or edge set | `docs/HLD.md` §3, `graph.md`, `harness.md` §5, `observability.md` §2 |
| Termination outcomes or budget caps | `harness.md`, `HLD.md` §5, `api.md` (`JobView`), `AGENT.md` §1.2 |
| Alias set or routing | `gateway.md`, `HLD.md` §6, `AGENT.md` §2, `planning.md`, `qc.md` |
| `ContinuityBible` / `StoryPlan` shape | `planning.md`, `persistence.md` §2, `providers.md` §5, `qc.md` §3, `api.md` (delivered JSON is a public contract) |
| Continuity threshold or repair cap | `qc.md`, `graph.md` §3.1, `HLD.md` §11, `persistence.md` (CHECK constraints) |
| Provider protocol or capabilities | `providers.md`, `HLD.md` §8.4, `graph.md` |
| **The video provider itself** | `providers.md` §7 (adapter), `.env.example`, `HLD.md` §8.4 + App. A, `README.md` status note, `AGENT.md` §2/§8 — and check seed/cost/resolution assumptions in `qc.md` §6, `persistence.md` §2, `harness.md` §4, `assembly.md` §4.1, `observability.md` §6. **Never** `docs/specs/*`. |
| Postgres schema / RLS | `persistence.md`, `HLD.md` §9, `AGENT.md` §4 |
| Error codes | `observability.md` §6, `api.md` §4, and the module that raises them |
| Build scope (which epics ship) | `HLD.md` §12, `README.md` status note, `AGENT.md` §8, and the `implementation_status` front-matter of every LLD |
| Auth scheme | `api.md` §6, `persistence.md` §2/§3, `AGENT.md` §3/§4 |
| Queue transport | `graph.md` §6, `persistence.md` §5.1, `HLD.md` §2 |
| Never-logged list or redaction | `observability.md` §5, `AGENT.md` §3, `persistence.md` §7 |
| Non-negotiables in the CPS | `AGENT.md` §1 first, then every module that implements one |
