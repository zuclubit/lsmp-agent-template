#!/usr/bin/env bash
# =============================================================================
# backup_legacy_data.sh
# =============================================================================
# Pre-migration backup of the legacy PostgreSQL database.
#
# Steps:
#   1. Validates database connectivity
#   2. Creates a timestamped pg_dump (custom format for fast restore)
#   3. Verifies the backup file (pg_restore --list)
#   4. Computes and stores SHA-256 checksum
#   5. Optionally uploads to cloud storage (AWS S3, Azure Blob, or GCS)
#   6. Cleans up backups older than retention period
#
# Usage:
#   ./scripts/backup_legacy_data.sh [OPTIONS]
#
# Options:
#   --run-id ID          Tag the backup with this run ID
#   --output-dir DIR     Write backup to this directory (default: data/backups)
#   --no-upload          Skip cloud storage upload
#   --retention-days N   Delete backups older than N days (default: 30)
#   --tables-only        Backup only specific tables (Account, Contact)
#   --schema-only        Backup schema only (no data)
#   -h, --help           Show this help
#
# Environment variables required:
#   LEGACY_DB_URL        Connection string: postgresql://user:pass@host:port/db
#   BACKUP_S3_BUCKET     (optional) s3://bucket/prefix for uploads
#   BACKUP_AZURE_CONTAINER (optional) Azure Blob container name
#   BACKUP_AZURE_CONN    (optional) Azure Blob connection string
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

log_info()    { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${CYAN}[BACKUP]${RESET} $*"; }
log_ok()      { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${GREEN}[OK]${RESET}    $*"; }
log_warn()    { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${YELLOW}[WARN]${RESET}  $*"; }
log_error()   { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${RED}[ERROR]${RESET} $*" >&2; }

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
OUTPUT_DIR="$PROJECT_ROOT/data/backups"
NO_UPLOAD=false
RETENTION_DAYS=30
TABLES_ONLY=false
SCHEMA_ONLY=false

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case $1 in
    --run-id)         RUN_ID="$2";          shift 2 ;;
    --output-dir)     OUTPUT_DIR="$2";      shift 2 ;;
    --no-upload)      NO_UPLOAD=true;       shift ;;
    --retention-days) RETENTION_DAYS="$2";  shift 2 ;;
    --tables-only)    TABLES_ONLY=true;     shift ;;
    --schema-only)    SCHEMA_ONLY=true;     shift ;;
    -h|--help)
      head -40 "$0" | grep "^#"
      exit 0 ;;
    *) log_warn "Unknown option: $1"; shift ;;
  esac
done

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
ENV_FILE="$PROJECT_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE"
  set +a
fi

# ---------------------------------------------------------------------------
# Validate environment
# ---------------------------------------------------------------------------
if [ -z "${LEGACY_DB_URL:-}" ]; then
  log_error "LEGACY_DB_URL is not set"
  exit 1
fi

