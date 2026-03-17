# =============================================================================
# Makefile — Legacy-to-Salesforce Migration Platform (LSMP)
# Developer workflow commands
# =============================================================================

.PHONY: help setup install start stop restart logs \
        seed seed-legacy migrate mock-sf dashboard \
        sf-setup sf-deploy sf-deploy-dry sf-open \
        validate test test-unit test-security test-integration \
        clean clean-data status

PYTHON        := python3
PIP           := $(PYTHON) -m pip
COMPOSE       := docker compose -f infrastructure/docker/docker-compose.yml
COMPOSE_TEST  := docker compose -f infrastructure/docker/docker-compose.test.yml
ENV_FILE      := .env
PROJECT_ROOT  := $(shell pwd)
BOLD          := \033[1m
GREEN         := \033[32m
YELLOW        := \033[33m
RED           := \033[31m
RESET         := \033[0m

# Default target
.DEFAULT_GOAL := help

## ─── HELP ──────────────────────────────────────────────────────────────────

help: ## Show this help message
	@echo ""
	@echo "$(BOLD)LSMP — Development Environment$(RESET)"
	@echo ""
	@echo "$(BOLD)SETUP$(RESET)"
	@grep -E '^(setup|install):.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  $(GREEN)make %-22s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(BOLD)SERVICES$(RESET)"
	@grep -E '^(start|stop|restart|logs|status):.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  $(GREEN)make %-22s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(BOLD)DATA$(RESET)"
	@grep -E '^(seed|seed-legacy|mock-sf|dashboard):.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  $(GREEN)make %-22s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(BOLD)MIGRATION$(RESET)"
	@grep -E '^(migrate|demo|sf-.*):.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  $(GREEN)make %-22s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(BOLD)VALIDATION & TESTS$(RESET)"
	@grep -E '^(validate|test.*):.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  $(GREEN)make %-22s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(BOLD)CLEANUP$(RESET)"
	@grep -E '^clean.*:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  $(GREEN)make %-22s$(RESET) %s\n", $$1, $$2}'
	@echo ""

## ─── SETUP ──────────────────────────────────────────────────────────────────

setup: ## Full environment bootstrap (run this first)
	@echo "$(BOLD)$(GREEN)▶ Setting up LSMP development environment...$(RESET)"
	@$(MAKE) _check_python
	@$(MAKE) _check_docker
	@$(MAKE) _copy_env
	@$(MAKE) install
	@echo ""
	@echo "$(BOLD)$(GREEN)✓ Setup complete!$(RESET)"
	@echo ""
	@echo "  Next steps:"
	@echo "    1. Edit $(BOLD).env$(RESET) — add your ANTHROPIC_API_KEY and SF sandbox credentials"
	@echo "    2. Run $(BOLD)make start$(RESET) — start Docker services"
	@echo "    3. Run $(BOLD)make seed$(RESET)  — seed demo legacy data"
	@echo "    4. Run $(BOLD)make demo$(RESET)   — run the full demo pipeline"
	@echo ""

install: ## Install Python dependencies (all layers)
	@echo "$(BOLD)▶ Installing dependencies...$(RESET)"
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -r agents/requirements.txt
	@$(PIP) install --quiet pyyaml python-dotenv asyncpg psycopg2-binary fastapi uvicorn httpx
	@echo "  $(GREEN)✓ Dependencies installed$(RESET)"

_check_python:
	@echo "  Checking Python version..."
	@$(PYTHON) -c "import sys; v=sys.version_info; print(f'  Python {v.major}.{v.minor}.{v.micro}'); assert (v.major,v.minor)>=(3,11), f'\n  ERROR: Python 3.11+ required (found {v.major}.{v.minor}). Install via: brew install python@3.11\n  Then run: make setup PYTHON=python3.11'" 2>&1 || (echo "  $(YELLOW)⚠ Python 3.9 detected. Continuing (some features may need 3.11+)$(RESET)")

_check_docker:
	@echo "  Checking Docker..."
	@docker info > /dev/null 2>&1 && echo "  $(GREEN)✓ Docker running$(RESET)" || (echo "  $(RED)✗ Docker not running — start Docker Desktop first$(RESET)" && exit 1)

_copy_env:
	@if [ ! -f "$(ENV_FILE)" ]; then \
		cp .env.example $(ENV_FILE); \
		echo "  $(GREEN)✓ .env created from .env.example$(RESET)"; \
		echo "  $(YELLOW)  → Edit .env and add your ANTHROPIC_API_KEY$(RESET)"; \
	else \
		echo "  $(YELLOW)⚠ .env already exists — skipping copy$(RESET)"; \
	fi

## ─── SERVICES ───────────────────────────────────────────────────────────────

start: ## Start all Docker services (postgres, redis, kafka)
	@echo "$(BOLD)▶ Starting Docker services...$(RESET)"
	@$(COMPOSE) up -d postgres redis
	@echo "  Waiting for PostgreSQL to be ready..."
	@sleep 5
	@$(COMPOSE) exec -T postgres pg_isready -U sfmigrationadmin -d sfmigration 2>/dev/null \
		&& echo "  $(GREEN)✓ PostgreSQL ready$(RESET)" \
		|| echo "  $(YELLOW)⚠ PostgreSQL not yet ready — try: make status$(RESET)"
	@echo ""
	@echo "  Services:"
	@echo "    PostgreSQL  → localhost:5432 (sfmigrationadmin / Dev_P@ssw0rd_2024!)"
	@echo "    Redis       → localhost:6379"
	@echo "    PgAdmin     → http://localhost:5050 (admin@migration.dev / admin)"
	@echo ""

start-all: ## Start all services including Kafka, PgAdmin
	@echo "$(BOLD)▶ Starting all Docker services...$(RESET)"
	@$(COMPOSE) up -d
	@echo "  $(GREEN)✓ All services starting$(RESET)"
	@echo ""
	@echo "  Services:"
	@echo "    PostgreSQL  → localhost:5432"
	@echo "    Redis       → localhost:6379"
	@echo "    Kafka       → localhost:29092"
	@echo "    Kafka UI    → http://localhost:9090"
	@echo "    PgAdmin     → http://localhost:5050"
	@echo ""

stop: ## Stop all Docker services
	@echo "$(BOLD)▶ Stopping Docker services...$(RESET)"
	@$(COMPOSE) down
	@echo "  $(GREEN)✓ Services stopped$(RESET)"

restart: ## Restart all Docker services
	@$(MAKE) stop
	@$(MAKE) start

logs: ## Tail Docker service logs
	@$(COMPOSE) logs -f --tail=100

logs-db: ## Tail PostgreSQL logs only
	@$(COMPOSE) logs -f --tail=50 postgres

status: ## Show service status
	@echo "$(BOLD)Service Status$(RESET)"
	@$(COMPOSE) ps

## ─── DATA ───────────────────────────────────────────────────────────────────

seed: seed-legacy ## Seed all demo data (legacy DB + migration tracking)
	@echo "$(GREEN)$(BOLD)✓ Demo data ready.$(RESET)"
	@echo "  Run $(BOLD)make demo$(RESET) to start the migration pipeline."

seed-legacy: ## Seed legacy database with demo records (Account, Contact, Opportunity)
	@echo "$(BOLD)▶ Seeding legacy database...$(RESET)"
	@$(PYTHON) demo/seed_legacy_db.py
	@echo "  $(GREEN)✓ Legacy database seeded$(RESET)"

mock-sf: ## Start local mock Salesforce API server (port 9001)
	@echo "$(BOLD)▶ Starting mock Salesforce server on http://localhost:9001$(RESET)"
	@$(PYTHON) demo/mock_sf_server.py

dashboard: ## Start local migration control dashboard (http://localhost:8080)
	@echo "$(BOLD)▶ Starting LSMP Dashboard on http://localhost:8080$(RESET)"
	@$(PYTHON) dashboard/app.py

## ─── MIGRATION ───────────────────────────────────────────────────────────────

demo: ## Run the full demo migration pipeline (dry-run)
	@echo "$(BOLD)▶ Running demo migration pipeline...$(RESET)"
	@$(PYTHON) demo/run_demo_pipeline.py

migrate: ## Run a real migration (requires SF credentials in .env)
	@echo "$(BOLD)$(YELLOW)▶ Running migration (MIGRATION_DRY_RUN controls whether writes happen)...$(RESET)"
	@$(PYTHON) -m agents.orchestrator.multi_agent_orchestrator \
		"Run the full migration pipeline for all pending legacy records."

sf-setup: ## Interactive Salesforce sandbox authentication and .env update
	@python3 scripts/setup_salesforce_sandbox.py

sf-deploy: ## Deploy Salesforce metadata (SFDX) to sandbox org
	@bash scripts/deploy_to_salesforce.sh $(ORG)

sf-deploy-dry: ## Validate metadata deploy without committing (dry-run)
	@bash scripts/deploy_to_salesforce.sh $(ORG) --dry-run

sf-open: ## Open the Salesforce org in browser
	@sf org open --target-org $(ORG) --path "/lightning/app/Migration_Control_Center" 2>/dev/null || \
	 sfdx force:org:open --targetusername $(ORG) 2>/dev/null || echo "Run: sf org open"

## ─── VALIDATION & TESTS ──────────────────────────────────────────────────────

validate: ## Validate system configuration (32 checks)
	@echo "$(BOLD)▶ Running system validation...$(RESET)"
	@$(PYTHON) scripts/validate_system.py

test: ## Run all tests
	@echo "$(BOLD)▶ Running test suite...$(RESET)"
	@$(PYTHON) -m pytest tests/ --override-ini="addopts=" -v --timeout=60 -q

test-unit: ## Run unit tests only (no I/O)
	@$(PYTHON) -m pytest tests/ --override-ini="addopts=" -m unit -v -q

test-security: ## Run security tests
	@$(PYTHON) -m pytest tests/security-tests/ tests/mcp-tests/ tests/context-tests/ \
		--override-ini="addopts=" -v --tb=short -q

test-integration: ## Run integration tests (requires ANTHROPIC_API_KEY)
	@$(PYTHON) -m pytest tests/integration-tests/ --override-ini="addopts=" \
		-v -m integration --timeout=120

## ─── CLEANUP ─────────────────────────────────────────────────────────────────

clean: ## Remove Python cache files and test artifacts
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@rm -rf .pytest_cache htmlcov .coverage coverage.xml 2>/dev/null || true
	@echo "  $(GREEN)✓ Cache files removed$(RESET)"

clean-data: ## Drop and recreate databases (WARNING: deletes all demo data)
	@echo "$(RED)$(BOLD)⚠ This will delete all database data. Press Ctrl+C to cancel.$(RESET)"
	@sleep 3
	@$(COMPOSE) exec -T postgres psql -U sfmigrationadmin -d postgres \
		-c "DROP DATABASE IF EXISTS legacy_db;" \
		-c "DROP DATABASE IF EXISTS sfmigration;" 2>/dev/null || true
	@echo "  $(GREEN)✓ Databases dropped. Run 'make start && make seed' to recreate.$(RESET)"

clean-all: clean clean-data ## Full cleanup including Docker volumes
	@$(COMPOSE) down -v
	@echo "  $(GREEN)✓ Full cleanup complete$(RESET)"
