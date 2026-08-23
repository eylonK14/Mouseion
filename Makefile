# Mouseion — Phases 1-3
#
# Recipes use a leading TAB (GNU make). If you are on Windows without make,
# every target's body is a plain docker compose / pytest command you can paste;
# see PROJECT_NOTES.md ("Running without make").

SHELL := /bin/sh
COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help up webui down build logs ps migrate revision test test-local shell clean env

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

webui: env ## Start the optional Open WebUI paper-QA chat surface
	$(COMPOSE) --profile webui up -d open-webui
	@echo "open webui: http://localhost:$${WEBUI_PORT:-3000}"

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

clean: ## Stop the stack and delete volumes (NOT ./data)
	$(COMPOSE) down -v
