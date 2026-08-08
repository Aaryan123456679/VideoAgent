"""`S0.6.2` — the tenant prefix, the checksum, and the scratch file that outlives the failure.

The rule that costs money if it is wrong is the last one. `persistence.md` §9: *keep the local
file so resume re-uploads instead of re-encoding — an artifact already paid for is never
regenerated.* So the interesting assertions here are not that a successful upload works; they
are that a **failed** one leaves the bytes on disk, and that a `PUT` which returned success but
stored something else is caught before the only other copy is deleted.

The transport is a fake, and it is a fake of `ObjectTransport` — this module's own four-method
interface — rather than of `boto3`. `S3ObjectTransport` is the one place the S3 dialect appears
and is exercised under `@pytest.mark.integration`; faking the dialect here would test the
spelling of `Bucket=` and nothing about the policy above it.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest

from video_agent.observability.codes import ErrorCode
from video_agent.persistence.enums import ArtifactKind
from video_agent.persistence.objects import (
    BACKOFF_BASE_SECONDS,
    JOB_LEVEL_SEGMENT,
    KEY_SEGMENTS,
    MAX_UPLOAD_ATTEMPTS,
    ArtifactLocation,
    ArtifactStore,
    ArtifactStoreError,
    ChecksumMismatchError,
    ObjectTransport,
    StorageKeyError,
    sha256_of,
    storage_key,
    tenant_of,
)

TENANT: Final = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT: Final = UUID("99999999-9999-9999-9999-999999999999")
JOB: Final = UUID("22222222-2222-2222-2222-222222222222")
ARTIFACT: Final = UUID("33333333-3333-3333-3333-333333333333")

PAYLOAD: Final = b"\x00\x00\x00\x18ftypmp42 rendered clip bytes"
CONTENT_TYPE: Final = "video/mp4"

PLANTED_MARKER: Final = "planted-store-credential-marker-not-a-real-value"
"""A stand-in for an object-store credential. Deliberately low-entropy and hyphenated so it can
be searched for in a log line without itself tripping the redaction tripwire — a value that
tripped the tripwire would fail `test_credentials_never_logged` for the wrong reason."""

SHOT_LOCATION: Final = ArtifactLocation(
    tenant_id=TENANT,
    job_id=JOB,
    kind=ArtifactKind.SHOT_CLIP,
    artifact_id=ARTIFACT,
    extension="mp4",
    shot_index=3,
)

JOB_LOCATION: Final = ArtifactLocation(
    tenant_id=TENANT,
    job_id=JOB,
    kind=ArtifactKind.FINAL_VIDEO,
    artifact_id=ARTIFACT,
    extension="mp4",
)


class FakeTransport:
    """An object store that can be made to fail, to lie, or to corrupt what it stored."""

    def __init__(
        self,
        *,
        put_failures: int = 0,
        get_failures: int = 0,
        stores_nothing: bool = False,
        corrupt_with: bytes | None = None,
    ) -> None:
        """Each switch models one real failure, and they are deliberately separate.

        `stores_nothing` is a `PUT` that reports success and keeps nothing — the failure the
        read-back verification exists for and the one a success-code check cannot see.
        `corrupt_with` is bit rot, which the read-side checksum catches.
        """
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[str] = []
        self.get_calls: list[str] = []
        self.presigned: list[tuple[str, int]] = []
        self.closed = False
        self._put_failures = put_failures
        self._get_failures = get_failures
        self._stores_nothing = stores_nothing
        self._corrupt_with = corrupt_with

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        """Store the object unless this call is one of the configured failures."""
        self.put_calls.append(key)
        if len(self.put_calls) <= self._put_failures:
            message = f"upstream refused {content_type} write"
            raise ConnectionError(message)
        if self._stores_nothing:
            return
        self.objects[key] = self._corrupt_with if self._corrupt_with is not None else body

    async def get(self, key: str) -> bytes:
        """Read the object back, or fail if this call is one of the configured failures."""
        self.get_calls.append(key)
        if len(self.get_calls) <= self._get_failures:
            message = "upstream refused read"
            raise ConnectionError(message)
        if key not in self.objects:
            message = f"no such key: {key}"
            raise KeyError(message)
        return self.objects[key]

    def presign_get(self, key: str, ttl_seconds: int) -> str:
        """Record the request; the URL's shape is `test_persistence_presign`'s subject."""
        self.presigned.append((key, ttl_seconds))
        return f"https://store.example/{key}?X-Amz-Signature=deadbeef"

    async def aclose(self) -> None:
        """Record the close."""
        self.closed = True


def scratch(tmp_path: Path, payload: bytes = PAYLOAD) -> Path:
    """A local file standing in for the encoder's output."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(payload)
    return path


