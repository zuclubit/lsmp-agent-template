#!/usr/bin/env bash
##############################################################################
# rollback.sh
# Project : Legacy-to-Salesforce Migration Platform
# Purpose : Kubernetes rollback script — supports image-based rollback and
#           Kubernetes native rollout undo
#
# Required environment variables:
#   ENVIRONMENT    — dev | staging | prod
#   NAMESPACE      — Kubernetes namespace (e.g. sfmigration-prod)
#
# Optional environment variables:
#   PREVIOUS_IMAGE — Specific image tag to roll back to (overrides undo)
#   REASON         — Human-readable rollback reason for audit logging
#   ROLLBACK_TIMEOUT — kubectl rollout timeout in seconds (default: 300)
#   NOTIFY_SLACK   — "true" to send Slack notification (requires SLACK_DEPLOYMENT_WEBHOOK)
#   REVISION       — Specific revision to roll back to (k8s rollout history)
##############################################################################

set -Eeuo pipefail
trap 'on_error $LINENO' ERR

##############################################################################
# Defaults
##############################################################################

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly LOG_PREFIX="[rollback][${TIMESTAMP}]"

ENVIRONMENT="${ENVIRONMENT:?'ENVIRONMENT is required'}"
NAMESPACE="${NAMESPACE:?'NAMESPACE is required'}"
REASON="${REASON:-Manual rollback requested}"
ROLLBACK_TIMEOUT="${ROLLBACK_TIMEOUT:-300}"
NOTIFY_SLACK="${NOTIFY_SLACK:-false}"
PREVIOUS_IMAGE="${PREVIOUS_IMAGE:-}"
REVISION="${REVISION:-}"

ROLLBACK_ACTOR="${GITHUB_ACTOR:-${GITLAB_USER_NAME:-${USER:-CI}}}"

# Deployments to roll back
readonly DEPLOYMENTS=("migration-worker" "api-gateway")

##############################################################################
# Logging
##############################################################################

log() {
  echo "${LOG_PREFIX} $*" >&2
}

log_success() {
  echo "${LOG_PREFIX} [SUCCESS] $*" >&2
}

log_warning() {
  echo "${LOG_PREFIX} [WARNING] $*" >&2
}

log_error() {
  echo "${LOG_PREFIX} [ERROR] $*" >&2
}

on_error() {
  local lineno="$1"
  log_error "Rollback script failed at line ${lineno}."
  notify_failure "Rollback script failed at line ${lineno}."
  exit 1
}

##############################################################################
# Prerequisites
##############################################################################

check_prerequisites() {
  local tools=("kubectl")
  for tool in "${tools[@]}"; do
    if ! command -v "${tool}" &>/dev/null; then
      log_error "Required tool '${tool}' is not installed."
      exit 1
    fi
  done

  if ! kubectl cluster-info &>/dev/null; then
    log_error "Cannot connect to Kubernetes cluster."
    exit 1
  fi

  if ! kubectl get namespace "${NAMESPACE}" &>/dev/null; then
    log_error "Namespace '${NAMESPACE}' does not exist."
    exit 1
  fi

  log "Prerequisites check passed."
}

##############################################################################
# Capture pre-rollback state
##############################################################################

