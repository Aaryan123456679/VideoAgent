# Setup — running Video Agent locally

← [Back to README](../README.md)

**Stack:** Python 3.12 (FastAPI, async), PostgreSQL 16, Redis 7, an S3-compatible store,
LiteLLM proxy, ffmpeg. Config lives in `.env` (see [`.env.example`](../.env.example)) — model
aliases and provider keys are never hardcoded in code.

## Mock provider — no account needed

Full pipeline (real API, Postgres/Redis/S3, real graph), shots rendered by
[`MockVideoProvider`](../src/video_agent/providers/mock.py) via ffmpeg — no network, no cost, no
auth required.

```bash
make compose-up                              # Postgres, Redis, MinIO, LiteLLM
uv run python scripts/dev_server.py          # terminal 1 — the API
uv run python scripts/dev_worker.py          # terminal 2 — worker, mock shots
cd ui && npm install && npm run dev          # terminal 3 — http://localhost:5173
```

Open the UI, enter a prompt, click **Create video**, and watch `current_node`/`budget` update
live. Headless one-shot: `uv run python scripts/mock_trial_run.py "your prompt here"`.

## Real Magic Hour run

Same API, graph, and UI — only the provider differs, and this spends real credits.

1. Get a key at [magichour.ai](https://magichour.ai/settings/developer) (`mhk_live_...`). One
   job renders 4 shots at ~240 credits each on the pinned model (`ltx-2.3`) — budget **at least
   ~1,000 credits**. A second key rotates in automatically on `402` (insufficient credits) only.
2. Fill in `.env`:
   ```bash
   MAGICHOUR_API_KEY=mhk_live_...
   MAGICHOUR_API_KEY_2=mhk_live_...     # optional — second account, rotated onto on 402 only
   MAGICHOUR_MODEL=ltx-2.3
   MAGICHOUR_USD_PER_1K_CREDITS=0.90    # match your account's billing tier
   ```
   `.env` is git-ignored and must never be committed.
3. Same four terminals as above, but use `scripts/real_dev_worker.py` in place of
   `dev_worker.py` (the latter is hardcoded to `MockVideoProvider`).

Gotchas: `get_settings()` is `@lru_cache`d — restart the server/worker after editing `.env`.
Use `http://localhost:5173`, not `127.0.0.1` (the Vite dev server binds IPv6 loopback). A real
4-shot job takes anywhere from ~6 to 30+ minutes depending on Magic Hour's queue depth.

Terminal equivalent of the UI's "Create video":

```bash
curl -s -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Authorization: Bearer dev-no-auth-placeholder-token-000000" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"prompt": "a violinist plays on a rooftop at dawn as the city wakes below"}'
```
