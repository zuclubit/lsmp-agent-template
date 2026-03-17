#!/usr/bin/env bash
# =============================================================================
# deploy_to_salesforce.sh
# LSMP — Deploy Salesforce metadata to sandbox org via SFDX CLI
#
# Prerequisites:
#   1. Salesforce CLI installed: npm install -g @salesforce/cli
#   2. Authenticated to sandbox: sf org login web --instance-url https://test.salesforce.com
#   3. Set TARGET_ORG env var or pass as first argument
#
# Usage:
#   bash scripts/deploy_to_salesforce.sh [org-alias] [--dry-run]
#   TARGET_ORG=my-sandbox bash scripts/deploy_to_salesforce.sh
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

log()  { echo -e "${BOLD}${BLUE}▶${RESET} $*"; }
ok()   { echo -e "  ${GREEN}✓${RESET} $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET} $*"; }
fail() { echo -e "  ${RED}✗${RESET} $*"; exit 1; }

# ── Args ─────────────────────────────────────────────────────────────────────
TARGET_ORG="${1:-${TARGET_ORG:-}}"
DRY_RUN=false
for arg in "$@"; do
    [[ "$arg" == "--dry-run" ]] && DRY_RUN=true
done

echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  LSMP — Salesforce Metadata Deployment${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

# ── 1. Check Salesforce CLI ───────────────────────────────────────────────────
log "Checking Salesforce CLI..."
if ! command -v sf &>/dev/null && ! command -v sfdx &>/dev/null; then
    fail "Salesforce CLI not installed.\nInstall: npm install -g @salesforce/cli\nDocs: https://developer.salesforce.com/tools/salesforcecli"
fi
SF_CMD="sf"
command -v sf &>/dev/null || SF_CMD="sfdx"
SF_VERSION=$($SF_CMD version --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('sfdxCLIVersion', d.get('version','?')))" 2>/dev/null || echo "unknown")
ok "Salesforce CLI: $SF_CMD ($SF_VERSION)"

# ── 2. Resolve org alias ──────────────────────────────────────────────────────
log "Resolving target org..."
if [ -z "$TARGET_ORG" ]; then
    # Try default org from sfdx-project.json
    if [ -f "sfdx-project.json" ]; then
        TARGET_ORG=$($SF_CMD config get target-org --json 2>/dev/null \
            | python3 -c "import sys,json; r=json.load(sys.stdin); print(r.get('result',[{}])[0].get('value',''))" 2>/dev/null || echo "")
    fi
fi
if [ -z "$TARGET_ORG" ]; then
    fail "No target org specified.\nRun: sf org login web --alias my-sandbox --instance-url https://test.salesforce.com\nThen: bash scripts/deploy_to_salesforce.sh my-sandbox"
fi
ok "Target org: $TARGET_ORG"

# ── 3. Validate org auth ──────────────────────────────────────────────────────
log "Checking org authentication..."
ORG_INFO=$($SF_CMD org display --target-org "$TARGET_ORG" --json 2>/dev/null || echo '{"status":1}')
ORG_STATUS=$(echo "$ORG_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',1))" 2>/dev/null || echo "1")
if [ "$ORG_STATUS" != "0" ]; then
    fail "Not authenticated to org '$TARGET_ORG'.\nRun: sf org login web --alias $TARGET_ORG --instance-url https://test.salesforce.com"
fi
ORG_URL=$(echo "$ORG_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('instanceUrl',''))" 2>/dev/null || echo "")
ok "Authenticated: $ORG_URL"

# ── 4. Validate SFDX project ──────────────────────────────────────────────────
log "Validating SFDX project..."
[ -f "sfdx-project.json" ] || fail "sfdx-project.json not found at $PROJECT_ROOT"
[ -d "force-app/main/default" ] || fail "force-app/main/default not found"
ok "SFDX project valid"

# ── 5. Pre-deployment checks ──────────────────────────────────────────────────
log "Running pre-deployment security checks..."
python3 scripts/validate_system.py 2>&1 | tail -4
ok "System validation passed"

# ── 6. Deploy (or validate) ───────────────────────────────────────────────────
echo ""
if [ "$DRY_RUN" = true ]; then
    log "DRY-RUN: Validating metadata (no deploy)..."
    DEPLOY_CMD="$SF_CMD project deploy validate"
else
    log "Deploying metadata to $TARGET_ORG..."
    DEPLOY_CMD="$SF_CMD project deploy start"
fi

DEPLOY_RESULT=$($DEPLOY_CMD \
    --source-dir force-app \
    --target-org "$TARGET_ORG" \
    --json \
    --test-level RunLocalTests \
    2>&1 || true)

DEPLOY_STATUS=$(echo "$DEPLOY_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',1))" 2>/dev/null || echo "1")

if [ "$DEPLOY_STATUS" = "0" ]; then
    DEPLOY_ID=$(echo "$DEPLOY_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('id',''))" 2>/dev/null || echo "")
    ok "Deployment successful! ID: $DEPLOY_ID"
    echo ""

    if [ "$DRY_RUN" = false ]; then
        # ── 7. Post-deploy: assign permission set ─────────────────────────────
        log "Assigning permission set to integration user..."
        CURRENT_USER=$($SF_CMD org display --target-org "$TARGET_ORG" --json 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('username',''))" 2>/dev/null || echo "")
        if [ -n "$CURRENT_USER" ]; then
            $SF_CMD org assign permset \
                --name Legacy_Migration_PS \
                --target-org "$TARGET_ORG" \
                --on-behalf-of "$CURRENT_USER" 2>/dev/null && ok "Permission set assigned to $CURRENT_USER" || warn "Could not auto-assign permission set"
        fi

        # ── 8. Open org in browser ────────────────────────────────────────────
        log "Opening org in browser..."
        $SF_CMD org open --target-org "$TARGET_ORG" --path "/lightning/app/Migration_Control_Center" 2>/dev/null || \
        $SF_CMD org open --target-org "$TARGET_ORG" 2>/dev/null || true
    fi

    echo ""
    echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}${GREEN}  ✓ Deployment complete!${RESET}"
    echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo "  Deployed components:"
    echo "    · Custom fields: Legacy_ID__c, Migration_Status__c, Source_System__c on Account/Contact/Opportunity"
    echo "    · Custom object: MigrationLog__c"
    echo "    · Lightning App: Migration Control Center"
    echo "    · LWC: migrationDashboard, migrationStatusBadge"
    echo "    · Apex: MigrationDashboardController, MigrationBatchProcessor"
    echo "    · Permission set: Legacy_Migration_PS"
    echo ""
    echo "  Next:"
    echo "    1. Open the org and navigate to App Launcher → Migration Control Center"
    echo "    2. Add the migrationDashboard LWC to a Lightning App Page"
    echo "    3. Set SF credentials in .env and run: make migrate"
    echo ""
else
    # Show deployment errors
    echo "$DEPLOY_RESULT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    errs = d.get('result', {}).get('details', {}).get('componentFailures', [])
    for e in errs[:10]:
        print(f\"  ERROR [{e.get('componentType','')}] {e.get('fullName','')}: {e.get('problem','')}\")
except:
    pass
" 2>/dev/null || true
    fail "Deployment failed. See errors above."
fi
