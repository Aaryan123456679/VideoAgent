"""`S0.3.1` — the append-only register, and the five ways it refuses to be edited.

The committed register is checked against the live enum, which is the gate that runs in CI.
Everything else here plants a violation in a **fabricated** register and asserts the check
catches it, because a guard that has only ever been observed to pass is indistinguishable from
a guard that cannot fail. Each negative test also asserts the *kind* of violation, so a check
that reported every problem as the same generic failure would not satisfy them either.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_agent.observability.codes import ErrorCode
from video_agent.observability.registry import (
    REGISTRY_PATH,
    CodeFacts,
    RegisteredCode,
    RegistryFormatError,
    RegistryStatus,
    ViolationKind,
    check_registry,
    format_violations,
    load_registry,
    parse_registry,
    taxonomy_facts,
)

VICTIM = "VA-PROV-005"
"""The code the plan names for the re-pointing fixture. Any code would do; naming one keeps
the tests concrete about what a renumbering actually looks like."""


@pytest.fixture
def registry() -> dict[str, RegisteredCode]:
    """A fresh copy of the committed register, safe for a test to vandalise."""
    return load_registry()


@pytest.fixture
def facts() -> dict[str, CodeFacts]:
    """A fresh copy of the enum's facts, safe for a test to vandalise."""
    return taxonomy_facts()


# --- The committed register ---------------------------------------------------------------


def test_the_committed_register_agrees_with_the_enum(
    facts: dict[str, CodeFacts], registry: dict[str, RegisteredCode]
) -> None:
    """The gate itself. `S0.3.1` acceptance 5."""
    violations = check_registry(facts, registry)
    assert violations == [], format_violations(violations)


def test_the_register_file_is_committed_beside_the_enum() -> None:
    assert REGISTRY_PATH.is_file()
    assert REGISTRY_PATH.name == "codes.registry.json"


def test_every_code_in_the_enum_is_registered(registry: dict[str, RegisteredCode]) -> None:
    assert set(registry) >= {member.value for member in ErrorCode}


def test_the_register_records_where_each_code_was_issued(
    registry: dict[str, RegisteredCode],
) -> None:
    """Without a provenance field, a retired row is a number with no story attached."""
    assert all(entry.issued_in for entry in registry.values())


# --- Re-pointing --------------------------------------------------------------------------


def test_repointing_a_code_is_a_violation(
    facts: dict[str, CodeFacts], registry: dict[str, RegisteredCode]
) -> None:
    """`[D-55]` — a code's meaning never changes. This is the fixture the plan names."""
    facts[VICTIM] = CodeFacts(meaning="Something else entirely", retryability="no")

    violations = check_registry(facts, registry)

    assert [violation.kind for violation in violations] == [ViolationKind.REPOINTED]
    assert violations[0].code == VICTIM
    assert "Something else entirely" in violations[0].detail


def test_changing_retryability_is_a_violation(
    facts: dict[str, CodeFacts], registry: dict[str, RegisteredCode]
) -> None:
    """`[D-62]` — flipping `VA-PROV-009` to retryable would resurrect the retry storm."""
    original = facts["VA-PROV-009"]
    facts["VA-PROV-009"] = CodeFacts(meaning=original.meaning, retryability="yes")

    violations = check_registry(facts, registry)

    assert [violation.kind for violation in violations] == [ViolationKind.RETRYABILITY_CHANGED]


# --- Removal and retirement -----------------------------------------------------------------


def test_deleting_an_active_code_is_a_violation(
    facts: dict[str, CodeFacts], registry: dict[str, RegisteredCode]
) -> None:
    del facts[VICTIM]

    violations = check_registry(facts, registry)

    assert [violation.kind for violation in violations] == [ViolationKind.REMOVED]
    assert "retired" in violations[0].detail


def test_a_retired_code_may_be_absent_from_the_enum(
    facts: dict[str, CodeFacts], registry: dict[str, RegisteredCode]
) -> None:
    """Retirement is tolerated; that is the whole difference between retiring and deleting."""
    del facts[VICTIM]
    registry[VICTIM] = RegisteredCode(
        facts=registry[VICTIM].facts,
        status=RegistryStatus.RETIRED,
        issued_in=registry[VICTIM].issued_in,
    )

    assert check_registry(facts, registry) == []