async def no_sleep(_seconds: float) -> None:
    """Backoff without waiting. The delays are asserted from `ArtifactStore.backoffs`."""


def store_over(transport: ObjectTransport) -> ArtifactStore:
    """An `ArtifactStore` whose backoff is instant and recorded."""
    return ArtifactStore(transport, sleep=no_sleep)


# --- The layout -----------------------------------------------------------------------------------


def test_key_is_tenant_prefixed() -> None:
    """`S0.6.2` acceptance 1: the first path segment is the tenant id, always.

    The bucket policy is the second isolation layer after RLS, and it can only be written
    against a prefix that is genuinely first.
    """
    key = storage_key(SHOT_LOCATION)

    assert key.split("/")[0] == str(TENANT)
    assert tenant_of(key) == str(TENANT)


def test_the_key_matches_the_documented_layout() -> None:
    """`{tenant_id}/{job_id}/{kind}/{shot_index}/{artifact_id}.{ext}` `[persistence.md §6]`."""
    assert storage_key(SHOT_LOCATION) == f"{TENANT}/{JOB}/shot_clip/3/{ARTIFACT}.mp4"


def test_a_job_level_artifact_keeps_the_arity() -> None:
    """`artifact.shot_index` is nullable, and the layout has a segment for it either way.

    A key whose number of segments depended on its kind would need a parser that knew the kinds.
    """
    key = storage_key(JOB_LOCATION)

    assert key.split("/")[3] == JOB_LEVEL_SEGMENT
    assert len(key.split("/")) == KEY_SEGMENTS


def test_two_tenants_never_share_a_prefix() -> None:
    """The same job and artifact ids under a different tenant produce a disjoint key."""
    other = ArtifactLocation(
        tenant_id=OTHER_TENANT,
        job_id=JOB,
        kind=ArtifactKind.SHOT_CLIP,
        artifact_id=ARTIFACT,
        extension="mp4",
        shot_index=3,
    )

    assert not storage_key(other).startswith(f"{TENANT}/")


@pytest.mark.parametrize("extension", ["../../etc/passwd", "mp4/", "", "m p4", "mp4.bak"])
def test_an_extension_that_would_escape_the_prefix_is_rejected(extension: str) -> None:
    """A `/` or a `..` in the extension moves the object out of its tenant's prefix."""
    location = ArtifactLocation(
        tenant_id=TENANT,
        job_id=JOB,
        kind=ArtifactKind.SHOT_CLIP,
        artifact_id=ARTIFACT,
        extension=extension,
        shot_index=0,
    )

    with pytest.raises(StorageKeyError):
        storage_key(location)


def test_a_negative_shot_index_is_rejected() -> None:
    """`-1` would render `.../-1/...`, which is neither a shot nor the job-level segment."""
    location = ArtifactLocation(
        tenant_id=TENANT,
        job_id=JOB,
        kind=ArtifactKind.SHOT_CLIP,
        artifact_id=ARTIFACT,
        extension="mp4",
        shot_index=-1,
    )

    with pytest.raises(StorageKeyError):
        storage_key(location)


