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
    # The typed settings module, which pins the video model as a configurable default.
    "src/video_agent/config/settings.py",
    # The alias table itself, which is the whole point of the rule.
    "config/aliases.yaml",
)
"""Every path exempt from the provider-name rule, by **exact file**, never by directory.

`AGENT.md` §2 says of this guard: *do not add an exclusion to it*. A directory entry is an
exclusion that has not been added yet — `src/video_agent/config` exempted every file under it,
current and future, so a module dropped into that package next month would inherit an exemption
nobody granted it. Three exact paths, and `test_the_allowlist_is_pinned_to_exactly_three_files`
pins the tuple itself: asserting behaviour on a handful of sample paths let a fourth entry be
added silently, which is how the directory entry survived review in the first place.
"""

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


# --- Guard 4: no `print`, and no logger obtained outside the observability module ------------

STRUCTURED_LOGGING_MODULE = "src/video_agent/observability/logging.py"
"""The one module that may touch `logging` directly. `observability.md` §11 requires that no
`print` and no unstructured logger exists in the tree; a single accessor is what makes the rest
of the rule checkable, because "unstructured" is otherwise a judgement call."""

STDIO_STREAMS: frozenset[str] = frozenset({"stdout", "stderr"})

PRE_LOGGING_BOOTSTRAP_STREAM: str = "stderr"
"""The one stream the bootstrap exemption covers.

`stdout` is the application's data channel and, in a container, the log stream the collector
reads; a bare sentence there is indistinguishable from a log line and is what breaks a JSON
parser. A diagnostic before logging exists belongs on `stderr` and nowhere else.
"""

PRE_LOGGING_BOOTSTRAP_PATHS: tuple[str, ...] = ("src/video_agent/__main__.py",)
"""Files that may write to `sys.stderr` because they run before logging can be configured.

Exactly one, and the exemption is architectural rather than temporary. The startup preflight's
first assertion is the media-toolchain pin, which must hold *even when configuration is
unreadable* — and `configure_logging` needs `Settings`. A process refusing to start because it
cannot read its own configuration has no `Settings`, no trace and no sink; a stderr sentence is
the only thing it can honestly emit.

The exemption is narrow in three directions, and `tests/unit/test_logging_guards.py` pins each.
It covers `sys.stderr.write` only, and `PRE_LOGGING_BOOTSTRAP_STREAM` is what makes that true
rather than merely stated — the docstring said `stderr` while the check matched both streams,
so `sys.stdout.write` could be added to the exempt file with every test still green. `print`
remains banned here like everywhere else. And the tuple is pinned, so a second module cannot
inherit the exemption by living in the same package.
"""

UNSTRUCTURED_LOGGING_CALLS: frozenset[str] = frozenset(
    {
        "getLogger",
        "basicConfig",
        "log",
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "fatal",
    }
)
"""Attributes of the `logging` module that either mint a logger or write to the root one.

`logging.info(...)` is the quieter half of the problem: it calls `basicConfig` implicitly, so
the first module to use it decides the format for the whole process — usually to plain text,
usually long before `configure_logging` runs."""


def _stdio_write_stream(node: ast.Call) -> str | None:
    """Which standard stream this call writes to, or `None` if it is not such a call.

    Returns the stream *name* rather than a boolean because the bootstrap exemption is about
    one specific stream, and a boolean cannot carry that. Collapsing the two into "is a stdio
    write" is what let the exemption cover `sys.stdout.write` while its docstring said stderr.
    """
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "write":
        return None
    stream = func.value
    if (
        isinstance(stream, ast.Attribute)
        and stream.attr in STDIO_STREAMS
        and isinstance(stream.value, ast.Name)
        and stream.value.id == "sys"
    ):
        return stream.attr
    return None


def is_exempt_bootstrap_write(relative_path: str, stream: str) -> bool:
    """Whether `relative_path` may write to `stream` because it predates logging."""
    return stream == PRE_LOGGING_BOOTSTRAP_STREAM and relative_path in PRE_LOGGING_BOOTSTRAP_PATHS


def find_print_calls(root: Path) -> list[Violation]:
    """Every `print()` or `sys.stdout.write()` under `src/`.

    `AGENT.md` §3 and `observability.md` §4: output is JSON on one line with a `trace_id`, and
    a `print` is none of those. It also bypasses levels, filters and redaction — the last of
    which is why this is a hard gate rather than a style preference.
    """
    violations: list[Violation] = []
    for path in _python_files(root):
        relative = _relative_posix(path, root)
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_print = isinstance(node.func, ast.Name) and node.func.id == "print"
            stream = _stdio_write_stream(node)
            is_stdio = stream is not None and not is_exempt_bootstrap_write(relative, stream)
            if is_print or is_stdio:
                violations.append(
                    Violation(
                        path=relative,
                        line=node.lineno,
                        detail=(
                            "writes to standard output directly. Use "
                            "`video_agent.observability.get_logger(__name__)`; a print carries "
                            "no trace_id and passes through no redaction."
                        ),
                    )
                )
    return sorted(violations, key=lambda violation: (violation.path, violation.line))


def _logging_call_name(node: ast.Call, logging_aliases: set[str]) -> str | None:
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in logging_aliases
        and func.attr in UNSTRUCTURED_LOGGING_CALLS
    ):
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name) and func.id in logging_aliases:
        return func.id
    return None


def _binds_logging(alias: ast.alias) -> str | None:
    """The local name an `import` statement binds to the `logging` package, if any.

    `import logging.handlers` binds the name `logging` — the *package*, with the submodule
    attached — so `logging.getLogger(...)` on the next line works exactly as it would after
    `import logging`. Matching only `alias.name == "logging"` recorded no binding for that
    spelling, and the guard then found nothing to check in the file at all.
    """
    if alias.name == "logging":
        return alias.asname or alias.name
    if alias.name.startswith("logging."):
        return alias.asname or alias.name.partition(".")[0]
    return None


def _logging_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to the `logging` module, and names bound to `logging.getLogger` itself."""
    modules: set[str] = set()
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                bound for alias in node.names if (bound := _binds_logging(alias)) is not None
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "logging":
            functions.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in UNSTRUCTURED_LOGGING_CALLS
            )
    return modules, functions


def find_unstructured_loggers(root: Path) -> list[Violation]:
    """Every direct use of the stdlib `logging` API outside the one module that owns it.

    Matching on the *binding* rather than the spelling, so `import logging as stdlib_logging`
    and `from logging import getLogger` are caught alongside the obvious form. The exemption
    is a single path, not a pattern: one module configures logging, and everything else asks
    it for a logger.
    """
    violations: list[Violation] = []
    for path in _python_files(root):
        relative = _relative_posix(path, root)
        if relative == STRUCTURED_LOGGING_MODULE:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        modules, functions = _logging_aliases(tree)
        if not modules and not functions:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = _logging_call_name(node, modules | functions)
            if called is not None:
                violations.append(
                    Violation(
                        path=relative,
                        line=node.lineno,
                        detail=(
                            f"calls `{called}` directly. Loggers come from "
                            f"`video_agent.observability.get_logger`, which is the only path "
                            f"that carries trace context, redaction and the JSON format."
                        ),
                    )
                )
    return sorted(violations, key=lambda violation: (violation.path, violation.line))
