"""Run one *complete* 4-shot job through the real graph against the real local dev stack,
using the real `MagicHourProvider` (with key rotation) instead of the mock — the actual
product pipeline end to end: real LLM planning, real continuity bible, four real Magic Hour
renders chained by real extracted frames, real assembly, real delivery.

Credit-aware: at ~240 credits per 10s/480p shot, four shots cost ~960. Only run this with a
combined balance (across every key in `MAGICHOUR_API_KEY`/`MAGICHOUR_API_KEY_2`) comfortably
above that.

Requires `make compose-up` and a `.env` with real `DATABASE_URL`/`REDIS_URL`/`ARTIFACT_*`/
`LITELLM_*`/`MAGICHOUR_*` values. Not part of the test suite, not linted or type-checked
(`scripts/` is outside the `src`/`tests` roots — a manual dev tool, not shipped code.

Usage: uv run python scripts/real_full_job_trial.py "a lighthouse at dawn, waves crashing"
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
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
from video_agent.providers.magichour import build_magichour_provider
from video_agent.providers.registry import PinnedProviderRegistry

# The tenant seeded by earlier local smoke runs; reused rather than inserted, since seeding a
# tenant row needs the database owner role and this script only ever needs the app role.
TRIAL_TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")

DEFAULT_PROMPT = "a violinist plays on a rooftop at dawn as the city wakes below"


def _field(obj: object, name: str) -> object:
    """`final["shots"]` entries may be dicts or still-typed `ShotState` objects depending on
    how the compiled graph happened to serialise this run — handle either without guessing."""
    return obj[name] if isinstance(obj, dict) else getattr(obj, name)

# Generous wall-clock cap for this one trial job only — real render queue time has been
# observed to vary widely; the default 20-minute cap would risk a PARTIAL result after
# real money was already spent on the shots that did finish.
TRIAL_MAX_WALL_CLOCK_S = 3600.0


async def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT
    settings = get_settings()

    engine = create_database_engine(settings)
    redis_client = create_redis_client(settings)
    object_transport = S3ObjectTransport(create_s3_client(settings), settings.ARTIFACT_BUCKET)
    artifacts = S3ArtifactStore(transport=object_transport)
    gateway_resources = build_gateway(settings, redis_client=redis_client)
    magichour_provider, magichour_client = build_magichour_provider(settings, artifacts=artifacts)
    providers = PinnedProviderRegistry(
        providers=(magichour_provider,),
        breaker=CircuitBreaker(store=InMemoryCircuitStateStore(), clock=SystemClock()),
    )
    key_count = len(settings.magichour_api_keys())
    print(f"magic hour keys configured: {key_count}")  # noqa: T201

    job_id = uuid4()
    caps = BudgetCaps(
        max_iterations=40,
        max_wall_clock_s=TRIAL_MAX_WALL_CLOCK_S,
        max_tokens=250_000,
        max_usd=Decimal(5),
    )
    async with tenant_session(engine, TRIAL_TENANT_ID) as session:
        record = await JobRepository(session).create(
            NewJob(
                idempotency_key=f"real-trial-{job_id}",
                request_fingerprint=f"real-trial-{job_id}",
                prompt=prompt,
                trace_id=f"real-trial-{job_id}",
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
    harness = JobHarness(job_id=record.id, shots_required=SHOT_COUNT)
    deps = GraphDeps(
        engine=engine,
        gateway=gateway_resources.gateway,
        checkpointer=InMemorySaver(),
        harness=harness,
        now=lambda: datetime.now(UTC),
        providers=providers,
        artifacts=artifacts,
    )
    compiled = build_graph(deps)

    print(f"running job {record.id} — prompt: {prompt!r} — model={settings.MAGICHOUR_MODEL}")  # noqa: T201
    raw_final = await compiled.ainvoke(
        state, config={"configurable": {"thread_id": str(record.id)}}
    )
    final = raw_final if isinstance(raw_final, dict) else raw_final.model_dump()
    print(f"outcome={final['outcome']!r} degraded={final['degraded']!r}")  # noqa: T201
    print(f"final_video_artifact_id={final['final_video_artifact_id']!r}")  # noqa: T201
    print(f"manifest={final['manifest']!r}")  # noqa: T201
    if magichour_provider.key_rotator is not None:
        print(f"final key index used: {magichour_provider.key_rotator.index}")  # noqa: T201
    for shot in final["shots"]:
        print(  # noqa: T201
            f"shot {_field(shot, 'index')}: status={_field(shot, 'status')!r} "
            f"attempts_used={_field(shot, 'attempts_used')} "
            f"repairs_used={_field(shot, 'repairs_used')}"
        )

    await gateway_resources.aclose()
    await magichour_client.aclose()
    await object_transport.aclose()
    await redis_client.aclose()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