# --- Upload, verification and the scratch file ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_verified_upload_records_the_digest_and_releases_the_file(tmp_path: Path) -> None:
    """The digest on the artifact row is the digest of the bytes that were sent."""
    transport = FakeTransport()
    local = scratch(tmp_path)
    key = storage_key(SHOT_LOCATION)

    stored = await store_over(transport).upload(local, key, CONTENT_TYPE)

    assert stored.checksum_sha256 == sha256_of(PAYLOAD)
    assert stored.size_bytes == len(PAYLOAD)
    assert stored.attempts == 1
    assert transport.objects[key] == PAYLOAD
    assert not local.exists()


@pytest.mark.asyncio
async def test_upload_retries_then_raises_store_001(tmp_path: Path) -> None:
    """`S0.6.2` acceptance 3: four attempts, then `VA-STORE-001`, with the backoff asserted."""
    transport = FakeTransport(put_failures=MAX_UPLOAD_ATTEMPTS)
    store = store_over(transport)

    with pytest.raises(ArtifactStoreError) as raised:
        await store.upload(scratch(tmp_path), storage_key(SHOT_LOCATION), CONTENT_TYPE)

    assert raised.value.code is ErrorCode.VA_STORE_001
    assert raised.value.retryable is True
    assert len(transport.put_calls) == MAX_UPLOAD_ATTEMPTS
    assert store.backoffs == [
        BACKOFF_BASE_SECONDS,
        BACKOFF_BASE_SECONDS * 2,
        BACKOFF_BASE_SECONDS * 4,
    ]


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_rather_than_surfaced(tmp_path: Path) -> None:
    """Three failures and a success is a success. Without this, the retry could be doing nothing."""
    transport = FakeTransport(put_failures=MAX_UPLOAD_ATTEMPTS - 1)

    stored = await store_over(transport).upload(
        scratch(tmp_path), storage_key(SHOT_LOCATION), CONTENT_TYPE
    )

    assert stored.attempts == MAX_UPLOAD_ATTEMPTS


@pytest.mark.asyncio
async def test_local_file_retained_on_upload_failure(tmp_path: Path) -> None:
    """`S0.6.2` acceptance 3 and 4: resume re-uploads, it never re-encodes and re-bills."""
    local = scratch(tmp_path)
    store = store_over(FakeTransport(put_failures=MAX_UPLOAD_ATTEMPTS))

    with pytest.raises(ArtifactStoreError):
        await store.upload(local, storage_key(SHOT_LOCATION), CONTENT_TYPE)

    assert local.exists()
    assert local.read_bytes() == PAYLOAD


@pytest.mark.asyncio
async def test_local_file_deleted_only_after_checksum_confirmed(tmp_path: Path) -> None:
    """A `PUT` that reported success and stored something else must not cost us the only copy.

    `stores_nothing` is that exact failure. The upload's read-back finds no object, the
    exception is `VA-STORE-001`, and the scratch file is still there to re-upload.
    """
    local = scratch(tmp_path)
    store = store_over(FakeTransport(stores_nothing=True))

    with pytest.raises(ArtifactStoreError):
        await store.upload(local, storage_key(SHOT_LOCATION), CONTENT_TYPE)

    assert local.exists()


@pytest.mark.asyncio
async def test_a_silently_corrupted_write_is_caught_before_the_file_is_released(
    tmp_path: Path,
) -> None:
    """The store accepted the write and kept different bytes: `VA-STORE-004`, file retained."""
    local = scratch(tmp_path)
    store = store_over(FakeTransport(corrupt_with=b"not the clip"))

    with pytest.raises(ChecksumMismatchError) as raised:
        await store.upload(local, storage_key(SHOT_LOCATION), CONTENT_TYPE)

    assert raised.value.code is ErrorCode.VA_STORE_004
    assert raised.value.retryable is False
    assert local.exists()


@pytest.mark.asyncio
async def test_the_upload_verifies_by_reading_the_object_back(tmp_path: Path) -> None:
    """The verification is a real round trip, not a re-hash of the local file.

    Re-hashing the buffer would agree with itself forever and prove nothing about the store.
    """
    transport = FakeTransport()
    key = storage_key(SHOT_LOCATION)

    await store_over(transport).upload(scratch(tmp_path), key, CONTENT_TYPE)

    assert transport.get_calls == [key]


