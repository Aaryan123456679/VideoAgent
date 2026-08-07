"""`S0.4.2` — who the caller is, and everything the answer must not reveal.

Three properties are load-bearing and each is asserted against a mutation that would break it:

- **Every rejection is byte-identical apart from the trace id.** Asserted by comparing whole
  bodies across five different reasons for failing, so a helpful "key revoked" added later
  fails here rather than becoming an enumeration oracle in production.
- **A cross-tenant read is a `404`, not a `403`.** Asserted on the status *and* on the log
  carrying `VA-AUTH-002`, because dropping either half — telling the client too much, or
  telling ourselves too little — is a distinct regression.
- **The credential never reaches a log line.** Asserted against the serialised output of the
  real handler, not against a mock, since it is the serialiser that would leak it.
"""

from __future__ import annotations

import json
from typing import Final

import pytest

from tests.unit.test_api_support import (
    JOB_ID,
    KEY_ID_A,
    OK,
    TENANT_A,
    VALID_KEY,
    RecordingProbe,
    StaticVerifier,
    api_client,
    authorised,
    build_app,
    build_resources,
)
from tests.unit.test_app_shell import captured_logs
from video_agent.api.errors import (
    HTTP_NOT_FOUND,
    HTTP_UNAUTHORIZED,
    ErrorEnvelope,
    message_for,
)
from video_agent.api.principal import (
    KEY_PREFIX_LENGTH,
    MIN_KEY_LENGTH,
    Principal,
    UnconfiguredApiKeyVerifier,
    parse_bearer,
)
from video_agent.observability.codes import ErrorCode

REJECTED_AUTHORIZATIONS: Final[tuple[str | None, ...]] = (
    None,
    "",
    f"Basic {VALID_KEY}",
    "Bearer short",
    f"Bearer {'x' * MIN_KEY_LENGTH}",
    f"Bearer {VALID_KEY}x",
)
REJECTED_IDS: Final[tuple[str, ...]] = (
    "absent",
    "empty",
    "wrong-scheme",
    "too-short",
    "unknown-key",
    "wrong-secret",
)
"""Six ways to fail authentication. `api.md` §6 requires all six to be indistinguishable —
an unknown key and a revoked one must not differ, and neither may differ from a missing one."""


def _headers(authorization: str | None) -> dict[str, str]:
    return {} if authorization is None else {"Authorization": authorization}


# --- Parsing ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    ["", "Basic abcdefghijklmnopqrstuvwxyz", "Bearer", "Bearer tooshort", "Token abcdefghijkl"],
    ids=["empty", "basic", "no-credential", "short", "other-scheme"],
)
def test_parse_bearer_rejects_anything_that_is_not_a_bearer_key(authorization: str) -> None:
    """Nothing but a long-enough `Bearer` credential produces a `PresentedKey`."""
    assert parse_bearer(authorization) is None


def test_parse_bearer_splits_at_the_lookup_prefix() -> None:
    """The prefix is the non-secret lookup handle; the remainder is what gets verified."""
    presented = parse_bearer(f"Bearer {VALID_KEY}")

    assert presented is not None
    assert presented.prefix == VALID_KEY[:KEY_PREFIX_LENGTH]
    assert presented.prefix + presented.secret == VALID_KEY


def test_presented_key_repr_hides_the_secret() -> None:
    """A credential must have no representation that prints it.

    `repr` is how a value reaches a log line without anyone deciding to log it: an exception's
    argument list, an f-string in a debug statement, a pytest assertion diff.
    """
    presented = parse_bearer(f"Bearer {VALID_KEY}")

    assert presented is not None
    assert presented.secret not in repr(presented)
    assert "<redacted>" in repr(presented)


def test_the_bearer_scheme_is_case_insensitive() -> None:
    """RFC 7235 says the scheme is case-insensitive; rejecting `bearer` would be our bug."""
    assert parse_bearer(f"bearer {VALID_KEY}") is not None
    assert parse_bearer(f"BEARER {VALID_KEY}") is not None


# --- Rejection -------------------------------------------------------------------------------


@pytest.mark.parametrize("authorization", REJECTED_AUTHORIZATIONS, ids=REJECTED_IDS)
@pytest.mark.asyncio
async def test_unauthenticated_is_401(authorization: str | None) -> None:
    """Every failure to authenticate is `401 VA-AUTH-001`, never `422` and never `403`."""
    app = build_app(verifier=StaticVerifier())

    async with api_client(app) as client:
        response = await client.get("/probe/whoami", headers=_headers(authorization))

    assert response.status_code == HTTP_UNAUTHORIZED
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.VA_AUTH_001.value


@pytest.mark.asyncio
async def test_every_rejection_is_indistinguishable() -> None:
    """An unknown key, a revoked-shaped key and a missing header produce the same body.

    The trace id is the only field allowed to differ. If it were not excluded the assertion
    would be vacuous; if anything else were excluded the assertion would be a lie.
    """
    app = build_app(verifier=StaticVerifier())
    bodies: list[str] = []

    async with api_client(app) as client:
        for authorization in REJECTED_AUTHORIZATIONS:
            response = await client.get("/probe/whoami", headers=_headers(authorization))
            payload = response.json()
            payload["error"].pop("trace_id")
            bodies.append(json.dumps(payload, sort_keys=True))

    assert len(set(bodies)) == 1, "authentication failures must not be distinguishable"


