"""Typed settings bound to the `.env.example` contract.

`.env.example` is the contract; this module is its executable half. There is exactly one
field per variable in that file, spelled identically, and the contract test in
`tests/contract/test_env_example_contract.py` diffs the two key sets in both directions — so
a variable added to one and not the other fails CI rather than surfacing as a `KeyError` in
production.

Three rules shape the field definitions:

**Presence is required; emptiness is allowed.** A variable that `.env.example` declares with
no value (`MAGICHOUR_API_KEY=`) is still a variable an operator must have copied into their
`.env`. Where the value is genuinely load-bearing the field has no default, so a `.env` that
predates the variable fails immediately with every missing name in one message. Where the
value may legitimately be blank — an unset credential for an upstream this deployment does
not call — the field defaults to an empty `SecretStr` and the code path that needs it demands
it at point of use via `require()`. Failing at import for a credential nothing has asked for
yet would stop the application from starting for a capability it may never exercise.

**Credentials are `SecretStr`.** `[CPS §Observability]` forbids logging credentials. A plain
`str` is one `f"{settings}"` away from a log line; `SecretStr` renders as `**********` under
`repr`, `str` and `model_dump_json()`, so the accident is not available.

**Derived rates are derived, never re-typed.** `[D-65]` The credits-to-USD rate comes from
`MAGICHOUR_USD_PER_1K_CREDITS` and nowhere else, and volume discounts are deliberately not
modelled: applying one would *lower* the computed cost of a job and let it run further before
tripping the cap. Over-estimating spend makes the cap trip early, which is the only direction
a cap may err.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from video_agent.config.errors import MissingCredentialError
from video_agent.observability.redaction import contains_never_logged_value

CREDITS_PER_RATE_UNIT = Decimal(1000)
"""The provider quotes its rate per 1,000 credits, so the per-credit rate divides by this."""

REDACTED_DETAIL = "<redacted: the message carried a value that must never be emitted>"
"""Stands in for a per-error message the scanner refused. The *name* is never redacted, so an
operator still learns which variable is wrong even when the reason cannot be shown."""


class Settings(BaseSettings):
    """Every variable in `.env.example`, typed, validated and frozen.

    Field names are upper-case to match the contract character for character. Environment
    binding is case-insensitive, so this costs nothing at the boundary and makes the contract
    test a set comparison rather than a naming convention both sides have to remember.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        validate_default=True,
    )

    # --- Video generation provider -----------------------------------------------------
    # Required, but may be empty: the application starts without it and only a code path
    # that actually calls the provider may demand it. See `require_magichour_api_key`.
    MAGICHOUR_API_KEY: SecretStr
    MAGICHOUR_BASE_URL: str = "https://api.magichour.ai"
    # `ltx-2.3` over `wan-2.2`: same measured cost (240 credits for a 10s/480p shot) and same
    # 10s duration support, but `wan-2.2` optimises for output quality at the expense of
    # render speed while `ltx-2.3` is the faster of the two per Magic Hour's own comparison —
    # and queue/render time, not credits, was the actual bottleneck this was chosen to fix.
    MAGICHOUR_MODEL: str = "ltx-2.3"
    # 1080p is a hard ceiling: nothing above it is offered at the 10s clip length v1 needs,
    # and a higher value would be accepted by the provider only to be billed and refused.
    # "480p" is a temporary account-tier accommodation, not a product option — see
    # providers.models.Capability.RES_480P's docstring for why it exists and when to remove it.
    MAGICHOUR_RESOLUTION: Literal["480p", "720p", "1080p"] = "720p"
    MAGICHOUR_WEBHOOK_SECRET: SecretStr = SecretStr("")
    # A second credential to rotate onto when the first is rejected for insufficient credits
    # (402). Optional — empty means single-key behaviour, unchanged. `[D-62]` still holds that
    # a 402 is never retried against the *same* credential; this exists only for the case
    # where a second, genuinely different account can succeed where the first cannot.
    MAGICHOUR_API_KEY_2: SecretStr = SecretStr("")
    # Undiscounted list rate for the account's tier. See the module docstring and [D-65].
    MAGICHOUR_USD_PER_1K_CREDITS: Decimal = Field(default=Decimal("0.90"), gt=0)

    # --- LLM gateway --------------------------------------------------------------------
    LITELLM_BASE_URL: str = "http://localhost:4000"
    LITELLM_MASTER_KEY: SecretStr = SecretStr("")
    # Upstream keys are held by the LiteLLM proxy, not by application code. They are in the
    # contract so a single-host deployment can pass them through, and are optional here
    # because a deployment pointing at a managed proxy has none of them.
    GEMINI_API_KEY: SecretStr = SecretStr("")
    OPENAI_API_KEY: SecretStr = SecretStr("")
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")

    # --- Observability ------------------------------------------------------------------
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"
    # Public by name and by design; the paired secret below is what must never be rendered.
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: SecretStr = SecretStr("")

    # --- Persistence --------------------------------------------------------------------
    # `SecretStr`, because the canonical DSN form of both embeds a password in its userinfo:
    # `postgresql+asyncpg://user:hunter2@host/db`, `redis://:hunter2@host:6379/0`. The
    # credential convention in this file is suffix-driven — `_KEY`, `_SECRET` — and `_URL` is
    # not one of those suffixes, so these two were the only variables carrying a password that
    # rendered in full under `repr`, `str`, `model_dump`, `model_dump_json` and every f-string.
    #
    # The structured-logging path already defended them twice over, by name
    # (`CREDENTIAL_KEY_PHRASES` lists `database_url` and `redis_url`) and by shape
    # (`is_credentialed_url`). What the wrapper closes is everywhere else a value travels:
    # a driver's exception message, an HTTP error body, a traceback, a debugger frame. None of
    # those is an emission path anybody redacts, and `AGENT.md` §3 names DB URLs on the
    # never-logged list without qualifying which path they were on.
    DATABASE_URL: SecretStr
    REDIS_URL: SecretStr

    # --- Artifact storage ---------------------------------------------------------------
    ARTIFACT_BUCKET: str = "video-agent-artifacts"
    ARTIFACT_ENDPOINT_URL: str = ""
    # Half of a credential pair and credential-shaped (`AKIA...`), so it is treated as one.
    AWS_ACCESS_KEY_ID: SecretStr = SecretStr("")
    AWS_SECRET_ACCESS_KEY: SecretStr = SecretStr("")
    AWS_REGION: str = "us-east-1"
    PRESIGNED_URL_TTL_SECONDS: int = Field(default=3600, gt=0)

    # --- Budget caps --------------------------------------------------------------------
    # Global per-job ceiling. A tenant may lower it; `max_usd_for_tenant` applies the
    # override and treats NULL as "inherit" [D-70].
    BUDGET_MAX_USD_PER_JOB: Decimal = Field(default=Decimal("5.00"), gt=0)
    BUDGET_MAX_WALL_CLOCK_SECONDS: int = Field(default=1200, gt=0)
    BUDGET_MAX_TOKENS: int = Field(default=250_000, gt=0)
    BUDGET_MAX_SUPERSTEPS: int = Field(default=40, gt=0)

    # --- QC ------------------------------------------------------------------------------
    # Configuration rather than a compile-time constant [D-71]: the threshold has not been
    # calibrated against a labelled set [D-66], and freezing an uncalibrated number as a
    # commitment would be dishonest. Any value other than the default must be logged at
    # startup and surfaced on the job manifest, so a loosened gate is never invisible.
    QC_ACCEPT_THRESHOLD: float = Field(default=0.75, ge=0.0, le=1.0)
    # Exactly two. The repair budget is the PRD's named mitigation for runaway repair loops;
    # the database CHECK is the last line of defence and this is the first. Pinned with
    # `ge`/`le` rather than `Literal[2]` because environment values arrive as strings and a
    # `Literal[int]` refuses to coerce one, which would reject the documented default itself.
    QC_MAX_REPAIR_ATTEMPTS: int = Field(default=2, ge=2, le=2)

    # --- Media toolchain ------------------------------------------------------------------
    FFMPEG_REQUIRED_VERSION: str = "7.1"
    FFMPEG_BINARY: str = ""
    FFPROBE_BINARY: str = ""

    # --- App --------------------------------------------------------------------------------
    # Deliberately an open string: the deployment environments are an operational fact, not a
    # specified enumeration, and inventing the set here would reject a valid one.
    ENV: str = "local"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @property
    def usd_per_credit(self) -> Decimal:
        """The undiscounted USD cost of one provider credit, derived from the configured rate."""
        return self.MAGICHOUR_USD_PER_1K_CREDITS / CREDITS_PER_RATE_UNIT

    def usd_for_credits(self, credit_amount: int | Decimal) -> Decimal:
        """Convert a provider credit amount to USD at the configured list rate.

        There is no discount parameter and there must never be one `[D-65]`. A volume
        discount lowers the computed cost of a job, which would let it run further before
        tripping the USD cap; the discount is a reconciliation-time credit, never a
        pre-flight allowance.
        """
        return Decimal(credit_amount) * self.usd_per_credit

    def max_usd_for_tenant(self, tenant_max_usd_per_job: Decimal | None) -> Decimal:
        """The per-job USD cap for a tenant: their override, or the global cap when NULL.

        `tenant.max_usd_per_job` is nullable and NULL means *inherit* `[D-70]`, not
        *unlimited*. Reading it as unlimited would turn an unset column into a removed cap.
        """
        if tenant_max_usd_per_job is None:
            return self.BUDGET_MAX_USD_PER_JOB
        return tenant_max_usd_per_job

    def require_magichour_api_key(self) -> str:
        """Return the provider API key, raising if it is empty.

        Called only from the code path that is about to talk to the provider. The
        application starts fine without the key; a job that needs it fails with a sentence
        naming the variable instead of a 401 from an upstream.
        """
        return self._require(self.MAGICHOUR_API_KEY, "MAGICHOUR_API_KEY", "call Magic Hour")

    def magichour_api_keys(self) -> tuple[str, ...]:
        """The configured provider credentials, in rotation order.

        Always at least the primary key (raising the same way `require_magichour_api_key`
        does if even that one is empty); `MAGICHOUR_API_KEY_2` is appended only if set. Order
        matters — `providers.magichour.RotatingApiKey` advances forward through this tuple and
        never wraps, so the primary account is always tried first.
        """
        keys = [self.require_magichour_api_key()]
        secondary = self.MAGICHOUR_API_KEY_2.get_secret_value()
        if secondary:
            keys.append(secondary)
        return tuple(keys)

    def require_magichour_webhook_secret(self) -> str:
        """Return the webhook signing secret, raising if it is empty.

        Webhooks are optional — polling is the fallback — but an *unverified* webhook is not
        an acceptable degradation, so the verification path demands the secret rather than
        skipping the check.
        """
        return self._require(
            self.MAGICHOUR_WEBHOOK_SECRET,
            "MAGICHOUR_WEBHOOK_SECRET",
            "verify an inbound provider webhook signature",
        )

    def require_litellm_master_key(self) -> str:
        """Return the gateway master key, raising if it is empty."""
        return self._require(self.LITELLM_MASTER_KEY, "LITELLM_MASTER_KEY", "call the LLM gateway")

    @staticmethod
    def _require(value: SecretStr, name: str, purpose: str) -> str:
        secret = value.get_secret_value()
        if not secret:
            message = f"{name} is empty; it is required to {purpose}. Set it in .env."
            raise MissingCredentialError(message)
        return secret


