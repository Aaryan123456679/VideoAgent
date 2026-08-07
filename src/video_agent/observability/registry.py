"""The historical register of every error code ever issued, and the checks that police it.

`[D-55]` gives two rules — a code's meaning never changes, a retired code is never reused —
and the enum alone cannot enforce either, because an enum only knows its present contents. A
developer who deletes `VA-PROV-005` and later adds a new `VA-PROV-005` for something else
leaves the enum perfectly self-consistent and every runbook, alert and support ticket quoting
the old code silently wrong.

`codes.registry.json` is the memory the enum lacks. It is **append-only**: a code enters it
when it is first issued and never leaves. Retiring a code means flipping its `status` to
`retired`, which records that the number is spent — not that it is available.

The five things this module refuses:

- **`unregistered`** — a code is in the enum but not in the register. The register would stop
  being complete, and the next check would have nothing to compare against.
- **`removed`** — a code is `active` in the register but gone from the enum. Deleting a code
  erases the meaning support is still quoting; retire it instead.
- **`reissued`** — a `retired` code is back in the enum. This is the reuse `[D-55]` forbids.
- **`repointed`** — the meanings differ. A code that changes meaning is a code support cannot
  act on, which is the whole reason the taxonomy promises stability.
- **`retryability_changed`** — the flags differ. `[D-62]` exists because this flag decides
  whether a failure is retried or escalated, so changing it changes what the code means.

Every check is a pure function over two mappings, so the negative tests can drive it with a
fabricated registry rather than by vandalising the committed one — a guard that has only ever
been observed to pass is a guard nobody can trust.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from video_agent.observability.codes import ErrorCode

REGISTRY_PATH: Path = Path(__file__).with_name("codes.registry.json")
"""The committed register. Lives beside the enum so the two are read together."""

REGISTRY_SCHEMA_VERSION = "1"


class RegistryStatus(StrEnum):
    """Whether a registered code is in the enum today or has been spent."""

    ACTIVE = "active"
    RETIRED = "retired"


class ViolationKind(StrEnum):
    """The five ways the enum and the register can disagree."""

    UNREGISTERED = "unregistered"
    REMOVED = "removed"
    REISSUED = "reissued"
    REPOINTED = "repointed"
    RETRYABILITY_CHANGED = "retryability_changed"


@dataclass(frozen=True, slots=True)
class CodeFacts:
    """The facts about a code that may never change once it is issued."""

    meaning: str
    retryability: str


@dataclass(frozen=True, slots=True)
class RegisteredCode:
    """One row of the register: the immutable facts, plus whether the code is still in use."""

    facts: CodeFacts
    status: RegistryStatus
    issued_in: str


@dataclass(frozen=True, slots=True)
class RegistryViolation:
    """One disagreement between the enum and the register, named and explained."""

    code: str
    kind: ViolationKind
    detail: str

    def __str__(self) -> str:
        return f"{self.code} [{self.kind}]: {self.detail}"


def format_violations(violations: Iterable[RegistryViolation]) -> str:
    """Render violations one per line for an assertion message."""
    return "\n".join(str(violation) for violation in violations)


class RegistryFormatError(ValueError):
    """`codes.registry.json` is unreadable, so no check can be trusted to have run."""


def taxonomy_facts() -> dict[str, CodeFacts]:
    """The current enum, flattened to the same shape the register stores."""
    return {
        member.value: CodeFacts(meaning=member.meaning, retryability=member.retryability.value)
        for member in ErrorCode
    }


def _parse_row(row: object) -> tuple[str, RegisteredCode]:
    if not isinstance(row, dict):
        message = f"registry row is {type(row).__name__}, expected an object"
        raise RegistryFormatError(message)
    try:
        code = str(row["code"])
        registered = RegisteredCode(
            facts=CodeFacts(meaning=str(row["meaning"]), retryability=str(row["retryability"])),
            status=RegistryStatus(row["status"]),
            issued_in=str(row["issued_in"]),
        )
    except (KeyError, ValueError) as exc:
        message = f"registry row {row!r} is malformed: {exc}"
        raise RegistryFormatError(message) from exc
    return code, registered


def parse_registry(document: Mapping[str, Any]) -> dict[str, RegisteredCode]:
    """Validate and flatten a parsed `codes.registry.json` document.

    Fails loudly rather than returning an empty register: an unreadable file that quietly
    yields no rows would turn every check in this module into a no-op, which is the one
    failure mode a guard must not have.
    """
    version = document.get("schema_version")
    if version != REGISTRY_SCHEMA_VERSION:
        message = f"registry schema_version is {version!r}, expected {REGISTRY_SCHEMA_VERSION!r}"
        raise RegistryFormatError(message)
    rows = document.get("codes")
    if not isinstance(rows, list) or not rows:
        message = "registry has no `codes` list, or it is empty"
        raise RegistryFormatError(message)
    registry: dict[str, RegisteredCode] = {}
    for row in rows:
        code, registered = _parse_row(row)
        if code in registry:
            message = f"{code} appears twice in the register"
            raise RegistryFormatError(message)
        registry[code] = registered
    return registry


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, RegisteredCode]:
    """Read and validate the committed register."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        message = f"{path} could not be read as JSON: {exc}"
        raise RegistryFormatError(message) from exc
    if not isinstance(document, dict):
        message = f"{path} is not a JSON object"
        raise RegistryFormatError(message)
    return parse_registry(document)


