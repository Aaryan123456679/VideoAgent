"""The job worker: claim a delivery, hold the job lock, run the graph, ack. `graph.md` §6, S1.4.1.

**v1 crash-recovery is simpler than `graph.md` §5's full resume design.** That section's
`resume()`/`regenerate_shot()`/checkpoint-precise re-entry is E3 scope per the doc's own status
header. What v1 actually does when a worker dies mid-job: the lock's TTL expires, `XAUTOCLAIM`
reassigns the stalled entry to another worker (`claim_stalled`, `[D-67]`), and that worker
re-runs the job **from the graph's entry point**, not from the last checkpoint — there is
nothing yet that reconstructs a mid-job `JobState` from a checkpoint row. This is safe, not
free: `generate_shot`'s own request-fingerprint idempotency (`[D-24]`) means a re-run cannot
double-bill a shot that already has a provider attempt in flight, but a re-run before that point
(during `plan_story`/`lock_bible`) does re-spend one LLM call. Documented here rather than
silently assumed, and the gap this leaves — precise resume — is exactly what `graph.md` §5
already scopes to E3.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from langgraph.checkpoint.memory import InMemorySaver

from video_agent.graph.build import build_graph
from video_agent.graph.deps import GraphDeps
from video_agent.graph.guard import JobHarness
from video_agent.graph.lock import JobLock, LockToken
from video_agent.graph.state import SHOT_COUNT, JobState
from video_agent.harness.budget import BudgetCaps, BudgetLedger
from video_agent.observability.errors import VideoAgentError
from video_agent.persistence.queue import Delivery, JobQueue
from video_agent.persistence.repositories import JobRepository
from video_agent.persistence.session import tenant_session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncEngine

    from video_agent.gateway.gateway import Gateway
    from video_agent.persistence.repositories import JobRecord

__all__ = ["JobNotFoundError", "JobWorker"]

HEARTBEAT_INTERVAL_S: float = 20.0
"""Well under `JOB_LOCK_TTL_SECONDS` (60s, `[D-10]`) — several heartbeats before a lock could
expire out from under a still-healthy worker."""

ORPHAN_MIN_IDLE_MS: int = 90_000
"""A margin over the lock TTL: an entry idle this long was held by a worker whose lock has
already lapsed, not one running a slow superstep."""


class JobNotFoundError(VideoAgentError):
    """A queue entry named a job with no row. The queue outlived the job it pointed to."""


class JobWorker:
    """Claims deliveries off `JobQueue`, runs the compiled graph to completion, releases both.

    One worker instance is one consumer identity; run several processes with distinct
    `consumer` names for concurrency across jobs (`graph.md` §6.1: jobs run concurrently,
    exactly one writer per job, never one writer across several).
    """

    def __init__(
        self,
        *,
        consumer: str,
        queue: JobQueue,
        lock: JobLock,
        engine: AsyncEngine,
        gateway: Gateway,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._consumer = consumer
        self._queue = queue
        self._lock = lock
        self._engine = engine
        self._gateway = gateway
        self._clock = clock

    async def run_forever(self, *, poll_block_ms: int = 5000) -> None:
        """Poll for work indefinitely. Intended to be the whole body of a worker process."""
        await self._queue.ensure_group()
        while True:
            await self._drain(
                await self._queue.claim_stalled(self._consumer, min_idle_ms=ORPHAN_MIN_IDLE_MS)
            )
            await self._drain(await self._queue.read_own_pending(self._consumer))
            await self._drain(
                await self._queue.read_new(self._consumer, block_ms=poll_block_ms)
            )

    async def _drain(self, deliveries: list[Delivery]) -> None:
        for delivery in deliveries:
            await self.handle_one(delivery)

    async def handle_one(self, delivery: Delivery) -> None:
        """Process exactly one delivery. Public so tests and `run_forever` share one path."""
        job_id = delivery.message.job_id
        token = await self._lock.acquire(job_id)
        if token is None:
            return  # another worker already owns this job; leave it pending for XAUTOCLAIM

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(token))
        try:
            await self._run_job(delivery.message.tenant_id, job_id)
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            await self._lock.release(token)
        await self._queue.ack(delivery.entry_id)

    async def _heartbeat_loop(self, token: LockToken) -> None:
        """Renew the lock until cancelled. A `JobLockLostError` here propagates and, per
        `graph.md` §6.2's `[D-10]`, must not be swallowed — losing the lock mid-job means a
        second worker now owns it, and this one has to stop.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            await self._lock.heartbeat(token)

    async def _run_job(self, tenant_id: UUID, job_id: UUID) -> None:
        async with tenant_session(self._engine, tenant_id) as session:
            job_record = await JobRepository(session).get(job_id)
        if job_record is None:
            message = f"job {job_id} has no row; the queue message outlived its job"
            raise JobNotFoundError(message)

        state = _fresh_state(job_record)
        harness = JobHarness(job_id=job_id, shots_required=SHOT_COUNT)
        deps = GraphDeps(
            engine=self._engine,
            gateway=self._gateway,
            checkpointer=InMemorySaver(),
            harness=harness,
            now=self._clock,
        )
        compiled = build_graph(deps)
        await compiled.ainvoke(state, config={"configurable": {"thread_id": str(job_id)}})


def _fresh_state(job: JobRecord) -> JobState:
    caps = BudgetCaps.model_validate(job.budget_caps) if job.budget_caps else _fallback_caps()
    return JobState(
        job_id=job.id,
        tenant_id=job.tenant_id,
        trace_id=job.trace_id,
        prompt=job.prompt,
        music_bed=job.music_bed,
        budget=BudgetLedger(caps=caps, started_at=datetime.now(UTC)),
    )


def _fallback_caps() -> BudgetCaps:
    """A job row created before `budget_caps` was populated. Generous, not unlimited — a
    programming-error fallback, not a policy.
    """
    return BudgetCaps(
        max_iterations=40, max_wall_clock_s=3600.0, max_tokens=200_000, max_usd=Decimal(20)
    )
