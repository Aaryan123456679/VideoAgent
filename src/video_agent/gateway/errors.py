"""Gateway failures that fail honestly: what happened, what was preserved, what to do next.

`gateway.md` §4.5 asks for exactly those three facts plus a stable code and the `trace_id`, and
the reason it is a type rather than a convention is that the three are useful only together. A
message saying *the alias group is exhausted* tells an operator nothing about whether the job's
completed shots survived; a message saying *nothing was lost* tells them nothing about what to
do. `VideoAgentError` already pins the code and captures the `trace_id` at the raise site, so
this module adds the narrative and nothing else.

Every code here already exists in `observability.codes`. None is invented: `[D-55]` makes a
code's meaning permanent, and a module that minted its own would put a number into the
append-only register that the canonical table never issued.
"""

from __future__ import annotations

from video_agent.observability.codes import ErrorCode
from video_agent.observability.errors import VideoAgentError

__all__ = [
    "AliasGroupExhaustedError",
    "AliasResolutionError",
    "ContentPolicyError",
    "ContextLengthExceededError",
    "GatewayError",
    "PaymentRequiredError",
    "PromptRegistryError",
    "StructuredOutputError",
    "UpstreamRequestError",
]

NOTHING_PRESERVED = "no partial result — the call produced nothing to keep"
"""The honest value of *what was preserved* when a call failed before producing anything.

Spelled out rather than left empty because an empty string reads as "the field was not filled
in", and the difference between "nothing survived" and "nobody said" is what an operator
deciding whether to resume actually needs.
"""


class GatewayError(VideoAgentError):
    """A gateway failure carrying the three facts `gateway.md` §4.5 requires.

    The message is assembled from the parts rather than accepted whole, so the parts cannot be
    omitted at a call site in a hurry. They also stay separately readable, because the API
    envelope renders a code and the log line renders a sentence, and those want different
    slices of the same failure.
    """

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


class AliasResolutionError(GatewayError):
    """The alias, or the model it resolved to, cannot be used. Fail closed, never guess.

    `gateway.md` §8: *Alias not in config → `VA-GW-002`, non-retryable, fail closed. Never
    guess a model.* The capability check lands here for the same reason: a `vision-default`
    group whose member cannot accept an image would answer a question about a frame without
    having seen it, and a confident wrong score is worse than a refusal.
    """

    code = ErrorCode.VA_GW_002


class AliasGroupExhaustedError(GatewayError):
    """Every model in the alias group failed or is circuit-open. `gateway.md` §4.3."""

    code = ErrorCode.VA_GW_001


class StructuredOutputError(GatewayError):
    """Structured output would not parse, after exactly one reformat attempt. §5, §8."""

    code = ErrorCode.VA_GW_004


class ContextLengthExceededError(GatewayError):
    """The rendered prompt exceeds the model's context window.

    Non-retryable, and nothing is truncated to make it fit. `gateway.md` §8 is explicit about
    why: *a truncated bible breaks continuity*, so silently dropping the end of the prompt
    converts a loud failure into a run whose shots quietly stop matching each other.
    """

    code = ErrorCode.VA_GW_005


class ContentPolicyError(GatewayError):
    """The upstream refused on content policy. Surfaced honestly, naming the stage. §8."""

    code = ErrorCode.VA_GW_006


class PaymentRequiredError(GatewayError):
    """Upstream credits are exhausted (`402`). Non-retryable, and it escalates. `[D-62]`.

    Not retried and not failed over. Retrying is guaranteed to fail and only delays the one
    action that fixes it — topping up the account — and falling over to a sibling model would
    hide the exhaustion behind a degraded-but-served response until the whole group ran out.

    The code is `VA-PROV-009`, whose recorded meaning is *provider payment required (402) —
    credits exhausted*: the exact condition, with the exact retryability. `observability.md`
    §6 defines no `VA-GW-` code for payment, and `[D-55]` forbids minting one here.
    """

    code = ErrorCode.VA_PROV_009


class UpstreamRequestError(GatewayError):
    """The proxy rejected the request itself — a fault on this side of the wire.

    `VA-INT-001` rather than a gateway code, and that is the honest classification: a `400`,
    `401`, `403` or `422` from the proxy means the request we built was malformed or the
    deployment's proxy credential is wrong. Neither is something the caller did, neither is
    retryable, and the documented outcome for `VA-INT-001` — a generic `500` with a `trace_id`
    — is right, because the response must not disclose which credential is wrong while the log
    line, which is not attacker-readable, can.
    """

    code = ErrorCode.VA_INT_001


class PromptRegistryError(GatewayError):
    """A prompt was requested that the registry does not hold.

    `VA-INT-001`: asking for a prompt that does not exist is a defect in the calling code, not
    a condition the caller can act on. It never falls back to an inline string `[D-72]` —
    a prompt with no version is a prompt a trace cannot name.
    """

    code = ErrorCode.VA_INT_001
