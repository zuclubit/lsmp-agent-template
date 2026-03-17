#!/usr/bin/env bash
##############################################################################
# deploy.sh
# Project : Legacy-to-Salesforce Migration Platform
# Purpose : Kubernetes deployment script used by all CI/CD platforms
#           Supports standard rolling update and blue-green deploy modes
#
# Required environment variables:
#   ENVIRONMENT        — dev | staging | prod
#   NAMESPACE          — Kubernetes namespace (e.g. sfmigration-prod)
#   IMAGE_TAG          — Docker image tag to deploy
#   REGISTRY           — Container registry FQDN (e.g. myacr.azurecr.io)
#
# Optional environment variables:
#   DEPLOYMENT_TIMEOUT — kubectl rollout timeout in seconds (default: 600)
#   BLUE_GREEN         — "true" to enable blue-green mode (default: false)
#   KUBECONFIG         — Path to kubeconfig (falls back to ~/.kube/config)
#   DRY_RUN_DEPLOY     — "true" to run kubectl with --dry-run=server
#   IMAGES_OVERRIDE    — JSON map of image overrides e.g. '{"worker":"tag1"}'
##############################################################################

set -Eeuo pipefail
trap 'on_error $LINENO' ERR

##############################################################################
# Defaults
##############################################################################

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly LOG_PREFIX="[deploy][${TIMESTAMP}]"

ENVIRONMENT="${ENVIRONMENT:?'ENVIRONMENT is required'}"
NAMESPACE="${NAMESPACE:?'NAMESPACE is required'}"
IMAGE_TAG="${IMAGE_TAG:?'IMAGE_TAG is required'}"
REGISTRY="${REGISTRY:?'REGISTRY is required'}"
DEPLOYMENT_TIMEOUT="${DEPLOYMENT_TIMEOUT:-600}"
BLUE_GREEN="${BLUE_GREEN:-false}"
DRY_RUN_DEPLOY="${DRY_RUN_DEPLOY:-false}"

# Image names
IMAGE_MIGRATION="${REGISTRY}/sfmigration-worker:${IMAGE_TAG}"
IMAGE_API="${REGISTRY}/sfmigration-api:${IMAGE_TAG}"

##############################################################################
# Utilities
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
  log_error "Script failed at line ${lineno}. Deployment FAILED."
  exit 1
}

# Check required tools
check_prerequisites() {
  local tools=("kubectl" "jq")
  for tool in "${tools[@]}"; do
    if ! command -v "${tool}" &>/dev/null; then
      log_error "Required tool '${tool}' is not installed."
      exit 1
    fi
  done
  log "Prerequisites check passed."
}

# Verify kubectl can reach the cluster
verify_cluster_access() {
  log "Verifying cluster access..."
  if ! kubectl cluster-info &>/dev/null; then
    log_error "Cannot connect to Kubernetes cluster. Check KUBECONFIG."
    exit 1
  fi
  log "Cluster access verified."
}

# Verify namespace exists
verify_namespace() {
  log "Verifying namespace '${NAMESPACE}'..."
  if ! kubectl get namespace "${NAMESPACE}" &>/dev/null; then
    log_error "Namespace '${NAMESPACE}' does not exist."
    exit 1
  fi
  log "Namespace '${NAMESPACE}' exists."
}

# Verify images exist in registry
verify_images() {
  log "Verifying images in registry..."
  # We use kubectl run --dry-run to indirectly validate image refs
  kubectl run verify-migration-img \
    --image="${IMAGE_MIGRATION}" \
    --dry-run=server \
    --restart=Never \
    -n "${NAMESPACE}" &>/dev/null && \
    log "  Migration image: OK" || \
    log_warning "  Migration image not verified (may be a private registry auth issue)"

  kubectl run verify-api-img \
    --image="${IMAGE_API}" \
    --dry-run=server \
    --restart=Never \
    -n "${NAMESPACE}" &>/dev/null && \
    log "  API image: OK" || \
    log_warning "  API image not verified"
}

##############################################################################
# Capture current state (for rollback reference)
##############################################################################

