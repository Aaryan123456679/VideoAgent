"""`S0.3.3` — the never-logged list, asserted by attempting to log each item.

Every test here plants a real secret, a real presigned URL or real media bytes and asserts the
value is **absent** from the output. Not that a redaction function exists, not that a rule is
configured — absent. `[D-54]`: any leak blocks the build.

Absence rather than masking is asserted deliberately. `observability.md` §5 forbids a mask that
reveals length, and the strongest way to satisfy that is to have no key at all, so the tests
assert the key is gone rather than that its value changed.

The planted values in this file are fabricated. They are shaped like the real thing — that is
the point of a canary — but none of them authenticates anything.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr

from video_agent.observability import redaction
from video_agent.observability.redaction import (
    ALLOWED_FIELDS,
    CREDENTIAL_KEY_PATTERNS,
    MAX_IDENTIFIER_CHARS,
    PROMPT_PREVIEW_CHARS,
    REDACTION_TRIPWIRE_ALARM,
    FieldKind,
    HitKind,
    RedactionTripwireError,
    TripwireMode,
    is_credential_key,
    is_presigned_url,
    looks_like_media,
    looks_like_secret,
    redact,
    scan_payload,
    summarise_prompt,
    tripwire_mode_for_env,
)

# --- Planted values -----------------------------------------------------------------------------

PLANTED_API_KEY = "sk-proj-9dK2mQ7xVn4RtY8sLp3JhW6zBc1FgA5eDu0iOq2X"
"""Shaped like a vendor key: issuer prefix, then 40 characters of base62."""

PLANTED_OPAQUE_CREDENTIAL = "Zk9pQ2mR7xVn4RtY8sLp3JhW6zBc1FgA5eDu0iOq"
"""40 characters, mixed case, digits, no issuer prefix — caught by shape alone."""

PLANTED_S3_PRESIGNED = (
    "https://artifacts.example.com/tenant/job/shot-0.mp4"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20260808%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Expires=3600"
    "&X-Amz-Signature=8f4b2c1d9e7a6f5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b"
)
"""An object-store presign. `[D-52]`: the authorisation is in the query string."""

PLANTED_PROVIDER_UPLOAD_URL = (
    "https://videos.example.com/uploads/9f2c.mp4?token=Qm5xR8vT2wY6zA0bC4dE7fH1jK3lM9nP"
)
"""The video provider's `upload_url` shape. `[D-64]`: it carries auth in the query too."""

PLANTED_PROVIDER_DOWNLOAD_URL = (
    "https://videos.example.com/renders/9f2c.mp4?Expires=1786000000&Signature=aB3dE5gH7jK9"
)
"""The provider's `downloads[].url` shape `[D-58]`, `[D-64]`."""

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10"
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 32
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode("ascii")
MP4_BASE64 = base64.b64encode(MP4_BYTES).decode("ascii")
PNG_DATA_URI = f"data:image/png;base64,{PNG_BASE64}"

SHA256_HEX_CHARS = 64
MINIMUM_CREDENTIAL_PATTERNS = 10

LONG_PROMPT = (
    "A lighthouse keeper discovers a message in a bottle and the town changes forever. " * 40
)


@pytest.fixture(autouse=True)
def _reset_alarm() -> None:
    """The tripwire counter is process-wide; a test that asserts on it needs it at zero."""
    REDACTION_TRIPWIRE_ALARM.reset()


def _drop(payload: Mapping[str, object]) -> dict[str, object]:
    """Redact in production mode, so a planted value is dropped rather than raising.

    The raising path has its own tests. Here the question is what survives, and an exception
    would answer a different question.
    """
    return redact(payload, mode=TripwireMode.DROP)


# --- Deny by default ----------------------------------------------------------------------------


def test_an_unknown_field_is_dropped_not_masked() -> None:
    """`S0.3.3` acceptance 1 — the output dict has no such key."""
    result = _drop({"msg": "shot accepted", "internal_debug_blob": "anything at all"})

    assert "internal_debug_blob" not in result
    assert result["msg"] == "shot accepted"


