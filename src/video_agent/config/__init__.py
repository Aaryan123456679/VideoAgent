"""config — typed settings and the alias table.

The only location in the tree where a concrete model or provider name may appear
(AGENT.md S2). Bound to the ``.env.example`` contract.
"""

from video_agent.config.aliases import (
    Alias,
    AliasEntry,
    AliasTable,
    CanaryRef,
    ModelPrice,
    ModelRef,
    get_alias_table,
    load_alias_table,
)
from video_agent.config.errors import (
    AliasConfigError,
    ConfigError,
    MissingCredentialError,
)
from video_agent.config.settings import Settings, get_settings

__all__ = [
    "Alias",
    "AliasConfigError",
    "AliasEntry",
    "AliasTable",
    "CanaryRef",
    "ConfigError",
    "MissingCredentialError",
    "ModelPrice",
    "ModelRef",
    "Settings",
    "get_alias_table",
    "get_settings",
    "load_alias_table",
]
