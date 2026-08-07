"""Expand/contract encoded as a lint, so a violating migration fails a test not a review.

`[CPS §Rollout]` and `AGENT.md` §4 give five hard rules for migrations. All five are the kind
of rule that reads as obvious and is broken anyway, because the violating diff is always small
and always looks like a tidy-up: one `RENAME`, one `DROP COLUMN` in the release that stopped
writing it, one `CREATE INDEX` on a table with forty million rows. Each of those is a deploy
that either takes the site down or cannot be rolled back, and none of them looks dangerous in
a pull request.

So the rules are checked mechanically, against the **SQL the migration actually emits** rather
than against the Python that emits it. Alembic's offline mode (`--sql`) renders the whole
upgrade path to text without connecting to anything, which means this check runs in CI with no
database and cannot be defeated by wrapping the DDL in a helper function.

**Upgrade SQL only.** A downgrade of an expand revision drops what the expand added — that is
what makes it a tested rollback, not a violation — so linting the downgrade path would reject
every correct migration in the tree.

**The lock budget.** A migration that waits on a lock is worse than one that fails: it queues
every subsequent query behind itself and takes the service down while reporting that it is
still working. `lock_timeout` makes PostgreSQL abort the statement instead, so the deploy
fails and rolls back. `require_lock_budget` asserts the emitted script sets one before it
touches a table.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

PHASE_FIELD = "Phase"
"""The required docstring field. `Phase: expand` — parsed, not read by a human."""


class Phase(StrEnum):
    """Which of the three expand/contract steps a revision is.

    The phase is what makes "never drop in the same release that stops writing" checkable:
    a drop is legal only in a `contract` revision, and a `contract` revision may contain
    nothing but drops. A revision that both adds and drops is, by construction, a release
    that changed the write path and removed the old shape at the same time.
    """

    EXPAND = "expand"
    MIGRATE = "migrate"
    CONTRACT = "contract"


class MigrationLintError(ValueError):
    """A revision that cannot be linted at all, as opposed to one that fails a rule."""


@dataclass(frozen=True, slots=True)
class LintFinding:
    """One rule broken by one statement, named well enough to fix without asking."""

    revision: str
    rule: str
    detail: str
    statement: str

    def __str__(self) -> str:
        return f"{self.revision} [{self.rule}]: {self.detail}\n    {self.statement}"


def format_findings(findings: Iterable[LintFinding]) -> str:
    """Render findings for an assertion message that says which revision and which rule."""
    return "\n".join(str(finding) for finding in findings)


# --- Phase declaration ---------------------------------------------------------------------

_PHASE_PATTERN = re.compile(rf"^\s*{PHASE_FIELD}\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_phase(docstring: str | None) -> Phase:
    """Read the `Phase:` field out of a revision docstring.

    Raises rather than defaulting. A default would be `expand`, which is the permissive
    phase, so a revision whose author forgot the field would be granted the loosest rules —
    exactly backwards.
    """
    if not docstring:
        message = f"revision has no docstring, so it declares no {PHASE_FIELD}"
        raise MigrationLintError(message)
    match = _PHASE_PATTERN.search(docstring)
    if match is None:
        valid = ", ".join(phase.value for phase in Phase)
        message = f"revision docstring has no `{PHASE_FIELD}:` field; expected one of {valid}"
        raise MigrationLintError(message)
    raw = match.group(1).strip().lower()
    try:
        return Phase(raw)
    except ValueError as exc:
        valid = ", ".join(phase.value for phase in Phase)
        message = f"{PHASE_FIELD} {raw!r} is not one of {valid}"
        raise MigrationLintError(message) from exc


# --- Statement splitting -------------------------------------------------------------------


class _Splitter:
    """A character-at-a-time scanner that knows what a `;` means where it finds one.

    Four regions where a semicolon is data rather than a terminator: a dollar-quoted body, a
    single-quoted literal, a line comment and a block comment. Each is a small method, so the
    rule for each is readable on its own and the dispatch below reads as the list of regions.
    """

    def __init__(self, sql: str) -> None:
        self._sql = sql
        self._index = 0
        self._current: list[str] = []
        self._statements: list[str] = []
        self._dollar_tag: str | None = None
        self._in_single_quote = False
        self._in_line_comment = False
        self._in_block_comment = False

    def run(self) -> list[str]:
        """Scan the whole script and return its statements, comments stripped."""
        while self._index < len(self._sql):
            self._step()
        self._end_statement()
        return [cleaned for cleaned in (_strip(raw) for raw in self._statements) if cleaned]

    def _step(self) -> None:
        if self._in_line_comment:
            self._scan_line_comment()
        elif self._in_block_comment:
            self._scan_block_comment()
        elif self._dollar_tag is not None:
            self._scan_dollar_body()
        elif self._in_single_quote:
            self._scan_single_quote()
        else:
            self._scan_code()

    # -- regions where a semicolon is data --------------------------------------------------

    def _scan_line_comment(self) -> None:
        if self._char() == "\n":
            self._in_line_comment = False
        self._take(1)

    def _scan_block_comment(self) -> None:
        if self._pair() == "*/":
            self._in_block_comment = False
            self._take(2)
        else:
            self._take(1)

    def _scan_dollar_body(self) -> None:
        tag = self._dollar_tag
        assert tag is not None
        if self._sql.startswith(tag, self._index):
            self._dollar_tag = None
            self._take(len(tag))
        else:
            self._take(1)

    def _scan_single_quote(self) -> None:
        if self._char() == "'":
            self._in_single_quote = False
        self._take(1)

    # -- ordinary SQL -----------------------------------------------------------------------

    def _scan_code(self) -> None:
        pair = self._pair()
        if pair == "--":
            self._in_line_comment = True
            self._take(2)
        elif pair == "/*":
            self._in_block_comment = True
            self._take(2)
        elif self._char() == "'":
            self._in_single_quote = True
            self._take(1)
        elif self._char() == ";":
            self._end_statement()
            self._index += 1
        else:
            self._scan_word()

    def _scan_word(self) -> None:
        tag = _dollar_tag_at(self._sql, self._index) if self._char() == "$" else None
        if tag is None:
            self._take(1)
        else:
            self._dollar_tag = tag
            self._take(len(tag))

    # -- cursor -----------------------------------------------------------------------------

    def _char(self) -> str:
        return self._sql[self._index]

    def _pair(self) -> str:
        return self._sql[self._index : self._index + 2]

    def _take(self, count: int) -> None:
        self._current.append(self._sql[self._index : self._index + count])
        self._index += count

    def _end_statement(self) -> None:
        self._statements.append("".join(self._current))
        self._current = []


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements, respecting dollar quoting and comments.

    Naive splitting on `;` cuts the immutability trigger in half — its `plpgsql` body contains
    two of them — and a linter that mangles the one statement in the schema that enforces an
    invariant is worse than no linter.
    """
    return _Splitter(sql).run()


