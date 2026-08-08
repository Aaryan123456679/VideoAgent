"""Serve the real API against the real local dev stack, with auth switched off and CORS
wide open, so `ui/` (a plain Vite dev server on a different port) can call it directly.

Dev/trial only. `AllowAllApiKeyVerifier` resolves every request to the one seeded trial
tenant regardless of what — if anything — is presented as a bearer token; it is defined here,
not in `src/`, so nothing in the shipped application can reach it by accident. Job processing
still needs a worker: run `scripts/dev_worker.py` alongside this to actually see jobs
complete rather than sit `queued` forever.

Usage: uv run python scripts/dev_server.py
"""

from __future__ import annotations

from uuid import UUID, uuid4

import uvicorn

from video_agent.api.app import create_app
from video_agent.api.principal import Principal, PresentedKey
from video_agent.config.settings import get_settings

# The tenant scripts/mock_trial_run.py also uses — seeded by earlier local smoke runs.
TRIAL_TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")

DEV_HOST = "127.0.0.1"
DEV_PORT = 8000


class AllowAllApiKeyVerifier:
    """Every presented credential resolves to the same fixed tenant. No credential store,
    no rejection path — a deliberate opposite of `api.principal.UnconfiguredApiKeyVerifier`,
    for a UI that asks for no auth at all rather than one waiting on a real one."""

    async def verify(self, presented: PresentedKey, /) -> Principal | None:
        del presented
        return Principal(tenant_id=TRIAL_TENANT_ID, key_id=uuid4())


def main() -> None:
    settings = get_settings()
    app = create_app(
        settings=settings,
        verifier=AllowAllApiKeyVerifier(),
        cors_origins=("*",),
    )
    uvicorn.run(app, host=DEV_HOST, port=DEV_PORT, log_level="info")


if __name__ == "__main__":
    main()