# Parse DB URL: postgresql[+asyncpg]://user:password@host:port/dbname
# Normalise away the +asyncpg driver suffix
CLEAN_DB_URL="${LEGACY_DB_URL//+asyncpg/}"
DB_USER=$(echo "$CLEAN_DB_URL" | python3 -c "
import sys, urllib.parse
u = urllib.parse.urlparse(sys.stdin.read().strip())
print(u.username or '')
")
DB_PASS=$(echo "$CLEAN_DB_URL" | python3 -c "
import sys, urllib.parse
u = urllib.parse.urlparse(sys.stdin.read().strip())
print(u.password or '')
")
DB_HOST=$(echo "$CLEAN_DB_URL" | python3 -c "
import sys, urllib.parse
u = urllib.parse.urlparse(sys.stdin.read().strip())
print(u.hostname or 'localhost')
")
DB_PORT=$(echo "$CLEAN_DB_URL" | python3 -c "
import sys, urllib.parse
u = urllib.parse.urlparse(sys.stdin.read().strip())
print(u.port or 5432)
")
DB_NAME=$(echo "$CLEAN_DB_URL" | python3 -c "
import sys, urllib.parse
u = urllib.parse.urlparse(sys.stdin.read().strip())
print(u.path.lstrip('/'))
")

log_info "Database: $DB_HOST:$DB_PORT/$DB_NAME as $DB_USER"

# ---------------------------------------------------------------------------
# Validate pg_dump is available
# ---------------------------------------------------------------------------
if ! command -v pg_dump &>/dev/null; then
  log_error "pg_dump not found. Install postgresql-client."
  exit 1
fi

PG_DUMP_VERSION=$(pg_dump --version | head -1)
log_info "Using: $PG_DUMP_VERSION"

# ---------------------------------------------------------------------------
# Test database connectivity
# ---------------------------------------------------------------------------
log_info "Testing database connectivity…"
export PGPASSWORD="$DB_PASS"
if psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
     -c "SELECT 1" --no-password --quiet --tuples-only &>/dev/null; then
  log_ok "Database connection successful"
else
  log_error "Cannot connect to database $DB_HOST:$DB_PORT/$DB_NAME"
  exit 1
fi

# ---------------------------------------------------------------------------
# Create output directory
# ---------------------------------------------------------------------------
mkdir -p "$OUTPUT_DIR"
BACKUP_FILE="$OUTPUT_DIR/legacy_backup_${RUN_ID}.dump"
CHECKSUM_FILE="$BACKUP_FILE.sha256"
MANIFEST_FILE="$OUTPUT_DIR/legacy_backup_${RUN_ID}.manifest.json"

# ---------------------------------------------------------------------------
# Run pg_dump
# ---------------------------------------------------------------------------
log_info "Starting database dump: $BACKUP_FILE"

DUMP_ARGS=(
  "-h" "$DB_HOST"
  "-p" "$DB_PORT"
  "-U" "$DB_USER"
  "-d" "$DB_NAME"
  "--format=custom"
  "--compress=9"
  "--verbose"
  "--no-password"
  "--file=$BACKUP_FILE"
)

if [ "$SCHEMA_ONLY" = true ]; then
  DUMP_ARGS+=("--schema-only")
fi

if [ "$TABLES_ONLY" = true ]; then
  # Dump only the migration-relevant tables
  TABLES=("erp.accounts" "erp.contacts" "erp.opportunities" "erp.leads")
  for tbl in "${TABLES[@]}"; do
    DUMP_ARGS+=("--table=$tbl")
  done
fi

START_DUMP=$(date +%s)
pg_dump "${DUMP_ARGS[@]}" 2>&1 | grep -v "^pg_dump:" || true

if [ ! -f "$BACKUP_FILE" ]; then
  log_error "Backup file was not created: $BACKUP_FILE"
  exit 1
fi

END_DUMP=$(date +%s)
DUMP_SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
DUMP_BYTES=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || stat -f%z "$BACKUP_FILE")
DUMP_DURATION=$((END_DUMP - START_DUMP))

log_ok "Dump completed in ${DUMP_DURATION}s — size: $DUMP_SIZE"

# ---------------------------------------------------------------------------
# Verify backup integrity
# ---------------------------------------------------------------------------
log_info "Verifying backup integrity with pg_restore --list…"
if pg_restore --list "$BACKUP_FILE" > /dev/null 2>&1; then
  OBJECT_COUNT=$(pg_restore --list "$BACKUP_FILE" | wc -l | tr -d ' ')
  log_ok "Backup verified: $OBJECT_COUNT objects in archive"
else
  log_error "Backup verification failed – the dump file may be corrupt"
  exit 1
fi

# ---------------------------------------------------------------------------
# Compute checksum
# ---------------------------------------------------------------------------
log_info "Computing SHA-256 checksum…"
if command -v sha256sum &>/dev/null; then
  sha256sum "$BACKUP_FILE" > "$CHECKSUM_FILE"
elif command -v shasum &>/dev/null; then
  shasum -a 256 "$BACKUP_FILE" > "$CHECKSUM_FILE"
else
  log_warn "Neither sha256sum nor shasum found – skipping checksum"
fi

if [ -f "$CHECKSUM_FILE" ]; then
  CHECKSUM=$(awk '{print $1}' "$CHECKSUM_FILE")
  log_ok "Checksum: $CHECKSUM"
fi

# ---------------------------------------------------------------------------
# Write manifest
# ---------------------------------------------------------------------------
python3 - <<EOF
import json, datetime
manifest = {
    "run_id": "$RUN_ID",
    "backup_file": "$BACKUP_FILE",
    "checksum_file": "$CHECKSUM_FILE",
    "database_host": "$DB_HOST",
    "database_name": "$DB_NAME",
    "dump_size_bytes": $DUMP_BYTES,
    "dump_duration_seconds": $DUMP_DURATION,
    "object_count": "$OBJECT_COUNT",
    "schema_only": $( [ "$SCHEMA_ONLY" = true ] && echo "true" || echo "false" ),
    "tables_only": $( [ "$TABLES_ONLY" = true ] && echo "true" || echo "false" ),
    "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    "pg_dump_version": "$PG_DUMP_VERSION"
}
with open("$MANIFEST_FILE", "w") as f:
    json.dump(manifest, f, indent=2)
print("Manifest written")
EOF

log_ok "Manifest: $MANIFEST_FILE"

# ---------------------------------------------------------------------------
# Upload to cloud storage (optional)
# ---------------------------------------------------------------------------
if [ "$NO_UPLOAD" = false ]; then
  if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
    log_info "Uploading to S3: $BACKUP_S3_BUCKET"
    if command -v aws &>/dev/null; then
      aws s3 cp "$BACKUP_FILE" "$BACKUP_S3_BUCKET/$(basename "$BACKUP_FILE")" \
        --storage-class STANDARD_IA \
        --metadata "run_id=$RUN_ID,db=$DB_NAME" && log_ok "Uploaded to S3"
      aws s3 cp "$CHECKSUM_FILE" "$BACKUP_S3_BUCKET/$(basename "$CHECKSUM_FILE")"
      aws s3 cp "$MANIFEST_FILE" "$BACKUP_S3_BUCKET/$(basename "$MANIFEST_FILE")"
    else
      log_warn "AWS CLI not found – skipping S3 upload"
    fi
  elif [ -n "${BACKUP_AZURE_CONTAINER:-}" ] && [ -n "${BACKUP_AZURE_CONN:-}" ]; then
    log_info "Uploading to Azure Blob: $BACKUP_AZURE_CONTAINER"
    if command -v az &>/dev/null; then
      az storage blob upload \
        --connection-string "$BACKUP_AZURE_CONN" \
        --container-name "$BACKUP_AZURE_CONTAINER" \
        --name "$(basename "$BACKUP_FILE")" \
        --file "$BACKUP_FILE" \
        --overwrite && log_ok "Uploaded to Azure Blob"
    else
      log_warn "Azure CLI not found – skipping Azure upload"
    fi
  else
    log_info "No cloud storage configured – backup kept locally only"
  fi
fi

# ---------------------------------------------------------------------------
# Cleanup old backups
# ---------------------------------------------------------------------------
log_info "Cleaning up backups older than $RETENTION_DAYS days…"
DELETED_COUNT=0
while IFS= read -r old_file; do
  rm -f "$old_file" "${old_file}.sha256" "${old_file%.dump}.manifest.json"
  log_info "Deleted old backup: $old_file"
  ((DELETED_COUNT++))
done < <(find "$OUTPUT_DIR" -name "legacy_backup_*.dump" -mtime +"$RETENTION_DAYS" 2>/dev/null || true)

[ "$DELETED_COUNT" -gt 0 ] && log_ok "Deleted $DELETED_COUNT old backup(s)" || log_ok "No old backups to delete"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log_ok "Backup complete"
echo ""
echo -e "  ${BOLD}Backup Summary${RESET}"
echo -e "  Run ID:     $RUN_ID"
echo -e "  File:       $BACKUP_FILE"
echo -e "  Size:       $DUMP_SIZE"
echo -e "  Duration:   ${DUMP_DURATION}s"
echo -e "  Checksum:   ${CHECKSUM:-N/A}"
echo -e "  Objects:    ${OBJECT_COUNT:-N/A}"
echo ""