def test_a_field_added_tomorrow_is_dropped_until_allow_listed() -> None:
    """Fail-safe, not fail-open `[observability.md §10]`. New fields are invisible by default."""
    result = _drop({"a_field_nobody_has_thought_of_yet": "value"})

    assert result == {}


def test_nothing_is_masked_in_a_way_that_reveals_length() -> None:
    """A `****` of the right width publishes the length of the secret it hides."""
    result = _drop({"api_key": PLANTED_API_KEY, "msg": "x"})

    assert "*" not in str(result)
    assert str(len(PLANTED_API_KEY)) not in str(result)


# --- Credentials --------------------------------------------------------------------------------


def test_a_credential_is_dropped_by_key_name() -> None:
    result = _drop({"api_key": PLANTED_API_KEY, "msg": "calling upstream"})

    assert "api_key" not in result
    assert PLANTED_API_KEY not in str(result)


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "API_KEY",
        "x-api-key",
        "MAGICHOUR_API_KEY",
        "webhook_secret",
        "db_password",
        "access_token",
        "authorization",
        "aws_secret_access_key",
        "connection_string",
        "database_url",
        "session_id",
    ],
)
def test_credential_key_names_are_recognised(name: str) -> None:
    assert is_credential_key(name)


@pytest.mark.parametrize("name", ["storage_key", "provider_key", "artifact_id", "job_id", "node"])
def test_identifier_key_names_are_not_mistaken_for_credentials(name: str) -> None:
    """`storage_key` and `provider_key` are required span attributes `[§2.2]`, `[§5]`.

    A pattern that matched a bare `key` would drop them, and the fix someone would reach for
    is renaming the *credential* fields — which defeats the rule entirely.
    """
    assert not is_credential_key(name)


def test_no_allow_listed_field_is_credential_shaped_by_name() -> None:
    """A guard on the allow-list itself, so widening it cannot admit a credential by name."""
    offenders = [name for name in ALLOWED_FIELDS if is_credential_key(name)]
    assert offenders == []


def test_widening_the_allow_list_cannot_admit_a_credential_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key rule runs *before* the allow-list, and this is the only way to observe that.

    Today no allow-listed field has a credential-shaped name, so the two rules agree and
    neither can be seen working alone. The guarantee that matters is about tomorrow: someone
    allow-lists `api_key` for a debugging session. Widening the list here proves the credential
    rule still refuses it — which is the claim the module docstring makes.
    """
    monkeypatch.setattr(redaction, "ALLOWED_FIELDS", {**ALLOWED_FIELDS, "api_key": FieldKind.TEXT})

    result = _drop({"api_key": "hunter2", "msg": "ok"})

    assert "api_key" not in result
    assert result["msg"] == "ok"


def test_the_key_rule_does_not_depend_on_the_value_looking_like_a_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A weak password under a credential name is still a credential."""
    monkeypatch.setattr(
        redaction, "ALLOWED_FIELDS", {**ALLOWED_FIELDS, "db_password": FieldKind.TEXT}
    )

    assert _drop({"db_password": "letmein"}) == {}


ONE_FIELD_PER_KIND: dict[FieldKind, str] = {
    FieldKind.IDENTIFIER: "node",
    FieldKind.HASH: "prompt_sha256",
    FieldKind.TEXT: "msg",
    FieldKind.NUMBER: "shot_index",
    FieldKind.BOOLEAN: "degraded",
    FieldKind.TIMESTAMP: "ts",
    FieldKind.PROMPT: "prompt",
    FieldKind.NESTED: "capabilities_required",
}


def test_every_field_kind_is_covered_by_the_type_matrix() -> None:
    """So a kind added later is not silently excused from the test below."""
    assert set(ONE_FIELD_PER_KIND) == set(FieldKind)
    assert all(ALLOWED_FIELDS[field] is kind for kind, field in ONE_FIELD_PER_KIND.items())


