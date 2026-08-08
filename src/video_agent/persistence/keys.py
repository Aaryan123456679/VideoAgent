"""The typed Redis key registry: every key pattern in `persistence.md` §5, once.

`S0.6.1`. Redis holds cache, locks, rate limits, idempotency and progress, and is **never
authoritative** `[persistence.md §5]`. The keys are therefore not an implementation detail of
whichever module happened to need one — they are a schema, and this module is its single
definition.

**One place per pattern.** A raw pattern string appears exactly once, in `KEY_REGISTRY`. Every
constructor renders from `spec.pattern`, so there is no second spelling to drift. The failure
this prevents is not a typo — a typo is loud, the read misses — but a *rename*: a key written
under `progress:{job_id}` and read under `progress:{job}` produces an empty SSE stream and no
error anywhere.

**The TTL travels with the key, not with the call site.** `RedisKey` carries the TTL the
registry documents, and `persistence.redis_client` refuses to write a key that has none unless
the registry says the key is deliberately TTL-less. Redis is a cache with a job to do; a key
written without an expiry stays until someone notices the memory, and the someone is usually
an incident.

**Three TTL policies, because §5 genuinely has three.** Most keys have a fixed documented
expiry. Two do not: `sig:{job_id}` expires with *its job*, and `rl:{tenant}:{window}` expires
with *its window* — both are values the caller knows and the registry cannot. Those are
`TtlPolicy.CALLER`, and the constructor demands the number rather than defaulting it, because a
default here would silently be wrong for every caller that forgot. `jobs:stream` is the single
`TtlPolicy.NONE`: it is the job queue `[D-67]`, and a queue that expires drops work.

**Ad hoc keys are a static-check failure, not a review comment.** `REGISTERED_PREFIXES` is what
`tests/unit/test_persistence_redis_keys.py` scans `src/` for: a string literal starting with any
of them, outside this module, fails the build. That is the rule that keeps the paragraph above
true — a registry nobody is obliged to use documents the keys that happen to go through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

# --- Types ---------------------------------------------------------------------------------


class KeyName(StrEnum):
    """The names of the key patterns in `persistence.md` §5, one per table row."""

    IDEMPOTENCY = "idempotency"
    JOB_LOCK = "job_lock"
    JOBS_STREAM = "jobs_stream"
    PROGRESS = "progress"
    FAILURE_SIGNATURE = "failure_signature"
    RATE_LIMIT = "rate_limit"
    CIRCUIT_BREAKER = "circuit_breaker"
    LLM_CACHE = "llm_cache"


class RedisType(StrEnum):
    """The Redis data type a key holds. Part of the schema, so it is declared, not remembered."""

    HASH = "hash"
    STRING = "string"
    STREAM = "stream"


class TtlPolicy(StrEnum):
    """How a key's expiry is decided.

    `FIXED` — the registry knows the number and applies it.
    `CALLER` — the number is a property of the thing the key describes (the job, the rate-limit
    window) and only the caller has it. The constructor requires it; there is no default.
    `NONE` — the key deliberately does not expire, and the write path must not invent one.
    """

    FIXED = "fixed"
    CALLER = "caller"
    NONE = "none"


SECONDS_PER_HOUR: Final = 3600


@dataclass(frozen=True, slots=True)
class KeySpec:
    """One row of `persistence.md` §5: the pattern, its type, and how it expires."""

    pattern: str
    redis_type: RedisType
    ttl_policy: TtlPolicy
    ttl_seconds: int | None
    purpose: str

    @property
    def prefix(self) -> str:
        """The literal head of the pattern, up to the first placeholder.

        What the static check greps for. Derived rather than declared so a pattern change
        cannot leave the scanner looking for the old prefix — which would make the check pass
        by finding nothing.
        """
        head, separator, _ = self.pattern.partition("{")
        return head if separator else self.pattern


@dataclass(frozen=True, slots=True)
class RedisKey:
    """A rendered key, the registry entry it came from, and the TTL it must be written with.

    Every operation in `persistence.redis_client` takes one of these and never a `str`, so
    "the TTL was applied" is a property of the value rather than of the caller's memory.
    """

    name: KeyName
    value: str
    ttl_seconds: int | None

    def __str__(self) -> str:
        """The key as Redis sees it."""
        return self.value


# --- The registry ----------------------------------------------------------------------------

IDEMPOTENCY_TTL_SECONDS: Final = 24 * SECONDS_PER_HOUR
"""24h `[persistence.md §5]`, `[D-16]`. The same window `api.idempotency` documents; asserted
equal to it by `test_idempotency_ttl_matches_the_api_module`, because two constants that must
agree and are never compared are two constants that will disagree."""

JOB_LOCK_TTL_SECONDS: Final = 60
"""60s with a heartbeat `[D-10]`. Short on purpose: the lock is what makes one writer per job
true, so a crashed writer has to stop being the writer quickly or the job stalls for the TTL."""

PROGRESS_TTL_SECONDS: Final = SECONDS_PER_HOUR
CIRCUIT_BREAKER_TTL_SECONDS: Final = 300
LLM_CACHE_TTL_SECONDS: Final = SECONDS_PER_HOUR

KEY_REGISTRY: Final[dict[KeyName, KeySpec]] = {
    KeyName.IDEMPOTENCY: KeySpec(
        pattern="idem:{tenant}:{route}:{key}",
        redis_type=RedisType.HASH,
        ttl_policy=TtlPolicy.FIXED,
        ttl_seconds=IDEMPOTENCY_TTL_SECONDS,
        purpose="Idempotency record; mirrored by the job_idem_uq constraint [D-16]",
    ),
    KeyName.JOB_LOCK: KeySpec(
        pattern="job:{job_id}",
        redis_type=RedisType.STRING,
        ttl_policy=TtlPolicy.FIXED,
        ttl_seconds=JOB_LOCK_TTL_SECONDS,
        purpose="One writer per job, fencing token [D-10]",
    ),
    KeyName.JOBS_STREAM: KeySpec(
        pattern="jobs:stream",
        redis_type=RedisType.STREAM,
        ttl_policy=TtlPolicy.NONE,
        ttl_seconds=None,
        purpose="Job queue, at-least-once delivery via a consumer group [D-67]",
    ),
    KeyName.PROGRESS: KeySpec(
        pattern="progress:{job_id}",
        redis_type=RedisType.STREAM,
        ttl_policy=TtlPolicy.FIXED,
        ttl_seconds=PROGRESS_TTL_SECONDS,
        purpose="SSE progress events [D-09]",
    ),
    KeyName.FAILURE_SIGNATURE: KeySpec(
        pattern="sig:{job_id}",
        redis_type=RedisType.HASH,
        ttl_policy=TtlPolicy.CALLER,
        ttl_seconds=None,
        purpose="Failure-signature counts; mirrored into the checkpoint [D-02]",
    ),
    KeyName.RATE_LIMIT: KeySpec(
        pattern="rl:{tenant}:{window}",
        redis_type=RedisType.STRING,
        ttl_policy=TtlPolicy.CALLER,
        ttl_seconds=None,
        purpose="Rate limit token bucket; expires with its window",
    ),
    KeyName.CIRCUIT_BREAKER: KeySpec(
        pattern="cb:{dependency}",
        redis_type=RedisType.HASH,
        ttl_policy=TtlPolicy.FIXED,
        ttl_seconds=CIRCUIT_BREAKER_TTL_SECONDS,
        purpose="Circuit-breaker state, shared across workers [D-22]",
    ),
    KeyName.LLM_CACHE: KeySpec(
        pattern="cache:llm:{hash}",
        redis_type=RedisType.STRING,
        ttl_policy=TtlPolicy.FIXED,
        ttl_seconds=LLM_CACHE_TTL_SECONDS,
        purpose="Gateway response cache; never for planning or the bible",
    ),
}
"""`persistence.md` §5, transcribed once. `test_all_documented_keys_have_constructors` diffs
this against the LLD table itself, so a row added to the document and not to this dict fails."""

REGISTERED_PREFIXES: Final[tuple[str, ...]] = tuple(
    sorted({spec.prefix for spec in KEY_REGISTRY.values()})
)
"""Every literal key head, for the static check. Derived from the registry, never listed."""


# --- Rendering -------------------------------------------------------------------------------


class UnregisteredKeyError(LookupError):
    """A key name with no registry entry. Only reachable by constructing `KeyName` dynamically."""


class KeySegmentError(ValueError):
    """A segment that would change the shape of the rendered key.

    Redis keys are `:`-separated by convention and this registry treats that as structure. A
    route containing a `:` would silently move a boundary — `idem:{tenant}:{route}:{key}` with
    a route of `a:b` and a key of `c` renders identically to a route of `a` and a key of `b:c`,
    so two different requests would share one idempotency record.

    The rule applies to **non-terminal** segments only. The last placeholder in a pattern has
    nothing after it, so no `:` inside it can be mistaken for a boundary — and the last
    placeholder is usually the one this system does not control: the client chooses its own
    `Idempotency-Key`, and `order:12345` is a perfectly reasonable thing for it to send.
    Rejecting that would turn a valid request into a `500` in the name of a collision that
    cannot happen.
    """


class MissingTtlError(ValueError):
    """A `TtlPolicy.CALLER` key was constructed without the TTL only the caller knows."""


def spec_for(name: KeyName) -> KeySpec:
    """The registry entry for `name`, or a clear failure."""
    try:
        return KEY_REGISTRY[name]
    except KeyError as exc:
        message = f"{name} has no entry in KEY_REGISTRY"
        raise UnregisteredKeyError(message) from exc


_CONTROL_CHARACTER_CEILING: Final = 32
"""Anything below `space` is a control character. A newline inside a key breaks every log line
and every `redis-cli` transcript that quotes it, and cannot be intended."""


def _segment(value: str, field: str, *, terminal: bool) -> str:
    """Validate one interpolated segment. Rejected, never sanitised — see `KeySegmentError`."""
    if not value or value.strip() != value:
        message = f"{field} is empty or padded; it would render an ambiguous key"
        raise KeySegmentError(message)
    if any(ord(character) < _CONTROL_CHARACTER_CEILING for character in value):
        message = f"{field} contains a control character"
        raise KeySegmentError(message)
    if not terminal and ":" in value:
        message = f"{field} contains the segment separator and is not the last segment"
        raise KeySegmentError(message)
    return value


def _terminal_field(pattern: str) -> str | None:
    """The name of the last placeholder in `pattern`, when the pattern ends with one."""
    if not pattern.endswith("}"):
        return None
    return pattern.rsplit("{", 1)[1].removesuffix("}")


def _render(name: KeyName, ttl_seconds: int | None = None, /, **fields: str) -> RedisKey:
    """Render `name`'s pattern with `fields`, and attach the TTL the policy requires."""
    spec = spec_for(name)
    if spec.ttl_policy is TtlPolicy.FIXED:
        resolved: int | None = spec.ttl_seconds
    elif spec.ttl_policy is TtlPolicy.CALLER:
        if ttl_seconds is None or ttl_seconds <= 0:
            message = f"{name} expires with its subject; a positive ttl_seconds is required"
            raise MissingTtlError(message)
        resolved = ttl_seconds
    else:
        resolved = None
    terminal = _terminal_field(spec.pattern)
    validated = {
        field: _segment(value, field, terminal=field == terminal) for field, value in fields.items()
    }
    return RedisKey(name=name, value=spec.pattern.format(**validated), ttl_seconds=resolved)


