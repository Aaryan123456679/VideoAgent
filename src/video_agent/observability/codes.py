"""The stable error-code taxonomy — one enum, one meaning per code, forever.

`[CPS §Failure behaviour]` promises that every error response carries a stable code, and the
operational reason is concrete: support pastes the code into a search and opens the exact
trace. That promise is only worth anything if the code is *stable*. So `[D-55]` states the
two rules this module exists to make mechanical — **a code's meaning never changes, and a
retired code is never reused.**

Documentation cannot enforce either. Two things do:

- `observability.md` §6 is cross-checked against this enum in both directions by
  `tests/unit/test_error_codes.py`. A code in the table and not here, or here and not in the
  table, or with a different meaning or a different retryability, is a test failure.
- `codes.registry.json` records every code ever issued. `registry.py` compares it against this
  enum, so deleting a code, re-pointing one at a new meaning, or resurrecting a retired one
  fails the build rather than a review.

**Why retryability lives on the code and not in a retry policy.** `[D-62]`: a `402` from the
video provider means credits are exhausted, and retrying it is not merely useless, it delays
the escalation that is the only thing that can fix it. The retry policy that lands later reads
this flag; it does not get to form its own opinion per call site. Encoding the distinction at
the point the code is defined is what stops "just one retry, it's cheap" appearing in a
handler somewhere.

`Retryability` is deliberately a four-valued enum rather than a bare `bool`, because the table
distinguishes four cases and flattening them here would silently discard two: `yes (once)` is
a bounded retry, and `n/a` marks a code that is an internal signal and never an outcome at
all. `retryable` remains available as the `bool` every consumer actually branches on.
"""

from __future__ import annotations

from enum import StrEnum


class Retryability(StrEnum):
    """The four values the `Retryable` column of `observability.md` §6 takes.

    The member *values* are the table's own wording, so the cross-check is a string equality
    against the normalised cell rather than a translation table that could itself be wrong.
    """

    NO = "no"
    YES = "yes"
    YES_ONCE = "yes (once)"
    NOT_APPLICABLE = "n/a"