@pytest.mark.parametrize("field", sorted(ONE_FIELD_PER_KIND.values()))
@pytest.mark.parametrize(
    "value",
    [PNG_BYTES, MP4_BYTES, bytearray(PNG_BYTES), SecretStr("x"), memoryview(PNG_BYTES)],
    ids=["png", "mp4", "bytearray", "secretstr", "memoryview"],
)
def test_no_field_kind_admits_raw_bytes_or_a_secret_wrapper(field: str, value: object) -> None:
    """`observability.md` §5 — no bytes anywhere, under any field, whatever its kind.

    Asserted across every kind rather than at one choke point, so a cleaner that grows
    permissive later fails here instead of being covered by a guard nobody can see.
    """
    assert _drop({field: value}) == {}


def test_a_credential_is_dropped_by_value_shape_under_an_innocuous_key() -> None:
    """`S0.3.3` acceptance 2 — 40 characters of entropy filed under `node`."""
    result = _drop({"node": PLANTED_OPAQUE_CREDENTIAL, "msg": "entering node"})

    assert "node" not in result
    assert PLANTED_OPAQUE_CREDENTIAL not in str(result)


def test_a_credential_in_the_message_text_is_dropped() -> None:
    """The commonest accident: interpolating the value into the message instead of a field."""
    result = _drop({"msg": f"authenticating with {PLANTED_API_KEY}"})

    assert PLANTED_API_KEY not in str(result)
    assert "msg" not in result


def test_a_secret_wrapper_is_dropped() -> None:
    """A `SecretStr` reaching an emission path is itself the defect, not a safe rendering."""
    result = _drop({"node": SecretStr(PLANTED_API_KEY), "msg": "ok"})

    assert "node" not in result
    assert PLANTED_API_KEY not in str(result)
    assert "**" not in str(result)


@pytest.mark.parametrize(
    "value",
    [
        PLANTED_OPAQUE_CREDENTIAL,
        "aB3dE5gH7jK9lM1nO2pQ4rS6tU8vW0xY2zA4bC6d",
        "Xy7Kp2Lm9Qr4Tv6Wz1Bd3Fh5Jn8Sc0Ge2Iu4Oa6M",
    ],
)
def test_high_entropy_tokens_are_recognised(value: str) -> None:
    assert looks_like_secret(value)


@pytest.mark.parametrize(
    "value",
    [
        "shot accepted after two attempts",
        "0d8f6c2a-4e1b-4f7a-9c3d-5b6e7f8a9b0c",
        "e1630f843370f402870799e14abbf2b06af2d23b0153658e1211dffabc61ad8f",
        "tenant/job/shot-0.mp4",
        "reasoning-high",
    ],
)
def test_ordinary_values_are_not_mistaken_for_secrets(value: str) -> None:
    """False positives matter: a shape check that dropped UUIDs and digests would delete the
    identifiers the trace model is built on, and the pressure would be to weaken it."""
    assert not looks_like_secret(value)


def test_a_uuid_job_id_survives_redaction() -> None:
    job_id = "0d8f6c2a-4e1b-4f7a-9c3d-5b6e7f8a9b0c"
    assert _drop({"job_id": job_id})["job_id"] == job_id


def test_a_sha256_digest_survives_under_a_hash_field() -> None:
    digest = "e1630f843370f402870799e14abbf2b06af2d23b0153658e1211dffabc61ad8f"
    assert _drop({"checksum_sha256": digest})["checksum_sha256"] == digest


def test_a_hash_field_holding_something_that_is_not_a_hash_is_dropped() -> None:
    """A field repurposed is a field whose contents nobody has vetted."""
    assert _drop({"checksum_sha256": PLANTED_S3_PRESIGNED}) == {}


# --- Presigned URLs -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [PLANTED_S3_PRESIGNED, PLANTED_PROVIDER_UPLOAD_URL, PLANTED_PROVIDER_DOWNLOAD_URL],
)
def test_presigned_urls_are_recognised(url: str) -> None:
    """`S0.3.3` acceptance 3 — `[D-52]` extended to the provider's URLs by `[D-64]`."""
    assert is_presigned_url(url)


