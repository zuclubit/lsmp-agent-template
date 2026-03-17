#!/usr/bin/env bash
# =============================================================================
# setup_dev_environment.sh
# LSMP — Full development environment bootstrap
# =============================================================================
set -euo pipefail

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

log()  { echo -e "${BOLD}${BLUE}▶${RESET} $*"; }
ok()   { echo -e "  ${GREEN}✓${RESET} $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET} $*"; }
fail() { echo -e "  ${RED}✗${RESET} $*"; exit 1; }

echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  LSMP — Development Environment Setup${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

# ─── 1. Python version check ─────────────────────────────────────────────────
log "Checking Python version..."
PYTHON_BIN="python3"
PYTHON_VERSION=$($PYTHON_BIN --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]; }; then
    fail "Python 3.9+ required. Found: $PYTHON_VERSION"
fi

if [ "$PYTHON_MINOR" -lt 11 ]; then
    warn "Python $PYTHON_VERSION detected. Project targets 3.11+."
    warn "Install 3.11 with: brew install python@3.11"
    warn "Then re-run: PYTHON=python3.11 bash scripts/setup_dev_environment.sh"
    warn "Continuing with Python $PYTHON_VERSION (may have compatibility issues)..."
else
    ok "Python $PYTHON_VERSION"
fi

# ─── 2. Docker check ─────────────────────────────────────────────────────────
log "Checking Docker..."
if ! command -v docker &>/dev/null; then
    fail "Docker not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
fi
if ! docker info &>/dev/null 2>&1; then
    fail "Docker daemon not running. Start Docker Desktop."
fi
DOCKER_VERSION=$(docker --version | awk '{print $3}' | tr -d ',')
ok "Docker $DOCKER_VERSION"

# ─── 3. .env setup ───────────────────────────────────────────────────────────
log "Setting up environment file..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    ok ".env created from .env.example"
    warn "Edit .env and add your ANTHROPIC_API_KEY and SF sandbox credentials"
else
    ok ".env already exists"
fi

# ─── 4. Create required directories ──────────────────────────────────────────
log "Creating required directories..."
mkdir -p logs .halcon/retrospectives .audit demo
ok "Directories created"

# ─── 5. Install Python dependencies ──────────────────────────────────────────
log "Installing Python dependencies..."
$PYTHON_BIN -m pip install --quiet --upgrade pip
$PYTHON_BIN -m pip install --quiet -r agents/requirements.txt
$PYTHON_BIN -m pip install --quiet \
    pyyaml python-dotenv asyncpg psycopg2-binary \
    fastapi uvicorn httpx aiofiles \
    pytest pytest-asyncio pytest-cov pytest-timeout
ok "Dependencies installed"

# ─── 6. Start Docker services ────────────────────────────────────────────────
log "Starting Docker services..."
cd infrastructure/docker
docker compose up -d postgres redis 2>/dev/null || docker-compose up -d postgres redis
cd "$PROJECT_ROOT"

# Wait for PostgreSQL
echo "  Waiting for PostgreSQL..."
MAX_WAIT=30; WAITED=0
until docker exec sfmigration-postgres pg_isready -U sfmigrationadmin -q 2>/dev/null; do
    if [ $WAITED -ge $MAX_WAIT ]; then
        warn "PostgreSQL not ready after ${MAX_WAIT}s. Run 'make status' to check."
        break
    fi
    sleep 2; WAITED=$((WAITED + 2))
done
[ $WAITED -lt $MAX_WAIT ] && ok "PostgreSQL ready (localhost:5432)"

# ─── 7. Create databases ─────────────────────────────────────────────────────
log "Creating databases..."
docker exec sfmigration-postgres psql -U sfmigrationadmin -d postgres -tc \
    "SELECT 1 FROM pg_database WHERE datname='legacy_db'" | grep -q 1 || \
docker exec sfmigration-postgres psql -U sfmigrationadmin -d postgres \
    -c "CREATE DATABASE legacy_db WITH ENCODING='UTF8' LC_COLLATE='en_US.UTF-8' LC_CTYPE='en_US.UTF-8' TEMPLATE=template0;" \
    2>/dev/null && ok "legacy_db created" || ok "legacy_db already exists"

# ─── 8. System validation ─────────────────────────────────────────────────────
log "Running system validation..."
$PYTHON_BIN scripts/validate_system.py 2>&1 | tail -6

# ─── 9. Seed demo data ────────────────────────────────────────────────────────
log "Seeding demo legacy data..."
if [ -f "demo/seed_legacy_db.py" ]; then
    $PYTHON_BIN demo/seed_legacy_db.py && ok "Demo data seeded"
else
    warn "demo/seed_legacy_db.py not found — run 'make seed' after setup"
fi

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}${GREEN}  ✓ Environment ready!${RESET}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo "  Next steps:"
echo "    1. Edit ${BOLD}.env${RESET} — add ANTHROPIC_API_KEY + SF sandbox credentials"
echo "    2. ${BOLD}make mock-sf${RESET}  — start local mock Salesforce server"
echo "    3. ${BOLD}make demo${RESET}     — run the full demo migration pipeline"
echo "    4. ${BOLD}make validate${RESET} — verify system health"
echo "    5. ${BOLD}make test${RESET}     — run test suite"
echo ""
echo "  Services:"
echo "    PostgreSQL  → localhost:5432 (sfmigrationadmin / Dev_P@ssw0rd_2024!)"
echo "    PgAdmin     → http://localhost:5050  (if started with make start-all)"
echo "    Mock SF API → http://localhost:9001  (after make mock-sf)"
echo "    Control API → http://localhost:8000  (after make start-all)"
echo ""