capture_current_state() {
  log "Capturing current deployment state..."

  PREV_MIGRATION_IMAGE=$(kubectl get deployment migration-worker \
    -n "${NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "none")

  PREV_API_IMAGE=$(kubectl get deployment api-gateway \
    -n "${NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "none")

  PREV_MIGRATION_REPLICAS=$(kubectl get deployment migration-worker \
    -n "${NAMESPACE}" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")

  PREV_API_REPLICAS=$(kubectl get deployment api-gateway \
    -n "${NAMESPACE}" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")

  log "Current state:"
  log "  migration-worker : image=${PREV_MIGRATION_IMAGE} replicas=${PREV_MIGRATION_REPLICAS}"
  log "  api-gateway      : image=${PREV_API_IMAGE} replicas=${PREV_API_REPLICAS}"

  # Export for calling pipeline
  export PREVIOUS_MIGRATION_IMAGE="${PREV_MIGRATION_IMAGE}"
  export PREVIOUS_API_IMAGE="${PREV_API_IMAGE}"
}

##############################################################################
# Standard rolling update
##############################################################################

rolling_update() {
  log "Starting rolling update deployment..."
  log "  Namespace  : ${NAMESPACE}"
  log "  Image Tag  : ${IMAGE_TAG}"
  log "  Dry Run    : ${DRY_RUN_DEPLOY}"

  local dry_run_flag=""
  if [ "${DRY_RUN_DEPLOY}" = "true" ]; then
    dry_run_flag="--dry-run=server"
  fi

  # Apply all Kubernetes manifests
  log "Applying ConfigMaps..."
  kubectl apply \
    -f infrastructure/kubernetes/configmaps/app-config.yaml \
    -n "${NAMESPACE}" \
    ${dry_run_flag}

  log "Applying Services..."
  kubectl apply \
    -f infrastructure/kubernetes/services/migration-service.yaml \
    -n "${NAMESPACE}" \
    ${dry_run_flag}
  kubectl apply \
    -f infrastructure/kubernetes/services/api-service.yaml \
    -n "${NAMESPACE}" \
    ${dry_run_flag}

  # Update migration-worker deployment image
  log "Updating migration-worker image to ${IMAGE_MIGRATION}..."
  kubectl set image deployment/migration-worker \
    migration-worker="${IMAGE_MIGRATION}" \
    -n "${NAMESPACE}" \
    ${dry_run_flag}

  # Annotate with deployment info
  kubectl annotate deployment migration-worker \
    -n "${NAMESPACE}" \
    "deployment.company.com/image-tag=${IMAGE_TAG}" \
    "deployment.company.com/deployed-at=${TIMESTAMP}" \
    "deployment.company.com/deployed-by=${GITHUB_ACTOR:-${USER:-CI}}" \
    --overwrite \
    ${dry_run_flag}

  # Update api-gateway deployment image
  log "Updating api-gateway image to ${IMAGE_API}..."
  kubectl set image deployment/api-gateway \
    api-gateway="${IMAGE_API}" \
    -n "${NAMESPACE}" \
    ${dry_run_flag}

  kubectl annotate deployment api-gateway \
    -n "${NAMESPACE}" \
    "deployment.company.com/image-tag=${IMAGE_TAG}" \
    "deployment.company.com/deployed-at=${TIMESTAMP}" \
    "deployment.company.com/deployed-by=${GITHUB_ACTOR:-${USER:-CI}}" \
    --overwrite \
    ${dry_run_flag}

  if [ "${DRY_RUN_DEPLOY}" = "true" ]; then
    log_success "Dry-run deployment completed successfully."
    return 0
  fi

  # Wait for rollouts
  wait_for_rollout "migration-worker"
  wait_for_rollout "api-gateway"
}

##############################################################################
# Blue-green deployment
##############################################################################

blue_green_deploy() {
  log "Starting blue-green deployment..."

  # Determine current active color
  CURRENT_COLOR=$(kubectl get service api-gateway \
    -n "${NAMESPACE}" \
    -o jsonpath='{.spec.selector.color}' 2>/dev/null || echo "blue")

  if [ "${CURRENT_COLOR}" = "blue" ]; then
    NEW_COLOR="green"
  else
    NEW_COLOR="blue"
  fi

  log "  Current color: ${CURRENT_COLOR}"
  log "  New color    : ${NEW_COLOR}"

  # Deploy new (green) version alongside the current
  log "Deploying ${NEW_COLOR} version..."

  # Update image on the inactive deployment
  kubectl set image "deployment/migration-worker-${NEW_COLOR}" \
    "migration-worker=${IMAGE_MIGRATION}" \
    -n "${NAMESPACE}" \
    2>/dev/null || {
      # If green deployment doesn't exist, create it by copying blue
      log "Creating ${NEW_COLOR} deployment from ${CURRENT_COLOR}..."
      kubectl get deployment "migration-worker-${CURRENT_COLOR}" \
        -n "${NAMESPACE}" \
        -o json | \
        jq ".metadata.name = \"migration-worker-${NEW_COLOR}\" |
            .spec.selector.matchLabels.color = \"${NEW_COLOR}\" |
            .spec.template.metadata.labels.color = \"${NEW_COLOR}\" |
            .spec.template.spec.containers[0].image = \"${IMAGE_MIGRATION}\"" | \
        kubectl apply -f - -n "${NAMESPACE}"
    }

  # Wait for new version to be healthy
  wait_for_rollout "migration-worker-${NEW_COLOR}"

  # Smoke-test the new color before switching traffic
  log "Running pre-switch health checks..."
  verify_deployment_health "migration-worker-${NEW_COLOR}"

  # Switch service selector to new color
  log "Switching service traffic to ${NEW_COLOR}..."
  kubectl patch service migration-worker \
    -n "${NAMESPACE}" \
    -p "{\"spec\":{\"selector\":{\"app.kubernetes.io/name\":\"migration-worker\",\"color\":\"${NEW_COLOR}\"}}}"

  kubectl patch service api-gateway \
    -n "${NAMESPACE}" \
    -p "{\"spec\":{\"selector\":{\"app.kubernetes.io/name\":\"api-gateway\",\"color\":\"${NEW_COLOR}\"}}}"

  log_success "Traffic switched to ${NEW_COLOR} deployment."

  # Scale down old color after a stabilization window
  log "Waiting 60s for stabilization before scaling down ${CURRENT_COLOR}..."
  sleep 60

  # Verify new color is still healthy after traffic shift
  verify_deployment_health "migration-worker-${NEW_COLOR}"

  log "Scaling down ${CURRENT_COLOR} deployment..."
  kubectl scale deployment "migration-worker-${CURRENT_COLOR}" \
    --replicas=0 \
    -n "${NAMESPACE}" || log_warning "Could not scale down ${CURRENT_COLOR}"

  export ACTIVE_COLOR="${NEW_COLOR}"
  export INACTIVE_COLOR="${CURRENT_COLOR}"
}

##############################################################################
# Wait for rollout helper
##############################################################################

wait_for_rollout() {
  local deployment_name="$1"
  log "Waiting for rollout: ${deployment_name} (timeout: ${DEPLOYMENT_TIMEOUT}s)..."

  if ! kubectl rollout status "deployment/${deployment_name}" \
      -n "${NAMESPACE}" \
      --timeout="${DEPLOYMENT_TIMEOUT}s"; then
    log_error "Rollout of '${deployment_name}' timed out or failed!"
    # Print recent events for diagnostics
    kubectl get events \
      -n "${NAMESPACE}" \
      --field-selector "involvedObject.name=${deployment_name}" \
      --sort-by='.lastTimestamp' | tail -20 || true
    return 1
  fi

  log_success "Rollout of '${deployment_name}' completed successfully."
}

##############################################################################
# Verify deployment health
##############################################################################

verify_deployment_health() {
  local deployment_name="$1"
  log "Verifying health of deployment '${deployment_name}'..."

  local ready_replicas
  local desired_replicas

  # Wait up to 120s for desired replicas to be ready
  local attempts=0
  local max_attempts=24
  while [ "${attempts}" -lt "${max_attempts}" ]; do
    desired_replicas=$(kubectl get deployment "${deployment_name}" \
      -n "${NAMESPACE}" \
      -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")
    ready_replicas=$(kubectl get deployment "${deployment_name}" \
      -n "${NAMESPACE}" \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")

    if [ "${ready_replicas}" -ge "${desired_replicas}" ] && [ "${desired_replicas}" -gt "0" ]; then
      log_success "  ${deployment_name}: ${ready_replicas}/${desired_replicas} replicas ready."
      return 0
    fi

    log "  ${deployment_name}: ${ready_replicas:-0}/${desired_replicas:-?} replicas ready (attempt $((attempts+1))/${max_attempts})..."
    attempts=$((attempts + 1))
    sleep 5
  done

  log_error "Deployment '${deployment_name}' did not reach healthy state in time."
  kubectl describe deployment "${deployment_name}" -n "${NAMESPACE}" || true
  kubectl get pods -l "app.kubernetes.io/name=${deployment_name}" \
    -n "${NAMESPACE}" -o wide || true
  return 1
}

##############################################################################
# Print deployment summary
##############################################################################

print_summary() {
  log ""
  log "=========================================="
  log " DEPLOYMENT SUMMARY"
  log "=========================================="
  log " Environment : ${ENVIRONMENT}"
  log " Namespace   : ${NAMESPACE}"
  log " Image Tag   : ${IMAGE_TAG}"
  log " Mode        : ${BLUE_GREEN}"
  log " Timestamp   : ${TIMESTAMP}"
  log "------------------------------------------"

  kubectl get deployments -n "${NAMESPACE}" \
    -o custom-columns="NAME:.metadata.name,READY:.status.readyReplicas,DESIRED:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image" || true

  log "=========================================="
}

##############################################################################
# Main
##############################################################################

main() {
  log "Starting deployment for environment '${ENVIRONMENT}'..."

  check_prerequisites
  verify_cluster_access
  verify_namespace
  capture_current_state

  if [ "${BLUE_GREEN}" = "true" ]; then
    blue_green_deploy
  else
    rolling_update
  fi

  print_summary
  log_success "Deployment completed successfully."
}

main "$@"
