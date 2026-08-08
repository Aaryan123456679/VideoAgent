"""The retryable / non-retryable table from `gateway.md` §4.1, in one place.

This is the most load-bearing table in the module and it is worth being blunt about why it is
one table rather than a judgement made per call site. Retrying a non-retryable error burns
budget and wall-clock for a guaranteed failure — a `403` retried three times with backoff costs
a job several seconds and three billed-or-not requests to arrive at the same `403`. Not
retrying a transient one turns a one-second blip into a failed job. Both mistakes are cheap to
make once and impossible to find later, because both look like ordinary failures in a log.

Three axes, not one, because "retry" and "fall back" are different questions:

- **retryable** — try the *same* model again after a backoff.
- **availability** — the model is unavailable rather than the request being wrong, so trying a
  *different* model in the group may work. A `422` is not an availability problem: every model
  in the group will reject the same malformed request, and failing over would spend the whole
  group to learn it three times.
- **escalates** — neither retry nor fallback can help and a human has to act. `402` is the
  case `[D-62]` names: credits are exhausted, so a retry is guaranteed to fail and only delays
  the top-up, and a fallback hides the exhaustion behind a degraded response until the last
  member of the group runs out too.

Content-policy and context-length are matched on the *body*, not the status, and are checked
before the status table. A proxy may deliver either as a `400` or as a `422`, and both of those
statuses are non-retryable anyway — but the code they carry is what the user is shown, and
"the request was malformed" is a lie when the truth is "the prompt was longer than the model's
window". `gateway.md` §8 assigns those two their own codes precisely so the failure can be
surfaced honestly with the stage named.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from video_agent.gateway.transport import UpstreamNetworkError, UpstreamStatusError
from video_agent.observability.codes import ErrorCode

__all__ = [
    "NON_RETRYABLE_STATUSES",
    "RETRYABLE_STATUSES",
    "Classification",
    "classify",
]

RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})
"""`gateway.md` §4.1, left column, verbatim."""

NON_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({400, 401, 403, 404, 422})
"""`gateway.md` §4.1, right column, verbatim. `402` is handled separately `[D-62]`."""

PAYMENT_REQUIRED_STATUS: Final = 402
NOT_FOUND_STATUS: Final = 404
TOO_MANY_REQUESTS_STATUS: Final = 429

_CONTENT_POLICY_MARKERS: Final[tuple[str, ...]] = (
    "content_policy",
    "content policy",
    "contentpolicyviolation",
    "safety",
    "responsible_ai",
    "blocked by the safety",
    "prohibited_content",
)
"""Substrings that mean *the upstream refused this content*, across proxy error shapes.

Deliberately vendor-neutral wording: these are the words error envelopes use, not the names of
whoever produced them, so nothing here is a provider name `[AGENT.md §2]`.
"""

_CONTEXT_LENGTH_MARKERS: Final[tuple[str, ...]] = (
    "context_length_exceeded",
    "context length exceeded",
    "contextwindowexceeded",
    "maximum context",
    "context window",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
)

_OVERLOADED_MARKERS: Final[tuple[str, ...]] = (
    "overloaded",
    "capacity",
    "temporarily unavailable",
    "please try again later",
    "server is busy",
)
"""`gateway.md` §4.1 lists provider "overloaded"/"capacity" as retryable regardless of status.

Some upstreams deliver overload as a `4xx`, which the status table would call permanent. The
marker check runs first so a transient condition dressed as a client error still retries.
"""

_WHITESPACE_RE: Final = re.compile(r"\s+")
"""Error bodies arrive wrapped, indented and sometimes newline-separated mid-phrase. Collapsing
runs of whitespace means a marker like `context length exceeded` still matches when the proxy
line-wrapped it, without the marker list having to enumerate every wrapping."""


@dataclass(frozen=True, slots=True)
class Classification:
    """What the policy engine is allowed to do about one failure."""

    code: ErrorCode
    retryable: bool
    availability: bool
    escalates: bool = False

    @property
    def may_fall_back(self) -> bool:
        """Whether trying another model in the same group is a legitimate response."""
        return self.availability and not self.escalates


_NETWORK = Classification(code=ErrorCode.VA_GW_001, retryable=True, availability=True)
_RATE_LIMITED = Classification(code=ErrorCode.VA_GW_003, retryable=True, availability=True)
_UNAVAILABLE = Classification(code=ErrorCode.VA_GW_001, retryable=True, availability=True)
_MODEL_UNKNOWN = Classification(code=ErrorCode.VA_GW_002, retryable=False, availability=True)
_BAD_REQUEST = Classification(code=ErrorCode.VA_INT_001, retryable=False, availability=False)
_CONTENT_POLICY = Classification(code=ErrorCode.VA_GW_006, retryable=False, availability=False)
_CONTEXT_LENGTH = Classification(code=ErrorCode.VA_GW_005, retryable=False, availability=False)
_PAYMENT_REQUIRED = Classification(
    code=ErrorCode.VA_PROV_009,
    retryable=False,
    availability=False,
    escalates=True,
)
_UNKNOWN = Classification(code=ErrorCode.VA_INT_001, retryable=False, availability=False)
"""An unrecognised status is **not** retryable.

Failing closed on the unknown, because the two mistakes are not symmetric: treating an unknown
permanent failure as retryable multiplies its cost by three and still fails, while treating an
unknown transient failure as permanent costs one job that a fallback may still rescue.
"""


def _matches(haystack: str, markers: tuple[str, ...]) -> bool:
    return any(marker in haystack for marker in markers)


def _classify_body(text: str) -> Classification | None:
    """Body-derived outcomes, which outrank the status table. See the module docstring."""
    lowered = _WHITESPACE_RE.sub(" ", text.lower())
    if _matches(lowered, _CONTEXT_LENGTH_MARKERS):
        return _CONTEXT_LENGTH
    if _matches(lowered, _CONTENT_POLICY_MARKERS):
        return _CONTENT_POLICY
    if _matches(lowered, _OVERLOADED_MARKERS):
        return _UNAVAILABLE
    return None


def _classify_status(status: int) -> Classification:
    if status == PAYMENT_REQUIRED_STATUS:
        return _PAYMENT_REQUIRED
    if status == TOO_MANY_REQUESTS_STATUS:
        return _RATE_LIMITED
    if status in RETRYABLE_STATUSES:
        return _UNAVAILABLE
    if status == NOT_FOUND_STATUS:
        return _MODEL_UNKNOWN
    if status in NON_RETRYABLE_STATUSES:
        return _BAD_REQUEST
    return _UNKNOWN


def classify(exc: BaseException) -> Classification:
    """Classify one upstream failure. Total: anything unrecognised is non-retryable.

    Total on purpose. A classifier that raised on an unexpected exception would replace a
    classified failure with an unclassified one at exactly the moment the policy engine needs
    an answer, and the caller would see `VA-INT-001` from the classifier rather than from the
    thing that actually broke.
    """
    if isinstance(exc, UpstreamNetworkError):
        return _NETWORK
    if isinstance(exc, UpstreamStatusError):
        if exc.status == PAYMENT_REQUIRED_STATUS:
            return _PAYMENT_REQUIRED
        from_body = _classify_body(f"{exc.error_type or ''} {exc.body}")
        return from_body if from_body is not None else _classify_status(exc.status)
    if isinstance(exc, TimeoutError):
        return _NETWORK
    return _UNKNOWN