@pytest.mark.asyncio
async def test_delete_local_can_be_declined(tmp_path: Path) -> None:
    """A caller that still needs the bytes — the frame-upload path `[D-64]` — keeps them."""
    local = scratch(tmp_path)

    await store_over(FakeTransport()).upload(
        local, storage_key(SHOT_LOCATION), CONTENT_TYPE, delete_local=False
    )

    assert local.exists()


# --- Download -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checksum_verified_on_read() -> None:
    """`S0.6.2` acceptance 2: a corrupted object raises `VA-STORE-004` rather than returning."""
    transport = FakeTransport()
    key = storage_key(SHOT_LOCATION)
    transport.objects[key] = PAYLOAD

    good = await store_over(transport).download(key, sha256_of(PAYLOAD))
    transport.objects[key] = b"rotted"

    with pytest.raises(ChecksumMismatchError) as raised:
        await store_over(transport).download(key, sha256_of(PAYLOAD))

    assert good == PAYLOAD
    assert raised.value.code is ErrorCode.VA_STORE_004


@pytest.mark.asyncio
async def test_a_read_outage_is_retried_and_then_reported() -> None:
    """`persistence.md` §9 has one row for object-store unavailability, in both directions."""
    transport = FakeTransport(get_failures=MAX_UPLOAD_ATTEMPTS)

    with pytest.raises(ArtifactStoreError):
        await store_over(transport).download(storage_key(SHOT_LOCATION), sha256_of(PAYLOAD))

    assert len(transport.get_calls) == MAX_UPLOAD_ATTEMPTS


@pytest.mark.asyncio
async def test_there_is_no_way_to_skip_verification() -> None:
    """`download` takes the expected digest as a required argument, not an optional one.

    Asserted on the signature rather than by calling it wrongly, because calling it wrongly is
    a type error and `S0.1.2` allows no inline type-checker suppression to write one down. An
    optional
    parameter would be defaulted to `None` at the first call site in a hurry, and an artifact
    that plays but is not the one that was rendered is worse than a missing one.
    """
    parameters = inspect.signature(ArtifactStore.download).parameters

    assert parameters["expected_checksum"].default is inspect.Parameter.empty
    assert "verify" not in parameters


# --- Credentials ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_never_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """`S0.6.2` acceptance 5. The failure text carries the exception *type*, never its message.

    The planted value is inside the exception the fake store raises, which is where a real
    `botocore` error puts request metadata. It must reach neither the log nor the raised
    message.
    """
    transport = FakeTransport(put_failures=MAX_UPLOAD_ATTEMPTS)

    class _Leaky(FakeTransport):
        async def put(self, key: str, body: bytes, content_type: str) -> None:
            """Fail the way `botocore` does: with the request context in the message."""
            message = (
                f"AuthorizationHeaderMalformed writing {len(body)} bytes of {content_type} "
                f"to {key} with credential {PLANTED_MARKER}"
            )
            raise ConnectionError(message)

    leaky = _Leaky()
    with caplog.at_level(logging.DEBUG), pytest.raises(ArtifactStoreError) as raised:
        await store_over(leaky).upload(scratch(tmp_path), storage_key(SHOT_LOCATION), CONTENT_TYPE)

    assert PLANTED_MARKER not in raised.value.message
    assert PLANTED_MARKER not in caplog.text
    assert transport.put_calls == []


@pytest.mark.asyncio
async def test_the_store_emits_no_log_line_at_all(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing in the upload path logs. The caller's span carries `storage_key`, which is
    allow-listed; this layer has nothing to add that is not either an identifier it was given
    or a credential it must not repeat."""
    with caplog.at_level(logging.DEBUG):
        await store_over(FakeTransport()).upload(
            scratch(tmp_path), storage_key(SHOT_LOCATION), CONTENT_TYPE
        )

    assert caplog.records == []