def describe_validation_error(exc: ValidationError) -> str:
    """A configuration failure rendered as variable names and reasons, and nothing else.

    `str(exc)` is not usable on any emission path. Pydantic's `missing` error carries
    `input_value`, and for a settings model that is the **entire collected settings dict** — so
    a deployment missing `DATABASE_URL` prints every environment variable it *did* find,
    including `MAGICHOUR_API_KEY`, straight into the failure message. `AGENT.md` §3 forbids
    exactly this and names API keys and DB URLs while doing it.

    The leak is intermittent, which is what makes it dangerous: pydantic truncates a long
    `input_value` repr, so a fully-populated environment hides it and the sparse,
    half-configured deployment — the one that actually hits this path — does not.

    `include_input=False` is the fix; the scanner pass over each reason is the belt to its
    braces, applied per error so one unshowable reason does not take the other names down with
    it. The acceptance criterion — one message naming *every* missing variable — is preserved,
    because `loc` is the field name and a field name is not a secret.
    """
    lines = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        name = ".".join(str(part) for part in error["loc"]) or exc.title
        reason = error["msg"]
        lines.append(
            f"{name}: {reason if not contains_never_logged_value(reason) else REDACTED_DETAIL}"
        )
    return "; ".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings object, constructed once.

    Cached rather than module-level so importing `video_agent.config` never reads the
    environment as a side effect — the startup preflight decides when configuration is
    validated, and tests construct their own instances without fighting an import.
    """
    return Settings()
