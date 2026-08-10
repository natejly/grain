PNPM := npx --yes pnpm@9.15.9
# The API needs Python 3.10+ (MCP SDK floor). Override if your 3.10+ binary
# has a different name: make install PYTHON=python3.11
PYTHON ?= python3.12

.PHONY: install dev dev-api dev-web test test-e2e lint build eval migrate seed verify

dev:
	./scripts/dev.sh

install:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/pip install -e "apps/api[dev]"
	$(PNPM) install

dev-api:
	.venv/bin/uvicorn app.main:app --app-dir apps/api --reload --host 127.0.0.1 --port 8000

dev-web:
	$(PNPM) --filter @workspace/web dev

test:
	.venv/bin/pytest apps/api/tests
	$(PNPM) test

test-e2e:
	$(PNPM) test:e2e

lint:
	.venv/bin/ruff check apps/api
	.venv/bin/mypy apps/api/app
	$(PNPM) lint
	$(PNPM) typecheck

build:
	$(PNPM) build

eval:
	PYTHONPATH=apps/api .venv/bin/python apps/api/scripts/evaluate_retrieval.py

migrate:
	cd apps/api && ../../.venv/bin/alembic upgrade head

verify: lint test eval build test-e2e

seed:
	.venv/bin/python apps/api/scripts/seed.py