@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("msg", PLANTED_S3_PRESIGNED),
        ("storage_key", PLANTED_PROVIDER_UPLOAD_URL),
        ("reason", PLANTED_PROVIDER_DOWNLOAD_URL),
    ],
)
def test_a_presigned_url_is_dropped_whatever_field_it_arrives_in(field: str, url: str) -> None:
    result = _drop({field: url})

    assert field not in result
    assert "Signature" not in str(result)
    assert "token" not in str(result)


def test_a_presigned_url_nested_in_a_downloads_list_is_dropped() -> None:
    """The provider returns `downloads[].url`; a top-level-only rule would miss it entirely."""
    payload = {
        "capabilities_required": [{"url": PLANTED_PROVIDER_DOWNLOAD_URL, "expires_at": 1786}],
        "msg": "render complete",
    }

    result = _drop(payload)

    assert PLANTED_PROVIDER_DOWNLOAD_URL not in str(result)
    assert "aB3dE5gH7jK9" not in str(result)


def test_an_ordinary_url_without_query_auth_is_not_treated_as_a_credential() -> None:
    """Dropping every URL would make the rule useless in practice and invite an exception."""
    assert not is_presigned_url("https://api.example.com/v1/jobs/9f2c")
    assert not is_presigned_url("https://api.example.com/v1/jobs?limit=10&status=running")


def test_a_url_with_an_unfamiliar_but_high_entropy_query_parameter_is_dropped() -> None:
    """The provider may rename its parameter without telling us; the shape still gives it away."""
    unfamiliar = f"https://videos.example.com/r/9f2c.mp4?grant={PLANTED_OPAQUE_CREDENTIAL}"

    assert is_presigned_url(unfamiliar)
    assert PLANTED_OPAQUE_CREDENTIAL not in str(_drop({"msg": unfamiliar}))


# --- Media payloads -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [PNG_BASE64, MP4_BASE64, PNG_DATA_URI, "\x89PNG\r\n\x1a\n binary noise"],
)
def test_media_payloads_are_recognised(value: str) -> None:
    """PNG and MP4, raw and base64-encoded, plus the data-URI spelling `[§5]`."""
    assert looks_like_media(value) or value.startswith("data:")


@pytest.mark.parametrize("value", [PNG_BASE64, MP4_BASE64, PNG_DATA_URI])
def test_a_media_payload_is_dropped(value: str) -> None:
    result = _drop({"msg": value, "storage_key": "tenant/job/shot-0.mp4"})

    assert "msg" not in result
    assert result["storage_key"] == "tenant/job/shot-0.mp4"


@pytest.mark.parametrize("value", [PNG_BYTES, MP4_BYTES])
def test_raw_bytes_never_survive(value: bytes) -> None:
    """`observability.md` §5: no bytes, no base64, no data URIs, anywhere."""
    assert _drop({"msg": value}) == {}


def test_an_artifact_is_referenced_by_key_not_carried() -> None:
    payload = {
        "artifact_id": "art_9f2c",
        "storage_key": "tenant/job/shot-0.mp4",
        "content": PNG_BASE64,
    }

    assert _drop(payload) == {"artifact_id": "art_9f2c", "storage_key": "tenant/job/shot-0.mp4"}


# --- The user prompt ----------------------------------------------------------------------------


def test_a_prompt_is_emitted_as_a_digest_plus_sixty_four_characters() -> None:
    """`S0.3.3` acceptance 4."""
    result = _drop({"prompt": LONG_PROMPT})

    assert "prompt" not in result
    assert result["prompt_preview"] == LONG_PROMPT[:PROMPT_PREVIEW_CHARS]
    assert len(str(result["prompt_preview"])) == PROMPT_PREVIEW_CHARS
    assert len(str(result["prompt_sha256"])) == SHA256_HEX_CHARS


def test_the_prompt_digest_identifies_the_prompt() -> None:
    """Same prompt, same digest — otherwise the digest cannot correlate two runs."""
    assert summarise_prompt(LONG_PROMPT) == summarise_prompt(LONG_PROMPT)
    assert summarise_prompt(LONG_PROMPT) != summarise_prompt(LONG_PROMPT + "!")


