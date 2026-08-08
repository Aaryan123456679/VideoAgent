"""Run the real `JobWorker` loop against the real local dev stack, rendering shots with
`MockVideoProvider` instead of a real, slow, account-limited render.

Run this alongside `scripts/dev_server.py` — the server only accepts jobs and enqueues them;
this is what actually drains the queue and moves them to a terminal state. Dev/trial only.

Usage: uv run python scripts/dev_worker.py
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
from video_agent.providers.mock import MockVideoProvider
from video_agent.providers.registry import PinnedProviderRegistry

CONSUMER_NAME = "dev-mock-worker"


async def main() -> None:
    settings = get_settings()

    engine = create_database_engine(settings)
    cache = build_cache(settings)
    object_transport = S3ObjectTransport(create_s3_client(settings), settings.ARTIFACT_BUCKET)
    artifacts = S3ArtifactStore(transport=object_transport)
    gateway_resources = build_gateway(settings, redis_client=cache.client)
    providers = PinnedProviderRegistry(
        providers=(MockVideoProvider(artifacts=artifacts),),
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

    print(f"dev worker '{CONSUMER_NAME}' running — rendering shots with MockVideoProvider")  # noqa: T201
    try:
        await worker.run_forever()
    finally:
        await gateway_resources.aclose()
        await object_transport.aclose()
        await cache.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
