"""Configuration failures, each carrying the stable error code the taxonomy assigns it.

`T0.3` introduces the single `ErrorCode` enum that owns the whole taxonomy. Until it lands
the two codes this module needs are string constants here, and this is the only place in the
tree where they are spelled. When `ErrorCode` arrives these constants become aliases of its
members, not a second source of truth.

Every error here is raised at **startup or at point of use**, never swallowed. A
configuration file that will not load resolves nothing, so failing closed is the only
correct behaviour: guessing a model or pricing one at zero is worse than not starting.
"""

from __future__ import annotations

VA_GW_002 = "VA-GW-002"
"""Alias resolution failed closed — the alias table is absent, incomplete or malformed.

`gateway.md` §8 assigns this code to "alias not in config, non-retryable, fail closed. Never
guess a model." A table that fails validation is the same condition reached earlier: no alias
in it can be resolved, so the whole table is treated as absent.
"""


class ConfigError(RuntimeError):
    """A configuration value is missing, malformed or unusable.

    A `RuntimeError` subclass so the startup preflight catches it alongside the media
    toolchain assertion and reports a sentence rather than a traceback.
    """

    code: str | None = None

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}" if self.code else message)


class AliasConfigError(ConfigError):
    """`config/aliases.yaml` is missing, incomplete, malformed or references an unpriced model."""

    code = VA_GW_002


class MissingCredentialError(ConfigError):
    """A credential is declared in the contract but empty, and something needs it now.

    Raised at the point of use, never at import. The application must start without every
    optional upstream credential; only the code path that actually calls the upstream may
    demand one, and it must say which variable is empty.

    Deliberately has no taxonomy code yet: `observability.md` §6 does not define one for a
    missing credential, and inventing a code that the historical registry in `T0.3` has never
    issued would corrupt the registry. `T0.3` assigns it.
    """
