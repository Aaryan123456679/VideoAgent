# Decisions

Full register: [`docs/HLD.md` Appendix A](../../docs/HLD.md#appendix-a--design-decision-register) · machine-readable: `.cdr/index/decision.jsonl` (D-01 … D-57).

`D-01`–`D-15` are system-level (argued in the HLD). `D-16`–`D-57` are module-local (argued in the owning LLD).

The ones most likely to be challenged, and where the argument lives:

| ID | Decision | Argued in |
| --- | --- | --- |
| `D-01` | "Capped at 2 attempts" = 2 *repair* attempts, so max 3 generations per shot | HLD App. A |
| `D-02` | Failure signatures are scoped shot vs job, reconciling CPS "stop immediately" with the PRD repair loop | HLD App. A, `harness.md` §6 |
| `D-06` | Video generation does not traverse LiteLLM; parallel capability registry under the same policy | HLD §6, `providers.md` |
| `D-39` | 0.75 is the acceptance gate *and* the fleet metric; below-threshold shots still ship, flagged | `qc.md` §4 |
| `D-05` | After an abandoned shot, chain from the last *successful* frame; text-only if none | `graph.md` §3.3 |
| `D-23`/`D-24` | Checkpoint in the node's transaction; `in_flight` + provider `lookup()` reconciliation on resume | `graph.md` §4–5 |
| `D-08` | Concrete budget cap numbers are illustrative config defaults, not product commitments | `harness.md` §4 |
| `D-58` | **Magic Hour replaces Higgsfield MCP** — Higgsfield had no free/trial API tier. Sound only because Magic Hour satisfies `IMAGE_CONDITIONING`, preserving frame chaining | HLD App. A, `providers.md` §1 |
| `D-59` | **No seed control → the PRD's reproducibility promise is partially unmet**, recorded as `seed_supported=false` rather than faked. Supersedes `D-30`, amends `D-41` | `providers.md` §3.1 |
| `D-60` | Cost billed in credits; the ledger **reconciles** provisional charges instead of only accumulating | `providers.md` §7.5, `harness.md` §4 |
| `D-61` | `MAGICHOUR_MODEL=wan-2.2`, validated at startup for 10s support — `sora-2` cannot do 10s | `providers.md` §7.1 |
| `D-63` | 720p is the v1 target; 1080p is a ceiling, not a floor | `providers.md`, `assembly.md` §4.1 |

| `D-71` | **The QC threshold is configuration**, not a `Final` constant — `qc.md` said the opposite and was wrong. Non-default values are logged at startup and surfaced on the manifest | `qc.md` §4.1 |
| `D-73` | **"Zero deliverable" = no playable video artifact.** The old definition was unsatisfiable, so the metric could never fail | HLD §11 |
| `D-67` | Redis Streams job queue, at-least-once — safe *only* because `D-24`'s fingerprint reconciliation already made it so | `graph.md` §6.1 |
| `D-65` | Credits→USD from `MAGICHOUR_USD_PER_1K_CREDITS`; **volume discounts never applied pre-flight**, because a cap must err toward under-spending | `providers.md` §7.5 |
| `D-66` | QC calibration **deferred**; the threshold ships uncalibrated and is labelled as such | `qc.md` §5 |

**Superseded / amended:** `D-30` (superseded by `D-59`); `qc.md`'s `CONTINUITY_THRESHOLD` constant (superseded by `D-71`); `D-34` (amended — no MCP discovery on REST), `D-39`/`D-48`/`D-41`/`D-52` (amended). Do not cite any of them without reading the amendment.
