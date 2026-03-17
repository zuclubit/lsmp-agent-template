#!/usr/bin/env bash
# =============================================================================
# run_migration.sh
# =============================================================================
# Migration execution script.
#
# Orchestrates a complete Legacy → Salesforce migration run:
#   1. Loads environment
#   2. Validates prerequisites
#   3. Takes a pre-migration backup (unless --skip-backup)
#   4. Invokes the migration via the CLI adapter
#   5. Monitors progress and tails logs
#   6. Generates the final report
#
# Usage:
#   ./scripts/run_migration.sh [OPTIONS]
#
# Options:
#   --phase PHASE        Run only a specific phase (extraction|validation|
#                        transformation|load|verification|reconciliation)
#   --dry-run            Simulate migration without writing to Salesforce
#   --record-types LIST  Comma-separated record types (default: Account,Contact)
#   --batch-size N       Records per API batch (default: 200)
#   --skip-backup        Skip the pre-migration database backup
#   --skip-validation    Skip the data validation phase
#   --log-level LEVEL    Set log level (DEBUG|INFO|WARNING|ERROR)
#   --job-id JOB_ID      Resume or target an existing job ID
#   --report-format FMT  Report format: html|json|csv (default: html)
#   --notify EMAIL       Override notification email (repeatable)
#   -y, --yes            Non-interactive: skip confirmation prompts
#   -h, --help           Show this help
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colours and logging
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

log_info()    { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${CYAN}[INFO]${RESET}  $*"; }
log_ok()      { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${GREEN}[OK]${RESET}    $*"; }
log_warn()    { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${YELLOW}[WARN]${RESET}  $*"; }
log_error()   { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${RED}[ERROR]${RESET} $*" >&2; }
log_section() { echo -e "\n${BOLD}==============================${RESET}"; echo -e "${BOLD}  $*${RESET}"; echo -e "${BOLD}==============================${RESET}"; }

# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------
PHASE="all"
DRY_RUN=false
RECORD_TYPES="Account,Contact"
BATCH_SIZE=200
SKIP_BACKUP=false
SKIP_VALIDATION=false
LOG_LEVEL="INFO"
JOB_ID=""
REPORT_FORMAT="html"
NOTIFY_EMAILS=()
NON_INTERACTIVE=false
START_TIME=$(date +%s)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case $1 in
    --phase)        PHASE="$2";         shift 2 ;;
    --dry-run)      DRY_RUN=true;       shift ;;
    --record-types) RECORD_TYPES="$2";  shift 2 ;;
    --batch-size)   BATCH_SIZE="$2";    shift 2 ;;
    --skip-backup)  SKIP_BACKUP=true;   shift ;;
    --skip-validation) SKIP_VALIDATION=true; shift ;;
    --log-level)    LOG_LEVEL="$2";     shift 2 ;;
    --job-id)       JOB_ID="$2";        shift 2 ;;
    --report-format) REPORT_FORMAT="$2"; shift 2 ;;
    --notify)       NOTIFY_EMAILS+=("$2"); shift 2 ;;
    -y|--yes)       NON_INTERACTIVE=true; shift ;;
    -h|--help)
      sed -n '/^# Usage/,/^# =====/p' "$0" | head -30
      exit 0 ;;
    *) log_error "Unknown option: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
LOG_DIR="$PROJECT_ROOT/logs"
REPORT_DIR="$PROJECT_ROOT/reports"
RUN_ID="$(date +%Y%m%d_%H%M%S)_$$"
LOG_FILE="$LOG_DIR/migration_${RUN_ID}.log"

mkdir -p "$LOG_DIR" "$REPORT_DIR"

# Redirect stdout/stderr to both terminal and log file
exec > >(tee -a "$LOG_FILE") 2>&1

log_section "Legacy → Salesforce Migration"

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
ENV_FILE="$PROJECT_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE"
  set +a
  log_ok "Environment loaded from $ENV_FILE"
else
  log_error ".env file not found at $ENV_FILE"
  log_error "Run ./scripts/setup_environment.sh first"
  exit 1
