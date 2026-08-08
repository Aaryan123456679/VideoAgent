"""Artifact bytes: the tenant-prefixed layout, the checksum, and the retry that keeps the file.

`S0.6.2`, `persistence.md` §6. Postgres holds metadata and a key; the object store holds bytes;
neither holds both. Four properties are load-bearing.

**Every key begins with the tenant id.** `[persistence.md §6]` calls the bucket policy *a second
isolation layer after RLS*, and a second layer only works if it is independent of the first: RLS
is enforced by Postgres from a session setting, the prefix is enforced by the bucket from the
key itself. A bug in one is not a bug in the other. `storage_key` is the only function that
builds a key, so "tenant-prefixed" is a property of the constructor rather than of every caller.

**The checksum is computed once, on the bytes that were written, and verified on every read.**
This is what makes byte-identity assertable `[PRD §Resilience]`. A digest computed from a
re-read of the local file rather than from the buffer that was uploaded would agree with itself
forever and prove nothing about the object.

**A failed upload keeps the local file.** `persistence.md` §9: *keep the local file so resume
re-uploads instead of re-encoding — an artifact already paid for is never regenerated.* The
scratch file is therefore deleted at exactly one point, after the stored object has been read
back and its digest matched. Deleting on a successful `PUT` would be earlier and wrong: a
`PUT` that returns 200 and stores nothing is precisely the failure the verification exists for.

**No credential is ever in a message from here.** The access key and secret come from settings
as `SecretStr` and are handed to the client; the failure messages name the operation, the
attempt number and the key, and nothing else. `botocore`'s own exception text is reduced to its
type for the same reason — a `ClientError` repr carries request metadata and, on some paths, the
signed URL that failed.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

import boto3
from botocore.config import Config

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError
from video_agent.persistence.enums import ArtifactKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mypy_boto3_s3.client import S3Client

    from video_agent.config.settings import Settings

# --- The layout --------------------------------------------------------------------------------

JOB_LEVEL_SEGMENT: Final = "job"
"""The `{shot_index}` segment for an artifact that belongs to the job rather than to a shot.

