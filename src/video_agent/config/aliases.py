"""The alias table and the per-model price table: loader, schema and startup validation.

`config/aliases.yaml` is where every concrete **LLM** model name lives, and the only file that
may name more than one `[CPS §Model routing]`, `[AGENT.md §2]`. Application code names a logical
alias; the gateway resolves it at call time; swapping a model is therefore a config change with
zero code diff. `tests/static_guards.py` is what makes that a property of the repository rather
than a convention — this module is what makes the config side of it typed.

One name lives elsewhere, and the claim that this file is the *only* one was simply wrong: the
video model is a single typed default on the settings object with an environment override, not
a routed alias, because there is one video provider and no failover group to route within. The
static guard's allow-list is exactly two files for exactly that reason, and it is pinned so a
third cannot appear quietly.

Everything here fails **closed**. `gateway.md` §8: "Alias not in config → `VA-GW-002`,
non-retryable, fail closed. Never guess a model." The same reasoning covers a table that will
not parse, an alias group that is missing, and a model an alias references but the price table
does not price: each leaves some alias unresolvable or unpriceable, and continuing would mean
either guessing a model or charging zero for one. `[D-21]` An unpriced model must never look
free to a budget cap, so a *referenced* model missing a price is a startup failure, while a
model seen only at runtime (a provider silently renaming one, say) prices at the configured
pessimistic ceiling.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from video_agent.config.errors import AliasConfigError
from video_agent.observability.logging import get_logger

_LOGGER = get_logger(__name__)

ALIAS_FILE_RELATIVE_PATH = Path("config") / "aliases.yaml"
"""Where the table lives, relative to a repository root or to an installed package root."""

SUPPORTED_SCHEMA_VERSION = 1


class Alias(StrEnum):
    """The complete logical alias set, fixed by `[CPS §Model routing]`.

    Adding a member is a specification change, not a configuration change. `REALTIME_VOICE`
    and `EMBED_DEFAULT` have no consumer in v1 `[D-13]` — they are declared because the set is
    fixed, and must validate without one rather than being quietly dropped.
    """

    REASONING_HIGH = "reasoning-high"
    REASONING_FAST = "reasoning-fast"
    REALTIME_VOICE = "realtime-voice"
    EMBED_DEFAULT = "embed-default"
    VISION_DEFAULT = "vision-default"


class ModelRef(BaseModel):
    """One concrete model inside an alias group."""

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    model: str = Field(min_length=1)
    weight: int = Field(default=100, ge=0, le=100)


class CanaryRef(ModelRef):
    """A canary model and the share of traffic it receives.

    `[CPS §Rollout]` puts model and prompt changes in front of 10% of traffic first.
    Assignment is deterministic per `job_id` so one job never mixes models across its shots,
    which would itself be a continuity hazard `[D-20]`.
    """

    traffic_pct: int = Field(ge=0, le=100)


class AliasEntry(BaseModel):
    """One alias group: a failover unit with exactly one primary.

    Fallback is to an alternate model *within the group* and never across groups
    `[CPS §Failure behaviour]` — a `vision-default` failure that fell back to `reasoning-high`
    would silently answer a vision question with a model that cannot see the image.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: ModelRef
    fallbacks: tuple[ModelRef, ...] = ()
    canary: CanaryRef | None = None
    required_capabilities: tuple[str, ...] = ()

    @property
    def models(self) -> tuple[str, ...]:
        """Every concrete model this group may resolve to, primary first."""
        members = [self.primary, *self.fallbacks]
        if self.canary is not None:
            members.append(self.canary)
        return tuple(member.model for member in members)

    @model_validator(mode="after")
    def _reject_duplicate_models(self) -> AliasEntry:
        names = self.models
        if len(set(names)) != len(names):
            message = f"alias group lists the same model more than once: {names}"
            raise ValueError(message)
        return self


class ModelPrice(BaseModel):
    """USD per 1,000 tokens for one model, in each direction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_usd_per_1k_tokens: Decimal = Field(ge=0)
    output_usd_per_1k_tokens: Decimal = Field(ge=0)


class _AliasDocument(BaseModel):
    """The on-disk shape of `config/aliases.yaml`. Private: the loader returns `AliasTable`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    aliases: dict[Alias, AliasEntry]
    prices: dict[str, ModelPrice]
    unpriced_ceiling: ModelPrice


@dataclass(frozen=True, slots=True)
class AliasTable:
    """The loaded, validated, immutable alias and price table.

    Immutable in substance, not only by convention: the dataclass is frozen and both mappings
    are `MappingProxyType`, so there is no setter and no reachable mutation. A table that
    could be edited at runtime would make a Langfuse trace a record of what the config used to
    say.
    """

    aliases: Mapping[Alias, AliasEntry]
    prices: Mapping[str, ModelPrice]
    unpriced_ceiling: ModelPrice

    def resolve(self, alias: Alias) -> AliasEntry:
        """Return the group for `alias`, failing closed if it is absent."""
        entry = self.aliases.get(alias)
        if entry is None:
            message = f"alias {alias.value!r} is not present in {ALIAS_FILE_RELATIVE_PATH}"
            raise AliasConfigError(message)
        return entry

    def price_for(self, model: str) -> ModelPrice:
        """Return the price for `model`, or the pessimistic ceiling if it is unknown.

        Never returns zero for an unknown model `[D-21]`: a model that looks free to a budget
        cap is a cap that does not hold. Every model an alias *references* is priced at load
        time, so reaching the ceiling means a model appeared that the config has never seen —
        which the caller is expected to alarm on.
        """
        return self.prices.get(model, self.unpriced_ceiling)

    def is_priced(self, model: str) -> bool:
        """Whether `model` has an explicit price rather than falling back to the ceiling."""
        return model in self.prices

    def referenced_models(self) -> frozenset[str]:
        """Every concrete model reachable through any alias group."""
        return frozenset(model for entry in self.aliases.values() for model in entry.models)