_DOLLAR_TAG_PATTERN = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _dollar_tag_at(sql: str, index: int) -> str | None:
    match = _DOLLAR_TAG_PATTERN.match(sql, index)
    return match.group(0) if match else None


def _strip(statement: str) -> str:
    """Drop comment lines and collapse whitespace, so the rules match on one canonical form."""
    without_line_comments = re.sub(r"--[^\n]*", " ", statement)
    without_block_comments = re.sub(r"/\*.*?\*/", " ", without_line_comments, flags=re.DOTALL)
    return re.sub(r"\s+", " ", without_block_comments).strip()


# --- Revision splitting --------------------------------------------------------------------

_RUNNING_UPGRADE = re.compile(r"^--\s*Running upgrade\s*(\S*)\s*->\s*(\S+)", re.MULTILINE)


def split_revisions(sql: str) -> list[tuple[str, str]]:
    """Split an offline `alembic upgrade --sql` script into `(revision, sql)` pairs.

    Alembic writes a `-- Running upgrade <from> -> <to>` banner before each revision's
    statements, so the boundaries are in the output rather than having to be reconstructed.
    """
    markers = list(_RUNNING_UPGRADE.finditer(sql))
    sections: list[tuple[str, str]] = []
    for position, marker in enumerate(markers):
        start = marker.end()
        end = markers[position + 1].start() if position + 1 < len(markers) else len(sql)
        sections.append((marker.group(2), sql[start:end]))
    return sections