def test_a_prompt_preview_containing_a_credential_is_dropped_but_the_digest_survives() -> None:
    """A user who pastes a key into their prompt must not have it published in the preview."""
    summary = summarise_prompt(f"{PLANTED_API_KEY} make me a film")

    assert "prompt_preview" not in summary
    assert len(summary["prompt_sha256"]) == SHA256_HEX_CHARS


def test_a_short_prompt_is_still_never_emitted_in_full_under_its_own_key() -> None:
    result = _drop({"prompt": "a cat"})

    assert "prompt" not in result
    assert result["prompt_preview"] == "a cat"


# --- Row-level results --------------------------------------------------------------------------


def test_query_rows_are_dropped_and_the_count_survives() -> None:
    """`[§5]` — log the statement identity and the row count, never the rows."""
    payload = {
        "statement_id": "select_jobs_by_tenant",
        "row_count": 3,
        "rows": [{"id": 1, "prompt": "private"}, {"id": 2, "prompt": "also private"}],
    }

    result = _drop(payload)

    assert result == {"statement_id": "select_jobs_by_tenant", "row_count": 3}


# --- The tripwire -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("local", TripwireMode.RAISE),
        ("dev", TripwireMode.RAISE),
        ("ci", TripwireMode.RAISE),
        ("test", TripwireMode.RAISE),
        ("staging", TripwireMode.RAISE),
        ("production", TripwireMode.DROP),
        ("PRODUCTION", TripwireMode.DROP),
        ("prod", TripwireMode.DROP),
    ],
)
def test_the_tripwire_mode_is_read_from_env(env: str, expected: TripwireMode) -> None:
    """`S0.3.3` acceptance 5. Anything unrecognised raises, so a typo makes CI louder."""
    assert tripwire_mode_for_env(env) is expected


def test_the_tripwire_raises_in_ci() -> None:
    with pytest.raises(RedactionTripwireError) as raised:
        redact({"msg": PLANTED_S3_PRESIGNED}, mode=tripwire_mode_for_env("ci"))

    assert "presigned_url" in str(raised.value)
    assert REDACTION_TRIPWIRE_ALARM.count == 0


def test_the_tripwire_drops_and_alarms_in_production() -> None:
    """`[D-57]` — a telemetry concern never takes the product down."""
    result = redact({"msg": PLANTED_S3_PRESIGNED}, mode=tripwire_mode_for_env("production"))

    assert result == {}
    assert REDACTION_TRIPWIRE_ALARM.count == 1


def test_the_tripwire_fires_even_for_a_field_the_allow_list_would_have_dropped() -> None:
    """Filtering silently is how a leaking call site survives to the next release."""
    with pytest.raises(RedactionTripwireError):
        redact({"some_unlisted_field": PLANTED_API_KEY}, mode=TripwireMode.RAISE)


def test_a_clean_payload_does_not_trip_the_wire() -> None:
    hits = scan_payload({"msg": "shot accepted", "shot_index": 1, "job_id": "9f2c"})

    assert hits == []


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"api_key": PLANTED_API_KEY}, HitKind.CREDENTIAL_KEY),
        ({"node": PLANTED_OPAQUE_CREDENTIAL}, HitKind.CREDENTIAL_SHAPE),
        ({"node": PLANTED_API_KEY}, HitKind.KNOWN_KEY_PREFIX),
        ({"msg": PLANTED_S3_PRESIGNED}, HitKind.PRESIGNED_URL),
        ({"msg": MP4_BASE64}, HitKind.MEDIA_PAYLOAD),
        ({"msg": PNG_DATA_URI}, HitKind.DATA_URI),
        ({"msg": SecretStr("x")}, HitKind.CREDENTIAL_OBJECT),
        ({"msg": b"plain bytes"}, HitKind.RAW_BYTES),
    ],
)
def test_the_tripwire_names_which_rule_fired(payload: Mapping[str, object], kind: HitKind) -> None:
    """An alarm that said only "something leaked" would not tell anyone where to look."""
    hits = scan_payload(payload)

    assert [hit.kind for hit in hits] == [kind]


