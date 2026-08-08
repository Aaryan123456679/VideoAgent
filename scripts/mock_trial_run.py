"""Run one job through the real graph against the real local dev stack, with the shot
provider swapped for `video_agent.providers.mock.MockVideoProvider` — no network call, no
credits, no wait. For trial and testing only, while a real render is slow or account-limited.

Requires `make compose-up` and a `.env` with real `DATABASE_URL`/`REDIS_URL`/`ARTIFACT_*`/
`LITELLM_*` values. Not part of the test suite, not linted or type-checked (`scripts/` is
outside the `src`/`tests` roots `pyproject.toml` and the Makefile scan) — a manual dev tool,
not shipped code.

Usage: uv run python scripts/mock_trial_run.py "a lighthouse at dawn, waves crashing"
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver

from video_agent.api.clients import build_gateway
from video_agent.config.settings import get_settings
from video_agent.gateway.breaker import CircuitBreaker, InMemoryCircuitStateStore
from video_agent.gateway.clock import SystemClock
from video_agent.graph.build import build_graph
from video_agent.graph.deps import GraphDeps
from video_agent.graph.guard import JobHarness
from video_agent.graph.state import SHOT_COUNT, JobState
from video_agent.harness.budget import BudgetCaps, BudgetLedger
from video_agent.persistence.objects import S3ObjectTransport, create_s3_client
from video_agent.persistence.redis_client import create_redis_client
from video_agent.persistence.repositories import JobRepository, NewJob
from video_agent.persistence.session import create_database_engine, tenant_session
from video_agent.providers.artifact_store import S3ArtifactStore
from video_agent.providers.mock import MockVideoProvider
from video_agent.providers.registry import PinnedProviderRegistry

# The tenant seeded by earlier local smoke runs; reused rather than inserted, since seeding a
# tenant row needs the database owner role and this script only ever needs the app role.
TRIAL_TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")

DEFAULT_PROMPT = "a lighthouse at dawn, waves crashing against dark rocks"


async def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT
    settings = get_settings()

    engine = create_database_engine(settings)
    redis_client = create_redis_client(settings)
    object_transport = S3ObjectTransport(create_s3_client(settings), settings.ARTIFACT_BUCKET)
    artifacts = S3ArtifactStore(transport=object_transport)
    gateway_resources = build_gateway(settings, redis_client=redis_client)
    providers = PinnedProviderRegistry(
        providers=(MockVideoProvider(artifacts=artifacts),),
        breaker=CircuitBreaker(store=InMemoryCircuitStateStore(), clock=SystemClock()),
    )

    job_id = uuid4()
    caps = BudgetCaps.from_settings(settings)
    async with tenant_session(engine, TRIAL_TENANT_ID) as session:
        record = await JobRepository(session).create(
            NewJob(
                idempotency_key=f"mock-trial-{job_id}",
                request_fingerprint=f"mock-trial-{job_id}",
                prompt=prompt,
                trace_id=f"mock-trial-{job_id}",
                budget_caps=caps.model_dump(mode="json"),
            )
        )

    state = JobState(
        job_id=record.id,
        tenant_id=TRIAL_TENANT_ID,
        trace_id=record.trace_id,
        prompt=record.prompt,
        music_bed=record.music_bed,
        budget=BudgetLedger(caps=caps, started_at=datetime.now(UTC)),
    )
    deps = GraphDeps(
        engine=engine,
        gateway=gateway_resources.gateway,
        checkpointer=InMemorySaver(),
        harness=JobHarness(job_id=record.id, shots_required=SHOT_COUNT),
        now=lambda: datetime.now(UTC),
        providers=providers,
        artifacts=artifacts,
    )
    compiled = build_graph(deps)

    print(f"running job {record.id} — prompt: {prompt!r}")  # noqa: T201
    raw_final = await compiled.ainvoke(
        state, config={"configurable": {"thread_id": str(record.id)}}
    )
    final = raw_final if isinstance(raw_final, dict) else raw_final.model_dump()
    print(f"outcome={final['outcome']!r} degraded={final['degraded']!r}")  # noqa: T201
    print(f"final_video_artifact_id={final['final_video_artifact_id']!r}")  # noqa: T201
    print(f"manifest={final['manifest']!r}")  # noqa: T201

    await gateway_resources.aclose()
    await object_transport.aclose()
    await redis_client.aclose()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
