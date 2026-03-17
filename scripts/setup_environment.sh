#!/usr/bin/env bash
# =============================================================================
# setup_environment.sh
# =============================================================================
# Full environment setup for the Legacy-to-Salesforce migration project.
#
# What this script does:
#   1. Checks system prerequisites (Python 3.11+, pip, pg_dump, jq, curl)
#   2. Creates and activates a virtual environment
#   3. Installs Python dependencies (from pyproject.toml or requirements)
#   4. Creates the .env file from .env.example if it does not exist
#   5. Creates required runtime directories (logs, data/tmp, reports)
#   6. Validates that all required environment variables are set
#   7. Optionally runs database migrations (Alembic)
#   8. Prints a summary of the environment
#
# Usage:
#   chmod +x scripts/setup_environment.sh
#   ./scripts/setup_environment.sh [--skip-deps] [--skip-db]
#
# Options:
#   --skip-deps   Skip Python dependency installation
#   --skip-db     Skip database migration step
#   --dev         Install development dependencies as well
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colour output
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

log_info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
log_ok()      { echo -e "${GREEN}[OK]${RESET}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
log_section() { echo -e "\n${BOLD}=== $* ===${RESET}"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
SKIP_DEPS=false
SKIP_DB=false
DEV_MODE=false

for arg in "$@"; do
  case $arg in
    --skip-deps) SKIP_DEPS=true ;;
    --skip-db)   SKIP_DB=true ;;
    --dev)       DEV_MODE=true ;;
    -h|--help)
      echo "Usage: $0 [--skip-deps] [--skip-db] [--dev]"
      exit 0 ;;
    *) log_warn "Unknown argument: $arg" ;;
  esac
done

# ---------------------------------------------------------------------------
# Determine project root (the directory containing this script's parent)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

log_section "Legacy → Salesforce Migration Environment Setup"
log_info "Project root: $PROJECT_ROOT"

# ---------------------------------------------------------------------------
# 1. Check system prerequisites
# ---------------------------------------------------------------------------
log_section "Checking System Prerequisites"

check_command() {
  local cmd="$1"
  local min_version="${2:-}"
  if command -v "$cmd" &>/dev/null; then
    log_ok "$cmd found: $(command -v "$cmd")"
  else
    log_error "$cmd is not installed or not in PATH"
    return 1
  fi
}

PREREQ_FAILED=false

# Python 3.11+
if command -v python3 &>/dev/null; then
  PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
  PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
  if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 11 ]; then
    log_ok "Python $PY_VERSION found"
  else
    log_error "Python 3.11+ required, found $PY_VERSION"
    PREREQ_FAILED=true
  fi
else
  log_error "python3 not found"
  PREREQ_FAILED=true
fi

check_command pip3         || PREREQ_FAILED=true
check_command git          || log_warn "git not found (optional)"
check_command jq           || log_warn "jq not found (optional, used by validation scripts)"
check_command curl         || log_warn "curl not found (optional)"
check_command pg_dump      || log_warn "pg_dump not found (required for backup script)"
check_command openssl      || log_warn "openssl not found (optional, used for checksum verification)"

if [ "$PREREQ_FAILED" = true ]; then
  log_error "One or more required prerequisites are missing. Aborting."
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Create virtual environment
# ---------------------------------------------------------------------------
log_section "Setting Up Virtual Environment"

VENV_DIR="$PROJECT_ROOT/.venv"
if [ -d "$VENV_DIR" ]; then
  log_ok "Virtual environment already exists: $VENV_DIR"
else
  log_info "Creating virtual environment…"
  python3 -m venv "$VENV_DIR"
  log_ok "Virtual environment created: $VENV_DIR"
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log_ok "Virtual environment activated"
log_info "Python: $(python --version)"
log_info "Pip:    $(pip --version)"

# ---------------------------------------------------------------------------
# 3. Install dependencies
# ---------------------------------------------------------------------------
if [ "$SKIP_DEPS" = false ]; then
  log_section "Installing Python Dependencies"

  # Upgrade pip and setuptools first
  pip install --upgrade pip setuptools wheel --quiet

  if [ -f "$PROJECT_ROOT/pyproject.toml" ]; then
    log_info "Installing from pyproject.toml…"
    if [ "$DEV_MODE" = true ]; then
      pip install -e ".[dev,test]" --quiet
    else
      pip install -e "." --quiet
    fi
    log_ok "Dependencies installed from pyproject.toml"
  elif [ -f "$PROJECT_ROOT/requirements.txt" ]; then
    log_info "Installing from requirements.txt…"
    pip install -r requirements.txt --quiet
    if [ "$DEV_MODE" = true ] && [ -f "$PROJECT_ROOT/requirements-dev.txt" ]; then
      pip install -r requirements-dev.txt --quiet
    fi
    log_ok "Dependencies installed from requirements.txt"
  else
    log_warn "No pyproject.toml or requirements.txt found; skipping package install"
  fi