# --- The rules -----------------------------------------------------------------------------

_CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", re.IGNORECASE)
_ADD_COLUMN = re.compile(
    r"\bALTER\s+TABLE\s+(?:ONLY\s+)?([\w.\"]+)\s+ADD\s+COLUMN\b", re.IGNORECASE
)
_RENAME = re.compile(r"\bALTER\s+(?:TABLE|INDEX|TYPE)\b.*\bRENAME\b", re.IGNORECASE)
_CREATE_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?"
    r"(?:IF\s+NOT\s+EXISTS\s+)?[\w.\"]+\s+ON\s+(?:ONLY\s+)?([\w.\"]+)",
    re.IGNORECASE,
)
_ADD_CONSTRAINT = re.compile(
    r"\bALTER\s+TABLE\s+(?:ONLY\s+)?([\w.\"]+)\s+ADD\s+CONSTRAINT\b", re.IGNORECASE
)
_VALIDATABLE_CONSTRAINT = re.compile(r"\b(CHECK|FOREIGN\s+KEY)\b", re.IGNORECASE)
_DROP = re.compile(
    r"\b(?:DROP\s+(?:TABLE|TYPE|INDEX|VIEW|TRIGGER|FUNCTION|SCHEMA|SEQUENCE)"
    r"|ALTER\s+TABLE\b[^;]*\bDROP\s+(?:COLUMN|CONSTRAINT))\b",
    re.IGNORECASE,
)
_NOT_NULL = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
_DEFAULT_CLAUSE = re.compile(r"\bDEFAULT\b", re.IGNORECASE)
_SET_NOT_NULL = re.compile(r"\bALTER\s+(?:COLUMN\s+)?[\w\".]+\s+SET\s+NOT\s+NULL\b", re.IGNORECASE)
_LOCK_TIMEOUT = re.compile(r"\bSET\b[^;]*\block_timeout\b", re.IGNORECASE)

_ALEMBIC_BOOKKEEPING = re.compile(r"\b(?:alembic_version|BEGIN|COMMIT)\b", re.IGNORECASE)


def _table_of(match: re.Match[str] | None, group: int = 1) -> str:
    return match.group(group).strip('"').lower() if match else ""


def lint_statements(revision: str, phase: Phase, statements: Sequence[str]) -> list[LintFinding]:
    """Apply the five expand/contract rules to one revision's emitted statements."""
    created = {_table_of(_CREATE_TABLE.search(statement)) for statement in statements} - {""}
    findings: list[LintFinding] = []
    for statement in statements:
        findings.extend(_statement_findings(revision, phase, statement, created))
    return findings


