"""S0.2.3 — the three static guards, run against the real tree and against planted violations.

Two halves. The *positive* half asserts the guards pass on the current tree, which is what
makes them a gate rather than a decoration. The *negative* half plants each violation in a
fake repository under `tmp_path` and asserts the guard catches it and names the file — a guard
that has never been observed to fail is a guard nobody can trust.

The banned strings appear freely in this file. That is deliberate and safe: the guards scan
`src/` and `config/`, never `tests/`. A test suite that could not spell the thing it forbids
could not test that it is forbidden.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.static_guards import (
    BANNED_NAMES,
    MAGICHOUR_MODEL_NAMES,
    Violation,
    find_inline_prompts,
    find_model_branches,
    find_provider_name_leaks,
    format_violations,
    is_allowlisted,
)

# Transcribed from providers.md S9 independently of tests/static_guards.py, so that dropping a
# model from the banned list fails here instead of silently agreeing with itself.
MAGIC_HOUR_MODEL_ENUM: frozenset[str] = frozenset(
    {
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
    }
)

MAGIC_HOUR_MODEL_COUNT = 16

LONG_PROMPT = "You are a storyboard planner.\nProduce four shots.\n" + ("Describe them. " * 20)

# Line numbers of the planted violations in the fake modules below.
PLANTED_LEAK_LINE = 5
PLANTED_BRANCH_LINE = 2

PROSE_MODULE = "# the default threshold\ndef f(x: int = 1) -> int:\n    return x\n"
DOCSTRING_MODULE = f'"""{LONG_PROMPT}"""\n\n\ndef plan() -> None:\n    """{LONG_PROMPT}"""\n'


def _fake_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


# --- The current tree --------------------------------------------------------------------


def test_the_tree_names_no_provider(repo_root: Path) -> None:
    violations = find_provider_name_leaks(repo_root)
    assert violations == [], format_violations(violations)


def test_the_tree_holds_no_inline_prompt(repo_root: Path) -> None:
    violations = find_inline_prompts(repo_root)
    assert violations == [], format_violations(violations)


def test_the_tree_branches_on_no_model(repo_root: Path) -> None:
    violations = find_model_branches(repo_root)
    assert violations == [], format_violations(violations)


# --- Guard 1: provider and model names ---------------------------------------------------


def test_provider_name_leak_detected(tmp_path: Path) -> None:
    root = _fake_repo(
        tmp_path,
        {
            "src/video_agent/graph/nodes.py": (
                '"""A node."""\n\n\ndef submit() -> str:\n    return "magichour"\n'
            )
        },
    )

    violations = find_provider_name_leaks(root)

    assert [violation.path for violation in violations] == ["src/video_agent/graph/nodes.py"]
    assert violations[0].line == PLANTED_LEAK_LINE
    assert "magichour" in violations[0].detail


def test_provider_name_allowed_in_adapter_and_config(tmp_path: Path) -> None:
    root = _fake_repo(
        tmp_path,
        {
            "src/video_agent/providers/magichour.py": (
                '"""The one adapter."""\n\nBASE = "https://api.magichour.ai"\nMODEL = "wan-2.2"\n'
            ),
            "config/aliases.yaml": 'aliases:\n  reasoning-high:\n    primary: "gemini/x"\n',
            "src/video_agent/config/settings.py": 'GEMINI_API_KEY = ""\n',
        },
    )

    violations = find_provider_name_leaks(root)
    assert violations == [], format_violations(violations)


def test_the_allowlist_is_exactly_the_adapter_and_config() -> None:
    assert is_allowlisted("src/video_agent/providers/magichour.py")
    assert is_allowlisted("src/video_agent/config/settings.py")
    assert is_allowlisted("config/aliases.yaml")
    assert not is_allowlisted("src/video_agent/providers/registry.py")
    assert not is_allowlisted("src/video_agent/gateway/client.py")


@pytest.mark.parametrize("vendor", ["openai", "anthropic", "claude", "gemini", "higgsfield"])
def test_llm_vendor_names_are_banned(tmp_path: Path, vendor: str) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/qc/score.py": f'MODEL = "{vendor}/whatever"\n'})
    assert find_provider_name_leaks(root) != []


def test_a_provider_name_in_a_comment_is_still_a_leak(tmp_path: Path) -> None:
    """A comment saying which model a function is tuned for is how the coupling starts."""
    root = _fake_repo(
        tmp_path, {"src/video_agent/qc/score.py": "# tuned for gemini, do not change\nX = 1\n"}
    )
    assert find_provider_name_leaks(root) != []


def test_model_enum_names_are_all_covered() -> None:
    assert MAGIC_HOUR_MODEL_ENUM.issubset(BANNED_NAMES)
    assert set(MAGICHOUR_MODEL_NAMES) == MAGIC_HOUR_MODEL_ENUM
    assert len(MAGIC_HOUR_MODEL_ENUM) == MAGIC_HOUR_MODEL_COUNT


def test_an_ambiguous_model_name_is_banned_only_as_a_literal(tmp_path: Path) -> None:
    """`default` is a model name and an English word; banning the word would be absurd.

    Banning the bare literal is not: a sentinel string belongs in an enum, so there is no
    legitimate `"default"` in application code, while `default=` and the word in prose are
    everywhere.
    """
    prose = _fake_repo(tmp_path / "prose", {"src/video_agent/qc/score.py": PROSE_MODULE})
    assert find_provider_name_leaks(prose) == []

    literal = _fake_repo(tmp_path / "literal", {"src/video_agent/qc/score.py": 'M = "default"\n'})
    violations = find_provider_name_leaks(literal)
    assert violations != []
    assert "default" in violations[0].detail


# --- Guard 2: inline prompts -------------------------------------------------------------


def test_inline_prompt_detected(tmp_path: Path) -> None:
    root = _fake_repo(
        tmp_path, {"src/video_agent/planning/plan.py": f'PROMPT = """{LONG_PROMPT}"""\n'}
    )

    violations = find_inline_prompts(root)

    assert len(violations) == 1
    assert violations[0].path == "src/video_agent/planning/plan.py"
    assert "prompt" in violations[0].detail


def test_a_long_docstring_is_not_an_inline_prompt(tmp_path: Path) -> None:
    """Module and function docstrings are long and multi-line by design; that is the house
    style, not a pasted prompt."""
    root = _fake_repo(tmp_path, {"src/video_agent/planning/plan.py": DOCSTRING_MODULE})
    assert find_inline_prompts(root) == []


def test_a_short_multiline_literal_is_not_a_prompt(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, {"src/video_agent/planning/plan.py": 'SQL = """a\nb\nc"""\n'})
    assert find_inline_prompts(root) == []


# --- Guard 3: branching on model_used / provider_key ------------------------------------


def test_branch_on_model_used_detected(tmp_path: Path) -> None:
    root = _fake_repo(
        tmp_path,
        {
            "src/video_agent/graph/route.py": (
                "def route(response: object) -> bool:\n"
                '    if response.model_used == "gpt-oh-no":\n'
                "        return True\n"
                "    return False\n"
            )
        },
    )

    violations = find_model_branches(root)

    assert len(violations) == 1
    assert violations[0].line == PLANTED_BRANCH_LINE
    assert "model_used" in violations[0].detail


def test_reading_model_used_for_a_span_passes(tmp_path: Path) -> None:
    root = _fake_repo(
        tmp_path,
        {
            "src/video_agent/graph/route.py": (
                "def record(obs: object, r: object) -> None:\n"
                "    obs.span(model_used=r.model_used)\n"
            )
        },
    )
    assert find_model_branches(root) == []


def test_membership_against_a_literal_container_is_detected(tmp_path: Path) -> None:
    root = _fake_repo(
        tmp_path,
        {
            "src/video_agent/graph/route.py": (
                'def route(r: object) -> bool:\n    return r.provider_key in ("a", "b")\n'
            )
        },
    )
    assert find_model_branches(root) != []


def test_match_on_model_used_is_detected(tmp_path: Path) -> None:
    """A `match` is a comparison in another spelling and evades a naive `==` grep."""
    root = _fake_repo(
        tmp_path,
        {
            "src/video_agent/graph/route.py": (
                "def route(r: object) -> int:\n"
                "    match r.model_used:\n"
                '        case "some-model":\n'
                "            return 1\n"
                "        case _:\n"
                "            return 0\n"
            )
        },
    )
    assert find_model_branches(root) != []


# --- Reporting --------------------------------------------------------------------------


def test_a_violation_reports_the_file_and_the_line() -> None:
    rendered = str(Violation(path="src/video_agent/graph/nodes.py", line=12, detail="because"))
    assert rendered == "src/video_agent/graph/nodes.py:12: because"
