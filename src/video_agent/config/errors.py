"""Configuration failures, each carrying the stable error code the taxonomy assigns it.

The codes now come from `observability.ErrorCode`, which `T0.3` established as the single
source of the taxonomy. The string constants this module used to define are kept as aliases of
enum members so existing imports keep working, exactly as their own docstring promised — they
are a spelling of the enum, never a second source of truth.

Every error here is raised at **startup or at point of use**, never swallowed. A
configuration file that will not load resolves nothing, so failing closed is the only
correct behaviour: guessing a model or pricing one at zero is worse than not starting.
"""

from __future__ import annotations

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError

VA_GW_002 = ErrorCode.VA_GW_002
"""Alias resolution failed closed — the alias table is absent, incomplete or malformed.

`gateway.md` §8 assigns this code to "alias not in config, non-retryable, fail closed. Never
guess a model." A table that fails validation is the same condition reached earlier: no alias
in it can be resolved, so the whole table is treated as absent.
"""


class ConfigError(VideoAgentError, RuntimeError):
    """A configuration value is missing, malformed or unusable.

    Still a `RuntimeError` subclass so the startup preflight catches it alongside the media
    toolchain assertion and reports a sentence rather than a traceback. Now also a
    `VideoAgentError`, so it arrives at that preflight already carrying its code and the trace
    it was raised in, rather than being classified by whoever catches it.
    """

    code = ErrorCode.VA_INT_001


class AliasConfigError(ConfigError):
    """`config/aliases.yaml` is missing, incomplete, malformed or references an unpriced model."""

    code = ErrorCode.VA_GW_002


class MissingCredentialError(ConfigError):
    """A credential is declared in the contract but empty, and something needs it now.

    Raised at the point of use, never at import. The application must start without every
    optional upstream credential; only the code path that actually calls the upstream may
    demand one, and it must say which variable is empty.

    Carries `VA-INT-001`. That is the honest classification and not a placeholder:
    `observability.md` §6 defines no code for a locally absent credential, and inventing one
    would put a number into the append-only register that the canonical taxonomy table has
    never issued — the exact corruption `[D-55]` and `registry.py` exist to prevent. Of the
    codes that *are* defined, `VA-INT-001` is the one that fits: an unset environment variable
    is a deployment fault rather than anything the caller did, it is not retryable, and its
    documented outcome — *500, generic message* — is precisely right, because the response
    must not disclose which variable is unset while the log line, which is not attacker-
    readable, names it exactly. `VA-PROV-008` was considered and rejected: that code means the
    provider rejected a credential we sent, which is a different fact, and this error is also
    raised for the gateway and the webhook secret.
    """

    code = ErrorCode.VA_INT_001
