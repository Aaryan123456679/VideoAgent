"""Deny-by-default redaction, and the tripwire that catches what the allow-list did not.

> **Never logged:** credentials, raw PII, full media payloads, row-level query results.
> `[CPS §Observability]`, `AGENT.md` §3

That sentence is a rule about *every* emission path, and a rule enforced by review is a rule
that lasts until the first busy afternoon. So it is implemented as a serialiser with the
default inverted: a field is **dropped unless it is explicitly allow-listed**. The failure
mode of a forgotten allow-list entry is a missing field in a dashboard. The failure mode of a
deny-list is a credential in a log aggregator, which is unrecoverable — the secret must be
rotated, and every copy of the log is already gone.

**Dropped, never masked.** `observability.md` §5 is specific that credentials are never
`****`-masked in a way that reveals length, and the simplest way to guarantee that is to not
emit the key at all. A masked field also *teaches* the reader that this record type carries a
secret, which is exactly the fact worth not publishing.

**Three defences, deliberately overlapping**, because each catches what the others miss:

1. *Key name.* `api_key` is dropped whatever it contains, and before the allow-list is even
   consulted, so widening the allow-list cannot accidentally admit one.
2. *Value shape.* A credential does not stop being a credential because it was filed under an
   innocuous key. High-entropy tokens, known issuer prefixes, URLs carrying query-string auth,
   and media magic bytes — raw, base64-encoded or in a data URI — are dropped on sight.
3. *The tripwire.* The scan runs over the **whole** payload, including the parts the
   allow-list would have dropped anyway, because the interesting fact is not that the secret
   was filtered but that something handed it to the logging system at all. In dev and CI that
   raises; in production it drops and increments an alarm, since taking a request down over a
   telemetry concern would be the observability tool damaging the product it observes
   `[D-57]`.

**Presigned URLs are credentials, not links.** `[D-52]`, `[D-64]`. Anyone holding the URL can
read or write the object until it expires; the authorisation is in the query string. This
covers the video provider's `upload_url` and `downloads[].url` as well as object-store presign
output — all three are bearer tokens that happen to be shaped like a link.

**The user prompt is PII by assumption.** It is emitted as `prompt_sha256` plus the first 64
characters and never in full; the full text lives only in the RLS-protected `job` row.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from math import log2
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit

from video_agent.observability.alarms import AlarmCounter

# --- Modes ---------------------------------------------------------------------------------


class TripwireMode(StrEnum):
    """What to do when the tripwire finds something that must never be emitted."""

    RAISE = "raise"
    DROP = "drop"


PRODUCTION_ENVS: Final[frozenset[str]] = frozenset({"production", "prod"})
"""`ENV` values that mean "real users are on the other end of this process"."""


class RedactionTripwireError(RuntimeError):
    """A value that may never be emitted reached an emission path.

    Raised outside production so the build stops and the offending call site is fixed. In
    production the same condition drops the value and increments `REDACTION_TRIPWIRE_ALARM`.
    """


REDACTION_TRIPWIRE_ALARM: Final[AlarmCounter] = AlarmCounter("redaction_tripwire_hits")
"""Production hit count. A non-zero value means code is trying to log something it must not."""


def tripwire_mode_for_env(env: str) -> TripwireMode:
    """The tripwire mode implied by `ENV` `[observability.md §5]`.

    Anything that is not explicitly production raises. The default has to point that way: a
    misspelled environment name should make the build noisier, not quieter.
    """
    return TripwireMode.DROP if env.strip().lower() in PRODUCTION_ENVS else TripwireMode.RAISE


# --- Field allow-list ----------------------------------------------------------------------


class FieldKind(StrEnum):
    """What an allow-listed field is permitted to contain.

    The allow-list carries a *kind* rather than only a name because "this field may be
    emitted" and "this field may contain anything" are different permissions. A field declared
    to hold a hash that suddenly holds a URL has been repurposed, and the value is dropped.
    """

    IDENTIFIER = "identifier"
    HASH = "hash"
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    TIMESTAMP = "timestamp"
    PROMPT = "prompt"
    NESTED = "nested"


ALLOWED_FIELDS: Final[Mapping[str, FieldKind]] = {
    # The log line schema — observability.md §4.
    "ts": FieldKind.TIMESTAMP,
    "level": FieldKind.TEXT,
    "msg": FieldKind.TEXT,
    "logger": FieldKind.IDENTIFIER,
    "trace_id": FieldKind.IDENTIFIER,
    "span_id": FieldKind.IDENTIFIER,
    "job_id": FieldKind.IDENTIFIER,
    "tenant_id": FieldKind.IDENTIFIER,
    "node": FieldKind.IDENTIFIER,
    "code": FieldKind.IDENTIFIER,
    "degraded": FieldKind.BOOLEAN,
    "trace_synthesised": FieldKind.BOOLEAN,
    # Mandatory span attributes — observability.md §2.2.
    "shot_index": FieldKind.NUMBER,
    "attempt_no": FieldKind.NUMBER,
    "budget_epoch": FieldKind.NUMBER,
    # Generation attributes.
    "alias": FieldKind.IDENTIFIER,
    "model_used": FieldKind.IDENTIFIER,
    "prompt_name": FieldKind.IDENTIFIER,
    "prompt_version": FieldKind.IDENTIFIER,
    "input_tokens": FieldKind.NUMBER,
    "output_tokens": FieldKind.NUMBER,
    "cost_usd": FieldKind.NUMBER,
    "latency_ms": FieldKind.NUMBER,
    # Provider span attributes.
    "provider_key": FieldKind.IDENTIFIER,
    "provider_model": FieldKind.IDENTIFIER,
    "provider_project_id": FieldKind.IDENTIFIER,
    "capabilities_required": FieldKind.NESTED,
    "credits_charged": FieldKind.NUMBER,
    "cost_is_final": FieldKind.BOOLEAN,
    "request_fingerprint": FieldKind.IDENTIFIER,
    "seed": FieldKind.NUMBER,
    # Artifacts are referenced, never carried — observability.md §5.
    "artifact_id": FieldKind.IDENTIFIER,
    "storage_key": FieldKind.IDENTIFIER,
    "checksum_sha256": FieldKind.HASH,
    "bible_hash": FieldKind.HASH,
    # Queries are logged by identity and row count, never by row.
    "statement_id": FieldKind.IDENTIFIER,
    "row_count": FieldKind.NUMBER,
    # Outcomes, scores and events.
    "outcome": FieldKind.IDENTIFIER,
    "event": FieldKind.IDENTIFIER,
    "reason": FieldKind.TEXT,
    "score_name": FieldKind.IDENTIFIER,
    "score": FieldKind.NUMBER,
    "retryable": FieldKind.BOOLEAN,
    "http_status": FieldKind.NUMBER,
    "duration_ms": FieldKind.NUMBER,
    "exc_type": FieldKind.IDENTIFIER,
    # The user prompt, and only in the form §5 permits.
    "prompt": FieldKind.PROMPT,
    "prompt_sha256": FieldKind.HASH,
    "prompt_preview": FieldKind.TEXT,
}
"""Every field that may be emitted, and what it may hold.

