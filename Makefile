# AlphaAgent — developer shortcuts.
# Run `make` or `make help` to list every target.

.DEFAULT_GOAL := help
SHELL := /bin/bash
PYTHON ?= venv/bin/python
PIP ?= venv/bin/pip
PIP_COMPILE ?= venv/bin/pip-compile

.PHONY: help venv lock lock-dev install install-dev env up down start stop status logs migrate makemigrations audit \
        wiki-push \
        coverage coverage-badge \
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

wiki-push: ## Mirror docs/wiki/ to the GitHub Wiki (README.md is repo-only, not a page)
	@# The wiki is a separate repository, so it needs its own credentials. When
	@# .git-token exists it is used for this push only, via a throwaway credential
	@# file that is removed afterwards; otherwise git falls back to whatever
	@# credential helper the machine already has configured.
	@cred=""; \
	tmp=$$(mktemp -d) && \
	if [ -s .git-token ]; then \
	  cred=$$tmp/.gc; \
	  printf 'https://SergeyGer:%s@github.com\n' "$$(tr -d '[:space:]' < .git-token)" > $$cred; \
	  chmod 600 $$cred; \
	fi; \
	git -c credential.helper="$${cred:+store --file=$$cred}" clone --depth 1 https://github.com/SergeyGer/AlphaAgent.wiki.git $$tmp/wiki 2>/dev/null && \
	rm -f $$tmp/wiki/*.md && \
	find docs/wiki -maxdepth 1 -name '*.md' ! -name 'README.md' -exec cp {} $$tmp/wiki/ \; && \
	cd $$tmp/wiki && \
	git add -A && \
	(git diff --cached --quiet && echo "wiki already up to date" || \
	 (git -c user.name="AlphaAgent" -c user.email="noreply@github.com" \
	    commit -m "docs: sync wiki from docs/wiki" && \
	  git -c credential.helper="$${cred:+store --file=$$cred}" push origin master)) ; \
	rm -rf $$tmp

coverage: ## Run the suite under coverage and print the report
	$(PYTHON) -m coverage run manage.py test core.tests
	$(PYTHON) -m coverage report

coverage-badge: coverage ## Regenerate coverage.svg for the README
	$(PYTHON) scripts/coverage_badge.py -o coverage.svg

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

audit: ## Scan the pinned dependencies for known vulnerabilities
	@# WHY a throwaway environment rather than the project venv: pip-audit requires
	@# tomli>=2.2.1 and tomli-w>=1.2.0, while crewai pins tomli~=2.0.2 and
	@# tomli-w~=1.1.0. Those two ranges do not intersect, so no resolver can put
	@# both in one environment - adding pip-audit to requirements-dev.in fails with
	@# ResolutionImpossible, correctly. Auditing from outside sidesteps the conflict
	@# entirely and still checks the lockfile that actually ships.
	@tmp=$$(mktemp -d); \
	echo "  installing pip-audit in $$tmp"; \
	$(PYTHON) -m venv $$tmp/venv && $$tmp/venv/bin/pip install -q --upgrade pip pip-audit; \
	echo "  auditing requirements.txt"; \
	$$tmp/venv/bin/pip-audit -r requirements.txt --progress-spinner off \
		--ignore-vuln PYSEC-2026-311 \
		--ignore-vuln PYSEC-2026-3813 \
		--ignore-vuln PYSEC-2026-3814 \
		--ignore-vuln PYSEC-2026-3815; \
	rc=$$?; rm -rf $$tmp; exit $$rc

# WHY those four are ignored rather than fixed, and why ignoring them is not the
# same as pretending they are absent:
#
#   - chromadb 1.5.9 is the newest published release; none of the four advisories
#     has a fix version, so there is nothing to upgrade to.
#   - chromadb arrives transitively through crewai and is imported at load time,
#     but this project sets memory=False, so CrewAI's memory subsystem - the only
#     consumer of chromadb here - never runs.
#   - They are listed individually rather than silenced with an exclude, so a NEW
#     advisory, or a fix appearing for one of these, still fails the target.
#
# This target is the only place these are visible: GitHub's Dependabot advisory
# database does not report them, which is how they went unnoticed until the target
# existed.

# ---------------------------------------------------------------------------
# Start / stop the whole stack
#
# The three commands below are the ones you want day to day. They are separate on
# purpose, because "stop working on this project" has two different meanings and
# picking the wrong one is what makes services from two projects fight over a port.
#
#   make stop   containers stop, the stack stays assembled - fastest to resume,
#               and enough to free the CPU and the network ports
#   make down   containers are removed and the network is released - use this
#               when switching to another project for a while
#   make start  build if needed and start everything detached
#
# Volumes are never touched by any of them: your database and Redis data survive
# both. `docker compose down -v` is the only thing that deletes data, and it is
# deliberately not wrapped in a target.
# ---------------------------------------------------------------------------
start: ## Start the whole stack in the background (builds if needed)
	docker compose up -d --build
	@echo ""
	@echo "  Dashboard: http://127.0.0.1:$${ALPHA_WEB_PORT:-8080}/   (demo / demo-pass-123)"
	@echo "  Logs:      make logs"
	@echo "  Stop:      make stop"

stop: ## Stop every container, keeping the stack assembled (fast to resume)
	docker compose stop
	@echo ""
	@echo "  Stopped. Ports are free. Resume with: make start"

status: ## Show what is running and which ports are taken
	@docker compose ps --format "  {{.Service}}\t{{.Status}}\t{{.Ports}}"

docker-build: ## Build the application image
	docker compose build

docker-up start: ## Alias for `make start`
docker-down: ## Stop and remove every container (frees the network)
	docker compose down
	@echo ""
	@echo "  Removed. Resume with: make start"

dry-run: ## Exercise the AI crew against the live LLM with no database writes
	docker compose exec web python manage.py dry_run_agent --portfolio-id 1

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -delete
	rm -rf .ruff_cache .pytest_cache htmlcov .coverage
