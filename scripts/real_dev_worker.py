"""Run the real `JobWorker` loop against the real local dev stack, rendering shots with the
real `MagicHourProvider` (with key rotation) instead of `MockVideoProvider`.

Pairs with `scripts/dev_server.py` and `ui/` exactly like `scripts/dev_worker.py` does — same
API, same graph, same dashboard, only the provider differs. Run this instead of
`scripts/dev_worker.py` (not alongside it: both would race to claim the same queued jobs) when
you want a job submitted from the dashboard to render for real and spend real credits.

Dev/trial only, same as `dev_worker.py`.

Usage: uv run python scripts/real_dev_worker.py
"""

from __future__ import annotations

import asyncio

from video_agent.api.clients import build_cache, build_gateway
from video_agent.config.settings import get_settings
from video_agent.gateway.breaker import CircuitBreaker, InMemoryCircuitStateStore
from video_agent.gateway.clock import SystemClock
from video_agent.graph.lock import JobLock
from video_agent.graph.worker import JobWorker, WorkerResources
from video_agent.persistence.objects import S3ObjectTransport, create_s3_client
from video_agent.persistence.session import create_database_engine
from video_agent.providers.artifact_store import S3ArtifactStore
from video_agent.providers.magichour import build_magichour_provider
from video_agent.providers.registry import PinnedProviderRegistry

CONSUMER_NAME = "dev-real-worker"


async def main() -> None:
    settings = get_settings()

    engine = create_database_engine(settings)
    cache = build_cache(settings)
    object_transport = S3ObjectTransport(create_s3_client(settings), settings.ARTIFACT_BUCKET)
    artifacts = S3ArtifactStore(transport=object_transport)
    gateway_resources = build_gateway(settings, redis_client=cache.client)
    magichour_provider, magichour_client = build_magichour_provider(settings, artifacts=artifacts)
    providers = PinnedProviderRegistry(
        providers=(magichour_provider,),
        breaker=CircuitBreaker(store=InMemoryCircuitStateStore(), clock=SystemClock()),
    )

    worker = JobWorker(
        consumer=CONSUMER_NAME,
        queue=cache.queue,
        lock=JobLock(cache.store),
        resources=WorkerResources(
            engine=engine,
            gateway=gateway_resources.gateway,
            providers=providers,
            artifacts=artifacts,
        ),
        cancel_store=cache.store,
    )

    key_count = len(settings.magichour_api_keys())
    print(  # noqa: T201
        f"dev worker '{CONSUMER_NAME}' running — rendering shots with the real "
        f"MagicHourProvider (model={settings.MAGICHOUR_MODEL}, keys_configured={key_count}). "
        "This spends real credits."
    )
    try:
        await worker.run_forever()
    finally:
        await magichour_client.aclose()
        await gateway_resources.aclose()
        await object_transport.aclose()
        await cache.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
