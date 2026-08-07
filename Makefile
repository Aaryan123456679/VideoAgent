# Video Agent — developer entry points.
# `make check` is what CI runs and what a pull request must be green on.

UV  ?= uv
RUN := $(UV) run
SRC := src tests

.DEFAULT_GOAL := help
.PHONY: help install lock format lint type test test-integration check hooks \
        compose-up compose-down compose-config clean

help: ## Show the available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Create .venv and install runtime + dev dependencies from the lock file
	$(UV) sync --frozen

lock: ## Refresh uv.lock
	$(UV) lock

format: ## Rewrite code with the ruff formatter and apply safe lint fixes
	$(RUN) ruff format $(SRC)
	$(RUN) ruff check --fix $(SRC)

lint: ## ruff format --check + ruff check (zero findings required)
	$(RUN) ruff format --check $(SRC)
	$(RUN) ruff check $(SRC)

type: ## mypy in strict mode
	$(RUN) mypy

test: ## Unit + contract tests with coverage (integration deselected)
	$(RUN) pytest

test-integration: ## Integration tests only; requires the dev stack to be up
	$(RUN) pytest -m integration --no-cov

check: lint type test ## Everything CI gates on: lint, type, test

hooks: ## Install the pre-commit git hook
	$(RUN) pre-commit install

compose-config: ## Validate docker-compose.dev.yml without starting anything
	docker compose -f docker-compose.dev.yml config -q

compose-up: ## Start the local dev stack (Postgres, Redis, MinIO, LiteLLM)
	docker compose -f docker-compose.dev.yml up -d

compose-down: ## Stop the local dev stack and remove its volumes
	docker compose -f docker-compose.dev.yml down -v

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