fi

# ---------------------------------------------------------------------------
# Validate Python interpreter
# ---------------------------------------------------------------------------
if [ ! -x "$VENV_PYTHON" ]; then
  log_error "Python venv not found at $VENV_PYTHON"
  log_error "Run ./scripts/setup_environment.sh first"
  exit 1
fi

# ---------------------------------------------------------------------------
# Display run configuration
# ---------------------------------------------------------------------------
log_section "Run Configuration"
echo ""
echo -e "  Run ID:          ${CYAN}$RUN_ID${RESET}"
echo -e "  Mode:            $([ "$DRY_RUN" = true ] && echo "${YELLOW}DRY RUN${RESET}" || echo "${GREEN}LIVE${RESET}")"
echo -e "  Phase:           $PHASE"
echo -e "  Record types:    $RECORD_TYPES"
echo -e "  Batch size:      $BATCH_SIZE"
echo -e "  Log file:        $LOG_FILE"
echo -e "  Skip backup:     $SKIP_BACKUP"
echo -e "  Skip validation: $SKIP_VALIDATION"
echo -e "  Log level:       $LOG_LEVEL"
echo -e "  SF Org:          ${SF_INSTANCE_URL:-NOT SET}"
echo -e "  SF User:         ${SF_USERNAME:-NOT SET}"
[ -n "$JOB_ID" ] && echo -e "  Job ID:          $JOB_ID"
echo ""

# ---------------------------------------------------------------------------
# Confirm (unless non-interactive or dry-run)
# ---------------------------------------------------------------------------
if [ "$NON_INTERACTIVE" = false ] && [ "$DRY_RUN" = false ]; then
  echo -e "${YELLOW}${BOLD}WARNING: This will WRITE data to Salesforce org: ${SF_INSTANCE_URL:-unknown}${RESET}"
  echo -e "${YELLOW}Ensure you have taken a backup and tested in a sandbox first.${RESET}"
  read -r -p "Proceed with LIVE migration? [y/N] " confirm
  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    log_info "Migration cancelled by user."
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
log_section "Pre-flight Checks"

# Check required env vars
REQUIRED_VARS=("SF_USERNAME" "SF_PASSWORD" "SF_INSTANCE_URL" "LEGACY_DB_URL")
for var in "${REQUIRED_VARS[@]}"; do
  if [ -z "${!var:-}" ]; then
    log_error "Required environment variable $var is not set"
    exit 1
  fi
  log_ok "$var is set"
done

# Validate Salesforce connectivity
log_info "Testing Salesforce connectivity…"
if "$VENV_PYTHON" "$SCRIPT_DIR/validate_salesforce_connection.py" \
     --instance-url "$SF_INSTANCE_URL" \
     --username "$SF_USERNAME" \
     --password "$SF_PASSWORD" \
     --token "${SF_SECURITY_TOKEN:-}" \
     --quiet 2>/dev/null; then
  log_ok "Salesforce connectivity verified"
else
  log_error "Cannot connect to Salesforce. Check credentials in .env"
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 1: Backup (unless skipped)
# ---------------------------------------------------------------------------
if [ "$SKIP_BACKUP" = false ] && [ "$DRY_RUN" = false ]; then
  log_section "Pre-Migration Backup"
  if bash "$SCRIPT_DIR/backup_legacy_data.sh" --run-id "$RUN_ID"; then
    log_ok "Backup completed"
  else
    log_error "Backup failed. Aborting migration."
    exit 1
  fi
else
  log_info "Skipping backup (--skip-backup or dry-run mode)"
fi