# --- Constructors ------------------------------------------------------------------------------
#
# One per registry entry. `test_all_documented_keys_have_constructors` asserts the mapping is
# total in both directions, so a new row in §5 cannot ship without one.


def idempotency_key(tenant_id: UUID, route: str, key: str) -> RedisKey:
    """`idem:{tenant}:{route}:{key}` — the 24h idempotency record `[D-16]`.

    `tenant_id` is a `UUID` rather than a string for the reason `session.tenant_session` gives:
    the value comes from `Principal.tenant_id` and from nowhere else, and requiring a parsed
    `UUID` makes a value that arrived as text from a request body impossible to pass here
    without someone writing the parse.
    """
    return _render(KeyName.IDEMPOTENCY, tenant=str(tenant_id), route=route, key=key)


def job_lock_key(job_id: UUID) -> RedisKey:
    """`job:{job_id}` — the 60s fencing token that makes one writer per job true `[D-10]`."""
    return _render(KeyName.JOB_LOCK, job_id=str(job_id))


def jobs_stream_key() -> RedisKey:
    """`jobs:stream` — the job queue `[D-67]`. Deliberately without a TTL: a queue that
    expires drops work, and the work is a partially-billed job."""
    return _render(KeyName.JOBS_STREAM)


def progress_key(job_id: UUID) -> RedisKey:
    """`progress:{job_id}` — the 1h SSE progress stream `[D-09]`."""
    return _render(KeyName.PROGRESS, job_id=str(job_id))


