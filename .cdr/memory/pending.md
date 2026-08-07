# Pending

## Needs product confirmation
- **A1 — repair cap.** Resolved as `D-01` (2 repairs after the initial generation, max 3 generations/shot). The PRD sentence is ambiguous. Confirm before implementing `qc`/`graph`.
- **A2 — the 0.75 threshold.** Resolved as `D-39` (it is both the QC acceptance gate and the fleet metric). The PRD states 0.75 only as a metric. Confirm; it sets the repair rate and therefore the cost profile.
- **A4 — single egress vs video generation.** Resolved as `D-06` (LiteLLM is the single egress for every *LLM* call; video uses a parallel capability registry under the same failure policy). Confirm with platform.

- **`D-59` — reproducibility deviation.** The PRD promises "per-shot cost, model, **seed** and prompt — every job is reproducible". The v1 provider has no seed. We deliver traceability, not bit-exact re-rendering. **This is user-visible and needs product sign-off**, not just an engineering decision.
- **`D-60` — `credits_per_usd` rate.** Must be configured from real Magic Hour pricing before the USD budget cap means anything.

- **QC calibration set (≥200 labelled pairs).** Deferred by `D-66`; the threshold is uncalibrated until it exists. Needs real credit spend. Blocks nothing in E0–E2.
- **Deferred epics E3 and E4.** Designs are complete in `qc.md`, `observability.md` and the deferred sections of `graph.md`/`assembly.md`/`api.md`/`harness.md`. Not cancelled.

## Needs numbers, currently illustrative defaults
- `D-08` budget caps (20 min wall-clock, 40 supersteps, 250k tokens, per-job USD ceiling).
- `D-37` QC dimension weights; to be re-fitted on the labelled calibration set.
- `D-40` calibration targets (false-pass ≤ 0.10, false-fail ≤ 0.20).

## Not owned by the documentation agent
- `.cdr/index/task.jsonl` and `.cdr/index/regression.jsonl` are empty; planning and verification runs must seed them.
- The QC labelled calibration set (≥ 200 shot pairs) is a delivery dependency for M4 — see `docs/LLD/qc.md` §5.
