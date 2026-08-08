"""`S0.6.1` — the Redis key registry is the schema, and nothing builds a key beside it.

Two things are checked here and they are different kinds of claim.

*The registry matches the document.* `persistence.md` §5 is a table, so the test parses that
table and diffs it against `KEY_REGISTRY` in both directions. A row added to the LLD and not to
the code fails, and so does a key invented in the code and never documented. Transcribing the
expected patterns into the test instead would only assert that the test and the code agree.

*Nothing else builds a key.* An AST scan for a string literal beginning with any registered
prefix, over `src/`. The scanner is run against `tests/_fixtures/adhoc_redis_key_case.py` —
which really does spell two keys out by hand — so "no violations in `src/`" is a result rather
than a scanner that is quietly looking at nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest

from video_agent.api.idempotency import storage_key_for
from video_agent.persistence import keys as keys_module
from video_agent.persistence.keys import (
    KEY_CONSTRUCTORS,
    KEY_REGISTRY,
    REGISTERED_PREFIXES,
    KeyName,
    KeySegmentError,
    MissingTtlError,
    RedisKey,
    TtlPolicy,
    circuit_breaker_key,
    failure_signature_key,
    idempotency_key,
    job_lock_key,
    jobs_stream_key,
    llm_cache_key,
    progress_key,
    rate_limit_key,
    spec_for,
)

REGISTRY_MODULE: Final = "src/video_agent/persistence/keys.py"
FIXTURE: Final = "tests/_fixtures/adhoc_redis_key_case.py"
PLANTED_VIOLATIONS: Final = 2
"""The fixture spells out two keys, under two different prefixes, so a scanner that stopped at
the first one is caught."""

TENANT: Final = UUID("11111111-1111-1111-1111-111111111111")
JOB: Final = UUID("22222222-2222-2222-2222-222222222222")

HOUR: Final = 3600
DAY: Final = 24 * HOUR
FIVE_MINUTES: Final = 300
LOCK_SECONDS: Final = 60

DOCUMENTED_TTLS: Final = [
    (KeyName.IDEMPOTENCY, DAY),
    (KeyName.JOB_LOCK, LOCK_SECONDS),
    (KeyName.PROGRESS, HOUR),
    (KeyName.CIRCUIT_BREAKER, FIVE_MINUTES),
    (KeyName.LLM_CACHE, HOUR),
]
"""`persistence.md` §5 in seconds: idem 24h, job lock 60s, progress 1h, cb 5m, cache 1h.

Spelled as arithmetic on named units rather than as `86400`, so a reader can check the row
against the document without doing the division themselves.
"""


# --- The LLD table ------------------------------------------------------------------------------


def documented_patterns(repo_root: Path) -> set[str]:
    """Every key pattern in the `persistence.md` §5 table, read from the document itself."""
    text = (repo_root / "docs/LLD/persistence.md").read_text(encoding="utf-8")
    section = text.split("## 5. Redis 7", 1)[1].split("### 5.1", 1)[0]
    patterns: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cell = line.split("|")[1].strip()
        match = re.fullmatch(r"`([^`]+)`", cell)
        if match:
            patterns.add(match.group(1))
    return patterns


def test_the_lld_table_was_actually_found(repo_root: Path) -> None:
    """Guard the parser. If §5 is renamed, every diff below would pass against an empty set."""
    assert len(documented_patterns(repo_root)) == len(KeyName)


def test_all_documented_keys_have_constructors(repo_root: Path) -> None:
    """`persistence.md` §5 and `KEY_REGISTRY` are the same set of patterns, both ways."""
    registered = {spec.pattern for spec in KEY_REGISTRY.values()}

    assert documented_patterns(repo_root) == registered


def test_every_registry_entry_names_a_real_constructor() -> None:
    """`KEY_CONSTRUCTORS` maps every entry to a function that exists in the module."""
    assert set(KEY_CONSTRUCTORS) == set(KeyName)
    for name, function_name in KEY_CONSTRUCTORS.items():
        assert callable(getattr(keys_module, function_name)), f"{name} names {function_name}"


@pytest.mark.parametrize(("name", "ttl_seconds"), DOCUMENTED_TTLS)
def test_ttls_match_lld(name: KeyName, ttl_seconds: int) -> None:
    """Each fixed-TTL key expires exactly when `persistence.md` §5 says it does."""
    assert spec_for(name).ttl_policy is TtlPolicy.FIXED
    assert spec_for(name).ttl_seconds == ttl_seconds


def test_the_ttl_travels_on_the_constructed_key() -> None:
    """The registry's number is on the value, not merely in the table."""
    assert idempotency_key(TENANT, "POST /v1/jobs", "abc").ttl_seconds == DAY
    assert job_lock_key(JOB).ttl_seconds == LOCK_SECONDS
    assert progress_key(JOB).ttl_seconds == HOUR
    assert circuit_breaker_key("provider").ttl_seconds == FIVE_MINUTES
    assert llm_cache_key("deadbeef").ttl_seconds == HOUR


