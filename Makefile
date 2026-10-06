# AlphaAgent — developer shortcuts.
# Run `make` or `make help` to list every target.

.DEFAULT_GOAL := help
SHELL := /bin/bash
PYTHON ?= venv/bin/python
PIP ?= venv/bin/pip
PIP_COMPILE ?= venv/bin/pip-compile

.PHONY: help venv lock lock-dev install install-dev env up down logs migrate makemigrations \
        seed shell test test-guardrails lint format check docker-build docker-up \
        docker-down dry-run clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtual environment
	python3 -m venv venv

lock: ## Re-resolve requirements.txt from requirements.in (pip-tools)
	$(PIP_COMPILE) --generate-hashes --resolver=backtracking \
		--strip-extras --output-file=requirements.txt requirements.in
	@echo "requirements.txt regenerated - commit it alongside requirements.in"

lock-dev: ## Re-resolve requirements-dev.txt (--allow-unsafe pins pip/setuptools)
	$(PIP_COMPILE) --generate-hashes --allow-unsafe --resolver=backtracking \
		--strip-extras --output-file=requirements-dev.txt requirements-dev.in
	@echo "requirements-dev.txt regenerated - commit it alongside requirements-dev.in"

install: venv ## Install runtime dependencies (hash-verified)
	$(PIP) install --upgrade pip
	$(PIP) install --require-hashes -r requirements.txt
	$(PIP) install pip-tools   # provides `make lock`

install-dev: install ## Install runtime + development dependencies (hash-verified)
	$(PIP) install --require-hashes -r requirements-dev.txt

env: ## Create .env from the template (fails if one already exists)
	@test -f .env && echo ".env already exists - not overwriting" || cp .env.example .env

up: ## Start PostgreSQL + Redis only
	docker compose up -d db redis

down: ## Stop every container (keeps volumes)
	docker compose down

logs: ## Tail the worker logs
	docker compose logs -f worker

migrate: ## Apply database migrations
	$(PYTHON) manage.py migrate

makemigrations: ## Generate migrations from model changes
	$(PYTHON) manage.py makemigrations

seed: ## Create a demo user, portfolio and API token
	$(PYTHON) manage.py seed_demo --autonomous --risk high --balance 25000 --seed-position

shell: ## Open a Django shell
	$(PYTHON) manage.py shell

test: ## Run the full test suite
	$(PYTHON) manage.py test core.tests

test-guardrails: ## Run only the safety-critical guardrail tests
	$(PYTHON) manage.py test core.tests.test_guardrails core.tests.test_security

lint: ## Lint with Ruff
	venv/bin/ruff check .

format: ## Auto-fix and format with Ruff
	venv/bin/ruff check . --fix
	venv/bin/ruff format .

check: lint ## Lint, format check, Django checks and the test suite
	venv/bin/ruff format --check .
	$(PYTHON) manage.py check
	$(PYTHON) manage.py makemigrations --check --dry-run
	$(PYTHON) manage.py test core.tests

docker-build: ## Build the application image
	docker compose build

docker-up: ## Start the whole stack
	docker compose up -d --build

docker-down: ## Stop the whole stack
	docker compose down

dry-run: ## Exercise the AI crew against the live LLM with no database writes
	docker compose exec web python manage.py dry_run_agent --portfolio-id 1

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -delete
	rm -rf .ruff_cache .pytest_cache htmlcov .coverage
