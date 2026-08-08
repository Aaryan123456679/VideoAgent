"""`S0.6.2` and `S0.6.3` against a live S3-compatible store: round trip, `GET`-only, expiry.

Three claims cannot be made without a server, and the unit suite is explicit that it is not
making them:

- a presigned URL **authorises a `GET`**;
- the same URL **does not authorise a `PUT`**, which is what "`GET`-only" means;
- the URL **stops working** once `PRESIGNED_URL_TTL_SECONDS` has elapsed.

All three are properties of the store's signature verification. A unit test can assert the
query parameters and the `ClientMethod`, and it does — but a URL whose parameters look right and
that a bucket refuses is a URL the customer cannot use, and nothing short of a request finds
that out.

**Skipping, not erroring, not hanging.** A bounded `list_buckets` against `ARTIFACT_ENDPOINT_URL`
with a short connect timeout, once per module. `boto3` retries by default and would otherwise
turn a missing MinIO into a minute of silence per test.

**Its own bucket, created and emptied.** Never `ARTIFACT_BUCKET` itself: this suite writes and
deletes, and a developer's local artifacts are not test fixtures.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Final

import boto3
import httpx
import pytest
import pytest_asyncio
from botocore.config import Config
from botocore.exceptions import ClientError

from video_agent.config.settings import get_settings
from video_agent.persistence.enums import ArtifactKind
from video_agent.persistence.objects import (
    ArtifactLocation,
    ArtifactStore,
    ArtifactStoreError,
    ChecksumMismatchError,
    S3ObjectTransport,
    sha256_of,
    storage_key,
)
from video_agent.persistence.presign import mint_artifact_url, presign_ttl

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from mypy_boto3_s3.client import S3Client

pytestmark = pytest.mark.integration

PROBE_TIMEOUT_SECONDS: Final = 3.0
DEFAULT_ENDPOINT: Final = "http://localhost:9000"

TENANT: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
JOB: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")

PAYLOAD: Final = b"\x00\x00\x00\x18ftypmp42 integration clip bytes"
CONTENT_TYPE: Final = "video/mp4"

SHORT_TTL_SECONDS: Final = 1
"""Short enough for the expiry test to wait it out, long enough to sign and issue one request."""

HTTP_OK: Final = 200
HTTP_FORBIDDEN: Final = 403
REQUEST_TIMEOUT_SECONDS: Final = 5.0


def _endpoint() -> str:
    return get_settings().ARTIFACT_ENDPOINT_URL or DEFAULT_ENDPOINT


def _client(endpoint: str) -> S3Client:
    settings = get_settings()
    client: S3Client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID.get_secret_value() or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY.get_secret_value() or None,
        config=Config(
            signature_version="s3v4",
            connect_timeout=PROBE_TIMEOUT_SECONDS,
            read_timeout=PROBE_TIMEOUT_SECONDS,
            retries={"max_attempts": 1},
        ),
    )
    return client


def _unreachable_reason(endpoint: str) -> str | None:
    """None when the store answers within the probe timeout, otherwise why it did not."""
    try:
        _client(endpoint).list_buckets()
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    return None


@pytest.fixture(scope="module")
def endpoint() -> str:
    """The configured endpoint, or a skip naming why the store did not answer."""
    resolved = _endpoint()
    reason = _unreachable_reason(resolved)
    if reason is not None:
        pytest.skip(f"object store unavailable: {reason}")
    return resolved


@pytest.fixture(scope="module")
def bucket(endpoint: str) -> Iterator[str]:
    """A bucket of this run's own, emptied and removed afterwards."""
    name = f"video-agent-test-{uuid.uuid4().hex[:12]}"
    client = _client(endpoint)
    client.create_bucket(Bucket=name)
    try:
        yield name
    finally:
        listing = client.list_objects_v2(Bucket=name)
        for stored in listing.get("Contents", []):
            client.delete_object(Bucket=name, Key=stored["Key"])
        client.delete_bucket(Bucket=name)


@pytest.fixture
def transport(endpoint: str, bucket: str) -> S3ObjectTransport:
    """The production transport against the scratch bucket."""
    return S3ObjectTransport(_client(endpoint), bucket)


@pytest_asyncio.fixture
async def uploaded(
    transport: S3ObjectTransport, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[tuple[str, str]]:
    """One verified artifact in the store; yields its key and checksum."""
    location = ArtifactLocation(
        tenant_id=TENANT,
        job_id=JOB,
        kind=ArtifactKind.SHOT_CLIP,
        artifact_id=uuid.uuid4(),
        extension="mp4",
        shot_index=0,
    )
    local = tmp_path_factory.mktemp("scratch") / "clip.mp4"
    local.write_bytes(PAYLOAD)
    stored = await ArtifactStore(transport).upload(local, storage_key(location), CONTENT_TYPE)
    yield stored.storage_key, stored.checksum_sha256


# --- The round trip -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_verifies_and_releases_the_scratch_file(
    transport: S3ObjectTransport, tmp_path: Path
) -> None:
    """The read-back verification runs against a real store, then the local file goes."""
    location = ArtifactLocation(
        tenant_id=TENANT,
        job_id=JOB,
        kind=ArtifactKind.SHOT_CLIP,
        artifact_id=uuid.uuid4(),
        extension="mp4",
        shot_index=1,
    )
    local = tmp_path / "clip.mp4"
    local.write_bytes(PAYLOAD)

    stored = await ArtifactStore(transport).upload(local, storage_key(location), CONTENT_TYPE)

    assert stored.checksum_sha256 == sha256_of(PAYLOAD)
    assert not local.exists()
    assert await transport.get(stored.storage_key) == PAYLOAD