def test_the_queue_is_the_only_ttl_less_key() -> None:
    """`jobs:stream` never expires; everything else does. A queue that expires drops work."""
    ttl_less = {name for name, spec in KEY_REGISTRY.items() if spec.ttl_policy is TtlPolicy.NONE}

    assert ttl_less == {KeyName.JOBS_STREAM}
    assert jobs_stream_key().ttl_seconds is None


@pytest.mark.parametrize("name", [KeyName.FAILURE_SIGNATURE, KeyName.RATE_LIMIT])
def test_caller_supplied_ttl_keys_declare_no_default(name: KeyName) -> None:
    """`sig:` and `rl:` expire with their subject, so the registry must not invent a number."""
    assert spec_for(name).ttl_policy is TtlPolicy.CALLER
    assert spec_for(name).ttl_seconds is None


@pytest.mark.parametrize("ttl_seconds", [0, -1])
def test_a_caller_ttl_key_refuses_a_non_positive_ttl(ttl_seconds: int) -> None:
    """Zero is not "no expiry" — in Redis it is a refused command, and here it is a bug."""
    with pytest.raises(MissingTtlError):
        failure_signature_key(JOB, ttl_seconds)
    with pytest.raises(MissingTtlError):
        rate_limit_key(TENANT, "60s", ttl_seconds)


# --- Rendering ------------------------------------------------------------------------------------


def test_rendered_keys_match_their_patterns() -> None:
    """Every constructor renders the pattern its registry entry declares."""
    assert (
        idempotency_key(TENANT, "POST /v1/jobs", "abc").value == f"idem:{TENANT}:POST /v1/jobs:abc"
    )
    assert job_lock_key(JOB).value == f"job:{JOB}"
    assert jobs_stream_key().value == "jobs:stream"
    assert progress_key(JOB).value == f"progress:{JOB}"
    assert failure_signature_key(JOB, HOUR).value == f"sig:{JOB}"
    assert rate_limit_key(TENANT, "minute", 60).value == f"rl:{TENANT}:minute"
    assert circuit_breaker_key("video").value == "cb:video"
    assert llm_cache_key("deadbeef").value == "cache:llm:deadbeef"


@pytest.mark.parametrize("hostile", ["a:b", " padded", "", "two\nlines"])
def test_a_non_terminal_segment_that_would_move_a_boundary_is_rejected(hostile: str) -> None:
    """A `:` in the route silently redefines where the route ends and the client key begins."""
    with pytest.raises(KeySegmentError):
        idempotency_key(TENANT, hostile, "a-valid-client-key")


def test_a_route_with_a_space_is_accepted() -> None:
    """`POST /v1/jobs` is what `api.md` §3 calls a route. A space is not a boundary."""
    assert idempotency_key(TENANT, "POST /v1/jobs", "k").value.endswith(":POST /v1/jobs:k")


