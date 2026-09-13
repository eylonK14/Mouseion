# Mouseion — complete system
#
# Recipes use a leading TAB (GNU make). If you are on Windows without make,
# every target's body is a plain docker compose / pytest command you can paste;
# see PROJECT_NOTES.md ("Running without make").

SHELL := /bin/sh
COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help up webui down build logs ps migrate revision test test-local shell clean env first-paper backup backup-dry-run restore-drill reembed reindex-fts health-full

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env — set API_TOKEN and OPENROUTER_API_KEY")

up: env ## Build if needed, run migrations, start the stack
	$(COMPOSE) up -d --build
	@echo "api:      http://localhost:$${API_PORT:-8000}"
	@echo "library:  http://localhost:$${API_PORT:-8000}/"
	@echo "taxonomy: http://localhost:$${API_PORT:-8000}/taxonomy"

webui: env ## Start the optional Open WebUI QA and test-mode chat surface
	$(COMPOSE) --profile webui up -d open-webui
	@echo "open webui: http://localhost:$${WEBUI_PORT:-3000}"

first-paper: ## Ingest the Attention Is All You Need arXiv paper and wait for completion
	$(COMPOSE) exec -T api python -m mouseion.smoke_ingest

down: ## Stop the stack (keeps ./data and volumes)
	$(COMPOSE) down

build: ## Rebuild images without starting
	$(COMPOSE) build

logs: ## Tail api + worker logs
	$(COMPOSE) logs -f api worker

ps: ## Show service status
	$(COMPOSE) ps

migrate: ## Apply Alembic migrations to head
	$(COMPOSE) run --rm migrate

revision: ## Create a migration: make revision m="add x"
	$(COMPOSE) run --rm --entrypoint alembic api revision -m "$(m)"

test: ## Run the test suite inside the api image
	$(COMPOSE) run --rm --no-deps --entrypoint pytest api -q

test-local: ## Run the test suite against a local virtualenv (no docker)
	cd backend && python -m pytest -q

shell: ## Open a shell in the api image
	$(COMPOSE) run --rm --no-deps --entrypoint sh api

backup: ## Create a consistent dated SQLite/PDF/tree snapshot
	$(COMPOSE) run --rm --no-deps api python -m mouseion.maintenance backup

backup-dry-run: ## Show what a backup would capture and prune
	$(COMPOSE) run --rm --no-deps api python -m mouseion.maintenance backup --dry-run

restore-drill: backup ## Restore the latest snapshot into scratch and verify it
	$(COMPOSE) run --rm --no-deps api python -m mouseion.maintenance restore-drill

reembed: ## Re-embed papers missing the configured model marker or carrying an old one
	$(COMPOSE) run --rm --no-deps api python -m mouseion.maintenance reembed

reindex-fts: ## Rebuild the FTS index from source tables
	$(COMPOSE) run --rm --no-deps api python -m mouseion.maintenance reindex-fts

health-full: ## Run full startup diagnostics from the API image
	$(COMPOSE) run --rm --no-deps api python -m mouseion.maintenance health-check

clean: ## Stop the stack and delete volumes (NOT ./data)
	$(COMPOSE) down -v