`persistence.md` §6 gives one layout, `{tenant_id}/{job_id}/{kind}/{shot_index}/{artifact_id}
.{ext}`, while `artifact.shot_index` is nullable — the final video, the story plan and the
continuity bible have no shot. Dropping the segment for those would make the key's arity depend
on its kind, so a parse would have to know the kinds; a literal, non-numeric segment keeps every
key five segments long and cannot collide with a shot index, which is always a number.
"""

KEY_SEGMENTS: Final = 5
"""tenant / job / kind / shot / file. Asserted, so a layout change is a test change."""

_EXTENSION_CHARSET: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


class StorageKeyError(ValueError):
    """A component that would produce a malformed or ambiguous object key."""


@dataclass(frozen=True, slots=True)
class ArtifactLocation:
    """Everything the layout needs, as one value rather than six positional arguments.

    The identifiers are `UUID`s and not strings, for the reason `session.tenant_session` gives:
    a value that arrived as text in a request body cannot reach here without someone writing
    the parse, and the parse is the point at which a mistake becomes visible in a diff.
    """

    tenant_id: UUID
    job_id: UUID
    kind: ArtifactKind
    artifact_id: UUID
    extension: str
    shot_index: int | None = None


def storage_key(location: ArtifactLocation) -> str:
    """`{tenant_id}/{job_id}/{kind}/{shot_index}/{artifact_id}.{ext}` `[persistence.md §6]`.

    `tenant_id` first and always. The extension is validated rather than trusted: a `/` or a
    `..` in it would move the object out of its tenant prefix, which is the one thing this
    layout exists to prevent.
    """
    extension = location.extension
    if not extension or not set(extension.lower()) <= _EXTENSION_CHARSET:
        message = f"extension must be alphanumeric with no separator; got {extension!r}"
        raise StorageKeyError(message)
    if location.shot_index is not None and location.shot_index < 0:
        message = f"shot_index must not be negative; got {location.shot_index}"
        raise StorageKeyError(message)
    shot = JOB_LEVEL_SEGMENT if location.shot_index is None else str(location.shot_index)
    return (
        f"{location.tenant_id}/{location.job_id}/{location.kind.value}/"
        f"{shot}/{location.artifact_id}.{extension.lower()}"
    )


def tenant_of(key: str) -> str:
    """The first path segment of a storage key — the tenant the bucket policy will check."""
    return key.split("/", 1)[0]


# --- Failures -----------------------------------------------------------------------------------


class ArtifactStoreError(VideoAgentError):
    """The object store could not be reached after every retry. `VA-STORE-001`, retryable.

    One class for both directions. `persistence.md` §9 has a single row — *object store
    unavailable → retry with backoff; on exhaustion `VA-STORE-001`* — and does not distinguish
    a failed `PUT` from a failed `GET`; the taxonomy's description of the code
    (*"Artifact write failed"*) is narrower than the failure table that assigns it. Inventing a
    read code here would put a value in an error envelope that
    `observability/codes.registry.json` does not contain, which is a worse defect than a
    slightly wide description. The mismatch is reported rather than papered over.

    The local scratch file is intact when this is raised from an upload. That is part of the
    contract, not an implementation detail: resume re-uploads rather than re-encoding, and a
    re-encode of a shot that was already rendered is a second charge for the same clip.
    """

    code = ErrorCode.VA_STORE_001


class ChecksumMismatchError(VideoAgentError):
    """The bytes read back are not the bytes written. `VA-STORE-004`, not retryable.

    `persistence.md` §9: treat the artifact as lost, exclude it from assembly, flag degraded.
    Not retryable because a retry re-reads the same corrupt object.
    """

    code = ErrorCode.VA_STORE_004


# --- The transport ------------------------------------------------------------------------------


class ObjectTransport(Protocol):
    """The four things this module does to an object store, and nothing else.

    Deliberately not shaped like `boto3`. A protocol mirroring the S3 client would put its
    keyword spelling — `Bucket`, `Key`, `Body` — into every fake, and the fakes would then be
    testing that spelling rather than this module's policy. `S3ObjectTransport` is the one
    place the dialect appears.
    """

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        """Store `body` under `key`, overwriting."""
        ...  # pragma: no cover - protocol declaration

    async def get(self, key: str) -> bytes:
        """Read the object back. Raises if it is absent."""
        ...  # pragma: no cover - protocol declaration

    def presign_get(self, key: str, ttl_seconds: int) -> str:
        """A `GET`-only URL valid for `ttl_seconds`. See `persistence.presign`."""
        ...  # pragma: no cover - protocol declaration

    async def aclose(self) -> None:
        """Release whatever the client holds."""
        ...  # pragma: no cover - protocol declaration


GET_OBJECT: Final = "get_object"
"""The only `ClientMethod` this codebase ever presigns. See `persistence.presign`."""


class S3ObjectTransport:
    """The S3-dialect client, and the only module that knows the dialect.

    `boto3` is synchronous, so every call that touches the network runs in a worker thread.
    Awaiting a blocking client directly would stall the event loop for the duration of an
    upload — which for a rendered clip is seconds, during which the process answers no
    readiness probe and no SSE event.

    `presign_get` is deliberately *not* threaded: signing is pure computation with no I/O, and
    pushing it onto a thread would only add a scheduling hop.
    """

    def __init__(self, client: S3Client, bucket: str) -> None:
        """Hold an already-configured client and the bucket every key lives in."""
        self._client = client
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        """The bucket this transport writes to."""
        return self._bucket

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        """`PutObject`, on a worker thread."""
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    async def get(self, key: str) -> bytes:
        """`GetObject`, on a worker thread, read fully into memory."""
        return await asyncio.to_thread(self._read, key)

    def _read(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    def presign_get(self, key: str, ttl_seconds: int) -> str:
        """Sign a `GET` for `key`. `GET_OBJECT` is a constant; there is no parameter for it."""
        url: str = self._client.generate_presigned_url(
            ClientMethod=GET_OBJECT,
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
        return url

    async def aclose(self) -> None:
        """Close the underlying HTTP session."""
        await asyncio.to_thread(self._client.close)


def create_s3_client(settings: Settings) -> S3Client:
    """An S3-compatible client from settings, signing with SigV4.

    `signature_version="s3v4"` explicitly: the default varies with the region and the endpoint,
    and a presigned URL signed with the older algorithm is rejected by MinIO and by any bucket
    with a modern policy — a failure that appears only against the real store.

    The credentials are read out of their `SecretStr` here, at the last possible moment, and go
    straight into the client. They are never bound to a local that outlives this call.
    """
    client: S3Client = boto3.client(
        "s3",
        endpoint_url=settings.ARTIFACT_ENDPOINT_URL or None,
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID.get_secret_value() or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY.get_secret_value() or None,
        config=Config(signature_version="s3v4"),
    )
    return client


# --- The store -----------------------------------------------------------------------------------

MAX_UPLOAD_ATTEMPTS: Final = 4
"""Four tries `[persistence.md §9]`: retry with backoff, and on exhaustion `VA-STORE-001`."""

BACKOFF_BASE_SECONDS: Final = 0.5
BACKOFF_FACTOR: Final = 2.0
"""0.5s, 1s, 2s between the four attempts. Deterministic and un-jittered on purpose: the job
lock `[D-10]` means one writer per job, so there is no fleet of workers retrying the same key in
lockstep for a jitter to spread out, and a deterministic schedule is one a test can assert."""


def sha256_of(data: bytes) -> str:
    """The digest that goes on the artifact row and is checked on every read."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredObject:
    """What a completed upload produced: enough to write the artifact row, and no bytes."""

    storage_key: str
    checksum_sha256: str
    size_bytes: int
    attempts: int


