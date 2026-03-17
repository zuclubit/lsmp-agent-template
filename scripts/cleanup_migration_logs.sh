#!/usr/bin/env bash
# =============================================================================
# cleanup_migration_logs.sh
# =============================================================================
# Cleans up migration log files, temporary data, and old report artefacts.
#
# What is cleaned:
#   1. Log files older than LOG_RETENTION_DAYS (default: 90)
#   2. Temporary staging data in data/tmp (older than TMP_RETENTION_DAYS: 7)
#   3. HTML/JSON reports older than REPORT_RETENTION_DAYS (default: 180)
#   4. Dead-letter event files older than DEAD_LETTER_RETENTION_DAYS (default: 30)
#   5. Python cache files (__pycache__, *.pyc, *.pyo)
#   6. Coverage reports and test artefacts older than TEST_RETENTION_DAYS (default: 14)
#   7. Empty directories
#
# Usage:
#   ./scripts/cleanup_migration_logs.sh [OPTIONS]
#
# Options:
#   --dry-run             Show what would be deleted without deleting
#   --log-retention N     Retain logs for N days (default: 90)
#   --tmp-retention N     Retain tmp files for N days (default: 7)
#   --report-retention N  Retain reports for N days (default: 180)
#   --force               Skip confirmation prompt
#   --pycache             Also clean Python cache files
#   --all                 Clean everything (equivalent to --pycache + all retentions)
#   -h, --help            Show this help
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

log_info()    { echo -e "${CYAN}[CLEANUP]${RESET} $*"; }
log_ok()      { echo -e "${GREEN}[OK]${RESET}      $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${RESET}    $*"; }
log_error()   { echo -e "${RED}[ERROR]${RESET}   $*" >&2; }
log_delete()  { echo -e "${RED}[DELETE]${RESET}  $*"; }
log_skip()    { echo -e "${YELLOW}[SKIP]${RESET}    $*"; }

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY_RUN=false
LOG_RETENTION_DAYS=90
TMP_RETENTION_DAYS=7
REPORT_RETENTION_DAYS=180
DEAD_LETTER_RETENTION_DAYS=30
TEST_RETENTION_DAYS=14
CLEAN_PYCACHE=false
FORCE=false

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)           DRY_RUN=true;                        shift ;;
    --log-retention)     LOG_RETENTION_DAYS="$2";             shift 2 ;;
    --tmp-retention)     TMP_RETENTION_DAYS="$2";             shift 2 ;;
    --report-retention)  REPORT_RETENTION_DAYS="$2";          shift 2 ;;
    --force)             FORCE=true;                          shift ;;
    --pycache)           CLEAN_PYCACHE=true;                  shift ;;
    --all)               CLEAN_PYCACHE=true;                  shift ;;
    -h|--help)
      head -40 "$0" | grep "^#"
      exit 0 ;;
    *) log_warn "Unknown option: $1"; shift ;;
  esac
done

# ---------------------------------------------------------------------------
# Show configuration
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}Migration Cleanup Tool${RESET}"
echo -e "Project:        $PROJECT_ROOT"
echo -e "Mode:           $([ "$DRY_RUN" = true ] && echo "${YELLOW}DRY RUN${RESET}" || echo "${RED}LIVE${RESET}")"
echo -e "Log retention:  $LOG_RETENTION_DAYS days"
echo -e "Tmp retention:  $TMP_RETENTION_DAYS days"
echo -e "Report retention: $REPORT_RETENTION_DAYS days"
echo -e "Dead-letter retention: $DEAD_LETTER_RETENTION_DAYS days"
echo ""

# ---------------------------------------------------------------------------
# Confirm (unless forced or dry-run)
# ---------------------------------------------------------------------------
if [ "$FORCE" = false ] && [ "$DRY_RUN" = false ]; then
  read -r -p "Proceed with cleanup? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { log_info "Cancelled."; exit 0; }
fi

# ---------------------------------------------------------------------------
# Helper: delete files older than N days
# ---------------------------------------------------------------------------
TOTAL_DELETED=0
TOTAL_BYTES=0

delete_old_files() {
  local dir="$1"
  local pattern="$2"
  local days="$3"
  local label="$4"

  if [ ! -d "$dir" ]; then
    log_warn "Directory not found: $dir (skipping)"
    return
  fi

  local count=0
  local bytes=0

  while IFS= read -r file; do
    if [ -f "$file" ]; then
      fsize=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null || echo 0)
      bytes=$((bytes + fsize))
      count=$((count + 1))

      if [ "$DRY_RUN" = true ]; then
        log_skip "$label: $file ($(numfmt --to=iec "$fsize" 2>/dev/null || echo "${fsize}B"))"
      else
        log_delete "$file"
        rm -f "$file"
      fi
    fi
  done < <(find "$dir" -name "$pattern" -mtime +"$days" -type f 2>/dev/null || true)

  if [ "$count" -gt 0 ]; then
    SIZE_HUMAN=$(numfmt --to=iec "$bytes" 2>/dev/null || echo "${bytes}B")
    log_ok "$label: $( [ "$DRY_RUN" = true ] && echo "would delete" || echo "deleted") $count file(s) (~$SIZE_HUMAN)"
    TOTAL_DELETED=$((TOTAL_DELETED + count))
    TOTAL_BYTES=$((TOTAL_BYTES + bytes))
  else
    log_info "$label: nothing to clean (no files older than $days days)"
  fi
}