def test_reissuing_a_retired_code_is_a_violation(
    facts: dict[str, CodeFacts], registry: dict[str, RegisteredCode]
) -> None:
    """The case the register exists for: the number was spent, and it stays spent."""
    registry[VICTIM] = RegisteredCode(
        facts=CodeFacts(meaning="An older meaning", retryability="no"),
        status=RegistryStatus.RETIRED,
        issued_in="T0.3",
    )
    facts[VICTIM] = CodeFacts(meaning="A brand new meaning", retryability="yes")

    violations = check_registry(facts, registry)

    assert [violation.kind for violation in violations] == [ViolationKind.REISSUED]


def test_reissuing_a_retired_code_with_its_original_meaning_is_still_a_violation(
    facts: dict[str, CodeFacts], registry: dict[str, RegisteredCode]
) -> None:
    """Reuse is forbidden outright, not only reuse that changes the meaning."""
    registry[VICTIM] = RegisteredCode(
        facts=facts[VICTIM],
        status=RegistryStatus.RETIRED,
        issued_in="T0.3",
    )

    violations = check_registry(facts, registry)

    assert [violation.kind for violation in violations] == [ViolationKind.REISSUED]


# --- Adding without registering ---------------------------------------------------------------


def test_a_new_code_missing_from_the_register_is_a_violation(
    facts: dict[str, CodeFacts], registry: dict[str, RegisteredCode]
) -> None:
    facts["VA-REQ-099"] = CodeFacts(meaning="Newly invented", retryability="no")

    violations = check_registry(facts, registry)

    assert [violation.kind for violation in violations] == [ViolationKind.UNREGISTERED]
    assert violations[0].code == "VA-REQ-099"


def test_violations_render_with_the_code_and_the_kind(
    facts: dict[str, CodeFacts], registry: dict[str, RegisteredCode]
) -> None:
    """An assertion message that named neither would send the reader back to the diff."""
    del facts[VICTIM]

    rendered = format_violations(check_registry(facts, registry))

    assert VICTIM in rendered
    assert ViolationKind.REMOVED.value in rendered


# --- The register file format -------------------------------------------------------------------


def test_a_register_with_the_wrong_schema_version_is_rejected() -> None:
    with pytest.raises(RegistryFormatError, match="schema_version"):
        parse_registry({"schema_version": "99", "codes": [{"code": "VA-REQ-001"}]})


def test_an_empty_register_is_rejected() -> None:
    """An empty file would turn every check above into a no-op that reports success."""
    with pytest.raises(RegistryFormatError, match="codes"):
        parse_registry({"schema_version": "1", "codes": []})


def test_a_duplicate_row_is_rejected() -> None:
    """Two rows for one code means one of them is silently ignored, whichever is second."""
    row = {
        "code": "VA-REQ-001",
        "meaning": "Invalid prompt",
        "retryability": "no",
        "status": "active",
        "issued_in": "T0.3",
    }
    with pytest.raises(RegistryFormatError, match="twice"):
        parse_registry({"schema_version": "1", "codes": [row, dict(row)]})


def test_a_row_missing_a_field_is_rejected() -> None:
    with pytest.raises(RegistryFormatError, match="malformed"):
        parse_registry({"schema_version": "1", "codes": [{"code": "VA-REQ-001"}]})


def test_a_row_with_an_unknown_status_is_rejected() -> None:
    row = {
        "code": "VA-REQ-001",
        "meaning": "Invalid prompt",
        "retryability": "no",
        "status": "probationary",
        "issued_in": "T0.3",
    }
    with pytest.raises(RegistryFormatError, match="malformed"):
        parse_registry({"schema_version": "1", "codes": [row]})


def test_a_non_object_row_is_rejected() -> None:
    with pytest.raises(RegistryFormatError, match="expected an object"):
        parse_registry({"schema_version": "1", "codes": ["VA-REQ-001"]})


def test_an_unreadable_register_is_rejected(tmp_path: Path) -> None:
    broken = tmp_path / "codes.registry.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryFormatError, match="could not be read"):
        load_registry(broken)


def test_a_register_that_is_not_an_object_is_rejected(tmp_path: Path) -> None:
    listed = tmp_path / "codes.registry.json"
    listed.write_text(json.dumps(["VA-REQ-001"]), encoding="utf-8")
    with pytest.raises(RegistryFormatError, match="not a JSON object"):
        load_registry(listed)


def test_a_missing_register_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RegistryFormatError, match="could not be read"):
        load_registry(tmp_path / "absent.json")
