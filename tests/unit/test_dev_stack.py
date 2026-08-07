"""S0.1.4 — docker-compose.dev.yml is wired to the `.env.example` variable names."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

EXPECTED_SERVICES = frozenset({"postgres", "redis", "minio", "litellm"})
BANNED_SECRET_PREFIXES = ("mhk_live_", "sk-")
VARIABLE_REFERENCE = re.compile(r"^\$\{[A-Z0-9_]+(:?-[^}]*)?\}$")

# Host ports the `.env.example` defaults imply.
POSTGRES_PORT = 5432
REDIS_PORT = 6379
LITELLM_PORT = 4000
MINIO_PORT = 9000


@pytest.fixture(scope="module")
def compose(repo_root: Path) -> dict[str, Any]:
    text = (repo_root / "docker-compose.dev.yml").read_text(encoding="utf-8")
    parsed: dict[str, Any] = yaml.safe_load(text)
    return parsed


@pytest.fixture(scope="module")
def compose_text(repo_root: Path) -> str:
    return (repo_root / "docker-compose.dev.yml").read_text(encoding="utf-8")


def _published_ports(service: dict[str, Any]) -> set[int]:
    published: set[int] = set()
    for mapping in service.get("ports", []):
        host_port = str(mapping).split(":")[0]
        if host_port.isdigit():
            published.add(int(host_port))
    return published


def test_compose_declares_the_four_dev_services(compose: dict[str, Any]) -> None:
    assert set(compose["services"]) == EXPECTED_SERVICES


def test_every_service_has_a_healthcheck(compose: dict[str, Any]) -> None:
    """`compose up -d` must be able to report four *healthy* containers, not four running."""
    for name, service in compose["services"].items():
        assert "healthcheck" in service, f"{name} has no healthcheck"


def test_compose_urls_match_env_example(
    compose: dict[str, Any], env_example: dict[str, str]
) -> None:
    services: dict[str, Any] = compose["services"]

    database_url = env_example["DATABASE_URL"]
    assert database_url.endswith("@localhost:5432/videoagent")
    postgres_env = services["postgres"]["environment"]
    assert postgres_env["POSTGRES_USER"] in database_url
    assert postgres_env["POSTGRES_DB"] == database_url.rsplit("/", 1)[-1]
    assert POSTGRES_PORT in _published_ports(services["postgres"])

    redis_url = env_example["REDIS_URL"]
    assert redis_url == "redis://localhost:6379/0"
    assert REDIS_PORT in _published_ports(services["redis"])

    litellm_base_url = env_example["LITELLM_BASE_URL"]
    assert litellm_base_url == "http://localhost:4000"
    assert LITELLM_PORT in _published_ports(services["litellm"])


def test_artifact_endpoint_url_default_is_blank_in_the_contract(
    compose: dict[str, Any], env_example: dict[str, str]
) -> None:
    """`.env.example` ships ARTIFACT_ENDPOINT_URL empty, so there is no default to match.

    The compose file publishes MinIO on 9000 and the header comment tells a developer to set
    ARTIFACT_ENDPOINT_URL=http://localhost:9000. Recorded as a test so that the day the
    contract grows a default, this fails and the two are reconciled deliberately.
    """
    assert env_example["ARTIFACT_ENDPOINT_URL"] == ""
    assert MINIO_PORT in _published_ports(compose["services"]["minio"])


def test_compose_contains_no_literal_secrets(compose: dict[str, Any], compose_text: str) -> None:
    for prefix in BANNED_SECRET_PREFIXES:
        assert prefix not in compose_text, f"compose file contains a literal {prefix}... value"

    for name, service in compose["services"].items():
        for key, value in service.get("environment", {}).items():
            if key.endswith(("_KEY", "_KEY_ID", "_TOKEN", "_SECRET")):
                assert VARIABLE_REFERENCE.match(str(value)), (
                    f"{name}.{key} must be a ${{VAR}} reference, not a literal"
                )


def test_compose_references_only_env_example_variable_names(
    compose_text: str, env_example: dict[str, str]
) -> None:
    """No compose interpolation may invent a variable the contract does not declare."""
    directives = "\n".join(
        line for line in compose_text.splitlines() if not line.lstrip().startswith("#")
    )
    referenced = set(re.findall(r"\$\{([A-Z0-9_]+)", directives))
    unknown = referenced - set(env_example)
    assert unknown == set(), f"compose references variables absent from .env.example: {unknown}"
