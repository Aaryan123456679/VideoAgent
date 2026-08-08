"""`S0.3.2` — `print` and unstructured loggers are a build failure, not a review comment.

Two halves, like the other static guards. The *positive* half runs against the real tree, so
the rule is a gate rather than a decoration. The *negative* half plants each violation in a
fake repository and asserts the guard catches it and names the file, because a guard that has
never been observed to fail is a guard nobody can trust — and this file exists partly because
the `T0.1` review found tests that passed against a deliberately broken implementation.
"""

from __future__ import annotations

from pathlib import Path

from tests.static_guards import (
    PRE_LOGGING_BOOTSTRAP_PATHS,
    PRE_LOGGING_BOOTSTRAP_STREAM,
    STRUCTURED_LOGGING_MODULE,
    Violation,
    find_print_calls,
    find_unstructured_loggers,
    format_violations,
)

PRINTING_MODULE = "import sys\n\n\ndef report() -> None:\n    print('hello')\n"
STDOUT_MODULE = "import sys\n\n\ndef report() -> None:\n    sys.stdout.write('hello')\n"
STDERR_MODULE = "import sys\n\n\ndef report() -> None:\n    sys.stderr.write('hello')\n"

GETLOGGER_MODULE = "import logging\n\nlog = logging.getLogger(__name__)\n"
ALIASED_MODULE = "import logging as stdlib\n\nlog = stdlib.getLogger(__name__)\n"
FROM_IMPORT_MODULE = "from logging import getLogger\n\nlog = getLogger(__name__)\n"
ROOT_CALL_MODULE = 'import logging\n\n\ndef report() -> None:\n    logging.info("hello")\n'
BASIC_CONFIG_MODULE = "import logging\n\nlogging.basicConfig()\n"

SANCTIONED_MODULE = (
    "from video_agent.observability import get_logger\n\nlog = get_logger(__name__)\n"
)

PLANTED_PRINT_LINE = 5
PLANTED_LOGGER_LINE = 3


def _fake_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


# --- The current tree ------------------------------------------------------------------------


def test_the_tree_contains_no_print(repo_root: Path) -> None:
    """`S0.3.2` acceptance 3."""
    violations = find_print_calls(repo_root)
    assert violations == [], format_violations(violations)


def test_the_tree_contains_no_unstructured_logger(repo_root: Path) -> None:
    violations = find_unstructured_loggers(repo_root)
    assert violations == [], format_violations(violations)


def test_the_sanctioned_module_exists_where_the_guard_exempts_it(repo_root: Path) -> None:
    """The exemption must point at a real file, or the guard is exempting nothing.

    An exemption path that no longer resolves is the quiet way this rule would stop applying
    to the one module it was written for.
    """
    assert (repo_root / STRUCTURED_LOGGING_MODULE).is_file()


# --- Planted `print` -------------------------------------------------------------------------


