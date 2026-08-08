"""Capability checking: a resolved model that cannot do the job is refused, not used.

`gateway.md` §3 rule 3: *if a resolved model lacks a `required_capability`, resolution fails
closed with `VA-GW-002` rather than silently degrading quality.* The failure this prevents is
specific and quiet — a `vision-default` call routed to a member that cannot accept an image
does not error, it returns a confident score about a frame it never saw, and the QC gate then
acts on that number.

**Where the capability facts come from is the interesting part.** `config/aliases.yaml`
declares `required_capabilities` per alias *group*; it declares nothing per model, and the
loader forbids extra keys, so there is no per-model capability column to read. Nor should there
be a hard-coded one here: a table in this module mapping model names to what they support would
be a table of model names in application code, which is the one thing `[AGENT.md §2]` forbids.

So capabilities are **discovered from the proxy**, which is the component that actually knows
what it is serving, and cached for the process. That keeps the rule enforceable with zero model
names in code and makes it self-correcting when a proxy is reconfigured.

**Unknown means unsatisfied.** A model the registry cannot describe does not pass a required
capability. This is a deliberate fail-closed, and it has a real consequence worth stating
plainly: with no reachable proxy and no static registry configured, every alias that declares a
required capability refuses to resolve. That is the correct behaviour — the alternative is
asserting a capability nobody verified — but it means a deployment must either reach its proxy
or pin a `StaticCapabilityRegistry`, and it means the capability path cannot be verified
against a real proxy without one running.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol

from video_agent.gateway.transport import UpstreamNetworkError, UpstreamStatusError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from video_agent.gateway.transport import LLMTransport

__all__ = [
    "Capability",
    "CapabilityRegistry",
    "ProxyCapabilityRegistry",
    "StaticCapabilityRegistry",
    "missing_capabilities",
]


class Capability(StrEnum):
    """The capability vocabulary `config/aliases.yaml` uses, as a closed set.

    The values are exactly the strings in the table's `required_capabilities` lists. A typo in
    the table therefore fails to parse as a member and is reported as an unknown requirement,
    rather than silently matching nothing and passing.
    """

    STRUCTURED_OUTPUT = "structured_output"
    IMAGE_INPUT = "image_input"
    AUDIO_INPUT = "audio_input"
    AUDIO_OUTPUT = "audio_output"
    EMBEDDINGS = "embeddings"


_PROXY_FLAG_BY_CAPABILITY: Final[Mapping[Capability, tuple[str, ...]]] = {
    Capability.STRUCTURED_OUTPUT: ("supports_response_schema", "supports_function_calling"),
    Capability.IMAGE_INPUT: ("supports_vision",),
    Capability.AUDIO_INPUT: ("supports_audio_input",),
    Capability.AUDIO_OUTPUT: ("supports_audio_output",),
    Capability.EMBEDDINGS: ("supports_embedding_image_input", "mode_is_embedding"),
}
"""How the proxy's own metadata flags map onto this vocabulary.

Any one flag being true is enough, because the proxy publishes overlapping flags for the same
underlying ability and requiring all of them would refuse models that can do the job. These are
the proxy's field names, not a vendor's: nothing here identifies who is behind the model.
"""

EMBEDDING_MODE: Final = "embedding"


class CapabilityRegistry(Protocol):
    """What a concrete model can do. Async because the honest answer comes from the proxy."""

    async def capabilities(self, model: str, /) -> frozenset[Capability]: ...


class StaticCapabilityRegistry:
    """A pinned mapping, for tests and for a deployment that prefers to declare rather than ask.

    Returns an empty set for a model it has never heard of, which — given "unknown means
    unsatisfied" — makes an omission a refusal rather than a pass.
    """

    def __init__(self, table: Mapping[str, frozenset[Capability]]) -> None:
        self._table = dict(table)

    async def capabilities(self, model: str, /) -> frozenset[Capability]:
        """Everything `model` is declared to support."""
        return self._table.get(model, frozenset())


def _flag(entry: Mapping[str, Any], name: str) -> bool:
    info = entry.get("model_info")
    if name == "mode_is_embedding":
        return isinstance(info, dict) and info.get("mode") == EMBEDDING_MODE
    if isinstance(info, dict) and name in info:
        return bool(info[name])
    return bool(entry.get(name, False))


def parse_model_info(entries: object) -> dict[str, frozenset[Capability]]:
    """Turn the proxy's `/model/info` payload into this module's vocabulary.

    Tolerant of shape, because the payload is a deployment's proxy build talking and a missing
    field must read as "this model does not declare that", never as an exception in the middle
    of resolution.
    """
    table: dict[str, frozenset[Capability]] = {}
    if not isinstance(entries, list):
        return table
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("model_name") or entry.get("model")
        if not isinstance(name, str) or not name:
            continue
        supported = {
            capability
            for capability, flags in _PROXY_FLAG_BY_CAPABILITY.items()
            if any(_flag(entry, flag) for flag in flags)
        }
        table[name] = frozenset(supported)
    return table


class ProxyCapabilityRegistry:
    """Capabilities discovered from the proxy once, then cached for the process.

    Cached rather than fetched per call because the answer changes only when the proxy is
    reconfigured, and a network round trip inside alias resolution would put the gateway's
    fail-closed path behind the very dependency it is guarding.

    A failed fetch caches nothing and is retried on the next call. It does **not** cache an
    empty table: doing so would convert one transient proxy blip into a process that refuses
    every capability-bearing alias until it is restarted.
    """

    def __init__(self, transport: LLMTransport) -> None:
        self._transport = transport
        self._table: dict[str, frozenset[Capability]] | None = None
        self._lock = asyncio.Lock()

    async def capabilities(self, model: str, /) -> frozenset[Capability]:
        """Everything the proxy says `model` supports; empty if it cannot be asked."""
        table = await self._ensure_table()
        return table.get(model, frozenset())

    async def _ensure_table(self) -> dict[str, frozenset[Capability]]:
        async with self._lock:
            if self._table is not None:
                return self._table
            try:
                entries = await self._transport.model_info()
            except (UpstreamNetworkError, UpstreamStatusError):
                return {}
            self._table = parse_model_info(list(entries))
            return self._table


def missing_capabilities(declared: tuple[str, ...], supported: frozenset[Capability]) -> list[str]:
    """Which declared requirements `supported` does not meet, in a stable order.

    A requirement string that is not a `Capability` member is reported as missing rather than
    ignored. A misspelled requirement in the table is a requirement nobody is checking, and
    treating it as satisfied would make the table's strictest-looking entries the weakest.
    """
    have = {capability.value for capability in supported}
    return sorted(requirement for requirement in set(declared) if requirement not in have)
