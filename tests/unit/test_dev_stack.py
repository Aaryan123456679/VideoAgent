"""S0.1.4 — docker-compose.dev.yml is wired to the `.env.example` variable names."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from tests.support import BANNED_SECRET_PREFIXES, SECRET_SUFFIXES

EXPECTED_SERVICES = frozenset({"postgres", "redis", "minio", "litellm"})
VARIABLE_REFERENCE = re.compile(r"^\$\{[A-Z0-9_]+(:?-[^}]*)?\}$")

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


def test_compose_postgres_matches_the_database_url(
    compose: dict[str, Any], env_example: dict[str, str]
) -> None:
    """Compare the *parsed* URL against the service, component by component.

    The previous version asserted a suffix, a substring and a port, so mutating
    `POSTGRES_USER` to any value that stayed a substring of the URL — or mutating the
    password to something the URL does not contain — left the suite green. A correspondence
    test that passes when the correspondence is broken is not a test.
    """
    url = urlsplit(env_example["DATABASE_URL"])
    service = compose["services"]["postgres"]
    environment = service["environment"]

    assert environment["POSTGRES_USER"] == url.username
    assert environment["POSTGRES_DB"] == url.path.lstrip("/")
    assert url.hostname == "localhost"
    assert url.port in _published_ports(service)

    # The credential is not in the compose file at all: `.env.example` bundles it inside
    # DATABASE_URL, so there is no discrete variable to reference. `trust` accepts whatever
    # password the URL carries, which keeps the default connection string working while
    # leaving nothing credential-shaped committed. If this ever becomes a literal password
    # again, both halves of this assertion fail.
    assert "POSTGRES_PASSWORD" not in environment
    assert environment["POSTGRES_HOST_AUTH_METHOD"] == "trust"
    assert url.password, "DATABASE_URL must still carry a password for a non-trust deployment"


def test_compose_redis_matches_the_redis_url(
    compose: dict[str, Any], env_example: dict[str, str]
) -> None:
    url = urlsplit(env_example["REDIS_URL"])
    service = compose["services"]["redis"]

    assert url.scheme == "redis"
    assert url.hostname == "localhost"
    assert url.port in _published_ports(service)

    # `redis-server --databases N` must cover the database index the URL selects, or every
    # connection made with the documented default is rejected at runtime.
    database_index = int(url.path.lstrip("/") or "0")
    command: list[str] = service["command"]
    declared_databases = int(command[command.index("--databases") + 1])
    assert database_index < declared_databases


def test_compose_litellm_matches_the_base_url(
    compose: dict[str, Any], env_example: dict[str, str]
) -> None:
    url = urlsplit(env_example["LITELLM_BASE_URL"])
    service = compose["services"]["litellm"]

    assert url.scheme == "http"
    assert url.hostname == "localhost"
    assert url.port in _published_ports(service)

    # The proxy is told the same port the URL advertises, not merely mapped to it.
    command: list[str] = service["command"]
    assert int(command[command.index("--port") + 1]) == url.port


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

    checked = 0
    for name, service in compose["services"].items():
        for key, value in service.get("environment", {}).items():
            if key.endswith(SECRET_SUFFIXES):
                checked += 1
                assert VARIABLE_REFERENCE.match(str(value)), (
                    f"{name}.{key} must be a ${{VAR}} reference, not a literal"
                )
    # A deny-list that matches nothing passes vacuously. Assert it has teeth.
    assert checked > 0, "no credential-suffixed variable was checked; the suffix list is stale"


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
