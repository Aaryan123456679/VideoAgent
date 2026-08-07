"""`S0.3.1` — every error carries a stable code and the trace it was raised in.

`[CPS §Failure behaviour]` makes two promises about an error response and this file asserts
both at the point they can still be kept. The `trace_id` in particular is captured in
`__init__` rather than read when the envelope is rendered, and the test that matters most here
is the one that raises inside a traced block, leaves it, and *then* reads the id: at render
time the contextvar is long gone, and an envelope that pointed support at the wrong trace would
be worse than one that pointed them nowhere.

This file also pins the reconciliation `T0.3` owed `T0.2`. `config/errors.py` shipped with
`VA-GW-002` as a bare string and `MissingCredentialError` deliberately code-less, because the
registry did not exist yet. Both now resolve to enum members.
"""

from __future__ import annotations

import pytest

from video_agent.config.errors import (
    VA_GW_002,
    AliasConfigError,
    ConfigError,
    MissingCredentialError,
)
from video_agent.observability.codes import ErrorCode
from video_agent.observability.context import bind_trace, clear_trace
from video_agent.observability.errors import VideoAgentError
from video_agent.observability.registry import load_registry

CONFIG_ERROR_TYPES: tuple[type[ConfigError], ...] = (
    ConfigError,
    AliasConfigError,
    MissingCredentialError,
)


# --- The base type -------------------------------------------------------------------------


def test_an_unclassified_failure_is_an_internal_error() -> None:
    """Defaulting to a plausible domain code would file bugs under the wrong heading."""
    assert VideoAgentError("something broke").code is ErrorCode.VA_INT_001


def test_the_code_prefixes_the_message() -> None:
    """Support pastes what they were shown; what they were shown must contain the code."""
    error = VideoAgentError("the table is unreadable", code=ErrorCode.VA_GW_002)

    assert str(error).startswith("VA-GW-002: ")
    assert "the table is unreadable" in str(error)


def test_the_message_is_available_without_the_prefix() -> None:
    error = VideoAgentError("the table is unreadable", code=ErrorCode.VA_GW_002)

    assert error.message == "the table is unreadable"


def test_retryability_comes_from_the_code() -> None:
    """`[D-62]` — never set per raise site, or one call site relitigates the taxonomy."""
    assert VideoAgentError("nope", code=ErrorCode.VA_PROV_009).retryable is False
    assert VideoAgentError("later", code=ErrorCode.VA_PROV_001).retryable is True


def test_a_subclass_declares_its_code_once() -> None:
    class BibleMutationError(VideoAgentError):
        code = ErrorCode.VA_BIBLE_002

    assert BibleMutationError("hash mismatch").code is ErrorCode.VA_BIBLE_002


# --- The trace id ---------------------------------------------------------------------------


def test_the_trace_id_is_captured_from_the_context() -> None:
    with bind_trace("trace-abc"):
        error = VideoAgentError("failed here")

    assert error.trace_id == "trace-abc"


def test_the_trace_id_is_captured_at_construction_not_at_render() -> None:
    """The test that justifies the design: the context is gone by the time this is read."""
    with bind_trace("trace-inner"):
        error = VideoAgentError("failed inside the node")

    with bind_trace("trace-somewhere-else"):
        rendered = error.trace_id

    assert rendered == "trace-inner"


def test_an_error_raised_outside_a_trace_has_no_trace_id() -> None:
    """Honest absence. Inventing an id here would fabricate a trace that never existed."""
    with clear_trace():
        error = VideoAgentError("orphaned")

    assert error.trace_id is None


def test_a_raised_error_keeps_the_trace_of_its_raise_site() -> None:
    with pytest.raises(VideoAgentError) as raised, bind_trace("trace-raise"):
        raise VideoAgentError("inside")

    assert raised.value.trace_id == "trace-raise"


# --- Reconciliation with the configuration errors T0.2 landed --------------------------------


def test_the_alias_code_constant_is_now_the_enum_member() -> None:
    """`config/errors.py` promised these constants would become aliases, not a second source."""
    assert VA_GW_002 is ErrorCode.VA_GW_002
    assert str(VA_GW_002) == "VA-GW-002"


def test_alias_failures_carry_the_gateway_code() -> None:
    """`gateway.md` §8 — alias not in config, non-retryable, fail closed."""
    error = AliasConfigError("vision-default is absent")

    assert error.code is ErrorCode.VA_GW_002
    assert error.retryable is False
    assert "VA-GW-002" in str(error)


def test_a_missing_credential_now_has_a_code() -> None:
    """The gap `T0.2` recorded and left for the registry to close.

    `VA-INT-001` and not a new number: `observability.md` §6 defines no code for a locally
    absent credential, and issuing one the canonical table has never seen would corrupt the
    append-only register. An unset environment variable is a deployment fault, non-retryable,
    and its documented outcome — *500, generic message* — is exactly right, because the
    response must not disclose which variable is unset.
    """
    error = MissingCredentialError("MAGICHOUR_API_KEY is empty")

    assert error.code is ErrorCode.VA_INT_001
    assert error.retryable is False


def test_a_missing_credential_names_the_variable_in_the_message() -> None:
    """Not in the API response — in the message the operator reads, which is not attacker-facing."""
    error = MissingCredentialError("MAGICHOUR_API_KEY is empty; set it in .env.")

    assert "MAGICHOUR_API_KEY" in str(error)
    assert str(error).startswith("VA-INT-001: ")


@pytest.mark.parametrize("error_type", CONFIG_ERROR_TYPES)
def test_configuration_errors_are_still_runtime_errors(error_type: type[ConfigError]) -> None:
    """The startup preflight catches `RuntimeError`; changing the base would silence it."""
    error = error_type("boom")

    assert isinstance(error, RuntimeError)
    assert isinstance(error, VideoAgentError)


@pytest.mark.parametrize("error_type", CONFIG_ERROR_TYPES)
def test_every_declared_code_is_in_the_register(error_type: type[ConfigError]) -> None:
    """An exception carrying an unregistered code would be undocumented by construction."""
    assert error_type.code.value in load_registry()
