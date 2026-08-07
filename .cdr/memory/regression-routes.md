# Regression Routes

No executable regression routes yet — there is no application code. Recorded here so the
first implementation runs inherit them; each becomes a `.cdr/index/regression.jsonl` entry
once a test path exists.

| Trigger | Retest route (planned) | Defined in |
| --- | --- | --- |
| Any change under `docs/specs/` | Full CDR documentation `sync` | `README.md` |
| Graph topology change | Topology snapshot + guard-coverage + single-cycle assertions | `graph.md` §9 |
| Any new conditional edge | Assert it calls the harness guard first | `graph.md` §9 |
| Any new work-creating `POST` | Concurrent-duplicate idempotency property test | `api.md` §9 |
| Any new table or column | RLS matrix across all tables + old-code/new-schema migration test | `persistence.md` §10 |
| QC prompt or `vision-default` alias change | Calibration on the labelled set; block on > 3% regression | `qc.md` §9 |
| Any prompt or model change | 10% canary with Langfuse score comparison; auto-rollback | `gateway.md` §9 |
| Any new logged field | Redaction canary test (secrets, PII, presigned URLs, media bytes) | `observability.md` §11 |
| ffmpeg version bump | Golden-media byte-stability + normalisation profile tests | `assembly.md` §9 |
| Provider adapter added or changed | Shared protocol-conformance suite + recorded Magic Hour HTTP transcripts | `providers.md` §10 |
| `MAGICHOUR_MODEL` changed | Startup duration validation — the model must permit a 10s clip (`sora-2` cannot) | `providers.md` §7.1, `D-61` |
| `MAGICHOUR_RESOLUTION` changed | Assembly normalisation profile tests must follow the config, not a hard-coded 1080p | `assembly.md` §9, `D-63` |
| Provider pricing or `MAGICHOUR_USD_PER_1K_CREDITS` changed | Ledger reconciliation tests; USD cap enforcement; assert no rate literal | `harness.md` §9, `D-60`/`D-65` |
| `QC_ACCEPT_THRESHOLD` set to a non-default | Assert it is logged at startup **and** surfaced on the job manifest | `qc.md` §9, `D-71` |
| A new table added | RLS matrix **plus** the asserted two-table exemption list | `persistence.md` §10, `D-70`/`D-68` |
| A prompt under `prompts/` changed | Offline-bootstrap test (Langfuse unreachable) + startup registration idempotency | `observability.md` §11, `D-72` |
