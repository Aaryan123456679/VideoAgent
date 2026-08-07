"""S0.2.2 — the alias table and price table loader.

Fixture tables use invented model names (`vendor-a/model-1`) so the structural tests stay
true when the real table's models change. The one test that pins real names is the golden
price snapshot, which exists precisely to fail when they change.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from video_agent.config.aliases import (
    ALIAS_FILE_RELATIVE_PATH,
    Alias,
    AliasEntry,
    AliasTable,
    ModelPrice,
    get_alias_table,
    load_alias_table,
)
from video_agent.config.errors import VA_GW_002, AliasConfigError

PRICE = {"input_usd_per_1k_tokens": "0.001", "output_usd_per_1k_tokens": "0.002"}
CEILING = {"input_usd_per_1k_tokens": "0.05", "output_usd_per_1k_tokens": "0.15"}
CANARY_TRAFFIC_PCT = 10  # `[CPS §Rollout]` — model changes go to 10% of traffic first.

# The real table's prices, transcribed independently of config/aliases.yaml. A silent edit to
# either side fails this test, which is the whole point: every USD budget cap in the system
# is denominated in these numbers.
GOLDEN_PRICES: dict[str, tuple[str, str]] = {
    "gemini/gemini-2.5-pro": ("0.00125", "0.01000"),
    "gemini/gemini-2.5-flash": ("0.00030", "0.00250"),
    "gemini/gemini-2.5-flash-lite": ("0.00010", "0.00040"),
    "openai/gpt-4o": ("0.00250", "0.01000"),
    "openai/gpt-4o-mini": ("0.00015", "0.00060"),
    "openai/gpt-4o-realtime-preview": ("0.00500", "0.02000"),
    "openai/text-embedding-3-small": ("0.00002", "0.00000"),
    "anthropic/claude-sonnet-4-20250514": ("0.00300", "0.01500"),
}


def _document() -> dict[str, Any]:
    """A minimal table that validates: all five aliases, every referenced model priced."""
    return {
        "version": 1,
        "aliases": {
            "reasoning-high": {
                "primary": {"model": "vendor-a/model-1", "weight": 100},
                "fallbacks": [{"model": "vendor-b/model-2"}],
                "required_capabilities": ["structured_output"],
            },
            "reasoning-fast": {"primary": {"model": "vendor-a/model-3"}},
            "realtime-voice": {"primary": {"model": "vendor-b/model-4"}},
            "embed-default": {"primary": {"model": "vendor-b/model-5"}},
            "vision-default": {
                "primary": {"model": "vendor-a/model-6"},
                "required_capabilities": ["image_input"],
            },
        },
        "prices": {
            f"vendor-{vendor}/model-{index}": dict(PRICE)
            for vendor, index in (("a", 1), ("b", 2), ("a", 3), ("b", 4), ("b", 5), ("a", 6))
        },
        "unpriced_ceiling": dict(CEILING),
    }


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "aliases.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
    return path


@pytest.fixture
def real_table(repo_root: Path) -> AliasTable:
    return load_alias_table(repo_root / ALIAS_FILE_RELATIVE_PATH)


# --- The shipped table ---------------------------------------------------------------------


def test_the_shipped_table_loads(real_table: AliasTable) -> None:
    assert set(real_table.aliases) == set(Alias)


def test_every_referenced_model_in_the_shipped_table_is_priced(real_table: AliasTable) -> None:
    unpriced = sorted(
        model for model in real_table.referenced_models() if not real_table.is_priced(model)
    )
    assert unpriced == []


def test_price_table_golden(real_table: AliasTable) -> None:
    """A silent price edit fails CI rather than silently redefining every budget cap."""
    actual = {
        model: (str(price.input_usd_per_1k_tokens), str(price.output_usd_per_1k_tokens))
        for model, price in real_table.prices.items()
    }
    assert actual == GOLDEN_PRICES


def test_vision_default_cannot_waive_image_input(real_table: AliasTable) -> None:
    """A scorer that cannot see the frame returns a confident number about nothing."""
    assert "image_input" in real_table.resolve(Alias.VISION_DEFAULT).required_capabilities


def test_every_group_has_exactly_one_primary_listed_first(real_table: AliasTable) -> None:
    """`[CPS §Failure behaviour]` — a group is the failover unit and resolution starts at its
    primary. `fallbacks` is ordered, `primary` is singular by construction, and no model may
    appear twice in one group, which would make the failover order ambiguous."""
    for entry in real_table.aliases.values():
        assert entry.models[0] == entry.primary.model
        assert len(set(entry.models)) == len(entry.models)


def test_get_alias_table_is_loaded_once() -> None:
    get_alias_table.cache_clear()
    try:
        assert get_alias_table() is get_alias_table()
    finally:
        get_alias_table.cache_clear()


# --- Fail-closed validation ------------------------------------------------------------------


def test_missing_alias_fails_closed(tmp_path: Path) -> None:
    document = _document()
    del document["aliases"]["vision-default"]

    with pytest.raises(AliasConfigError) as raised:
        load_alias_table(_write(tmp_path, document))

    message = str(raised.value)
    assert VA_GW_002 in message
    assert "vision-default" in message


def test_unpriced_model_fails_startup(tmp_path: Path) -> None:
    """`[D-21]` — never priced at zero, and never discovered mid-job."""
    document = _document()
    document["aliases"]["reasoning-high"]["fallbacks"].append({"model": "vendor-c/unpriced"})

    with pytest.raises(AliasConfigError) as raised:
        load_alias_table(_write(tmp_path, document))

    assert "vendor-c/unpriced" in str(raised.value)


@pytest.mark.parametrize("traffic_pct", [101, -1])
def test_canary_pct_bounds(tmp_path: Path, traffic_pct: int) -> None:
    document = _document()
    document["aliases"]["reasoning-high"]["canary"] = {
        "model": "vendor-a/model-1-next",
        "traffic_pct": traffic_pct,
    }
    document["prices"]["vendor-a/model-1-next"] = dict(PRICE)

    with pytest.raises(AliasConfigError):
        load_alias_table(_write(tmp_path, document))


def test_canary_within_bounds_is_accepted(tmp_path: Path) -> None:
    document = _document()
    document["aliases"]["reasoning-high"]["canary"] = {
        "model": "vendor-a/model-1-next",
        "traffic_pct": CANARY_TRAFFIC_PCT,
    }
    document["prices"]["vendor-a/model-1-next"] = dict(PRICE)

    table = load_alias_table(_write(tmp_path, document))
    canary = table.resolve(Alias.REASONING_HIGH).canary
    assert canary is not None
    assert canary.traffic_pct == CANARY_TRAFFIC_PCT


def test_unconsumed_aliases_allowed(tmp_path: Path) -> None:
    """`[D-13]` — `realtime-voice` and `embed-default` have no consumer in v1 and still load."""
    table = load_alias_table(_write(tmp_path, _document()))

    assert table.resolve(Alias.REALTIME_VOICE).primary.model == "vendor-b/model-4"
    assert table.resolve(Alias.EMBED_DEFAULT).primary.model == "vendor-b/model-5"


def test_unknown_alias_key_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["aliases"]["reasoning-medium"] = {"primary": {"model": "vendor-a/model-1"}}

    with pytest.raises(AliasConfigError):
        load_alias_table(_write(tmp_path, document))


def test_duplicate_model_in_a_group_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["aliases"]["reasoning-high"]["fallbacks"].append({"model": "vendor-a/model-1"})

    with pytest.raises(AliasConfigError):
        load_alias_table(_write(tmp_path, document))


def test_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["version"] = 2

    with pytest.raises(AliasConfigError):
        load_alias_table(_write(tmp_path, document))


def test_malformed_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "aliases.yaml"
    path.write_text("aliases: [unclosed\n", encoding="utf-8")

    with pytest.raises(AliasConfigError):
        load_alias_table(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AliasConfigError):
        load_alias_table(tmp_path / "absent.yaml")


# --- Pricing and immutability -----------------------------------------------------------------


def test_unknown_model_prices_at_the_ceiling_not_zero(tmp_path: Path) -> None:
    """`[D-21]` — a model that looks free to a budget cap is a cap that does not hold."""
    table = load_alias_table(_write(tmp_path, _document()))
    price = table.price_for("vendor-z/never-seen")

    assert price == ModelPrice.model_validate(CEILING)
    assert price.input_usd_per_1k_tokens > Decimal(0)
    assert price.output_usd_per_1k_tokens > Decimal(0)


def test_the_ceiling_is_above_every_priced_model(real_table: AliasTable) -> None:
    ceiling = real_table.unpriced_ceiling
    for price in real_table.prices.values():
        assert ceiling.input_usd_per_1k_tokens >= price.input_usd_per_1k_tokens
        assert ceiling.output_usd_per_1k_tokens >= price.output_usd_per_1k_tokens


def test_the_table_has_no_setter(tmp_path: Path) -> None:
    """Immutable in substance: a frozen dataclass over `MappingProxyType`, not a convention.

    Both assignments are made dynamically so the *runtime* behaviour is what is asserted; a
    static type error would prove only that the type checker knows, not that the object
    refuses.
    """
    table = load_alias_table(_write(tmp_path, _document()))
    replacement = ModelPrice.model_validate(PRICE)
    field_name = "unpriced_ceiling"

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(table, field_name, replacement)

    mutable = cast("dict[str, ModelPrice]", table.prices)
    with pytest.raises(TypeError):
        mutable["vendor-a/model-1"] = replacement


def test_alias_entries_are_frozen(tmp_path: Path) -> None:
    table = load_alias_table(_write(tmp_path, _document()))
    entry = table.resolve(Alias.REASONING_HIGH)
    field_name = "model"

    with pytest.raises(ValueError, match="frozen"):
        setattr(entry.primary, field_name, "vendor-z/substituted")


def test_resolve_fails_closed_on_an_absent_alias(tmp_path: Path) -> None:
    """The loader cannot produce this table, but `resolve` still refuses to guess a model."""
    table = load_alias_table(_write(tmp_path, _document()))
    remaining: dict[Alias, AliasEntry] = {
        alias: entry for alias, entry in table.aliases.items() if alias is not Alias.EMBED_DEFAULT
    }
    stripped = AliasTable(
        aliases=remaining,
        prices=table.prices,
        unpriced_ceiling=table.unpriced_ceiling,
    )

    with pytest.raises(AliasConfigError):
        stripped.resolve(Alias.EMBED_DEFAULT)