def test_a_client_key_containing_a_colon_is_accepted() -> None:
    """The last segment has nothing after it, so no `:` inside it can be a boundary.

    Rejecting it would turn `Idempotency-Key: order:12345` — a reasonable header — into a `500`
    over a collision that the pattern makes impossible.
    """
    rendered = idempotency_key(TENANT, "POST /v1/jobs", "order:12345")

    assert rendered.value == f"idem:{TENANT}:POST /v1/jobs:order:12345"


@pytest.mark.parametrize("hostile", [" padded", "", "two\nlines"])
def test_a_terminal_segment_is_still_shape_checked(hostile: str) -> None:
    """Terminal does not mean unchecked: padding and control characters are refused anywhere."""
    with pytest.raises(KeySegmentError):
        rate_limit_key(TENANT, hostile, 60)


def test_the_job_lock_prefix_does_not_swallow_the_queue() -> None:
    """`job:` and `jobs:stream` are different keys, and neither is a prefix of the other."""
    assert not jobs_stream_key().value.startswith(spec_for(KeyName.JOB_LOCK).prefix)


# --- The API's idempotency key --------------------------------------------------------------------


def test_the_api_renders_its_key_through_the_registry() -> None:
    """`api.idempotency` no longer spells `idem:{tenant}:{route}:{key}` for itself."""
    rendered = storage_key_for(TENANT, "POST /v1/jobs", "client-supplied-key")

    assert rendered == idempotency_key(TENANT, "POST /v1/jobs", "client-supplied-key").value


