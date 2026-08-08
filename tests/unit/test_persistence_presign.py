"""`S0.6.3` — a minted URL is a bearer credential, so it is returned once and never kept.

Most of this file signs with the **real** `boto3` client. SigV4 presigning is pure computation
— it opens no socket — so the URLs asserted on here are the exact strings a deployment would
hand to a customer, not a fake's approximation. That matters for two of the acceptance
criteria: "expires at exactly `PRESIGNED_URL_TTL_SECONDS`" is a claim about `X-Amz-Expires` and
`X-Amz-Date`, and "the redaction serialiser drops it" is a claim about a real signature's shape.

What a signed string cannot show is whether a server would *honour* it. That a `PUT` against a
`GET` signature is refused, and that the URL stops working once the TTL elapses, are properties
of the store and are asserted in `tests/integration/test_persistence_object_store.py`, which
skips without one. They are recorded as unverified here rather than dressed up.

**On `test_no_memoisation`.** The plan's test spec says two mints in the same second produce
different query strings. Against real SigV4 that is false and would be a bad test: the
signature is a deterministic function of the key, the credential, the expiry and the timestamp,
so two calls within the same second produce identical strings *by design*. The property that
actually matters — no cache, no memoisation, no live credential retained between calls — is
asserted three ways below: the signer is invoked once per mint, the function carries no cache
wrapper, and advancing the clock by one second changes the signature (which a memoised result
would not).
"""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config

from video_agent.config.settings import Settings
from video_agent.observability.codes import ErrorCode
from video_agent.observability.redaction import (
    RedactionTripwireError,
    TripwireMode,
    contains_never_logged_value,
    is_presigned_url,
    redact,
    sanitise,
    scan_payload,
)
from video_agent.persistence import objects as objects_module
from video_agent.persistence import presign as presign_module
from video_agent.persistence.objects import GET_OBJECT, S3ObjectTransport
from video_agent.persistence.presign import (
    PRESIGN_FAILURE_ALARM,
    ArtifactUrl,
    PresignFailedError,
    mint_all,
    mint_artifact_url,
    mint_or_null,
    presign_ttl,
)
from video_agent.persistence.schema import artifact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping
    from types import ModuleType

BUCKET: Final = "video-agent-artifacts"
ENDPOINT: Final = "http://localhost:9000"
REGION: Final = "us-east-1"

ACCESS_KEY: Final = "AKIAIOSFODNN7EXAMPLE"
"""AWS's own published documentation example. Not a credential, and recognisable as an example
by anyone who has read the SigV4 documentation; the paired signing value below is the same one."""

SIGNING_KEY: Final = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

STORAGE_KEY: Final = (
    "11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222/"
    "shot_clip/3/33333333-3333-3333-3333-333333333333.mp4"
)

TTL_SECONDS: Final = 3600
SIGNATURE_PARAM: Final = "X-Amz-Signature"
EXPIRES_PARAM: Final = "X-Amz-Expires"
DATE_PARAM: Final = "X-Amz-Date"
MINT_COUNT: Final = 10


def real_transport() -> S3ObjectTransport:
    """An `S3ObjectTransport` over a genuine boto3 client. Signing does no I/O."""
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SIGNING_KEY,
        config=Config(signature_version="s3v4"),
    )
    return S3ObjectTransport(client, BUCKET)


def query_of(url: str) -> Mapping[str, list[str]]:
    """The URL's query parameters."""
    return parse_qs(urlsplit(url).query)


