"""`S0.3.1` — the enum is the taxonomy table, checked in both directions.

`observability.md` §6 says the codes are "declared in one enum, which is the single source for
this table". That sentence is a claim about two files agreeing, and two files agree only for as
long as someone checks. So the table is *parsed* here rather than transcribed: a code added to
the enum and not documented fails, a code documented and not implemented fails, and a meaning
or a retryability edited on one side and not the other fails.

The parser has its own test. A markdown parser that silently matches nothing would make every
assertion below vacuously true, which is precisely the failure mode a cross-check must not
have — the table is asserted to be non-trivially large before anything is compared against it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from video_agent.observability.codes import ErrorCode, Retryability

TAXONOMY_HEADING = "## 6. Error taxonomy"
CODE_PATTERN = re.compile(r"^VA-[A-Z]+-\d{3}$")
MINIMUM_DOCUMENTED_CODES = 40
"""A floor, not the count. Asserting the exact count here would only restate the enum; this
asserts that the parser found a real table rather than an empty match."""


def _normalise(cell: str) -> str:
    """Strip the markdown emphasis and code spans, leaving the prose the enum stores.

    `observability.md` bolds `VA-PROV-009` and italicises the parenthetical on `VA-QC-002`.
    Both are typography, not meaning, so they are removed rather than copied into the enum —
    an enum member whose `meaning` contained asterisks would leak markdown into an API
    response.
    """
    return re.sub(r"\s+", " ", cell.replace("**", "").replace("*", "").replace("`", "")).strip()


def _taxonomy_section(text: str) -> str:
    start = text.index(TAXONOMY_HEADING)
    remainder = text[start + len(TAXONOMY_HEADING) :]
    end = remainder.find("\n## ")
    return remainder if end == -1 else remainder[:end]


def _parse_taxonomy_table(text: str) -> dict[str, tuple[str, str]]:
    """`observability.md` §6 as `code -> (meaning, retryable)`."""
    rows: dict[str, tuple[str, str]] = {}
    for line in _taxonomy_section(text).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [_normalise(cell) for cell in stripped.strip("|").split("|")]
        expected_columns = 4
        if len(cells) != expected_columns or not CODE_PATTERN.match(cells[0]):
            continue
        rows[cells[0]] = (cells[1], cells[2])
    return rows


@pytest.fixture(scope="module")
def documented(repo_root: Path) -> dict[str, tuple[str, str]]:
    """The taxonomy table as the canonical document currently states it."""
    text = (repo_root / "docs" / "LLD" / "observability.md").read_text(encoding="utf-8")
    return _parse_taxonomy_table(text)


# --- The parser itself ---------------------------------------------------------------------


def test_the_taxonomy_table_was_actually_parsed(documented: dict[str, tuple[str, str]]) -> None:
    """Guards every other test in this module against a parser that matches nothing."""
    assert len(documented) >= MINIMUM_DOCUMENTED_CODES
    assert documented["VA-REQ-001"] == ("Invalid prompt", "no")


def test_the_parser_ignores_the_header_and_separator_rows(
    documented: dict[str, tuple[str, str]],
) -> None:
    assert "Code" not in documented
    assert all(CODE_PATTERN.match(code) for code in documented)


# --- Enum against table, both directions -----------------------------------------------------


def test_enum_matches_taxonomy_table(documented: dict[str, tuple[str, str]]) -> None:
    """`S0.3.1` acceptance 1 and 4 — no more, no fewer, in either direction."""
    in_enum = {member.value for member in ErrorCode}
    in_table = set(documented)

    assert in_enum - in_table == set(), (
        "codes in ErrorCode but undocumented in observability.md S6; every raised code must be "
        "documented [D-55]"
    )
    assert in_table - in_enum == set(), (
        "codes documented in observability.md S6 but absent from ErrorCode; the enum is the "
        "single source for the table"
    )


def test_enum_meanings_match_the_table(documented: dict[str, tuple[str, str]]) -> None:
    divergent = {
        member.value: (member.meaning, documented[member.value][0])
        for member in ErrorCode
        if member.value in documented and member.meaning != documented[member.value][0]
    }
    assert divergent == {}, "enum meaning != documented meaning, as (enum, document)"


def test_enum_retryability_matches_the_table(documented: dict[str, tuple[str, str]]) -> None:
    divergent = {
        member.value: (member.retryability.value, documented[member.value][1])
        for member in ErrorCode
        if member.value in documented and member.retryability.value != documented[member.value][1]
    }
    assert divergent == {}, "enum retryability != documented column, as (enum, document)"


# --- Properties of every member ----------------------------------------------------------------


def test_every_code_has_a_nonempty_meaning() -> None:
    blank = [member.name for member in ErrorCode if not member.meaning.strip()]
    assert blank == []


def test_every_code_matches_the_documented_format() -> None:
    """`observability.md` §6: `VA-<DOMAIN>-<NNN>`."""
    malformed = [member.value for member in ErrorCode if not CODE_PATTERN.match(member.value)]
    assert malformed == []


def test_member_names_are_their_values() -> None:
    """`ErrorCode.VA_PROV_009` and `VA-PROV-009` must be obviously the same code.

    A member whose name did not spell its value would make every grep for a code in a ticket
    miss the place it is raised.
    """
    mismatched = [
        member.name for member in ErrorCode if member.name != member.value.replace("-", "_")
    ]
    assert mismatched == []


def test_codes_are_unique() -> None:
    values = [member.value for member in ErrorCode]
    assert len(values) == len(set(values))


# --- The distinction T0.7's retry policy consumes --------------------------------------------


def test_402_is_non_retryable() -> None:
    """`[D-62]` — credits exhausted. A retry cannot succeed and delays the escalation."""
    assert ErrorCode.VA_PROV_009.retryable is False
    assert ErrorCode.VA_PROV_009.retryability is Retryability.NO


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.VA_PROV_001, True),
        (ErrorCode.VA_PROV_003, True),
        (ErrorCode.VA_GW_003, True),
        (ErrorCode.VA_ASM_001, True),
        (ErrorCode.VA_ASM_003, True),
        (ErrorCode.VA_PROV_009, False),
        (ErrorCode.VA_BUDGET_001, False),
        (ErrorCode.VA_QC_002, False),
        (ErrorCode.VA_SEC_001, False),
    ],
)
def test_retryable_is_derived_from_retryability(code: ErrorCode, *, expected: bool) -> None:
    assert code.retryable is expected


def test_bounded_retries_are_distinguishable_from_unbounded() -> None:
    """`yes (once)` is not the same permission as `yes`, and the enum keeps them apart.

    Flattening both to `True` would let a retry policy loop on `VA-ASM-001`, which the table
    caps at one attempt.
    """
    assert ErrorCode.VA_ASM_001.retryability is Retryability.YES_ONCE
    assert ErrorCode.VA_PROV_001.retryability is Retryability.YES
    assert ErrorCode.VA_ASM_001.retryable is ErrorCode.VA_PROV_001.retryable


def test_not_applicable_is_distinguishable_from_no() -> None:
    """`VA-QC-002` is an internal signal, never an HTTP error. `no` would imply it is one."""
    assert ErrorCode.VA_QC_002.retryability is Retryability.NOT_APPLICABLE
    assert ErrorCode.VA_REQ_001.retryability is Retryability.NO


# --- Serialisation and lookup -------------------------------------------------------------------


def test_a_code_serialises_as_its_wire_string() -> None:
    """A `StrEnum` so the envelope and the log line need no adapter."""
    assert f"{ErrorCode.VA_PROV_009}" == "VA-PROV-009"
    assert str(ErrorCode.VA_GW_002) == "VA-GW-002"


def test_from_value_round_trips_every_member() -> None:
    for member in ErrorCode:
        assert ErrorCode.from_value(member.value) is member


def test_from_value_rejects_an_unknown_code() -> None:
    with pytest.raises(KeyError):
        ErrorCode.from_value("VA-NOPE-999")


def test_domain_is_the_middle_segment() -> None:
    assert ErrorCode.VA_PROV_009.domain == "PROV"
    assert ErrorCode.VA_BUDGET_001.domain == "BUDGET"