def test_the_api_declares_no_ttl_of_its_own(repo_root: Path) -> None:
    """One 24h window `[D-16]`, not two constants that happen to be equal today.

    `T0.4` wrote `86_400` in `api/idempotency.py` while `persistence.md` §5 documented the same
    window in its key table. The two were never compared, so nothing would have failed if one
    of them moved — and the direction that hurts is silent: a shorter window in `api` than the
    Postgres unique constraint assumes turns a legitimate 24-hour retry into a second job.
    """
    source = (repo_root / "src/video_agent/api/idempotency.py").read_text(encoding="utf-8")
    assignments = {
        node.targets[0].id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name)
    } | {
        node.target.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    assert "IDEMPOTENCY_TTL_SECONDS" not in assignments
    assert "from video_agent.persistence.keys import IDEMPOTENCY_TTL_SECONDS" in source


# --- The static check -----------------------------------------------------------------------------


def _documentation_nodes(tree: ast.Module) -> set[int]:
    """Ids of the string constants that are documentation, and so exempt.

    A string that is a bare expression statement is discarded at runtime: module, class and
    function docstrings, and the PEP 257 attribute docstrings this codebase uses under its
    constants. A key, by contrast, is always *used* — returned, passed or concatenated.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def adhoc_key_literals(source: str, relative: str) -> list[str]:
    """Every string literal in `source` that begins with a registered key prefix."""
    tree = ast.parse(source)
    exempt = _documentation_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in exempt:
            continue
        prefix = next((p for p in REGISTERED_PREFIXES if node.value.startswith(p)), None)
        if prefix is not None:
            found.append(f"{relative}:{node.lineno}: literal {node.value!r} starts with {prefix!r}")
    return sorted(found)


def test_adhoc_key_literal_detected(repo_root: Path) -> None:
    """The planted fixture is caught — both of its keys, not just the first."""
    source = (repo_root / FIXTURE).read_text(encoding="utf-8")

    violations = adhoc_key_literals(source, FIXTURE)

    assert len(violations) == PLANTED_VIOLATIONS, violations
    assert any("sig:" in line for line in violations)
    assert any("progress:" in line for line in violations)


def test_the_scanner_ignores_a_key_rendered_through_a_constructor() -> None:
    """A call to `progress_key(job_id)` is the correct spelling and must not be flagged."""
    source = "from video_agent.persistence.keys import progress_key\nk = progress_key(job_id)\n"

    assert adhoc_key_literals(source, "probe.py") == []


PENDING_REGISTRY_ADOPTION: dict[str, str] = {}
"""Modules that build a registered key by hand today, each with the reason and the owning task.

Exact paths, never a directory or a pattern, so nothing inherits the exemption by living next
door — and asserted in both directions below, so the entry expires by itself the moment the
module adopts the registry. That is the opposite of the usual exemption, where the entry
outlives the reason for it and nobody notices. The pattern is `T0.5`'s, from
`test_persistence_boundary.py`.
"""


def module_violations(repo_root: Path) -> dict[str, list[str]]:
    """Every module under `src/` outside the registry, mapped to the keys it spells out."""
    found: dict[str, list[str]] = {}
    for path in sorted((repo_root / "src").rglob("*.py")):
        relative = path.relative_to(repo_root).as_posix()
        if relative == REGISTRY_MODULE:
            continue
        offences = adhoc_key_literals(path.read_text(encoding="utf-8"), relative)
        if offences:
            found[relative] = offences
    return found


def test_no_unexempted_adhoc_key_literal_in_src(repo_root: Path) -> None:
    """The gate: a new module that spells a key out cannot merge."""
    unexpected = [
        line
        for path, lines in module_violations(repo_root).items()
        if path not in PENDING_REGISTRY_ADOPTION
        for line in lines
    ]

    assert unexpected == [], "\n".join(unexpected)


def test_the_exemption_list_is_exactly_the_one_known_module() -> None:
    """It may not grow silently."""
    assert set(PENDING_REGISTRY_ADOPTION) == set()


@pytest.mark.parametrize("path", sorted(PENDING_REGISTRY_ADOPTION))
def test_each_exempted_module_still_actually_violates(repo_root: Path, path: str) -> None:
    """The exemption expires by itself once the module renders its key through the registry."""
    source = repo_root / path
    assert source.is_file(), f"{path} is exempted but does not exist; delete the entry"
    assert adhoc_key_literals(source.read_text(encoding="utf-8"), path), (
        f"{path} no longer spells a key out; delete its PENDING_REGISTRY_ADOPTION entry"
    )


@pytest.mark.parametrize("path", sorted(PENDING_REGISTRY_ADOPTION))
def test_each_exemption_carries_a_reason(path: str) -> None:
    assert PENDING_REGISTRY_ADOPTION[path].strip()


def test_the_registry_module_is_where_the_patterns_live(repo_root: Path) -> None:
    """The exclusion above is meaningful only because the excluded file really does this.

    Without this, deleting every pattern from the registry would leave the gate green while the
    check was enforcing the rule against nothing.
    """
    source = (repo_root / REGISTRY_MODULE).read_text(encoding="utf-8")

    assert len(adhoc_key_literals(source, REGISTRY_MODULE)) == len(KeyName)


def test_each_pattern_string_appears_exactly_once_in_src(repo_root: Path) -> None:
    """`S0.6.1` acceptance 1: the raw pattern string is in exactly one place."""
    sources = {
        path.relative_to(repo_root).as_posix(): path.read_text(encoding="utf-8")
        for path in (repo_root / "src").rglob("*.py")
    }
    for spec in KEY_REGISTRY.values():
        occurrences = [name for name, text in sources.items() if f'"{spec.pattern}"' in text]
        assert occurrences == [REGISTRY_MODULE], f"{spec.pattern} appears in {occurrences}"


def test_a_hand_built_key_is_still_a_redis_key() -> None:
    """`RedisKey` is constructible directly — which is why the write path re-checks the TTL.

    This is not a loophole being blessed. It is the reason
    `persistence.redis_client.require_ttl` exists: the type cannot prevent a caller from
    assembling one, so the enforcement lives where the command is issued.
    """
    hand_built = RedisKey(name=KeyName.PROGRESS, value="progress:anything", ttl_seconds=None)

    assert hand_built.ttl_seconds is None
