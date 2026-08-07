"""Static guards for the alias-only rule, inline prompts and model branching.

`gateway.md` §9 and `providers.md` §10 both specify these as CI greps. They are implemented
here as importable functions and driven from `tests/unit/test_static_guards.py`, so they run
in `make check` on a developer's machine and in CI from the same code path — a guard that only
exists in a workflow file is a guard nobody can run before pushing.

**Why a grep is the right instrument.** `[CPS §Model routing]` says code never names a
provider, so that swapping a model is a config change with zero code diff. Documentation makes
that discouraged; this makes it fail. The rule is worth enforcing mechanically precisely
because every individual violation looks harmless — one `if model_used == "..."` is a
five-line convenience, and the sum of them is a codebase that cannot change models.

**Three matching strategies, for three different failure shapes.**

- *Vendor and video-provider names* are matched against the **raw text** of a file, so a
  comment or a docstring naming a provider is caught too. A comment that says which model a
  function is tuned for is the first step toward code that depends on it.
- *Magic Hour model names* are matched the same way, **except** those that are also ordinary
  English (`default`). Those are matched only as the **exact full value of a string literal**,
  because banning the word `default` in `src/` would be absurd while banning the literal
  `"default"` is right: a bare sentinel string should be an enum member.
- *Branching on `model_used` / `provider_key`* is an **AST** check, because the thing being
  forbidden is a comparison, not a spelling. Reading the attribute for a trace is fine and
  required; comparing it to a literal is what re-introduces provider knowledge into code.

The allow-list is deliberately tiny and lives in one constant `[D-06]`: the single Magic Hour
adapter module, and the configuration that the whole rule exists to concentrate names into.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# --- Allow-list --------------------------------------------------------------------------

ALLOWLISTED_PATHS: tuple[str, ...] = (
    # The one adapter that is allowed to know which provider it is talking to [D-06].
    "src/video_agent/providers/magichour.py",
    # Typed settings and the alias-table loader.
    "src/video_agent/config",
    # config/aliases.yaml — the only file where a concrete model name may appear.
    "config",
)

SCANNED_ROOTS: tuple[str, ...] = ("src", "config")
SCANNED_SUFFIXES: tuple[str, ...] = (".py", ".yaml", ".yml")

# --- Banned names ------------------------------------------------------------------------

VIDEO_PROVIDER_NAMES: tuple[str, ...] = ("magichour", "magic hour", "higgsfield")

LLM_VENDOR_NAMES: tuple[str, ...] = (
    "openai",
    "anthropic",
    "claude",
    "gemini",
    "gpt-",
    "bedrock",
    "vertexai",
    "mistral",
    "cohere",
    "deepseek",
    "llama",
    "grok",
    "qwen",
)

MAGICHOUR_MODEL_NAMES: tuple[str, ...] = (
    "default",
    "ltx-2",
    "ltx-2.3",
    "wan-2.2",
    "seedance-1.5",
    "seedance-2.0",
    "seedance-2.0-mini",
    "kling-2.5",
    "kling-3.0",
    "veo3.1",
    "veo3.1-lite",
    "sora-2",
    "kling-1.6",
    "seedance",
    "kling-2.5-audio",
    "veo3.1-audio",
)
"""The `model` enum from `providers.md` §9, verbatim. All sixteen, including the ones v1
never selects: the rule is that code does not name a *model*, not that code does not name
*this* model."""

AMBIGUOUS_MODEL_NAMES: frozenset[str] = frozenset({"default"})
"""Model names that are also ordinary English. Matched as exact string-literal values only."""

BANNED_NAMES: tuple[str, ...] = VIDEO_PROVIDER_NAMES + LLM_VENDOR_NAMES + MAGICHOUR_MODEL_NAMES

# --- Inline-prompt and branch rules -------------------------------------------------------

MAX_INLINE_LITERAL_CHARS = 200
MIN_PROMPT_NEWLINES = 2
NON_PROMPT_LITERAL_ALLOWLIST: tuple[str, ...] = ()
"""Files holding a long multi-line literal that is provably not a prompt. Empty, and an entry
added here needs a stated reason: prompts belong in the registry `[D-72]`, by name and
version, so a trace can say which prompt produced an output."""

BRANCHABLE_ATTRIBUTES: frozenset[str] = frozenset({"model_used", "provider_key"})


@dataclass(frozen=True, slots=True)
class Violation:
    """One offending location. Carries the file and line, never just a boolean."""

    path: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.detail}"


def format_violations(violations: Iterable[Violation]) -> str:
    """Render violations one per line for an assertion message."""
    return "\n".join(str(violation) for violation in violations)


# --- File discovery ------------------------------------------------------------------------


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def is_allowlisted(relative_path: str) -> bool:
    """Whether `relative_path` (repo-relative, POSIX) is exempt from the provider-name rule."""
    candidate = PurePosixPath(relative_path)
    for allowed in ALLOWLISTED_PATHS:
        allowed_path = PurePosixPath(allowed)
        if candidate == allowed_path or allowed_path in candidate.parents:
            return True
    return False


def scanned_files(root: Path, suffixes: tuple[str, ...] = SCANNED_SUFFIXES) -> Iterator[Path]:
    """Every file under the scanned roots with a scanned suffix, in a stable order."""
    for root_name in SCANNED_ROOTS:
        base = root / root_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                yield path


def _python_files(root: Path) -> Iterator[Path]:
    yield from scanned_files(root, suffixes=(".py",))


def _parse(path: Path) -> ast.Module | None:
    """Parse `path`, returning None if it is not valid Python. A syntax error is ruff's job."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return None


# --- Guard 1: no provider or model name outside the adapter and config ---------------------