def alias_search_roots(working_dir: Path, module_dir: Path) -> tuple[Path, ...]:
    """The directories to walk up from, in precedence order: **deployment before build**.

    The file has two homes — the repository checkout during development, and the package
    directory once the wheel force-includes it — and which one wins is not a detail. This table
    exists so that swapping a model is a config change with zero code diff `[CPS §Model
    routing]`; an operator mounts an edited table over `config/aliases.yaml` and restarts. With
    the module-relative walk running first, the copy baked into the image at build time shadowed
    the mounted one permanently, and it did so in silence: the process started, resolved every
    alias, and used the build-time model. The precedence has to run the other way for the
    mechanism to mean anything.

    Walking *up* from each root rather than checking it directly, so a process started from a
    subdirectory of the checkout still finds the table.
    """
    return (working_dir.resolve(), module_dir.resolve())


def find_alias_file(roots: Sequence[Path]) -> Path | None:
    """The first `config/aliases.yaml` at or above any of `roots`, in order. `None` if absent."""
    for start in roots:
        for directory in (start, *start.parents):
            candidate = directory / ALIAS_FILE_RELATIVE_PATH
            if candidate.is_file():
                return candidate
    return None


def _default_alias_path() -> Path:
    roots = alias_search_roots(Path.cwd(), Path(__file__).parent)
    found = find_alias_file(roots)
    if found is None:
        searched = ", ".join(str(root) for root in roots)
        message = f"{ALIAS_FILE_RELATIVE_PATH} was not found at or above any of: {searched}"
        raise AliasConfigError(message)
    return found


def _parse_document(path: Path) -> _AliasDocument:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        message = f"{path} could not be read as YAML: {exc}"
        raise AliasConfigError(message) from exc

    if not isinstance(raw, dict):
        message = f"{path} must contain a mapping at the top level, got {type(raw).__name__}"
        raise AliasConfigError(message)

    try:
        return _AliasDocument.model_validate(raw)
    except ValidationError as exc:
        message = f"{path} is not a valid alias table: {exc}"
        raise AliasConfigError(message) from exc


def _validate_document(document: _AliasDocument, path: Path) -> None:
    if document.version != SUPPORTED_SCHEMA_VERSION:
        message = (
            f"{path} declares schema version {document.version}, "
            f"this build understands {SUPPORTED_SCHEMA_VERSION}"
        )
        raise AliasConfigError(message)

    missing = sorted(alias.value for alias in Alias if alias not in document.aliases)
    if missing:
        message = (
            f"{path} is missing required alias(es): {', '.join(missing)}. "
            "Refusing to start rather than resolving them at call time — an absent alias "
            "would be discovered mid-job, after the job had already been paid for."
        )
        raise AliasConfigError(message)

    unpriced = sorted(
        model
        for entry in document.aliases.values()
        for model in entry.models
        if model not in document.prices
    )
    if unpriced:
        message = (
            f"{path} references model(s) with no price entry: {', '.join(unpriced)}. "
            "An unpriced model would be charged at zero and would make the USD budget cap "
            "unenforceable for every job that routed to it."
        )
        raise AliasConfigError(message)


def load_alias_table(path: Path | None = None) -> AliasTable:
    """Parse, validate and freeze the alias table at `path` (default: `config/aliases.yaml`).

    Raises `AliasConfigError` (`VA-GW-002`) on anything that would leave an alias
    unresolvable or a referenced model unpriced.

    Logs which file was used, at `info`. Two candidates exist by design and only one wins, so
    "which table is this process actually running on" is a question an operator will ask — and
    a restart that appeared to change nothing is the worst moment to have no answer. The path
    goes in the message rather than in a new allow-listed field: `AGENT.md` §3 asks that the
    allow-list not grow, and a message is scanned and truncated exactly as a field would be.
    """
    resolved = _default_alias_path() if path is None else path
    document = _parse_document(resolved)
    _validate_document(document, resolved)
    _LOGGER.info("alias table loaded from %s", resolved, extra={"event": "alias_table_loaded"})
    return AliasTable(
        aliases=MappingProxyType(dict(document.aliases)),
        prices=MappingProxyType(dict(document.prices)),
        unpriced_ceiling=document.unpriced_ceiling,
    )


@lru_cache(maxsize=1)
def get_alias_table() -> AliasTable:
    """The process-wide alias table, loaded once at startup."""
    return load_alias_table()
