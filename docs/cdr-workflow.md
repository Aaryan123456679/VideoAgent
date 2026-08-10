# CDR workflow

← [Back to README](../README.md)

Canonical docs are maintained by CDR agents; their state lives in `.cdr/` (`runs/`, `index/`,
`memory/`, `schemas/`). Two entry points: **`init`** bootstraps HLD/LLDs/README from a repo
scan; **`sync`** regenerates only drifted sections after code changes, using
`.cdr/index/file.jsonl`.