def test_the_tripwire_reports_the_path_to_a_nested_hit() -> None:
    payload = {"capabilities_required": [{"api_key": PLANTED_API_KEY}]}

    hits = scan_payload(payload)

    assert hits[0].path == "capabilities_required[0].api_key"


def test_credential_patterns_are_not_empty() -> None:
    """A guard against the list being emptied, which would make the name rule vacuous."""
    assert len(CREDENTIAL_KEY_PATTERNS) >= MINIMUM_CREDENTIAL_PATTERNS


# --- The per-kind value rules --------------------------------------------------------------


def test_an_empty_string_has_no_entropy() -> None:
    """Guards the entropy calculation against a divide-by-zero on an empty field."""
    assert redaction.shannon_entropy("") == 0.0


def test_a_base64_shaped_string_that_is_not_media_survives() -> None:
    """The media rule decodes before it judges, so a long opaque token is not media."""
    assert not looks_like_media("VGhpc0lzSnVzdFNvbWVUZXh0Tm90TWVkaWFBdEFsbA")


def test_a_string_that_cannot_be_decoded_is_not_media() -> None:
    assert not looks_like_media("!!!!not base64 at all!!!!")


def test_a_base64_shaped_string_of_undecodable_length_is_not_media() -> None:
    """Seventeen characters is one more than a multiple of four, which no base64 encoder emits.

    Reachable rather than theoretical: the decode is inside the log formatter's path, so a
    string that merely *looks* like base64 must return an answer, not raise.
    """
    assert not looks_like_media("A" * 17)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "has a space", "x" * (MAX_IDENTIFIER_CHARS + 1), 17, None],
    ids=["empty", "blank", "spaced", "overlong", "number", "none"],
)
def test_an_identifier_field_refuses_anything_that_is_not_an_identifier(value: object) -> None:
    assert _drop({"node": value}) == {}


def test_an_identifier_field_refuses_a_credentialed_database_url() -> None:
    """`AGENT.md` §3 lists DB URLs; the password sits before the `@`, not in the query."""
    assert _drop({"storage_key": "postgresql://user:hunter2@db.internal:5432/videoagent"}) == {}


def test_a_decimal_cost_keeps_its_exact_value() -> None:
    """`[D-60]` — costs reconcile to the cent, so a float round-trip is not acceptable."""
    assert _drop({"cost_usd": Decimal("0.10")})["cost_usd"] == "0.10"


def test_a_boolean_is_not_a_number() -> None:
    """`True` is an `int` in Python; a `shot_index` of `True` is a bug, not shot one."""
    assert _drop({"shot_index": True}) == {}


def test_a_number_field_refuses_a_string() -> None:
    assert _drop({"shot_index": "two"}) == {}


def test_a_boolean_field_refuses_a_truthy_string() -> None:
    """`"false"` is truthy; admitting it would invert the meaning of `degraded`."""
    assert _drop({"degraded": "false"}) == {}
    assert _drop({"degraded": False})["degraded"] is False


def test_a_datetime_timestamp_is_rendered_in_iso_form() -> None:
    moment = datetime(2026, 8, 8, 10, 14, 2, tzinfo=UTC)

    assert _drop({"ts": moment})["ts"] == moment.isoformat()


def test_an_overlong_timestamp_string_is_dropped() -> None:
    assert _drop({"ts": "x" * (MAX_IDENTIFIER_CHARS + 1)}) == {}


def test_a_nested_object_is_redacted_by_the_same_rules() -> None:
    payload = {"capabilities_required": {"alias": "vision-default", "api_key": PLANTED_API_KEY}}

    assert _drop(payload) == {"capabilities_required": {"alias": "vision-default"}}


def test_a_nested_array_keeps_its_scalars_and_drops_the_rest() -> None:
    payload = {"capabilities_required": ["text-to-video", 10, True, PLANTED_API_KEY, None]}

    assert _drop(payload) == {"capabilities_required": ["text-to-video", 10, True]}


def test_a_nested_field_holding_a_scalar_is_dropped() -> None:
    """`capabilities_required` is a collection; a bare string there is a shape nobody vetted."""
    assert _drop({"capabilities_required": "text-to-video"}) == {}