class ArtifactStore:
    """Upload with verification, download with verification, and a scratch file that survives.

    Takes an `ObjectTransport` rather than a client, so the retry policy and the checksum rule
    are exercised against a store that can be made to fail on demand.
    """

    def __init__(
        self,
        transport: ObjectTransport,
        *,
        max_attempts: int = MAX_UPLOAD_ATTEMPTS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """`sleep` is an injection point so a test asserts the backoff rather than waiting it."""
        self._transport = transport
        self._max_attempts = max_attempts
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self.backoffs: list[float] = []

    async def upload(
        self,
        local_path: Path,
        key: str,
        content_type: str,
        *,
        delete_local: bool = True,
    ) -> StoredObject:
        """Upload, verify the stored bytes, and only then release the scratch file.

        The order is the whole method. Digest first, from the buffer that is about to be sent;
        `PUT` with backoff; read back and compare; delete last. Any earlier deletion turns a
        silent storage failure into an unrecoverable one, because the only copy is gone.
        """
        payload = await asyncio.to_thread(local_path.read_bytes)
        digest = sha256_of(payload)

        async def put() -> None:
            await self._transport.put(key, payload, content_type)

        attempts = await self._with_retries("write", key, put)
        stored = b""

        async def read_back() -> None:
            nonlocal stored
            stored = await self._transport.get(key)

        await self._with_retries("read-back", key, read_back)
        self._require_digest(key, digest, sha256_of(stored), "verifying")
        if delete_local:
            await asyncio.to_thread(local_path.unlink, True)
        return StoredObject(
            storage_key=key,
            checksum_sha256=digest,
            size_bytes=len(payload),
            attempts=attempts,
        )

    async def download(self, key: str, expected_checksum: str) -> bytes:
        """Read an object and refuse to return bytes that do not match `expected_checksum`.

        The verification is not optional and there is no parameter that skips it. An artifact
        whose bytes have drifted is worse than a missing one: it is delivered, it plays, and
        the reproducibility record says it is the thing that was rendered.
        """
        payload = b""

        async def read() -> None:
            nonlocal payload
            payload = await self._transport.get(key)

        await self._with_retries("read", key, read)
        self._require_digest(key, expected_checksum, sha256_of(payload), "reading")
        return payload

    async def _with_retries(
        self, operation: str, key: str, action: Callable[[], Awaitable[None]]
    ) -> int:
        """Run `action` until it succeeds or the attempt budget is spent.

        Returns the attempt number that worked, which is what a caller records on the artifact
        row and what a test asserts the backoff schedule against.
        """
        last: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                await action()
            except Exception as exc:
                last = exc
                if attempt < self._max_attempts:
                    delay = BACKOFF_BASE_SECONDS * BACKOFF_FACTOR ** (attempt - 1)
                    self.backoffs.append(delay)
                    await self._sleep(delay)
                continue
            return attempt
        message = (
            f"object store {operation} failed for {key} after {self._max_attempts} attempts: "
            f"{type(last).__name__}"
        )
        raise ArtifactStoreError(message) from last

    @staticmethod
    def _require_digest(key: str, expected: str, actual: str, phase: str) -> None:
        """Compare two digests, raising `VA-STORE-004` when they differ.

        The message carries both digests. A digest is not a credential — it is on the
        allow-list as `checksum_sha256` — and an operator comparing an artifact row against a
        stored object needs both numbers to tell which side drifted.
        """
        if actual == expected:
            return
        message = (
            f"checksum mismatch {phase} {key}: expected {expected}, stored object hashes "
            f"to {actual}"
        )
        raise ChecksumMismatchError(message)


def create_artifact_store(settings: Settings) -> ArtifactStore:
    """The store the application holds, bound to `ARTIFACT_BUCKET`."""
    transport = S3ObjectTransport(create_s3_client(settings), settings.ARTIFACT_BUCKET)
    return ArtifactStore(transport)
