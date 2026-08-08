"""Minting a presigned URL, which is a bearer credential and is treated as one. `[D-52]`

`S0.6.3`, `persistence.md` §6. Anyone holding the URL can read the object until it expires;
the authorisation is in the query string. That single fact decides everything in this module:

**It is returned and then forgotten.** No `return` value is stored, cached or memoised. There
is no `lru_cache`, and `test_no_memoisation` asserts the function carries no cache wrapper —
because a cache would hand a later caller a URL whose remaining lifetime is not
`PRESIGNED_URL_TTL_SECONDS` but whatever is left of an earlier one, and would keep a live
credential in process memory for the duration.

**It is never written down.** Not to Postgres — `artifact` has no URL column, and `S0.5`'s
schema is where that is enforced. Not to Redis. Not to a log line or a span attribute:
`observability.redaction` drops the shape anywhere in a string, and this module emits no log at
all rather than relying on that. The redaction rule is the net; not logging is the floor.
`[observability.md §5]` covers the provider's `upload_url` and `downloads[].url` under the same
rule `[D-58]`, `[D-64]`.

**A failure produces a null URL, not a missing artifact.** `persistence.md` §9: *presign fails →
`VA-STORE-002`; the manifest still lists the artifact with a null URL.* Dropping the artifact
from the manifest would tell the caller the render did not happen, when it did and was billed.
`mint_artifact_url` raises so a caller that needs the URL fails honestly; `mint_or_null` is the
manifest path, and it alarms rather than passing silently.

**The TTL comes from `PRESIGNED_URL_TTL_SECONDS` and from nowhere else.** There is no default
in this module and no per-call override that a route could widen — an expiry is the only thing
limiting the blast radius of a leaked link.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from video_agent.observability.alarms import AlarmCounter
from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.config.settings import Settings

PRESIGN_FAILURE_ALARM: Final = AlarmCounter("persistence.presign_failed")
"""Counts artifacts delivered with a null URL. A manifest full of nulls is a delivery outage
that every individual response reports as a success, so the count is the only signal."""


class PresignFailedError(VideoAgentError):
    """A URL could not be minted. `VA-STORE-002`, retryable `[persistence.md §9]`."""

    code = ErrorCode.VA_STORE_002


class UrlMinter(Protocol):
    """The one operation this module needs from an object store.

    A protocol with exactly one method, and no `put_object`, no `delete_object` and no method
    parameter. `persistence.objects.S3ObjectTransport.presign_get` signs `get_object` as a
    constant, so "the URL is `GET`-only" is a property of there being nothing else to ask for.
    """

    def presign_get(self, key: str, ttl_seconds: int) -> str:
        """Sign a `GET` for `key`, valid for `ttl_seconds`."""
        ...  # pragma: no cover - protocol declaration


@dataclass(frozen=True, slots=True)
class ArtifactUrl:
    """One artifact's delivery link, or the honest absence of one.

    `url` is `None` and never omitted. `api.md`'s manifest lists the artifact either way; a
    caller polling for a link needs to see that the artifact exists and the link does not.
    """

    storage_key: str
    url: str | None

    @property
    def available(self) -> bool:
        """Whether a link was minted for this artifact."""
        return self.url is not None


def presign_ttl(settings: Settings) -> int:
    """`PRESIGNED_URL_TTL_SECONDS`. One reader, so there is one place to change the expiry."""
    return settings.PRESIGNED_URL_TTL_SECONDS


def mint_artifact_url(minter: UrlMinter, storage_key: str, ttl_seconds: int) -> str:
    """Sign a `GET` for `storage_key`, or raise `VA-STORE-002`.

    The failure message names the storage key and the exception *type*. It does not carry the
    underlying exception's text: `botocore` puts request context and, on some paths, a partially
    constructed URL into that string, and an exception message reaches an HTTP error body and a
    traceback — neither of which the redaction serialiser sees.

    Nothing here is logged. The one thing worth logging would be the URL, and that is the one
    thing that must never be logged, so the caller's own span — which carries `storage_key` and
    `artifact_id`, both allow-listed — is where this shows up.
    """
    if ttl_seconds <= 0:
        message = f"presigned URL TTL must be positive; got {ttl_seconds}"
        raise PresignFailedError(message)
    try:
        return minter.presign_get(storage_key, ttl_seconds)
    except Exception as exc:
        message = f"could not presign {storage_key}: {type(exc).__name__}"
        raise PresignFailedError(message) from exc


def mint_or_null(minter: UrlMinter, storage_key: str, ttl_seconds: int) -> ArtifactUrl:
    """The manifest path: a link when one can be minted, `url: None` when it cannot.

    Catches `PresignFailedError` and nothing wider. A `KeyboardInterrupt` or a programming
    error inside the signer is not a presign failure and must not be reported to the caller as
    an artifact that merely has no link today.
    """
    try:
        return ArtifactUrl(
            storage_key=storage_key, url=mint_artifact_url(minter, storage_key, ttl_seconds)
        )
    except PresignFailedError:
        PRESIGN_FAILURE_ALARM.increment()
        return ArtifactUrl(storage_key=storage_key, url=None)


def mint_all(minter: UrlMinter, storage_keys: list[str], ttl_seconds: int) -> list[ArtifactUrl]:
    """Mint one URL per artifact, in order, one signature each.

    Not a dict keyed by storage key, and not deduplicated: the manifest is a list and the
    caller's ordering is the shot ordering. Deduplication would be the first step towards a
    cache, and there is no cache here.
    """
    return [mint_or_null(minter, key, ttl_seconds) for key in storage_keys]