def test_a_planted_print_is_caught(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/reporting.py": PRINTING_MODULE})

    violations = find_print_calls(root)

    assert violations == [
        Violation(
            path="src/video_agent/reporting.py",
            line=PLANTED_PRINT_LINE,
            detail=violations[0].detail,
        )
    ]
    assert "trace_id" in violations[0].detail


def test_a_planted_stdout_write_is_caught(tmp_path: Path) -> None:
    """`sys.stdout.write` is `print` with the guard filed off; both bypass every filter."""
    root = _fake_repo(tmp_path, {"src/video_agent/reporting.py": STDOUT_MODULE})

    assert [violation.line for violation in find_print_calls(root)] == [PLANTED_PRINT_LINE]


def test_a_planted_stderr_write_is_caught(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/reporting.py": STDERR_MODULE})

    assert [violation.line for violation in find_print_calls(root)] == [PLANTED_PRINT_LINE]


def test_the_sanctioned_spelling_is_not_flagged_as_printing(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/node.py": SANCTIONED_MODULE})

    assert find_print_calls(root) == []


# --- Planted unstructured loggers ---------------------------------------------------------------


def test_a_planted_getlogger_is_caught(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/node.py": GETLOGGER_MODULE})

    violations = find_unstructured_loggers(root)

    assert [violation.line for violation in violations] == [PLANTED_LOGGER_LINE]
    assert "get_logger" in violations[0].detail


def test_an_aliased_logging_import_is_caught(tmp_path: Path) -> None:
    """Matching the binding, not the spelling: `import logging as stdlib` is the same call."""
    root = _fake_repo(tmp_path, {"src/video_agent/node.py": ALIASED_MODULE})

    assert [violation.line for violation in find_unstructured_loggers(root)] == [
        PLANTED_LOGGER_LINE
    ]


def test_a_from_import_of_getlogger_is_caught(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/node.py": FROM_IMPORT_MODULE})

    assert [violation.line for violation in find_unstructured_loggers(root)] == [
        PLANTED_LOGGER_LINE
    ]


def test_a_call_on_the_root_logger_is_caught(tmp_path: Path) -> None:
    """`logging.info` configures the root logger implicitly, deciding the format for everyone."""
    root = _fake_repo(tmp_path, {"src/video_agent/node.py": ROOT_CALL_MODULE})

    assert [violation.line for violation in find_unstructured_loggers(root)] == [PLANTED_PRINT_LINE]


def test_basic_config_is_caught(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/node.py": BASIC_CONFIG_MODULE})

    assert find_unstructured_loggers(root) != []


def test_the_observability_module_itself_is_exempt(tmp_path: Path) -> None:
    """One module configures logging; exempting it is what makes the rule enforceable."""
    root = _fake_repo(tmp_path, {STRUCTURED_LOGGING_MODULE: GETLOGGER_MODULE})

    assert find_unstructured_loggers(root) == []


def test_the_exemption_is_one_path_not_a_directory(tmp_path: Path) -> None:
    """A sibling in the same package must not inherit the exemption."""
    sibling = "src/video_agent/observability/tracing.py"
    root = _fake_repo(tmp_path, {sibling: GETLOGGER_MODULE})

    assert [violation.path for violation in find_unstructured_loggers(root)] == [sibling]


def test_the_sanctioned_accessor_is_not_flagged(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/node.py": SANCTIONED_MODULE})

    assert find_unstructured_loggers(root) == []


# --- The pre-logging bootstrap exemption -----------------------------------------------------


def test_the_bootstrap_exemption_names_a_real_file(repo_root: Path) -> None:
    """An exemption pointing at a path that no longer exists is an exemption nobody notices."""
    assert PRE_LOGGING_BOOTSTRAP_PATHS
    assert all((repo_root / path).is_file() for path in PRE_LOGGING_BOOTSTRAP_PATHS)


def test_the_bootstrap_may_write_to_stderr(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {PRE_LOGGING_BOOTSTRAP_PATHS[0]: STDERR_MODULE})

    assert find_print_calls(root) == []


def test_the_bootstrap_may_still_not_print(tmp_path: Path) -> None:
    """The exemption covers the stream write, not the ban on `print`."""
    root = _fake_repo(tmp_path, {PRE_LOGGING_BOOTSTRAP_PATHS[0]: PRINTING_MODULE})

    assert [violation.line for violation in find_print_calls(root)] == [PLANTED_PRINT_LINE]


def test_no_other_module_inherits_the_bootstrap_exemption(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/other.py": STDERR_MODULE})

    assert [violation.path for violation in find_print_calls(root)] == ["src/video_agent/other.py"]


def test_the_bootstrap_may_not_write_to_stdout(tmp_path: Path) -> None:
    """The exemption's docstring says `stderr`; the check has to agree with it.

    It did not. `_is_stdio_write` matched both streams and the exemption was applied to the
    result, so `sys.stdout.write` inside the exempt file was silently permitted — and `stdout`
    is precisely the stream that must stay parseable, because in a container it is the one the
    log collector reads.
    """
    root = _fake_repo(tmp_path, {PRE_LOGGING_BOOTSTRAP_PATHS[0]: STDOUT_MODULE})

    violations = find_print_calls(root)

    assert [violation.line for violation in violations] == [PLANTED_PRINT_LINE]
    assert violations[0].path == PRE_LOGGING_BOOTSTRAP_PATHS[0]


def test_the_bootstrap_exemption_is_pinned_to_exactly_one_file() -> None:
    """Adding a second existing file to the tuple passed CI in silence.

    Every assertion around it was written against `PRE_LOGGING_BOOTSTRAP_PATHS[0]`, so the
    second entry was never looked at. `AGENT.md` §3 makes this one file architectural; pinning
    the tuple is what makes "one file" checkable rather than merely intended.
    """
    assert PRE_LOGGING_BOOTSTRAP_PATHS == ("src/video_agent/__main__.py",)
    assert PRE_LOGGING_BOOTSTRAP_STREAM == "stderr"


# --- The unstructured-logger guard sees every import spelling ---------------------------------

SUBMODULE_IMPORT_MODULE = "import logging.handlers\n\nlog = logging.getLogger(__name__)\n"
ALIASED_SUBMODULE_MODULE = (
    "import logging.config as logcfg\n\n\ndef go() -> None:\n    logcfg.basicConfig()\n"
)


def test_a_submodule_import_of_logging_is_caught(tmp_path: Path) -> None:
    """`import logging.handlers` binds the name `logging`, exactly like `import logging`.

    The alias scan recorded a binding only when `alias.name == "logging"`, so this spelling
    produced no bindings at all and the guard returned before looking at a single call — while
    `logging.getLogger` on the next line worked perfectly.
    """
    root = _fake_repo(tmp_path, {"src/video_agent/graph/nodes.py": SUBMODULE_IMPORT_MODULE})

    violations = find_unstructured_loggers(root)

    assert [violation.line for violation in violations] == [PLANTED_LOGGER_LINE]
    assert "logging.getLogger" in violations[0].detail


def test_an_aliased_submodule_import_of_logging_is_caught(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/graph/nodes.py": ALIASED_SUBMODULE_MODULE})

    assert find_unstructured_loggers(root) != []


def test_an_unrelated_module_whose_name_starts_with_logging_is_not_matched(
    tmp_path: Path,
) -> None:
    """`loggingx` is a different distribution, not this one under another spelling."""
    module = "import loggingx\n\nlog = loggingx.getLogger(__name__)\n"
    root = _fake_repo(tmp_path, {"src/video_agent/graph/nodes.py": module})

    assert find_unstructured_loggers(root) == []