else
  log_info "Skipping dependency installation (--skip-deps)"
fi

# ---------------------------------------------------------------------------
# 4. Create .env file
# ---------------------------------------------------------------------------
log_section "Environment Configuration"

ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"

if [ -f "$ENV_FILE" ]; then
  log_ok ".env file already exists"
else
  if [ -f "$ENV_EXAMPLE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    log_ok ".env created from .env.example"
    log_warn "IMPORTANT: Edit $ENV_FILE and fill in real credentials before running migrations"
  else
    log_warn ".env.example not found – creating minimal .env"
    cat > "$ENV_FILE" << 'EOF'
# Salesforce connection
SF_USERNAME=
SF_PASSWORD=
SF_SECURITY_TOKEN=
SF_INSTANCE_URL=https://login.salesforce.com
SF_API_VERSION=59.0

# Legacy database
LEGACY_DB_URL=postgresql+asyncpg://user:password@localhost:5432/legacy_erp

# Migration settings
MIGRATION_BATCH_SIZE=200
MIGRATION_ERROR_THRESHOLD=5.0
MIGRATION_DRY_RUN=false

# Logging
LOG_LEVEL=INFO
LOG_FILE=/var/log/migration/migration.log

# Notifications
NOTIFICATION_EMAILS=admin@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
EOF
  fi
fi

# ---------------------------------------------------------------------------
# 5. Create runtime directories
# ---------------------------------------------------------------------------
log_section "Creating Runtime Directories"

DIRS=(
  "$PROJECT_ROOT/logs"
  "$PROJECT_ROOT/data/tmp"
  "$PROJECT_ROOT/data/backups"
  "$PROJECT_ROOT/reports"
  "$PROJECT_ROOT/.cache"
)

for dir in "${DIRS[@]}"; do
  mkdir -p "$dir"
  log_ok "Created: $dir"
done

# Create .gitkeep files so directories are tracked
for dir in "${DIRS[@]}"; do
  touch "$dir/.gitkeep" 2>/dev/null || true
done

# ---------------------------------------------------------------------------
# 6. Validate required environment variables
# ---------------------------------------------------------------------------
log_section "Validating Environment Variables"

# Load .env
set -a
# shellcheck disable=SC1091
[ -f "$ENV_FILE" ] && source "$ENV_FILE"
set +a

REQUIRED_VARS=(
  "SF_USERNAME"
  "SF_PASSWORD"
  "SF_INSTANCE_URL"
  "LEGACY_DB_URL"
)

MISSING_VARS=()
for var in "${REQUIRED_VARS[@]}"; do
  if [ -z "${!var:-}" ]; then
    MISSING_VARS+=("$var")
    log_warn "Missing: $var"
  else
    log_ok "$var is set"
  fi
done

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
  log_warn "The following variables are not set in .env:"
  for v in "${MISSING_VARS[@]}"; do echo "  - $v"; done
  log_warn "Set them in $ENV_FILE before running migrations."
fi

# ---------------------------------------------------------------------------
# 7. Run database schema migrations (optional)
# ---------------------------------------------------------------------------
if [ "$SKIP_DB" = false ] && command -v alembic &>/dev/null; then
  log_section "Running Database Migrations"
  if [ -f "$PROJECT_ROOT/alembic.ini" ]; then
    log_info "Running Alembic migrations…"
    alembic upgrade head && log_ok "Database migrations applied" || log_warn "Alembic migration failed – check connection"
  else
    log_info "No alembic.ini found; skipping database migrations"
  fi
else
  log_info "Skipping database migrations (--skip-db or alembic not found)"
fi

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
log_section "Setup Complete"

echo ""
echo -e "  ${GREEN}✓${RESET} Project root:      $PROJECT_ROOT"
echo -e "  ${GREEN}✓${RESET} Virtual env:       $VENV_DIR"
echo -e "  ${GREEN}✓${RESET} Config file:       $ENV_FILE"
echo ""
echo -e "  ${BOLD}Next steps:${RESET}"
echo -e "    1. Edit ${CYAN}.env${RESET} with your credentials"
echo -e "    2. Run ${CYAN}./scripts/validate_salesforce_connection.py${RESET} to test connectivity"
echo -e "    3. Run ${CYAN}./scripts/run_migration.sh --dry-run${RESET} to validate data"
echo -e "    4. Run ${CYAN}./scripts/run_migration.sh${RESET} for the full migration"
echo ""
echo -e "  ${BOLD}Available Make targets:${RESET}"
echo -e "    ${CYAN}make test${RESET}         – Run test suite"
echo -e "    ${CYAN}make lint${RESET}         – Run ruff + mypy"
echo -e "    ${CYAN}make migrate${RESET}      – Start migration (interactive)"
echo -e "    ${CYAN}make report${RESET}       – Generate migration report"
echo ""
