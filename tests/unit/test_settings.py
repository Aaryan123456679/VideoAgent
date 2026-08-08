"""S0.2.1 — the typed settings object bound to the `.env.example` contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import SecretStr, ValidationError

from video_agent.config.errors import MissingCredentialError
from video_agent.config.settings import Settings, get_settings

# Enough to construct a valid object; every other field defaults to its `.env.example` value.
BASELINE_ENV: dict[str, str] = {
    "MAGICHOUR_API_KEY": "",
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/videoagent",
    "REDIS_URL": "redis://localhost:6379/0",
}

# Values planted so a leak is unambiguous: none of them can occur by chance.
PLANTED_SECRETS: dict[str, str] = {
    "MAGICHOUR_API_KEY": "mhk_live_PLANTED_provider_key",
    "MAGICHOUR_WEBHOOK_SECRET": "PLANTED_webhook_signing_secret",
    "LITELLM_MASTER_KEY": "sk-PLANTED_gateway_master_key",
    "GEMINI_API_KEY": "PLANTED_upstream_key_one",
    "OPENAI_API_KEY": "sk-PLANTED_upstream_key_two",
    "ANTHROPIC_API_KEY": "sk-ant-PLANTED_upstream_key_three",
    "LANGFUSE_SECRET_KEY": "sk-lf-PLANTED_observability_secret",
    "AWS_ACCESS_KEY_ID": "AKIAPLANTEDACCESSKEY",
    "AWS_SECRET_ACCESS_KEY": "PLANTED_object_store_secret",
}

# The two variables whose *canonical* form embeds a password in its userinfo. The credential
# convention in `settings.py` is suffix-driven and `_URL` is not one of the suffixes, so these
# escaped it entirely until they became `SecretStr`.
PLANTED_DSN_MARKER = "PLANTED_dsn_marker_value"
PLANTED_URLS: dict[str, str] = {
    "DATABASE_URL": f"postgresql+asyncpg://user:{PLANTED_DSN_MARKER}@localhost:5432/videoagent",
    "REDIS_URL": f"redis://:{PLANTED_DSN_MARKER}@localhost:6379/0",
}

SettingsFactory = Callable[..., Settings]

# The repair budget the PRD commits to. `QC_MAX_REPAIR_ATTEMPTS` accepts this and nothing else.
PERMITTED_REPAIR_ATTEMPTS = 2


@pytest.fixture
def build_settings(monkeypatch: pytest.MonkeyPatch, env_example: dict[str, str]) -> SettingsFactory:
    """Construct `Settings` from a known environment rather than the developer's own.

    Every contract variable is removed from `os.environ` first and `_env_file=None` stops the
    repository's real `.env` from leaking in, so these tests assert the code's behaviour
    rather than the machine's configuration.
    """
    for name in env_example:
        monkeypatch.delenv(name, raising=False)

    def build(**overrides: str) -> Settings:
        for name, value in {**BASELINE_ENV, **overrides}.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)

    return build


def test_missing_required_reports_all(
    monkeypatch: pytest.MonkeyPatch, env_example: dict[str, str]
) -> None:
    """One message naming every missing variable, not the first one only.

    An operator fixing a `.env` one `ValidationError` at a time restarts the process once per
    missing variable; the point of validating the whole contract is to hand back the whole
    list.
    """
    for name in env_example:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as raised:
        Settings(_env_file=None)

    body = str(raised.value)
    for name in ("DATABASE_URL", "REDIS_URL", "MAGICHOUR_API_KEY"):
        assert name in body, body


def test_required_fields_are_exactly_the_three_that_cannot_be_guessed() -> None:
    """`.env.example` supplies a localhost default for the two URLs; `Settings` does not.

    A default here would be a production deployment that silently connects to `localhost`
    when its `DATABASE_URL` is missing — an outage disguised as a working process. The
    template may suggest a value for a developer; the code may not assume one.
    """
    required = {name for name, field in Settings.model_fields.items() if field.is_required()}
    assert required == {"MAGICHOUR_API_KEY", "DATABASE_URL", "REDIS_URL"}


def _rendered(settings: Settings, name: str) -> str:
    """The field's value as `.env.example` would spell it.

    `str()` on a `SecretStr` is `**********`, which compares equal to nothing in the contract
    and unequal to everything — so a comparison built on it can only ever be made to pass by
    excluding the field. Unwrapping is what lets the nine credential variables be checked at
    all.
    """
    value = getattr(settings, name)
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value)


def test_defaults_match_the_env_example_values(
    build_settings: SettingsFactory, env_example: dict[str, str]
) -> None:
    """`.env.example` is the contract for values too, not only for names.

    Including the values it declares **empty**. An `if declared` filter here excused thirteen
    variables — every optional credential, both toolchain overrides and the object-store
    endpoint — from the contract entirely: changing a default from `""` to anything at all left
    the suite green, which is the one thing this test exists to catch. A declared-empty variable
    is a contract term saying *this deployment may legitimately not have one*, and a code
    default that quietly disagrees is how a deployment starts pointing somewhere nobody chose.
    """
    settings = build_settings()
    required = {name for name, field in Settings.model_fields.items() if field.is_required()}

    mismatches = {
        name: (declared, _rendered(settings, name))
        for name, declared in env_example.items()
        if name not in required and _rendered(settings, name) != declared
    }
    assert mismatches == {}


def test_the_contract_check_covers_the_variables_declared_empty(
    env_example: dict[str, str],
) -> None:
    """Guards the guard: if the empty declarations stop being checked, this says so.

    Named explicitly rather than counted, so removing one from `.env.example` fails the
    contract test rather than quietly shrinking this one.
    """
    declared_empty = {name for name, value in env_example.items() if not value}
    required = {name for name, field in Settings.model_fields.items() if field.is_required()}

    assert declared_empty >= {
        "MAGICHOUR_WEBHOOK_SECRET",
        "LITELLM_MASTER_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "ARTIFACT_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "FFMPEG_BINARY",
        "FFPROBE_BINARY",
    }
    assert declared_empty - required, "every empty declaration is required; nothing left to check"


def test_settings_load_when_the_provider_key_is_empty(build_settings: SettingsFactory) -> None:
    """The application starts without a provider credential; only a call needs one."""
    settings = build_settings(MAGICHOUR_API_KEY="")
    assert settings.MAGICHOUR_API_KEY.get_secret_value() == ""


def test_secrets_never_stringify(build_settings: SettingsFactory) -> None:
    """`str`, `repr` and JSON all render `**********`. `[CPS §Observability]`"""
    settings = build_settings(**PLANTED_SECRETS)
    renderings = (str(settings), repr(settings), settings.model_dump_json())

    for rendering in renderings:
        for name, planted in PLANTED_SECRETS.items():
            assert planted not in rendering, f"{name} leaked into {rendering[:80]!r}"

    dumped = json.loads(settings.model_dump_json())
    assert dumped["MAGICHOUR_API_KEY"] == "**********"


@pytest.mark.parametrize(
    "field",
    [
        "MAGICHOUR_API_KEY",
        "MAGICHOUR_WEBHOOK_SECRET",
        "LITELLM_MASTER_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LANGFUSE_SECRET_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        # Not a `_KEY` and not a `_SECRET`, and that is precisely why both were plain `str`
        # while carrying a password. The rule is what the value holds, not how it is spelled.
        "DATABASE_URL",
        "REDIS_URL",
    ],
)
def test_credentials_are_secret_str(field: str) -> None:
    assert Settings.model_fields[field].annotation is SecretStr


@pytest.mark.parametrize("rendering", ["str", "repr", "fstring", "model_dump", "model_dump_json"])
def test_connection_urls_never_render_their_password(
    build_settings: SettingsFactory, rendering: str
) -> None:
    """`DATABASE_URL` and `REDIS_URL` carry a password; no rendering of `Settings` shows it.

    Five renderings rather than one because they are five different code paths in pydantic and
    a wrapper that covered `repr` but not `model_dump` would look fixed. `model_dump` is the
    one that matters most in practice: it is what a debugger's variable pane, a `pytest`
    assertion diff and a hand-rolled error body all reach for, and none of those passes through
    `observability.redaction`.
    """
    settings = build_settings(**PLANTED_URLS)
    renderings: dict[str, str] = {
        "str": str(settings),
        "repr": repr(settings),
        "fstring": f"{settings}",
        "model_dump": str(settings.model_dump()),
        "model_dump_json": settings.model_dump_json(),
    }

    assert PLANTED_DSN_MARKER not in renderings[rendering], renderings[rendering][:200]


def test_the_connection_urls_are_still_usable_after_wrapping(
    build_settings: SettingsFactory,
) -> None:
    """Guards the guard: a wrapper that hid the value from its own consumers would also pass.

    Without this, deleting the `.get_secret_value()` calls in `persistence.session` and
    `persistence.redis_client` would leave the redaction assertions green while no process
    could connect to anything.
    """
    settings = build_settings(**PLANTED_URLS)

    assert settings.DATABASE_URL.get_secret_value() == PLANTED_URLS["DATABASE_URL"]
    assert settings.REDIS_URL.get_secret_value() == PLANTED_URLS["REDIS_URL"]


def test_resolution_literal_rejects_4k(build_settings: SettingsFactory) -> None:
    """1080p is a hard ceiling; a higher value is billed and then refused upstream."""
    with pytest.raises(ValidationError):
        build_settings(MAGICHOUR_RESOLUTION="4k")


@pytest.mark.parametrize("resolution", ["720p", "1080p"])
def test_resolution_accepts_both_documented_values(
    build_settings: SettingsFactory, resolution: str
) -> None:
    settings = build_settings(MAGICHOUR_RESOLUTION=resolution)
    assert resolution == settings.MAGICHOUR_RESOLUTION


def test_repair_cap_rejects_three(build_settings: SettingsFactory) -> None:
    """Config is the first line of defence; the database CHECK is the last."""
    with pytest.raises(ValidationError):
        build_settings(QC_MAX_REPAIR_ATTEMPTS="3")


def test_repair_cap_accepts_two(build_settings: SettingsFactory) -> None:
    settings = build_settings(QC_MAX_REPAIR_ATTEMPTS=str(PERMITTED_REPAIR_ATTEMPTS))
    assert settings.QC_MAX_REPAIR_ATTEMPTS == PERMITTED_REPAIR_ATTEMPTS


@pytest.mark.parametrize("value", ["-0.01", "1.01"])
def test_qc_threshold_is_bounded_to_the_unit_interval(
    build_settings: SettingsFactory, value: str
) -> None:
    with pytest.raises(ValidationError):
        build_settings(QC_ACCEPT_THRESHOLD=value)


def test_settings_are_frozen(build_settings: SettingsFactory) -> None:
    """Configuration read at startup must be the configuration in force at minute forty.

    Assigned dynamically so the assertion is about what the object does at runtime, not about
    what the type checker already knows.
    """
    settings = build_settings()
    field_name = "QC_ACCEPT_THRESHOLD"

    with pytest.raises(ValidationError):
        setattr(settings, field_name, 0.1)


def test_usd_conversion_derives_from_the_configured_rate(
    build_settings: SettingsFactory,
) -> None:
    """`[D-65]` — the rate comes from config, never from a literal in code."""
    settings = build_settings(MAGICHOUR_USD_PER_1K_CREDITS="1.20")

    assert settings.usd_per_credit == Decimal("0.0012")
    assert settings.usd_for_credits(1000) == Decimal("1.200")
    assert settings.usd_for_credits(250) == Decimal("0.300")


def test_usd_conversion_takes_no_discount(build_settings: SettingsFactory) -> None:
    """`[D-65]` — a discount would *lower* pre-flight spend and let a job run past the cap.

    Asserted on the signature rather than on a value, because the defect this guards against
    is someone adding the parameter, not someone passing a wrong one.
    """
    settings = build_settings()
    annotations = Settings.usd_for_credits.__annotations__
    assert set(annotations) == {"credit_amount", "return"}
    assert settings.usd_for_credits(0) == Decimal(0)


def test_tenant_override_lowers_the_cap_and_null_inherits(
    build_settings: SettingsFactory,
) -> None:
    """`[D-70]` — NULL on `tenant.max_usd_per_job` means inherit, never unlimited."""
    settings = build_settings(BUDGET_MAX_USD_PER_JOB="5.00")

    assert settings.max_usd_for_tenant(Decimal("1.50")) == Decimal("1.50")
    assert settings.max_usd_for_tenant(None) == Decimal("5.00")


def test_require_magichour_api_key_fails_at_point_of_use(
    build_settings: SettingsFactory,
) -> None:
    settings = build_settings(MAGICHOUR_API_KEY="")
    with pytest.raises(MissingCredentialError) as raised:
        settings.require_magichour_api_key()

    assert "MAGICHOUR_API_KEY" in str(raised.value)


def test_require_magichour_api_key_returns_the_value_when_set(
    build_settings: SettingsFactory,
) -> None:
    settings = build_settings(MAGICHOUR_API_KEY="mhk_live_PLANTED_provider_key")
    assert settings.require_magichour_api_key() == "mhk_live_PLANTED_provider_key"


@pytest.mark.parametrize(
    ("method", "variable"),
    [
        ("require_litellm_master_key", "LITELLM_MASTER_KEY"),
        ("require_magichour_webhook_secret", "MAGICHOUR_WEBHOOK_SECRET"),
    ],
)
def test_optional_credentials_are_demanded_only_at_point_of_use(
    build_settings: SettingsFactory, method: str, variable: str
) -> None:
    settings = build_settings()
    with pytest.raises(MissingCredentialError) as raised:
        getattr(settings, method)()

    assert variable in str(raised.value)


def test_get_settings_is_constructed_once() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