# ---------------------------------------------------------------------------
# Step 2: Data Validation
# ---------------------------------------------------------------------------
if [ "$SKIP_VALIDATION" = false ]; then
  log_section "Data Validation"
  IFS=',' read -ra RT_ARRAY <<< "$RECORD_TYPES"
  RT_ARGS=()
  for rt in "${RT_ARRAY[@]}"; do
    RT_ARGS+=("-r" "$rt")
  done

  log_info "Running data validation…"
  VALIDATION_OUTPUT=$("$VENV_PYTHON" -m adapters.inbound.cli_adapter migrate validate \
    "${RT_ARGS[@]}" \
    --output json 2>/dev/null || echo '{"can_proceed": false, "error": "validation command failed"}')

  CAN_PROCEED=$(echo "$VALIDATION_OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('can_proceed', False))" 2>/dev/null || echo "False")

  if [ "$CAN_PROCEED" = "True" ]; then
    log_ok "Validation passed – safe to proceed"
  else
    log_error "Validation found blocking errors. Review validation report before proceeding."
    echo "$VALIDATION_OUTPUT" | python3 -m json.tool 2>/dev/null || echo "$VALIDATION_OUTPUT"
    if [ "$NON_INTERACTIVE" = false ]; then
      read -r -p "Override validation failure and proceed anyway? [y/N] " override
      [[ "$override" =~ ^[Yy]$ ]] || exit 1
    else
      exit 1
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Step 3: Execute Migration
# ---------------------------------------------------------------------------
log_section "Executing Migration"

IFS=',' read -ra RT_ARRAY <<< "$RECORD_TYPES"
CLI_ARGS=()
for rt in "${RT_ARRAY[@]}"; do
  CLI_ARGS+=("-r" "$rt")
done
[ "$DRY_RUN" = true ] && CLI_ARGS+=("--dry-run")
CLI_ARGS+=("--batch-size" "$BATCH_SIZE")
CLI_ARGS+=("--source" "${LEGACY_SOURCE_SYSTEM:-legacy_erp}")
CLI_ARGS+=("--org-id" "$SF_ORG_ID")
for email in "${NOTIFY_EMAILS[@]}"; do
  CLI_ARGS+=("--notify" "$email")
done

log_info "Starting migration CLI: ${CLI_ARGS[*]}"

"$VENV_PYTHON" -m adapters.inbound.cli_adapter migrate start "${CLI_ARGS[@]}" \
  --output json | tee "$LOG_DIR/migration_output_${RUN_ID}.json"

MIGRATION_EXIT=${PIPESTATUS[0]}

if [ "$MIGRATION_EXIT" -ne 0 ]; then
  log_error "Migration CLI exited with code $MIGRATION_EXIT"
  exit "$MIGRATION_EXIT"
fi

# ---------------------------------------------------------------------------
# Step 4: Generate Report
# ---------------------------------------------------------------------------
log_section "Generating Migration Report"

REPORT_FILE="$REPORT_DIR/migration_${RUN_ID}.${REPORT_FORMAT}"

if [ -f "$LOG_DIR/migration_output_${RUN_ID}.json" ]; then
  CAPTURED_JOB_ID=$(python3 -c "
import json, sys
with open('$LOG_DIR/migration_output_${RUN_ID}.json') as f:
  data = json.load(f)
print(data.get('job_id', ''))
" 2>/dev/null || echo "")

  if [ -n "$CAPTURED_JOB_ID" ]; then
    log_info "Generating report for job $CAPTURED_JOB_ID…"
    "$VENV_PYTHON" "$SCRIPT_DIR/generate_migration_report.py" \
      --job-id "$CAPTURED_JOB_ID" \
      --format "$REPORT_FORMAT" \
      --output "$REPORT_FILE" || log_warn "Report generation failed (non-fatal)"
    [ -f "$REPORT_FILE" ] && log_ok "Report saved: $REPORT_FILE"
  fi
fi

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
MINUTES=$((ELAPSED / 60))
SECONDS=$((ELAPSED % 60))

log_section "Migration Complete"
echo ""
echo -e "  Run ID:       $RUN_ID"
echo -e "  Duration:     ${MINUTES}m ${SECONDS}s"
echo -e "  Log file:     $LOG_FILE"
[ -f "$REPORT_FILE" ] && echo -e "  Report:       $REPORT_FILE"
echo -e "  Mode:         $([ "$DRY_RUN" = true ] && echo "DRY RUN" || echo "LIVE")"
echo ""
log_ok "Migration run $RUN_ID finished"