def failure_signature_key(job_id: UUID, ttl_seconds: int) -> RedisKey:
    """`sig:{job_id}` — failure-signature counts, expiring with the job `[D-02]`.

    The TTL is the job's, so the caller supplies it. Redis is not authoritative here either:
    the same counts are mirrored into the checkpoint, which is what survives a flush.
    """
    return _render(KeyName.FAILURE_SIGNATURE, ttl_seconds, job_id=str(job_id))


def rate_limit_key(tenant_id: UUID, window: str, ttl_seconds: int) -> RedisKey:
    """`rl:{tenant}:{window}` — a token bucket expiring with its own window."""
    return _render(KeyName.RATE_LIMIT, ttl_seconds, tenant=str(tenant_id), window=window)


def circuit_breaker_key(dependency: str) -> RedisKey:
    """`cb:{dependency}` — 5m of shared breaker state `[D-22]`."""
    return _render(KeyName.CIRCUIT_BREAKER, dependency=dependency)


def llm_cache_key(request_hash: str) -> RedisKey:
    """`cache:llm:{hash}` — a 1h gateway response cache.

    Never for planning or the continuity bible: those are the artefacts a job's reproducibility
    record is built from, and a cached plan makes two jobs claim a provenance only one of them
    has. The registry cannot enforce that; the gateway's own cache policy does.
    """
    return _render(KeyName.LLM_CACHE, hash=request_hash)


KEY_CONSTRUCTORS: Final[dict[KeyName, str]] = {
    KeyName.IDEMPOTENCY: idempotency_key.__name__,
    KeyName.JOB_LOCK: job_lock_key.__name__,
    KeyName.JOBS_STREAM: jobs_stream_key.__name__,
    KeyName.PROGRESS: progress_key.__name__,
    KeyName.FAILURE_SIGNATURE: failure_signature_key.__name__,
    KeyName.RATE_LIMIT: rate_limit_key.__name__,
    KeyName.CIRCUIT_BREAKER: circuit_breaker_key.__name__,
    KeyName.LLM_CACHE: llm_cache_key.__name__,
}
"""Registry entry to the function that renders it. The test resolves each name against this
module, so an entry naming a function that does not exist is a failure rather than a comment."""