class ErrorCode(StrEnum):
    """Every error code the system may raise, with its meaning and its retryability.

    A `StrEnum` so a code serialises into the API error envelope and a log line as its own
    string with no adapter, while still being a closed set that a typo cannot join.

    Members carry three facts because a code with a meaning recorded somewhere else is a code
    whose meaning can drift. The tuple form makes the three inseparable: adding a member
    without a meaning is a `TypeError` at import, not a blank column noticed later.
    """

    retryability: Retryability
    meaning: str

    def __new__(cls, value: str, retryability: Retryability, meaning: str) -> ErrorCode:
        member = str.__new__(cls, value)
        member._value_ = value
        member.retryability = retryability
        member.meaning = meaning
        return member

    # --- Request and idempotency ----------------------------------------------------------
    VA_REQ_001 = ("VA-REQ-001", Retryability.NO, "Invalid prompt")
    VA_REQ_002 = ("VA-REQ-002", Retryability.NO, "Idempotency key missing")
    VA_REQ_003 = ("VA-REQ-003", Retryability.NO, "Idempotency key reused with a different body")
    VA_REQ_004 = ("VA-REQ-004", Retryability.YES, "Duplicate request in flight")
    VA_REQ_005 = ("VA-REQ-005", Retryability.NO, "Job not found (also returned cross-tenant)")
    VA_REQ_006 = ("VA-REQ-006", Retryability.NO, "Job not resumable")
    VA_REQ_007 = ("VA-REQ-007", Retryability.NO, "Request schema invalid")

    # --- Authentication and tenancy -------------------------------------------------------
    VA_AUTH_001 = ("VA-AUTH-001", Retryability.NO, "Unauthenticated")
    VA_AUTH_002 = ("VA-AUTH-002", Retryability.NO, "Tenant forbidden")

    # --- Planning -------------------------------------------------------------------------
    VA_PLAN_001 = ("VA-PLAN-001", Retryability.NO, "Plan unparseable")
    VA_PLAN_002 = ("VA-PLAN-002", Retryability.NO, "Beats do not sum to exactly 40s")
    VA_PLAN_003 = ("VA-PLAN-003", Retryability.NO, "Wrong beat count/kind/order")

    # --- Continuity bible -----------------------------------------------------------------
    VA_BIBLE_001 = ("VA-BIBLE-001", Retryability.NO, "Bible incomplete or too vague")
    VA_BIBLE_002 = ("VA-BIBLE-002", Retryability.NO, "Bible mutation attempted / hash mismatch")

    # --- Video provider -------------------------------------------------------------------
    VA_PROV_001 = ("VA-PROV-001", Retryability.YES, "Provider unavailable")
    VA_PROV_002 = (
        "VA-PROV-002",
        Retryability.NO,
        "No provider satisfies required capabilities",
    )
    VA_PROV_003 = ("VA-PROV-003", Retryability.YES, "Provider timeout")
    VA_PROV_004 = ("VA-PROV-004", Retryability.NO, "Content policy rejection")
    VA_PROV_005 = ("VA-PROV-005", Retryability.NO, "All providers in the group exhausted")
    VA_PROV_006 = (
        "VA-PROV-006",
        Retryability.NO,
        "Prompt exceeds provider limit even after policy truncation",
    )
    VA_PROV_007 = ("VA-PROV-007", Retryability.NO, "Provider rejected the request (400)")
    VA_PROV_008 = ("VA-PROV-008", Retryability.NO, "Provider credential rejected (401)")
    # [D-62]. Non-retryable by construction: credits are exhausted, so a retry cannot succeed
    # and only delays the escalation that can. See the module docstring.
    VA_PROV_009 = (
        "VA-PROV-009",
        Retryability.NO,
        "Provider payment required (402) — credits exhausted",
    )
    VA_PROV_010 = ("VA-PROV-010", Retryability.NO, "Provider project not found (404)")
    VA_PROV_011 = ("VA-PROV-011", Retryability.NO, "Provider unprocessable entity (422)")
    VA_PROV_012 = ("VA-PROV-012", Retryability.NO, "Render reached terminal error")
    VA_PROV_013 = ("VA-PROV-013", Retryability.NO, "Render reached terminal canceled")

    # --- Quality control ------------------------------------------------------------------
    VA_QC_001 = ("VA-QC-001", Retryability.YES, "QC model unavailable")
    VA_QC_002 = (
        "VA-QC-002",
        Retryability.NOT_APPLICABLE,
        "Score below threshold (internal signal, never an HTTP error)",
    )
    VA_QC_003 = ("VA-QC-003", Retryability.NO, "QC response unparseable")

    # --- Assembly -------------------------------------------------------------------------
    VA_ASM_001 = ("VA-ASM-001", Retryability.YES_ONCE, "ffmpeg failed / timed out")
    VA_ASM_002 = ("VA-ASM-002", Retryability.NO, "No usable clips to assemble")
    VA_ASM_003 = ("VA-ASM-003", Retryability.YES_ONCE, "Output duration mismatch")
    VA_ASM_004 = ("VA-ASM-004", Retryability.YES, "Disk exhausted")

    # --- Storage and persistence ----------------------------------------------------------
    VA_STORE_001 = ("VA-STORE-001", Retryability.YES, "Artifact write failed")
    VA_STORE_002 = ("VA-STORE-002", Retryability.YES, "Presign failed")
    VA_STORE_003 = ("VA-STORE-003", Retryability.YES, "Database unavailable")
    VA_STORE_004 = ("VA-STORE-004", Retryability.NO, "Artifact checksum mismatch")

    # --- Budget caps ----------------------------------------------------------------------
    VA_BUDGET_001 = ("VA-BUDGET-001", Retryability.NO, "USD cap exhausted")
    VA_BUDGET_002 = ("VA-BUDGET-002", Retryability.NO, "Wall-clock cap exhausted")
    VA_BUDGET_003 = ("VA-BUDGET-003", Retryability.NO, "Token cap exhausted")
    VA_BUDGET_004 = ("VA-BUDGET-004", Retryability.NO, "Iteration cap exhausted")

    # --- LLM gateway ----------------------------------------------------------------------
    VA_GW_001 = ("VA-GW-001", Retryability.YES, "Circuit open / alias group down")
    VA_GW_002 = ("VA-GW-002", Retryability.NO, "Alias unresolvable")
    VA_GW_003 = ("VA-GW-003", Retryability.YES, "Rate limited")
    VA_GW_004 = ("VA-GW-004", Retryability.NO, "Structured output unparseable")
    VA_GW_005 = ("VA-GW-005", Retryability.NO, "Context length exceeded")
    VA_GW_006 = ("VA-GW-006", Retryability.NO, "Content policy rejection at the LLM")

    # --- Security -------------------------------------------------------------------------
    VA_SEC_001 = (
        "VA-SEC-001",
        Retryability.NOT_APPLICABLE,
        "Instruction-shaped content quarantined",
    )

    # --- Internal -------------------------------------------------------------------------
    VA_INT_001 = ("VA-INT-001", Retryability.NO, "Internal error")
    VA_INT_002 = ("VA-INT-002", Retryability.NO, "No progress — repeated failure signature")
    VA_INT_003 = ("VA-INT-003", Retryability.NO, "Checkpoint schema drift")

    @property
    def retryable(self) -> bool:
        """Whether a caller or the retry policy may attempt the same operation again.

        `n/a` collapses to `False`: a code that is an internal signal is never something a
        caller retries. Any consumer that needs the distinction reads `retryability`.
        """
        return self.retryability in (Retryability.YES, Retryability.YES_ONCE)

    @property
    def domain(self) -> str:
        """The `<DOMAIN>` segment of `VA-<DOMAIN>-<NNN>`, for grouping and alerting."""
        return self.value.split("-")[1]

    @classmethod
    def from_value(cls, value: str) -> ErrorCode:
        """Look a code up by its wire string, raising `KeyError` for an unknown one.

        `ErrorCode(value)` is the idiomatic spelling but the three-argument `__new__` makes it
        unreadable to a type checker, so the lookup is spelled explicitly here instead of
        being suppressed at every call site.
        """
        for member in cls:
            if member.value == value:
                return member
        message = f"{value!r} is not a code in the taxonomy"
        raise KeyError(message)