@pytest.mark.asyncio
async def test_the_default_verifier_accepts_nothing() -> None:
    """An application built without a credential store rejects even a well-formed key.

    The alternative — a hard-coded development key — is the shortcut that reaches production.
    """
    app = build_app(verifier=UnconfiguredApiKeyVerifier())

    async with api_client(app) as client:
        response = await client.get("/probe/whoami", headers=authorised())

    assert response.status_code == HTTP_UNAUTHORIZED


# --- Resolution ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_credential_resolves_to_its_principal() -> None:
    """The positive case, without which every rejection test above passes vacuously."""
    app = build_app(verifier=StaticVerifier())

    async with api_client(app) as client:
        response = await client.get("/probe/whoami", headers=authorised())

    assert response.status_code == OK
    assert response.json() == {"tenant_id": str(TENANT_A), "key_id": str(KEY_ID_A)}


@pytest.mark.asyncio
async def test_the_tenant_is_bound_onto_the_trace() -> None:
    """Log lines emitted while serving an authenticated request carry `tenant_id`.

    Without the binding the line is unattributable, and `observability.md` §4's whole point is
    that a line can be joined to the job and tenant it belongs to.
    """
    app = build_app(verifier=StaticVerifier())

    with captured_logs() as lines:
        async with api_client(app) as client:
            await client.get("/probe/cross-tenant", headers=authorised())

    denials = [line for line in lines if line.get("code") == ErrorCode.VA_AUTH_002.value]
    assert denials
    assert all(line["tenant_id"] == str(TENANT_A) for line in denials)


@pytest.mark.asyncio
async def test_the_api_key_never_reaches_a_log_line() -> None:
    """Not the plaintext, not the remainder, on **any** path `[D-52]`.

    Every rejection path is walked, not just the accepted one and one near-miss. The paths
    differ in where they give up — before parsing, after parsing, after verification — and the
    one that gives up *before* parsing is the one holding the raw header value, which is
    precisely the one that would log it.
    """
    app = build_app(verifier=StaticVerifier())

    with captured_logs() as lines:
        async with api_client(app) as client:
            await client.get("/probe/whoami", headers=authorised())
            for authorization in REJECTED_AUTHORIZATIONS:
                await client.get("/probe/whoami", headers=_headers(authorization))

    serialised = json.dumps(lines)
    assert VALID_KEY not in serialised
    assert VALID_KEY[KEY_PREFIX_LENGTH:] not in serialised


# --- Cross-tenant ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_is_404_not_403() -> None:
    """The client is told the job does not exist; the log records that a tenant probed."""
    app = build_app(verifier=StaticVerifier())

    with captured_logs() as lines:
        async with api_client(app) as client:
            response = await client.get("/probe/cross-tenant", headers=authorised())

    assert response.status_code == HTTP_NOT_FOUND
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.VA_REQ_005.value
    assert envelope.error.job_id == JOB_ID

    denials = [line for line in lines if line.get("code") == ErrorCode.VA_AUTH_002.value]
    assert denials, "a cross-tenant attempt must be logged as VA-AUTH-002"
    assert denials[0]["job_id"] == str(JOB_ID)


@pytest.mark.asyncio
async def test_a_cross_tenant_404_is_indistinguishable_from_a_genuine_one() -> None:
    """Same status, same code, same message — otherwise the pair is an existence oracle."""
    app = build_app(verifier=StaticVerifier())

    async with api_client(app) as client:
        cross = await client.get("/probe/cross-tenant", headers=authorised())
        missing = await client.get("/probe/error/VA-REQ-005")

    assert cross.status_code == missing.status_code
    assert cross.json()["error"]["code"] == missing.json()["error"]["code"]
    assert cross.json()["error"]["message"] == missing.json()["error"]["message"]


def test_the_public_404_message_does_not_mention_tenancy() -> None:
    """The taxonomy's own sentence carries a parenthetical written for operators.

    `VA-REQ-005` means "Job not found (also returned cross-tenant)". Rendering that verbatim
    hands the caller the very hint the `404` exists to withhold. Both halves are asserted, so
    the test fails if the override is dropped *and* if the taxonomy is quietly reworded to make
    the override unnecessary without anyone noticing.
    """
    assert "cross-tenant" in ErrorCode.VA_REQ_005.meaning
    assert "cross-tenant" not in message_for(ErrorCode.VA_REQ_005)


def test_a_principal_cannot_be_built_from_extra_fields() -> None:
    """`Principal` forbids extras, so a verifier cannot smuggle a scope nobody checks."""
    with pytest.raises(ValueError, match="extra_forbidden"):
        Principal.model_validate(
            {"tenant_id": str(TENANT_A), "key_id": str(KEY_ID_A), "is_admin": True}
        )


@pytest.mark.asyncio
async def test_authentication_does_not_depend_on_a_reachable_database() -> None:
    """A `401` must not require Postgres: an outage would turn every request into a `500`."""
    resources = build_resources(cache=RecordingProbe(ping_error=ConnectionRefusedError()))
    app = build_app(resources=resources, verifier=StaticVerifier())

    async with api_client(app) as client:
        response = await client.get("/probe/whoami", headers=_headers(None))

    assert response.status_code == HTTP_UNAUTHORIZED