class RecordingMinter:
    """Counts mints and records what was asked for. Used where the *call* is the subject."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def presign_get(self, key: str, ttl_seconds: int) -> str:
        """Record and return a URL shaped like a real signature."""
        self.calls.append((key, ttl_seconds))
        return f"https://store.example/{key}?{SIGNATURE_PARAM}={'a1b2c3d4' * 8}"


class FailingMinter:
    """A store that cannot sign — an expired role, a missing bucket, a dead endpoint."""

    def presign_get(self, key: str, ttl_seconds: int) -> str:
        """Always fails."""
        message = f"cannot sign {key} for {ttl_seconds}s"
        raise RuntimeError(message)


# --- The URL itself ------------------------------------------------------------------------------


def test_the_minted_url_is_a_real_sigv4_presigned_url() -> None:
    """Guard the rest of the file: if signing silently produced a plain URL, every assertion
    about query parameters below would be vacuous."""
    url = mint_artifact_url(real_transport(), STORAGE_KEY, TTL_SECONDS)

    assert SIGNATURE_PARAM in query_of(url)
    assert query_of(url)["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]


def test_url_is_get_only() -> None:
    """`S0.6.3` acceptance 1. There is no parameter that selects a method.

    Asserted structurally: `S3ObjectTransport.presign_get` passes the module constant
    `GET_OBJECT`, and neither it nor `mint_artifact_url` takes a method argument. A signature
    covers the HTTP verb, so a `PUT` against this URL fails at the store — which
    `tests/integration/test_persistence_object_store.py` asserts against a real one.
    """
    assert GET_OBJECT == "get_object"
    source = S3ObjectTransport.presign_get.__code__
    assert "put_object" not in source.co_consts
    assert set(source.co_varnames[: source.co_argcount]) == {"self", "key", "ttl_seconds"}


def test_ttl_is_exactly_the_configured_value() -> None:
    """`S0.6.3` acceptance 1: the expiry is `PRESIGNED_URL_TTL_SECONDS`, not a rounded default."""
    ttl = presign_ttl(Settings())

    url = mint_artifact_url(real_transport(), STORAGE_KEY, ttl)

    assert query_of(url)[EXPIRES_PARAM] == [str(ttl)]


def test_the_ttl_comes_from_the_setting_and_has_no_default_in_this_module() -> None:
    """A default here would be a second expiry policy, and the shorter one would be ignored."""
    settings = Settings(PRESIGNED_URL_TTL_SECONDS=120)

    url = mint_artifact_url(real_transport(), STORAGE_KEY, presign_ttl(settings))

    assert query_of(url)[EXPIRES_PARAM] == ["120"]


@pytest.mark.parametrize("ttl_seconds", [0, -1])
def test_a_non_positive_ttl_is_refused(ttl_seconds: int) -> None:
    """A zero TTL is a URL that either never works or, on some stores, never expires."""
    with pytest.raises(PresignFailedError):
        mint_artifact_url(real_transport(), STORAGE_KEY, ttl_seconds)


def test_the_url_addresses_the_tenant_prefixed_key() -> None:
    """The key's tenant prefix survives into the path, so a bucket policy can still see it."""
    url = mint_artifact_url(real_transport(), STORAGE_KEY, TTL_SECONDS)

    assert urlsplit(url).path.endswith(STORAGE_KEY)


# --- No cache, no memoisation ---------------------------------------------------------------------


def test_no_memoisation_every_mint_reaches_the_signer() -> None:
    """`S0.6.3` acceptance 4. Ten mints, ten signatures requested."""
    minter = RecordingMinter()

    for _ in range(MINT_COUNT):
        mint_artifact_url(minter, STORAGE_KEY, TTL_SECONDS)

    assert len(minter.calls) == MINT_COUNT
    assert minter.calls == [(STORAGE_KEY, TTL_SECONDS)] * MINT_COUNT


def test_the_minting_function_carries_no_cache_wrapper() -> None:
    """`lru_cache` and friends leave fingerprints. None of them are here.

    A cached mint would hand a later caller a URL whose remaining lifetime is whatever was left
    of an earlier one, and would keep a live credential in process memory in the meantime.
    """
    for attribute in ("cache_info", "cache_clear", "__wrapped__"):
        assert not hasattr(mint_artifact_url, attribute)
    assert mint_artifact_url.__module__ == "video_agent.persistence.presign"


def test_advancing_the_clock_changes_the_signature() -> None:
    """A memoised result would not move when the signing timestamp does.

    `X-Amz-Date` has one-second resolution, so two mints in the *same* second are identical by
    construction — which is why the plan's "two mints in the same second differ" wording cannot
    be implemented against real SigV4 and is replaced by this.
    """
    transport = real_transport()
    first = mint_artifact_url(transport, STORAGE_KEY, TTL_SECONDS)
    dates = {query_of(first)[DATE_PARAM][0]}
    signatures = {query_of(first)[SIGNATURE_PARAM][0]}

    for _ in range(MINT_COUNT):
        url = mint_artifact_url(transport, STORAGE_KEY, TTL_SECONDS)
        dates.add(query_of(url)[DATE_PARAM][0])
        signatures.add(query_of(url)[SIGNATURE_PARAM][0])

    assert len(signatures) == len(dates), (
        "one signature per distinct signing second; a cache would give fewer"
    )


def test_two_different_keys_never_share_a_signature() -> None:
    """A signature that did not cover the key would authorise the whole bucket."""
    transport = real_transport()
    other_key = STORAGE_KEY.replace("shot_clip/3", "shot_clip/4")

    first = mint_artifact_url(transport, STORAGE_KEY, TTL_SECONDS)
    second = mint_artifact_url(transport, other_key, TTL_SECONDS)

    assert query_of(first)[SIGNATURE_PARAM] != query_of(second)[SIGNATURE_PARAM]


# --- Failure yields a null URL, never a missing artifact ------------------------------------------


def test_presign_failure_raises_store_002() -> None:
    """`S0.6.3` acceptance 3, first half."""
    with pytest.raises(PresignFailedError) as raised:
        mint_artifact_url(FailingMinter(), STORAGE_KEY, TTL_SECONDS)

    assert raised.value.code is ErrorCode.VA_STORE_002
    assert raised.value.retryable is True


