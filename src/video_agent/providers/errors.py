"""Provider failures that fail honestly: what happened, what was preserved, what to do next.

Mirrors `gateway.errors.GatewayError`'s three-fact shape exactly — the failure narrative
`gateway.md` §4.5 asks for is not an LLM-gateway-specific idea, it is a `[CPS §Failure
behaviour]` promise, and this module inherits the shape rather than reinventing it.

Only the codes T2.1 owns are defined here: `VA-PROV-001/003/005/006/009` plus the negotiation
failure `VA-PROV-002`. `VA-PROV-007/008/010/011/012/013` are concrete Magic-Hour HTTP-status
mappings (`providers.md` §7.4) and belong to T2.2, once an adapter exists to raise them —
minting them here now would be a code with no call site, and every code in `codes.py` already
exists, so none of this is invented `[D-55]`.
"""

from __future__ import annotations

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError

__all__ = [
    "NOTHING_PRESERVED",
    "NoProviderSatisfiesCapabilitiesError",
    "PromptExceedsLimitError",
    "ProviderError",
    "ProviderGroupExhaustedError",
    "ProviderPaymentRequiredError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
]

NOTHING_PRESERVED = "no partial result — the call produced nothing to keep"
"""The honest value of *what was preserved* when a call failed before producing a clip."""


class ProviderError(VideoAgentError):
    """A provider failure carrying the three facts a video-provider caller needs to act on."""

    code: ErrorCode = ErrorCode.VA_INT_001

    def __init__(
        self,
        *,
        what_happened: str,
        what_to_do_next: str,
        what_was_preserved: str = NOTHING_PRESERVED,
        code: ErrorCode | None = None,
    ) -> None:
        self.what_happened = what_happened
        self.what_was_preserved = what_was_preserved
        self.what_to_do_next = what_to_do_next
        super().__init__(
            f"{what_happened} Preserved: {what_was_preserved}. Next: {what_to_do_next}",
            code=code,
        )


class NoProviderSatisfiesCapabilitiesError(ProviderError):
    """No provider offers a non-negotiable capability. `providers.md` §3, `[D-31]`.

    Raised instead of ever generating an unchained or under-specified clip — an empty
    `select()` result is the honest outcome, not a silently degraded one.
    """

    code = ErrorCode.VA_PROV_002

    def __init__(self, message: str) -> None:
        super().__init__(
            what_happened=message,
            what_was_preserved=NOTHING_PRESERVED,
            what_to_do_next=(
                "no shot was generated; add a provider offering the missing capability or "
                "adjust the shot so it no longer requires it."
            ),
        )


class ProviderGroupExhaustedError(ProviderError):
    """Every candidate provider failed or was circuit-open. `providers.md` §4."""

    code = ErrorCode.VA_PROV_005

    def __init__(self, *, what_happened: str, what_was_preserved: str) -> None:
        super().__init__(
            what_happened=what_happened,
            what_was_preserved=what_was_preserved,
            what_to_do_next=(
                "resume the job; completed shots are checkpointed and are not regenerated."
            ),
        )


class PromptExceedsLimitError(ProviderError):
    """The composed prompt exceeds the provider's `max_prompt_chars` even after truncation.

    `providers.md` §5, `[D-33]`: the continuity-bible and negative-constraints sections are
    never truncated to make a prompt fit, so this fires rather than generating against a
    partial bible.
    """

    code = ErrorCode.VA_PROV_006

    def __init__(self, message: str) -> None:
        super().__init__(
            what_happened=message,
            what_was_preserved=NOTHING_PRESERVED,
            what_to_do_next=(
                "shorten the beat action or drop the continuity note; the continuity bible "
                "and negative constraints are never truncated."
            ),
        )


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached or returned a transient failure. Retryable."""

    code = ErrorCode.VA_PROV_001

    def __init__(self, message: str) -> None:
        super().__init__(
            what_happened=message,
            what_was_preserved=NOTHING_PRESERVED,
            what_to_do_next="retried automatically against the same or a fallback provider.",
        )


class ProviderTimeoutError(ProviderError):
    """The provider did not respond within `ShotRequest.timeout_s`. Retryable."""

    code = ErrorCode.VA_PROV_003

    def __init__(self, message: str) -> None:
        super().__init__(
            what_happened=message,
            what_was_preserved=NOTHING_PRESERVED,
            what_to_do_next="retried automatically against the same or a fallback provider.",
        )


class ProviderPaymentRequiredError(ProviderError):
    """Upstream credits are exhausted (`402`). `[D-62]`.

    Never retried and never falls over to a sibling provider: retrying cannot succeed, and
    falling over would hide the exhaustion behind a degraded-but-served response until every
    provider in the group ran out. The registry's failover loop special-cases this exception
    to short-circuit the whole policy rather than trying the next candidate.
    """

    code = ErrorCode.VA_PROV_009

    def __init__(self, *, what_happened: str, what_was_preserved: str) -> None:
        super().__init__(
            what_happened=what_happened,
            what_was_preserved=what_was_preserved,
            what_to_do_next=(
                "top up the provider account; this failure is not retried and does not fail "
                "over to another provider."
            ),
        )