# ---------------------------------------------------------------------------
# 1. Clean log files
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}--- Logs ---${RESET}"
delete_old_files "$PROJECT_ROOT/logs" "*.log"   "$LOG_RETENTION_DAYS"     "Migration logs"
delete_old_files "$PROJECT_ROOT/logs" "*.jsonl" "$DEAD_LETTER_RETENTION_DAYS" "Structured log files"
delete_old_files "$PROJECT_ROOT/logs" "migration_output_*.json" "$LOG_RETENTION_DAYS" "Migration output JSON"

# ---------------------------------------------------------------------------
# 2. Clean temporary data files
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}--- Temporary Data ---${RESET}"
delete_old_files "$PROJECT_ROOT/data/tmp" "*"       "$TMP_RETENTION_DAYS" "Temp files"
delete_old_files "/tmp" "migration_*.jsonl"           "$TMP_RETENTION_DAYS" "Dead-letter events"
delete_old_files "/tmp" "migration_*.json"            "$TMP_RETENTION_DAYS" "Temp migration files"

# ---------------------------------------------------------------------------
# 3. Clean old reports
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}--- Reports ---${RESET}"
delete_old_files "$PROJECT_ROOT/reports" "*.html"   "$REPORT_RETENTION_DAYS" "HTML reports"
delete_old_files "$PROJECT_ROOT/reports" "*.json"   "$REPORT_RETENTION_DAYS" "JSON reports"
delete_old_files "$PROJECT_ROOT/reports" "*.csv"    "$REPORT_RETENTION_DAYS" "CSV reports"
delete_old_files "$PROJECT_ROOT/reports" "*.pdf"    "$REPORT_RETENTION_DAYS" "PDF reports"

# ---------------------------------------------------------------------------
# 4. Clean old backup manifests (keep actual .dump files for safety)
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}--- Backup Manifests ---${RESET}"
delete_old_files "$PROJECT_ROOT/data/backups" "*.manifest.json" "$REPORT_RETENTION_DAYS" "Backup manifests"
delete_old_files "$PROJECT_ROOT/data/backups" "*.sha256"         "$REPORT_RETENTION_DAYS" "Backup checksums"

# ---------------------------------------------------------------------------
# 5. Clean Python cache files (optional)
# ---------------------------------------------------------------------------
if [ "$CLEAN_PYCACHE" = true ]; then
  echo -e "\n${BOLD}--- Python Cache ---${RESET}"
  PYCACHE_COUNT=0
  PYCACHE_DIRS=()
  while IFS= read -r dir; do
    PYCACHE_DIRS+=("$dir")
  done < <(find "$PROJECT_ROOT" -type d -name "__pycache__" 2>/dev/null | grep -v ".venv" || true)

  PYC_FILES=()
  while IFS= read -r file; do
    PYC_FILES+=("$file")
  done < <(find "$PROJECT_ROOT" -type f \( -name "*.pyc" -o -name "*.pyo" \) 2>/dev/null | grep -v ".venv" || true)

  if [ "$DRY_RUN" = true ]; then
    log_skip "Would delete ${#PYCACHE_DIRS[@]} __pycache__ dir(s) and ${#PYC_FILES[@]} .pyc/.pyo file(s)"
  else
    for dir in "${PYCACHE_DIRS[@]}"; do
      rm -rf "$dir"
    done
    for file in "${PYC_FILES[@]}"; do
      rm -f "$file"
    done
    log_ok "Python cache: deleted ${#PYCACHE_DIRS[@]} __pycache__ dir(s), ${#PYC_FILES[@]} .pyc file(s)"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Clean test artefacts
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}--- Test Artefacts ---${RESET}"
delete_old_files "$PROJECT_ROOT" ".coverage*" "$TEST_RETENTION_DAYS" "Coverage files"

# Clean htmlcov directory
if [ -d "$PROJECT_ROOT/htmlcov" ]; then
  if [ "$DRY_RUN" = true ]; then
    log_skip "Would remove $PROJECT_ROOT/htmlcov"
  else
    rm -rf "$PROJECT_ROOT/htmlcov"
    log_ok "Removed htmlcov directory"
  fi
fi

# ---------------------------------------------------------------------------
# 7. Remove empty directories (excluding .venv and .git)
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}--- Empty Directories ---${RESET}"
EMPTY_DIRS=()
while IFS= read -r dir; do
  EMPTY_DIRS+=("$dir")
done < <(find "$PROJECT_ROOT/logs" "$PROJECT_ROOT/data/tmp" "$PROJECT_ROOT/reports" \
         -type d -empty 2>/dev/null | grep -v ".gitkeep" || true)

if [ ${#EMPTY_DIRS[@]} -gt 0 ]; then
  for dir in "${EMPTY_DIRS[@]}"; do
    if [ "$DRY_RUN" = true ]; then
      log_skip "Would remove empty dir: $dir"
    else
      rmdir "$dir" 2>/dev/null || true
      log_ok "Removed empty dir: $dir"
    fi
  done
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}==============================${RESET}"
echo -e "${BOLD}  Cleanup Summary${RESET}"
echo -e "${BOLD}==============================${RESET}"
SIZE_HUMAN=$(numfmt --to=iec "$TOTAL_BYTES" 2>/dev/null || echo "${TOTAL_BYTES}B")
if [ "$DRY_RUN" = true ]; then
  echo -e "  ${YELLOW}DRY RUN – no files were deleted${RESET}"
  echo -e "  Would have deleted: $TOTAL_DELETED file(s) (~$SIZE_HUMAN)"
else
  echo -e "  Deleted:  $TOTAL_DELETED file(s)"
  echo -e "  Freed:    $SIZE_HUMAN"
fi
echo ""
log_ok "Cleanup complete"