def _token_pattern(name: str) -> re.Pattern[str]:
    """Compile `name` as a whole-token pattern.

    Boundaries are applied only at ends that are alphanumeric, so `gpt-` still matches
    `gpt-4o` while `gemini` does not match a longer word that merely contains it.
    """
    prefix = r"(?<![A-Za-z0-9])" if name[0].isalnum() else ""
    suffix = r"(?![A-Za-z0-9])" if name[-1].isalnum() else ""
    return re.compile(prefix + re.escape(name) + suffix, re.IGNORECASE)


_TEXT_SCANNED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, _token_pattern(name)) for name in BANNED_NAMES if name not in AMBIGUOUS_MODEL_NAMES
)


def _text_leaks(path: Path, relative: str) -> Iterator[Violation]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        found = sorted({name for name, pattern in _TEXT_SCANNED_PATTERNS if pattern.search(line)})
        if found:
            yield Violation(
                path=relative,
                line=line_number,
                detail=(
                    f"provider or model name {', '.join(repr(name) for name in found)} "
                    f"outside the adapter module and config/. Use a logical alias; put the "
                    f"model name in config/aliases.yaml."
                ),
            )


def _ambiguous_literal_leaks(tree: ast.Module, relative: str) -> Iterator[Violation]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.value.strip().lower() in AMBIGUOUS_MODEL_NAMES:
            yield Violation(
                path=relative,
                line=node.lineno,
                detail=(
                    f"string literal {node.value!r} is a provider model name. A sentinel "
                    f"belongs in an enum; a model name belongs in config/aliases.yaml."
                ),
            )


def find_provider_name_leaks(root: Path) -> list[Violation]:
    """Every place under `src/` or `config/` naming a provider or model outside the allow-list."""
    violations: list[Violation] = []
    for path in scanned_files(root):
        relative = _relative_posix(path, root)
        if is_allowlisted(relative):
            continue
        violations.extend(_text_leaks(path, relative))
        if path.suffix == ".py":
            tree = _parse(path)
            if tree is not None:
                violations.extend(_ambiguous_literal_leaks(tree, relative))
    return sorted(violations, key=lambda violation: (violation.path, violation.line))


# --- Guard 2: no raw prompt string literal in application code -----------------------------


def _documentation_nodes(tree: ast.Module) -> set[int]:
    """Ids of the string `Constant` nodes that are documentation, and so exempt.

    A string that is a bare expression statement is discarded at runtime: module, class and
    function docstrings, and the PEP 257 attribute docstrings this codebase uses under module
    constants. A prompt, by contrast, is always *used* — assigned, passed or returned — so
    exempting expression statements costs the check nothing and spares it from flagging the
    house documentation style.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def find_inline_prompts(root: Path) -> list[Violation]:
    """Long multi-line string literals in `src/`, which is what a pasted prompt looks like.

    Prompts come from the registry by name and version `[D-72]`, `[CPS §Observability]`; a
    literal in code has no version, so a trace cannot say what was actually sent.
    """
    violations: list[Violation] = []
    for path in _python_files(root):
        relative = _relative_posix(path, root)
        if relative in NON_PROMPT_LITERAL_ALLOWLIST:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        exempt = _documentation_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in exempt:
                continue
            newlines = node.value.count("\n")
            if len(node.value) > MAX_INLINE_LITERAL_CHARS and newlines >= MIN_PROMPT_NEWLINES:
                violations.append(
                    Violation(
                        path=relative,
                        line=node.lineno,
                        detail=(
                            f"string literal of {len(node.value)} characters over "
                            f"{newlines + 1} lines looks like an inline prompt. Register it "
                            f"and reference it by name and version."
                        ),
                    )
                )
    return sorted(violations, key=lambda violation: (violation.path, violation.line))


# --- Guard 3: no branch on model_used or provider_key --------------------------------------


def _is_branchable_reference(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr in BRANCHABLE_ATTRIBUTES:
        return node.attr
    if isinstance(node, ast.Name) and node.id in BRANCHABLE_ATTRIBUTES:
        return node.id
    return None


def _is_string_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return bool(node.elts) and all(_is_string_literal(element) for element in node.elts)
    return False


def _compare_violations(node: ast.Compare, relative: str) -> Iterator[Violation]:
    operands = [node.left, *node.comparators]
    references = [name for operand in operands if (name := _is_branchable_reference(operand))]
    if not references:
        return
    literals = [operand for operand in operands if _is_string_literal(operand)]
    if not literals:
        return
    yield Violation(
        path=relative,
        line=node.lineno,
        detail=(
            f"{references[0]} is compared against a string literal. It exists for "
            f"observability only; branching on it puts provider knowledge back into code. "
            f"Branch on a capability or an alias instead."
        ),
    )


def _match_violations(node: ast.Match, relative: str) -> Iterator[Violation]:
    reference = _is_branchable_reference(node.subject)
    if reference is None:
        return
    for case in node.cases:
        for pattern in ast.walk(case.pattern):
            if isinstance(pattern, ast.MatchValue) and _is_string_literal(pattern.value):
                yield Violation(
                    path=relative,
                    line=case.pattern.lineno,
                    detail=(
                        f"{reference} is matched against a string literal, which is a "
                        f"comparison in another spelling. Branch on a capability or an alias."
                    ),
                )
                break


def find_model_branches(root: Path) -> list[Violation]:
    """Every comparison of `model_used` or `provider_key` against a literal under `src/`."""
    violations: list[Violation] = []
    for path in _python_files(root):
        relative = _relative_posix(path, root)
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                violations.extend(_compare_violations(node, relative))
            elif isinstance(node, ast.Match):
                violations.extend(_match_violations(node, relative))
    return sorted(violations, key=lambda violation: (violation.path, violation.line))