@pytest.mark.asyncio
async def test_download_verifies_the_checksum(
    transport: S3ObjectTransport, uploaded: tuple[str, str]
) -> None:
    """A real object read back and hashed."""
    key, checksum = uploaded

    assert await ArtifactStore(transport).download(key, checksum) == PAYLOAD


@pytest.mark.asyncio
async def test_a_corrupted_object_raises_store_004(
    transport: S3ObjectTransport, uploaded: tuple[str, str]
) -> None:
    """Overwrite the stored bytes behind the store's back; the digest catches it on read."""
    key, checksum = uploaded
    await transport.put(key, b"different bytes entirely", CONTENT_TYPE)

    with pytest.raises(ChecksumMismatchError):
        await ArtifactStore(transport).download(key, checksum)


@pytest.mark.asyncio
async def test_a_missing_object_is_reported_not_returned_empty(
    transport: S3ObjectTransport,
) -> None:
    """A `404` from the store is `VA-STORE-001`, never an empty artifact."""
    with pytest.raises(ArtifactStoreError):
        await ArtifactStore(transport, max_attempts=1).download(
            f"{TENANT}/{JOB}/shot_clip/9/{uuid.uuid4()}.mp4", sha256_of(PAYLOAD)
        )


def test_the_key_is_tenant_prefixed_in_the_bucket_listing(
    uploaded: tuple[str, str], endpoint: str, bucket: str
) -> None:
    """The bucket policy's view: every object this code writes begins with a tenant id.

    Read from the store's own listing rather than from the string we constructed, because the
    prefix a policy matches is the one the store recorded.
    """
    key, _ = uploaded
    listing = _client(endpoint).list_objects_v2(Bucket=bucket, Prefix=f"{TENANT}/")

    assert key in {stored["Key"] for stored in listing.get("Contents", [])}


# --- Presigned URLs, against a store that verifies signatures -------------------------------------


@pytest.mark.asyncio
async def test_a_minted_url_authorises_a_get(
    transport: S3ObjectTransport, uploaded: tuple[str, str]
) -> None:
    """The URL works. Without this, every "`GET`-only" assertion could be about a dead link."""
    key, _ = uploaded
    url = mint_artifact_url(transport, key, presign_ttl(get_settings()))

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(url)

    assert response.status_code == HTTP_OK
    assert response.content == PAYLOAD


@pytest.mark.asyncio
async def test_url_is_get_only(transport: S3ObjectTransport, uploaded: tuple[str, str]) -> None:
    """`S0.6.3` acceptance 1: a `PUT` against a `GET` signature is refused by the store.

    The HTTP verb is inside the signature, so this is the store's own check and the only place
    it can be observed. A URL that authorised a write would let anyone holding a delivery link
    replace the customer's video.
    """
    key, _ = uploaded
    url = mint_artifact_url(transport, key, presign_ttl(get_settings()))

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.put(url, content=b"replacement bytes")

    assert response.status_code == HTTP_FORBIDDEN
    assert await transport.get(key) == PAYLOAD


@pytest.mark.asyncio
async def test_ttl_enforced(transport: S3ObjectTransport, uploaded: tuple[str, str]) -> None:
    """`S0.6.3` acceptance 1: the URL stops authorising once its expiry has passed.

    Waited out rather than simulated with a controlled clock, because the clock that decides is
    the store's. A short TTL keeps the wait to a second.
    """
    key, _ = uploaded
    url = mint_artifact_url(transport, key, SHORT_TTL_SECONDS)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        while_valid = await client.get(url)
        await asyncio.sleep(SHORT_TTL_SECONDS + 1)
        after_expiry = await client.get(url)

    assert while_valid.status_code == HTTP_OK
    assert after_expiry.status_code == HTTP_FORBIDDEN


def test_the_transport_closes_without_error(transport: S3ObjectTransport) -> None:
    """The lifespan's contract: the object store closes on shutdown."""
    asyncio.run(transport.aclose())


def test_a_write_to_a_missing_bucket_raises_client_error(endpoint: str) -> None:
    """A sanity check on the fixture: the scratch bucket really is what makes the rest work."""
    client = _client(endpoint)

    with pytest.raises(ClientError):
        client.put_object(Bucket=f"absent-{uuid.uuid4().hex[:8]}", Key="k", Body=b"v")