capture_state() {
  log "Capturing current deployment state before rollback..."

  for deployment in "${DEPLOYMENTS[@]}"; do
    local current_image
    local ready_replicas
    current_image=$(kubectl get deployment "${deployment}" \
      -n "${NAMESPACE}" \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "not-found")
    ready_replicas=$(kubectl get deployment "${deployment}" \
      -n "${NAMESPACE}" \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")

    log "  ${deployment}:"
    log "    current image   : ${current_image}"
    log "    ready replicas  : ${ready_replicas}"
  done
}

##############################################################################
# Get rollout history
##############################################################################

show_rollout_history() {
  log ""
  log "Rollout history (last 5 revisions):"
  for deployment in "${DEPLOYMENTS[@]}"; do
    log "  -- ${deployment} --"
    kubectl rollout history "deployment/${deployment}" \
      -n "${NAMESPACE}" 2>/dev/null | tail -6 || \
      log_warning "  Could not retrieve history for ${deployment}"
  done
  log ""
}

##############################################################################
# Image-based rollback
##############################################################################

image_rollback() {
  local target_image="$1"
  local deployment_name="$2"

  log "Rolling back '${deployment_name}' to image: ${target_image}..."

  kubectl set image "deployment/${deployment_name}" \
    "${deployment_name}=${target_image}" \
    -n "${NAMESPACE}"

  kubectl annotate deployment "${deployment_name}" \
    -n "${NAMESPACE}" \
    "deployment.company.com/rollback-at=${TIMESTAMP}" \
    "deployment.company.com/rollback-reason=${REASON}" \
    "deployment.company.com/rollback-by=${ROLLBACK_ACTOR}" \
    "deployment.company.com/rollback-to-image=${target_image}" \
    --overwrite
}

##############################################################################
# Native rollout undo
##############################################################################

native_rollback() {
  local deployment_name="$1"

  if [ -n "${REVISION}" ]; then
    log "Rolling back '${deployment_name}' to revision ${REVISION}..."
    kubectl rollout undo "deployment/${deployment_name}" \
      -n "${NAMESPACE}" \
      --to-revision="${REVISION}"
  else
    log "Rolling back '${deployment_name}' to previous revision..."
    kubectl rollout undo "deployment/${deployment_name}" \
      -n "${NAMESPACE}"
  fi

  # Get the image that was rolled back to
  local rolled_back_image
  rolled_back_image=$(kubectl get deployment "${deployment_name}" \
    -n "${NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "unknown")

  kubectl annotate deployment "${deployment_name}" \
    -n "${NAMESPACE}" \
    "deployment.company.com/rollback-at=${TIMESTAMP}" \
    "deployment.company.com/rollback-reason=${REASON}" \
    "deployment.company.com/rollback-by=${ROLLBACK_ACTOR}" \
    "deployment.company.com/rollback-to-image=${rolled_back_image}" \
    --overwrite

  log "  Rolled back to: ${rolled_back_image}"
}

##############################################################################
# Wait for rollback rollout
##############################################################################

wait_for_rollout() {
  local deployment_name="$1"
  log "Waiting for rollback of '${deployment_name}' (timeout: ${ROLLBACK_TIMEOUT}s)..."

  if ! kubectl rollout status "deployment/${deployment_name}" \
      -n "${NAMESPACE}" \
      --timeout="${ROLLBACK_TIMEOUT}s"; then
    log_error "Rollback of '${deployment_name}' did not complete in time!"

    kubectl get pods -l "app.kubernetes.io/name=${deployment_name}" \
      -n "${NAMESPACE}" -o wide || true
    kubectl get events \
      -n "${NAMESPACE}" \
      --field-selector "involvedObject.name=${deployment_name}" \
      --sort-by='.lastTimestamp' | tail -20 || true

    return 1
  fi

  log_success "Rollback of '${deployment_name}' completed."
}

##############################################################################
# Verify post-rollback health
##############################################################################

verify_health() {
  log "Verifying health after rollback..."

  local all_healthy=true
  for deployment in "${DEPLOYMENTS[@]}"; do
    local desired ready
    desired=$(kubectl get deployment "${deployment}" \
      -n "${NAMESPACE}" \
      -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")
    ready=$(kubectl get deployment "${deployment}" \
      -n "${NAMESPACE}" \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")

    if [ "${ready}" -ge "${desired}" ] && [ "${desired}" -gt "0" ]; then
      log_success "  ${deployment}: ${ready}/${desired} replicas healthy."
    else
      log_warning "  ${deployment}: ${ready:-0}/${desired:-?} replicas ready — DEGRADED"
      all_healthy=false
    fi
  done

  if [ "${all_healthy}" = "false" ]; then
    log_warning "Post-rollback health check shows degraded state. Manual investigation required."
    return 1
  fi

  log_success "All deployments are healthy after rollback."
}

##############################################################################
# Slack notification
##############################################################################

notify_slack() {
  local status="$1"
  local message="$2"
  local color

  if [ "${status}" = "success" ]; then
    color="warning"   # Rollback is an unusual event even if successful
  else
    color="danger"
  fi

  if [ "${NOTIFY_SLACK}" != "true" ] || [ -z "${SLACK_DEPLOYMENT_WEBHOOK:-}" ]; then
    log "Slack notification skipped (NOTIFY_SLACK=${NOTIFY_SLACK})."
    return 0
  fi

  log "Sending Slack notification..."
  curl -s -X POST "${SLACK_DEPLOYMENT_WEBHOOK}" \
    -H "Content-Type: application/json" \
    -d "{
      \"text\": \":warning: *Rollback ${status}* — sfmigration (${ENVIRONMENT})\",
      \"attachments\": [{
        \"color\": \"${color}\",
        \"fields\": [
          {\"title\": \"Environment\", \"value\": \"${ENVIRONMENT}\", \"short\": true},
          {\"title\": \"Namespace\", \"value\": \"${NAMESPACE}\", \"short\": true},
          {\"title\": \"Reason\", \"value\": \"${REASON}\", \"short\": false},
          {\"title\": \"Actor\", \"value\": \"${ROLLBACK_ACTOR}\", \"short\": true},
          {\"title\": \"Timestamp\", \"value\": \"${TIMESTAMP}\", \"short\": true},
          {\"title\": \"Status\", \"value\": \"${message}\", \"short\": false}
        ]
      }]
    }" || log_warning "Failed to send Slack notification."
}

notify_failure() {
  local msg="$1"
  notify_slack "failed" "${msg}"
}

##############################################################################
# Print rollback summary
##############################################################################

print_summary() {
  log ""
  log "=========================================="
  log " ROLLBACK SUMMARY"
  log "=========================================="
  log " Environment    : ${ENVIRONMENT}"
  log " Namespace      : ${NAMESPACE}"
  log " Reason         : ${REASON}"
  log " Actor          : ${ROLLBACK_ACTOR}"
  log " Timestamp      : ${TIMESTAMP}"
  log "------------------------------------------"

  kubectl get deployments -n "${NAMESPACE}" \
    -o custom-columns="NAME:.metadata.name,READY:.status.readyReplicas,DESIRED:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image" 2>/dev/null || true

  log "=========================================="
}

##############################################################################
# Write audit log entry
##############################################################################

write_audit_log() {
  local status="$1"
  local audit_file="/tmp/rollback-audit-${TIMESTAMP}.json"

  # Collect post-rollback images
  local migration_image api_image
  migration_image=$(kubectl get deployment migration-worker \
    -n "${NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "unknown")
  api_image=$(kubectl get deployment api-gateway \
    -n "${NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "unknown")

  cat > "${audit_file}" <<EOF
{
  "event": "rollback",
  "status": "${status}",
  "environment": "${ENVIRONMENT}",
  "namespace": "${NAMESPACE}",
  "timestamp": "${TIMESTAMP}",
  "actor": "${ROLLBACK_ACTOR}",
  "reason": "${REASON}",
  "ci_pipeline": "${CI_PIPELINE_URL:-N/A}",
  "deployed_images": {
    "migration_worker": "${migration_image}",
    "api_gateway": "${api_image}"
  }
}
EOF

  log "Audit log written to ${audit_file}"
  cat "${audit_file}"
}

##############################################################################
# Main
##############################################################################

main() {
  log "======================================"
  log "  ROLLBACK INITIATED"
  log "  Environment : ${ENVIRONMENT}"
  log "  Namespace   : ${NAMESPACE}"
  log "  Reason      : ${REASON}"
  log "  Actor       : ${ROLLBACK_ACTOR}"
  log "======================================"

  check_prerequisites
  capture_state
  show_rollout_history

  # --- Execute rollback for each deployment ---
  for deployment in "${DEPLOYMENTS[@]}"; do
    if [ -n "${PREVIOUS_IMAGE}" ] && [ "${deployment}" = "migration-worker" ]; then
      image_rollback "${PREVIOUS_IMAGE}" "${deployment}"
    else
      native_rollback "${deployment}"
    fi
  done

  # --- Wait for all rollouts to complete ---
  local rollback_success=true
  for deployment in "${DEPLOYMENTS[@]}"; do
    if ! wait_for_rollout "${deployment}"; then
      rollback_success=false
    fi
  done

  if [ "${rollback_success}" = "false" ]; then
    log_error "One or more rollbacks did not complete successfully."
    write_audit_log "partial_failure"
    notify_slack "partial failure" "Some rollbacks did not complete. Manual intervention required."
    print_summary
    exit 1
  fi

  # --- Verify health ---
  verify_health

  write_audit_log "success"
  notify_slack "success" "All deployments rolled back successfully to previous stable version."
  print_summary
  log_success "Rollback completed successfully."
}

main "$@"