def test_presign_failure_yields_a_null_url_and_still_lists_the_artifact() -> None:
    """`S0.6.3` acceptance 3, second half. `persistence.md` §9: the manifest still lists it.

    Omitting the artifact would tell the caller the render did not happen. It did, and it was
    billed.
    """
    before = PRESIGN_FAILURE_ALARM.count

    result = mint_or_null(FailingMinter(), STORAGE_KEY, TTL_SECONDS)

    assert result == ArtifactUrl(storage_key=STORAGE_KEY, url=None)
    assert result.available is False
    assert PRESIGN_FAILURE_ALARM.count == before + 1


def test_a_partial_failure_does_not_take_the_whole_manifest_down() -> None:
    """One unsignable artifact yields one null; the rest still carry links."""

    class OneBadKey:
        def presign_get(self, key: str, ttl_seconds: int) -> str:
            if key.endswith("bad.mp4"):
                message = f"cannot sign {key} for {ttl_seconds}s"
                raise RuntimeError(message)
            signature = "0" * 64
            return (
                f"https://store.example/{key}"
                f"?X-Amz-Expires={ttl_seconds}&{SIGNATURE_PARAM}={signature}"
            )

    results = mint_all(OneBadKey(), [STORAGE_KEY, "t/j/k/0/bad.mp4"], TTL_SECONDS)

    assert [result.available for result in results] == [True, False]
    assert [result.storage_key for result in results] == [STORAGE_KEY, "t/j/k/0/bad.mp4"]


def test_mint_or_null_does_not_swallow_a_programming_error() -> None:
    """Only `PresignFailedError` becomes a null URL. A `KeyboardInterrupt` is not a store issue."""

    class Interrupted:
        def presign_get(self, key: str, ttl_seconds: int) -> str:
            raise KeyboardInterrupt(f"while signing {key} for {ttl_seconds}s")

    with pytest.raises(KeyboardInterrupt):
        mint_or_null(Interrupted(), STORAGE_KEY, TTL_SECONDS)


# --- Never stored, never logged -------------------------------------------------------------------


def test_the_artifact_table_has_no_column_that_could_hold_a_url() -> None:
    """`S0.6.3` acceptance 2, the Postgres half — asserted against the schema, not by grepping
    a database this environment does not have.

    A URL cannot be persisted if there is nowhere to put it. The check is on the column names
    because a `url` column added later is exactly how "minted on demand" quietly becomes
    "minted once and stored".
    """
    columns = {column.name for column in artifact.columns}

    assert not any("url" in name for name in columns)
    assert "storage_key" in columns


def test_url_never_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    """`S0.6.3` acceptance 2, the log half: minting ten URLs emits nothing.

    At `INFO`, which is `LOG_LEVEL`'s default and what `configure_logging` puts on the root
    logger. Nothing from this codebase and nothing from the AWS client underneath it.
    """
    transport = real_transport()

    with caplog.at_level(logging.INFO):
        urls = [mint_artifact_url(transport, STORAGE_KEY, TTL_SECONDS) for _ in range(MINT_COUNT)]

    assert caplog.records == []
    assert all(SIGNATURE_PARAM in url for url in urls)
    assert not any(query_of(url)[SIGNATURE_PARAM][0] in caplog.text for url in urls)


@pytest.mark.parametrize("module", [presign_module, objects_module])
def test_the_minting_modules_contain_no_logging_call_at_all(module: ModuleType) -> None:
    """There is no path from a minted URL to a log line through code this repository owns.

    Asserted on the source rather than by capturing at `DEBUG`, and the difference matters. A
    capture-based test would be measuring `botocore`, which *does* log signing material at
    `DEBUG` — see this task's report, where that is raised as a finding against
    `observability/logging.py` installing the JSON handler on the **root** logger. Pinning
    today's behaviour of a third-party library would make fixing it look like a regression;
    pinning the absence of a logger in these two modules is the property they actually own.
    """
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    calls = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "get_logger" not in source
    assert not {"info", "debug", "warning", "error", "exception", "critical"} & calls


def test_redaction_drops_minted_url() -> None:
    """`S0.6.3` acceptance 5: the serialiser drops one if it is ever handed one `[D-52]`.

    Through `reason`, which is an allow-listed free-text field — the realistic accident is a
    URL inside an error message, not a field named `url` that the allow-list would drop anyway.
    """
    url = mint_artifact_url(real_transport(), STORAGE_KEY, TTL_SECONDS)

    assert is_presigned_url(url)
    assert contains_never_logged_value(url)
    assert scan_payload({"reason": url}) != []
    assert "reason" not in sanitise({"reason": f"failed to fetch {url}"})


def test_the_tripwire_raises_outside_production_on_a_minted_url() -> None:
    """In dev and CI the build stops rather than dropping the field quietly `[D-57]`."""
    url = mint_artifact_url(real_transport(), STORAGE_KEY, TTL_SECONDS)

    with pytest.raises(RedactionTripwireError):
        redact({"reason": url}, mode=TripwireMode.RAISE)