Adding an entry is a deliberate act with a spec reference behind it. `AGENT.md` §3: *do not
add an allow-list entry for a field that could carry any of the above.*
"""

PROMPT_PREVIEW_CHARS: Final = 64
"""`observability.md` §5: `prompt_sha256` plus the first 64 characters, never the full text."""

MAX_TEXT_CHARS: Final = 1024
"""Free text is truncated rather than trusted to be short. Model output is truncated too."""

MAX_IDENTIFIER_CHARS: Final = 256

_IDENTIFIER_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@=-]*$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")


# --- Credential key names -------------------------------------------------------------------

CREDENTIAL_KEY_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        "secret",
        "secrets",
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "token",
        "credential",
        "credentials",
        "authorization",
        "auth",
        "apikey",
        "signature",
        "cookie",
        "dsn",
        "bearer",
        "jwt",
    }
)
"""Whole `_`-separated segments of a field name that declare the value a credential.

Segments rather than substrings, and the distinction is load-bearing in both directions.
`token` as a substring would drop `input_tokens` and `output_tokens`, which are counts the cost
accounting depends on — and a rule that deletes the numbers on every generation gets weakened
within a week. Meanwhile `tokens` is deliberately absent: a plural is a count, a singular is a
credential.

Bare `key` is absent for the same reason. `storage_key` and `provider_key` are identifiers the
trace model requires `[observability.md §2.2, §5]`; a pattern that dropped them would push
someone into renaming the *credential* fields instead, which defeats the rule entirely.
"""

CREDENTIAL_KEY_PHRASES: Final[tuple[str, ...]] = (
    "api_key",
    "access_key",
    "secret_key",
    "private_key",
    "public_key",
    "master_key",
    "signing_key",
    "encryption_key",
    "session_id",
    "database_url",
    "redis_url",
    "connection_string",
)
"""Multi-word names where neither word alone is enough. Matched as substrings, so
`UPSTREAM_API_KEY` and `x-api-key` both hit while `storage_key` does not."""

CREDENTIAL_KEY_PATTERNS: Final[tuple[str, ...]] = (
    tuple(sorted(CREDENTIAL_KEY_SEGMENTS)) + CREDENTIAL_KEY_PHRASES
)
"""Everything the name rule knows about, for the test that asserts it is not empty."""


def _normalise_key(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def is_credential_key(name: str) -> bool:
    """Whether a field name declares its value to be a credential."""
    normalised = _normalise_key(name)
    if any(phrase in normalised for phrase in CREDENTIAL_KEY_PHRASES):
        return True
    return any(segment in CREDENTIAL_KEY_SEGMENTS for segment in normalised.split("_"))


# --- Value shapes -----------------------------------------------------------------------------

KNOWN_CREDENTIAL_PREFIXES: Final[tuple[str, ...]] = (
    "sk-",
    "sk_",
    "rk_live_",
    "pk_live_",
    "AKIA",
    "ASIA",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "AIza",
    "ya29.",
    "eyJ",
    "hf_",
    "-----BEGIN",
)
"""Issuer prefixes that identify a secret regardless of entropy or length."""

CASE_INSENSITIVE_CREDENTIAL_PREFIXES: Final[tuple[str, ...]] = ("bearer ", "basic ")

SIGNATURE_QUERY_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
        "goog-signature",
        "awsaccesskeyid",
        "signature",
        "sig",
        "token",
        "access_token",
        "key-pair-id",
        "policy",
        "se",
        "sp",
        "sr",
        "skoid",
    }
)
"""Query parameters that carry authorisation. Their presence makes a URL a bearer credential."""

MEDIA_MAGIC_PREFIXES: Final[tuple[bytes, ...]] = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
    b"\x1a\x45\xdf\xa3",
    b"OggS",
    b"ID3",
    b"%PDF",
    b"\x00\x00\x00\x18ftyp",
)
"""Leading bytes that identify a media container."""

ISO_BMFF_BOX_TYPES: Final[tuple[bytes, ...]] = (b"ftyp", b"moov", b"mdat", b"free")
"""MP4 and friends carry a length prefix first, so the type sits at offset 4."""

ISO_BMFF_HEADER_BYTES: Final = 8
"""Length prefix plus box type: the smallest slice that can identify an ISO-BMFF container."""

MIN_SECRET_LENGTH: Final = 24
MIN_SECRET_ENTROPY: Final = 3.5
_SECRET_CHARSET: Final = re.compile(r"^[A-Za-z0-9+/=_.~-]+$")
_URL_SCHEME_RE: Final = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_BASE64_RE: Final = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
_BYTES_REPR_RE: Final = re.compile(r"""^b['"]""")
"""`str(some_bytes)` produces `b'...'`. That is a payload wearing a text costume: the magic
bytes are backslash-escaped, so every byte-level check misses it while the content is intact."""
_BASE64_CHUNK: Final = 64


def shannon_entropy(value: str) -> float:
    """Bits of entropy per character. Cheap, and the standard test for "this looks random"."""
    if not value:
        return 0.0
    total = len(value)
    return -sum((count / total) * log2(count / total) for count in Counter(value).values())


def looks_like_secret(token: str) -> bool:
    """Whether a bare token has the shape of a generated credential.

    Four conditions together, because each alone has an obvious false positive. Length and
    entropy alone flag UUIDs and hex digests; the mixed-case-and-digit requirement is what
    separates a base64-ish generated secret from an identifier, since hex ids and UUIDs have
    no uppercase and English words have no digits. The charset check keeps ordinary prose out
    by construction — a credential has no spaces or punctuation in it.
    """
    if len(token) < MIN_SECRET_LENGTH or not _SECRET_CHARSET.match(token):
        return False
    has_upper = any(character.isupper() for character in token)
    has_lower = any(character.islower() for character in token)
    has_digit = any(character.isdigit() for character in token)
    if not (has_upper and has_lower and has_digit):
        return False
    return shannon_entropy(token) >= MIN_SECRET_ENTROPY


def has_known_credential_prefix(value: str) -> bool:
    """Whether a string starts with an issuer prefix that identifies it as a secret."""
    stripped = value.strip()
    lowered = stripped.lower()
    return stripped.startswith(KNOWN_CREDENTIAL_PREFIXES) or lowered.startswith(
        CASE_INSENSITIVE_CREDENTIAL_PREFIXES
    )


def is_presigned_url(value: str) -> bool:
    """Whether a string is a URL carrying authorisation in its query string.

    Two rules. The named-parameter rule catches the standard presign dialects. The
    high-entropy-parameter rule catches the ones this codebase has not met yet — including the
    video provider's `upload_url` and `downloads[].url` `[D-64]`, whose parameter names are
    the provider's business and may change without telling us.
    """
    stripped = value.strip()
    if not _URL_SCHEME_RE.match(stripped):
        return False
    query = urlsplit(stripped).query
    if not query:
        return False
    for name, parameter in parse_qsl(query, keep_blank_values=True):
        if name.strip().lower() in SIGNATURE_QUERY_PARAMS:
            return True
        if looks_like_secret(parameter) or has_known_credential_prefix(parameter):
            return True
    return False


def is_credentialed_url(value: str) -> bool:
    """Whether a string is a URL carrying a password in its userinfo.

    `postgresql+asyncpg://user:hunter2@host/db` is the shape, and `AGENT.md` §3 names DB URLs
    on the never-logged list for the obvious reason. The presigned-URL rule does not catch it:
    the credential is before the `@`, not in the query, and `:` and `@` are outside the charset
    the entropy check considers, so a connection string sails through every other detector.
    """
    stripped = value.strip()
    if not _URL_SCHEME_RE.match(stripped):
        return False
    userinfo = urlsplit(stripped).netloc.rpartition("@")[0]
    return ":" in userinfo


def _has_media_magic(raw: bytes) -> bool:
    if raw.startswith(MEDIA_MAGIC_PREFIXES):
        return True
    return len(raw) >= ISO_BMFF_HEADER_BYTES and raw[4:ISO_BMFF_HEADER_BYTES] in ISO_BMFF_BOX_TYPES


def is_data_uri(value: str) -> bool:
    """Whether a string is a `data:` URI. Those are inline payloads by definition."""
    return value.strip().lower().startswith("data:")


def looks_like_media(value: str) -> bool:
    """Whether a string carries media bytes, raw or base64-encoded.

    Decoding a prefix rather than matching known base64 preambles: the encodings of the same
    magic bytes differ by alignment, so a literal-prefix table would have three entries per
    format and miss the fourth.

    The raw check encodes as latin-1, not UTF-8, because latin-1 maps code points 0 to 255 back to
    the bytes they came from. UTF-8 would turn the `\x89` that opens a PNG into two bytes and
    the signature would stop matching — which is how a payload decoded somewhere upstream and
    passed along as text gets past a check that looked correct.
    """
    stripped = value.strip()
    if _has_media_magic(stripped.encode("latin-1", errors="ignore")):
        return True
    candidate = stripped
    if is_data_uri(candidate):
        _, _, candidate = candidate.partition(",")
        candidate = candidate.strip()
    head = candidate[:_BASE64_CHUNK]
    if not _BASE64_RE.match(head):
        return False
    padded = head + "=" * (-len(head) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return False
    return _has_media_magic(decoded)


def is_secret_object(value: object) -> bool:
    """Whether a value is a `SecretStr`/`SecretBytes`-style wrapper.

    Duck-typed on `get_secret_value` so the check also covers anything else that adopts the
    convention. A wrapper reaching a log call is itself the defect: the value was carried all
    the way to an emission path, and only the wrapper's `__repr__` stopped it.
    """
    return callable(getattr(value, "get_secret_value", None))


# --- Tripwire -----------------------------------------------------------------------------


class HitKind(StrEnum):
    """What the tripwire recognised. Named so an alarm says which rule fired."""

    CREDENTIAL_KEY = "credential_key"
    CREDENTIAL_SHAPE = "credential_shape"
    KNOWN_KEY_PREFIX = "known_key_prefix"
    PRESIGNED_URL = "presigned_url"
    CREDENTIALED_URL = "credentialed_url"
    MEDIA_PAYLOAD = "media_payload"
    DATA_URI = "data_uri"
    CREDENTIAL_OBJECT = "credential_object"
    RAW_BYTES = "raw_bytes"


@dataclass(frozen=True, slots=True)
class TripwireHit:
    """One thing that must never be emitted, and where in the payload it was."""

    path: str
    kind: HitKind
    detail: str

    def __str__(self) -> str:
        return f"{self.path} [{self.kind}]: {self.detail}"


_STRING_DETECTORS: Final[tuple[tuple[Callable[[str], bool], HitKind, str], ...]] = (
    (
        lambda candidate: bool(_BYTES_REPR_RE.match(candidate.strip())),
        HitKind.RAW_BYTES,
        "value is the repr of a bytes object",
    ),
    (is_data_uri, HitKind.DATA_URI, "value is a data: URI carrying inline bytes"),
    (looks_like_media, HitKind.MEDIA_PAYLOAD, "value carries media container bytes"),
    (
        is_presigned_url,
        HitKind.PRESIGNED_URL,
        "value is a URL carrying authorisation in its query string [D-52], [D-64]",
    ),
    (
        is_credentialed_url,
        HitKind.CREDENTIALED_URL,
        "value is a URL carrying a password in its userinfo",
    ),
    (has_known_credential_prefix, HitKind.KNOWN_KEY_PREFIX, "value has a known issuer prefix"),
    (looks_like_secret, HitKind.CREDENTIAL_SHAPE, "value has the shape of a generated credential"),
)
"""Every string rule, in order of specificity, each with the hit it reports.

A table rather than a chain of branches so that adding a detector is one line and cannot
accidentally shadow the one above it. Order decides only which *name* a hit is reported
under, since any one of them is already fatal.
"""


def _scan_string(value: str, path: str) -> Iterator[TripwireHit]:
    """The first rule that matches, and no more: one hit per value is enough to fail.

    Every rule is applied to the whole string **and** to each whitespace-separated token,
    because the commonest real leak is not a bare secret in a field — it is
    `log.info("uploading to %s", presigned_url)`, where the forbidden value sits in the middle
    of an otherwise innocent sentence. A whole-string-only check passes that line, and it is
    the line people actually write.
    """
    for candidate in (value, *value.split()):
        for detector, kind, detail in _STRING_DETECTORS:
            if detector(candidate):
                yield TripwireHit(path, kind, detail)
                return


def _scan(value: object, path: str) -> Iterator[TripwireHit]:
    if is_secret_object(value):
        yield TripwireHit(path, HitKind.CREDENTIAL_OBJECT, "value is a secret wrapper")
        return
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        kind = HitKind.MEDIA_PAYLOAD if _has_media_magic(raw) else HitKind.RAW_BYTES
        yield TripwireHit(path, kind, "raw bytes never belong on an emission path")
        return
    if isinstance(value, str):
        yield from _scan_string(value, path)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if is_credential_key(str(key)):
                yield TripwireHit(
                    child, HitKind.CREDENTIAL_KEY, "field name declares the value a credential"
                )
                continue
            yield from _scan(item, child)
        return
    if isinstance(value, Sequence):
        for index, item in enumerate(value):
            yield from _scan(item, f"{path}[{index}]")


def scan_payload(payload: Mapping[str, Any]) -> list[TripwireHit]:
    """Every value in `payload`, however deeply nested, that must never be emitted."""
    return list(_scan(payload, ""))


def format_hits(hits: Sequence[TripwireHit]) -> str:
    """Render tripwire hits one per line for an exception or an alarm."""
    return "\n".join(str(hit) for hit in hits)


def enforce(hits: Sequence[TripwireHit], mode: TripwireMode) -> None:
    """Raise in dev and CI, count in production `[observability.md §5]`, `[D-57]`."""
    if not hits:
        return
    if mode is TripwireMode.RAISE:
        message = (
            f"redaction tripwire: {len(hits)} value(s) must never be emitted:\n{format_hits(hits)}"
        )
        raise RedactionTripwireError(message)
    REDACTION_TRIPWIRE_ALARM.increment(len(hits))


# --- The serialiser -------------------------------------------------------------------------

_DROP: Final = object()
"""Sentinel meaning "this value does not survive". Distinct from `None`, which is emittable."""


def _clean_identifier(value: object) -> object:
    if not isinstance(value, str):
        return _DROP
    stripped = value.strip()
    if not stripped or len(stripped) > MAX_IDENTIFIER_CHARS:
        return _DROP
    if not _IDENTIFIER_RE.match(stripped):
        return _DROP
    if has_known_credential_prefix(stripped) or looks_like_secret(stripped):
        return _DROP
    if is_presigned_url(stripped) or is_credentialed_url(stripped):
        return _DROP
    return stripped


def _clean_hash(value: object) -> object:
    if isinstance(value, str) and _SHA256_RE.match(value.strip()):
        return value.strip()
    return _DROP


def _clean_text(value: object) -> object:
    if not isinstance(value, str):
        return _DROP
    if any(_scan_string(value, "")):
        return _DROP
    return value[:MAX_TEXT_CHARS]


def _clean_number(value: object) -> object:
    if isinstance(value, bool):
        return _DROP
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, int | float):
        return value
    return _DROP


def _clean_boolean(value: object) -> object:
    return value if isinstance(value, bool) else _DROP


def _clean_timestamp(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and len(value) <= MAX_IDENTIFIER_CHARS:
        return value
    return _DROP


def _clean_nested(value: object) -> object:
    """Recurse into an object or an array, and refuse everything else.

    The accepted types are named positively — `Mapping`, `list`, `tuple` — rather than by
    excluding the ones that are known to be wrong. `bytes`, `bytearray` and `memoryview` are
    all `Sequence`s, so a deny-list here would have to stay exhaustive forever, and the first
    omission renders a media payload as a JSON array of integers: every byte emitted, every
    magic-byte check bypassed, nothing that looks like a leak to anyone reading the line.
    """
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, list | tuple):
        cleaned = [_clean_element(element) for element in value]
        return [element for element in cleaned if element is not _DROP]
    return _DROP


def _clean_element(value: object) -> object:
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        return _clean_number(value)
    if isinstance(value, str):
        return _clean_identifier(value)
    return _DROP


_CLEANERS: Final[Mapping[FieldKind, Callable[[object], object]]] = {
    FieldKind.IDENTIFIER: _clean_identifier,
    FieldKind.HASH: _clean_hash,
    FieldKind.TEXT: _clean_text,
    FieldKind.NUMBER: _clean_number,
    FieldKind.BOOLEAN: _clean_boolean,
    FieldKind.TIMESTAMP: _clean_timestamp,
    FieldKind.NESTED: _clean_nested,
}


def summarise_prompt(prompt: str) -> dict[str, str]:
    """The only representation of a user prompt that may leave the database `[§5]`.

    A digest so two runs of the same prompt are recognisably the same, and 64 characters so an
    engineer reading a trace can tell *which* job this is without being handed the PII the
    prompt may contain.
    """
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    summary = {"prompt_sha256": digest}
    preview = prompt[:PROMPT_PREVIEW_CHARS]
    if not any(_scan_string(preview, "")):
        summary["prompt_preview"] = preview
    return summary


def _redact_mapping(payload: Mapping[Any, Any]) -> dict[str, Any]:
    """Apply the key rules, then the kind's own rule, keeping only what survives both.

    There is no separate "drop raw bytes and secret wrappers" clause here, deliberately. Every
    cleaner above admits only the concrete types its kind describes, so a `bytes` or a
    `SecretStr` is refused by whichever cleaner it reaches — and an extra guard at this level
    would be a defence no test could distinguish from its own absence. The property is pinned
    across *all* kinds by `test_no_field_kind_admits_raw_bytes_or_a_secret_wrapper`, so a
    cleaner that grows permissive later fails there rather than being quietly covered here.
    """
    result: dict[str, Any] = {}
    for raw_key, value in payload.items():
        key = _normalise_key(str(raw_key))
        if is_credential_key(key):
            continue
        kind = ALLOWED_FIELDS.get(key)
        if kind is None:
            continue
        if kind is FieldKind.PROMPT:
            if isinstance(value, str):
                result.update(summarise_prompt(value))
            continue
        cleaned = _CLEANERS[kind](value)
        if cleaned is not _DROP:
            result[key] = cleaned
    return result


def sanitise(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the allow-list and the value rules, and report nothing.

    The serialising half of redaction, split from the enforcing half because the two belong at
    different points. Enforcement has to happen somewhere an exception can still escape, and it
    has to happen exactly once; serialisation happens later, on a payload that has already been
    inspected. Enforcing in both places would count every hit twice and make the production
    alarm read double, which is how an alarm stops being believed.

    Use `redact` unless you are downstream of an explicit `enforce`.
    """
    return _redact_mapping(payload)


def redact(
    payload: Mapping[str, Any], *, mode: TripwireMode = TripwireMode.RAISE
) -> dict[str, Any]:
    """Serialise `payload` into the fields that may be emitted, and nothing else.

    The tripwire runs first and over everything, so a secret filed under an unlisted key still
    trips it — the allow-list would have dropped the value silently, and silence is how a
    leaking call site survives to the next release.
    """
    enforce(scan_payload(payload), mode)
    return sanitise(payload)