def _statement_findings(
    revision: str, phase: Phase, statement: str, created: set[str]
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    findings.extend(_not_null_findings(revision, statement))
    findings.extend(_rename_findings(revision, statement))
    findings.extend(_drop_findings(revision, phase, statement))
    findings.extend(_index_findings(revision, statement, created))
    findings.extend(_constraint_findings(revision, statement, created))
    findings.extend(_contract_purity_findings(revision, phase, statement))
    return findings


def _not_null_findings(revision: str, statement: str) -> list[LintFinding]:
    if _ADD_COLUMN.search(statement) and _NOT_NULL.search(statement):
        if _DEFAULT_CLAUSE.search(statement):
            return []
        return [
            LintFinding(
                revision,
                "not-null-without-default",
                "adds a NOT NULL column with no DEFAULT; every existing row fails the "
                "constraint the moment the statement runs. Add it nullable or with a "
                "default, backfill, then SET NOT NULL in a later revision.",
                statement,
            )
        ]
    if _SET_NOT_NULL.search(statement) and "not valid" not in statement.lower():
        return [
            LintFinding(
                revision,
                "not-null-without-default",
                "SET NOT NULL rewrites and exclusively locks the whole table. Add a "
                "NOT VALID CHECK, VALIDATE it, then SET NOT NULL.",
                statement,
            )
        ]
    return []


def _rename_findings(revision: str, statement: str) -> list[LintFinding]:
    if _RENAME.search(statement):
        return [
            LintFinding(
                revision,
                "rename-in-place",
                "renames in place, so the old code and the new schema cannot both be live. "
                "Add the new name, dual-write, backfill, then drop in a contract revision.",
                statement,
            )
        ]
    return []


def _drop_findings(revision: str, phase: Phase, statement: str) -> list[LintFinding]:
    if _DROP.search(statement) and phase is not Phase.CONTRACT:
        return [
            LintFinding(
                revision,
                "drop-outside-contract",
                f"drops in a {phase.value} revision. A drop belongs in a separate contract "
                f"deploy, after the code that stopped writing the old shape is fully rolled "
                f"out; dropping in the same release makes the rollback lossy.",
                statement,
            )
        ]
    return []


def _contract_purity_findings(revision: str, phase: Phase, statement: str) -> list[LintFinding]:
    if phase is not Phase.CONTRACT:
        return []
    if _DROP.search(statement) or _ALEMBIC_BOOKKEEPING.search(statement):
        return []
    return [
        LintFinding(
            revision,
            "contract-revision-is-not-only-drops",
            "a contract revision may contain only drops. Mixing an addition into it means "
            "one deploy both changed the write path and removed the old shape.",
            statement,
        )
    ]


def _index_findings(revision: str, statement: str, created: set[str]) -> list[LintFinding]:
    match = _CREATE_INDEX.search(statement)
    if match is None or match.group(1):
        return []
    if _table_of(match, group=2) in created:
        return []
    return [
        LintFinding(
            revision,
            "index-not-concurrent",
            "CREATE INDEX without CONCURRENTLY takes a write lock for the whole build. "
            "Allowed only on a table created in the same revision, which is empty and "
            "which nothing else can be waiting on.",
            statement,
        )
    ]


def _constraint_findings(revision: str, statement: str, created: set[str]) -> list[LintFinding]:
    match = _ADD_CONSTRAINT.search(statement)
    if match is None or not _VALIDATABLE_CONSTRAINT.search(statement):
        return []
    if _table_of(match) in created or "not valid" in statement.lower():
        return []
    return [
        LintFinding(
            revision,
            "constraint-without-not-valid",
            "adds a CHECK or FOREIGN KEY that scans and locks the whole table. Add it "
            "NOT VALID, then VALIDATE CONSTRAINT in a separate statement.",
            statement,
        )
    ]


def require_lock_budget(sql: str) -> list[LintFinding]:
    """Assert the script sets a lock timeout before it touches anything.

    Checked on the whole script rather than per revision: `lock_timeout` is a session setting
    and one `SET` at the top governs every revision that follows, which is also the only place
    it can be set for a script that is piped into `psql`.
    """
    statements = split_statements(sql)
    for statement in statements:
        if _LOCK_TIMEOUT.search(statement):
            return []
        if _CREATE_TABLE.search(statement) or _ADD_COLUMN.search(statement):
            break
    return [
        LintFinding(
            "<script>",
            "no-lock-budget",
            "the migration script sets no lock_timeout, so a statement that cannot take "
            "its lock waits behind whatever holds it and queues every query after it. "
            "A migration that exceeds its lock budget must abort, not block.",
            statements[0] if statements else "",
        )
    ]


def lint_migration_script(sql: str, phases: Mapping[str, Phase]) -> list[LintFinding]:
    """Lint a whole offline upgrade script, revision by revision.

    `phases` maps revision identifier to declared phase; a revision missing from it is itself
    a finding, because an undeclared phase means the rules cannot be applied and "cannot be
    checked" must not read the same as "passed".
    """
    findings = require_lock_budget(sql)
    for revision, section in split_revisions(sql):
        phase = phases.get(revision)
        if phase is None:
            findings.append(
                LintFinding(
                    revision,
                    "undeclared-phase",
                    f"no `{PHASE_FIELD}:` was declared for this revision, so expand/contract "
                    f"cannot be checked against it.",
                    "",
                )
            )
            continue
        findings.extend(lint_statements(revision, phase, split_statements(section)))
    return findings
