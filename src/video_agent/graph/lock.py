"""One writer per job, enforced by a Redis lock with a fencing token. `graph.md` §6.2, `[D-10]`.

A lease, not a mutex a live connection holds open: a worker claims `job:{job_id}` with a TTL
and renews it by heartbeat. A crashed or partitioned worker simply stops renewing, and the lock
expires within `JOB_LOCK_TTL_SECONDS` for another worker to reclaim — nothing has to notice the
crash for the job to become claimable again.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError
from video_agent.persistence.keys import job_lock_key
from video_agent.persistence.redis_client import RedisStore

__all__ = ["JobLock", "JobLockLostError", "LockToken"]


class JobLockLostError(VideoAgentError):
    """This worker's fencing token no longer matches Redis — another worker now holds the job.

    Raised by `heartbeat()`, not by `acquire()`: the caller decides when it is safe to check,
    and `graph.md` §6.2 requires the resulting abandon to happen *after* the current
    transaction commits, never mid-write. Detecting the loss is this module's job; timing the
    abandon around a transaction boundary is the worker's.
    """

    code = ErrorCode.VA_INT_001


@dataclass(frozen=True, slots=True)
class LockToken:
    """Proof of ownership for one job's lock. Opaque outside this module."""

    job_id: UUID
    fencing_token: str


class JobLock:
    """`job:{job_id}`, claimed with `SET NX EX`, held with a fencing token. `graph.md` §6.2."""

    def __init__(self, store: RedisStore) -> None:
        self._store = store

    async def acquire(self, job_id: UUID) -> LockToken | None:
        """Claim the lock, or `None` if another worker already holds it."""
        token = uuid4().hex
        created = await self._store.set_if_absent(job_lock_key(job_id), token)
        return LockToken(job_id=job_id, fencing_token=token) if created else None

    async def heartbeat(self, token: LockToken) -> None:
        """Renew the TTL, after confirming this token is still the one Redis holds.

        Read-then-write, not compare-and-swap — there is a race window between the two. Accepted
        for v1 because the fencing token is the real safety net: a worker that loses this race
        still notices on its *next* heartbeat or at release, and every node in this graph is
        already required to be safe to run twice (`graph.md` §6.1), so a missed detection costs
        one extra superstep, never a second writer's write going unnoticed forever.
        """
        key = job_lock_key(token.job_id)
        current = await self._store.get(key)
        if current != token.fencing_token:
            message = f"job {token.job_id}'s lock is now held by a different worker"
            raise JobLockLostError(message)
        await self._store.set(key, token.fencing_token)

    async def release(self, token: LockToken) -> None:
        """Release, but only if this token still owns the lock.

        Releasing unconditionally would let an expired-and-reclaimed lock be torn down by the
        worker that just lost it, handing the job to two writers at once — exactly what the
        fencing token exists to prevent.
        """
        key = job_lock_key(token.job_id)
        current = await self._store.get(key)
        if current == token.fencing_token:
            await self._store.delete(key)