def check_registry(
    current: Mapping[str, CodeFacts],
    registry: Mapping[str, RegisteredCode],
) -> list[RegistryViolation]:
    """Every way `current` breaks the append-only promise recorded in `registry`."""
    violations: list[RegistryViolation] = []
    for code in sorted(set(current) | set(registry)):
        facts = current.get(code)
        registered = registry.get(code)
        if registered is None:
            violations.append(
                RegistryViolation(
                    code=code,
                    kind=ViolationKind.UNREGISTERED,
                    detail=(
                        "in the enum but not in codes.registry.json. Append it there in the "
                        "same commit that introduces it."
                    ),
                )
            )
        elif facts is None:
            violations.extend(_check_absent(code, registered))
        else:
            violations.extend(_check_present(code, facts, registered))
    return violations


def _check_absent(code: str, registered: RegisteredCode) -> Iterable[RegistryViolation]:
    if registered.status is RegistryStatus.ACTIVE:
        yield RegistryViolation(
            code=code,
            kind=ViolationKind.REMOVED,
            detail=(
                f"registered as active in {registered.issued_in} but absent from the enum. A "
                f"code is retired, never deleted: set its status to "
                f"{RegistryStatus.RETIRED.value!r}."
            ),
        )


def _check_present(
    code: str,
    facts: CodeFacts,
    registered: RegisteredCode,
) -> Iterable[RegistryViolation]:
    if registered.status is RegistryStatus.RETIRED:
        yield RegistryViolation(
            code=code,
            kind=ViolationKind.REISSUED,
            detail=(
                "was retired and has been reissued. [D-55] forbids reusing a retired code; "
                "allocate the next unused number in the domain instead."
            ),
        )
        return
    if facts.meaning != registered.facts.meaning:
        yield RegistryViolation(
            code=code,
            kind=ViolationKind.REPOINTED,
            detail=(
                f"means {facts.meaning!r} in the enum but was issued as "
                f"{registered.facts.meaning!r}. A code's meaning never changes."
            ),
        )
    if facts.retryability != registered.facts.retryability:
        yield RegistryViolation(
            code=code,
            kind=ViolationKind.RETRYABILITY_CHANGED,
            detail=(
                f"is {facts.retryability!r} in the enum but was issued as "
                f"{registered.facts.retryability!r}. Retryability drives whether a failure is "
                f"retried or escalated; changing it changes what the code means."
            ),
        )
