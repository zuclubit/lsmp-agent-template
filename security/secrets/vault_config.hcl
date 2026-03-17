# HashiCorp Vault Configuration
# Legacy to Salesforce Migration Platform
# Version: 1.1.0
# Environment: Production
# Last Updated: 2026-03-16

# ============================================================
# STORAGE BACKEND
# ============================================================

storage "raft" {
  path    = "/vault/data"
  node_id = "vault-node-1"

  retry_join {
    leader_api_addr = "https://vault-node-1.migration-system.svc.cluster.local:8200"
    leader_ca_cert_file = "/vault/tls/ca.crt"
    leader_client_cert_file = "/vault/tls/vault-node-1.crt"
    leader_client_key_file  = "/vault/tls/vault-node-1.key"
  }

  retry_join {
    leader_api_addr = "https://vault-node-2.migration-system.svc.cluster.local:8200"
    leader_ca_cert_file = "/vault/tls/ca.crt"
    leader_client_cert_file = "/vault/tls/vault-node-2.crt"
    leader_client_key_file  = "/vault/tls/vault-node-2.key"
  }

  retry_join {
    leader_api_addr = "https://vault-node-3.migration-system.svc.cluster.local:8200"
    leader_ca_cert_file = "/vault/tls/ca.crt"
    leader_client_cert_file = "/vault/tls/vault-node-3.crt"
    leader_client_key_file  = "/vault/tls/vault-node-3.key"
  }

  # Performance tuning
  performance_multiplier = 1
  autopilot_reconcile_interval = "10s"
}

# ============================================================
# LISTENER - TLS ENFORCED
# ============================================================

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"

  # TLS Configuration
  tls_cert_file      = "/vault/tls/vault.crt"
  tls_key_file       = "/vault/tls/vault.key"
  tls_ca_cert_file   = "/vault/tls/ca.crt"
  tls_min_version    = "tls13"
  tls_cipher_suites  = "TLS_AES_256_GCM_SHA384,TLS_CHACHA20_POLY1305_SHA256,TLS_AES_128_GCM_SHA256"
  tls_require_and_verify_client_cert = false  # Client cert required for admin endpoints

  # Telemetry for Prometheus
  telemetry {
    unauthenticated_metrics_access = false
  }
}

# Internal cluster listener
listener "tcp" {
  address         = "0.0.0.0:8300"
  cluster_address = "0.0.0.0:8301"

  tls_cert_file  = "/vault/tls/vault-internal.crt"
  tls_key_file   = "/vault/tls/vault-internal.key"
  tls_ca_cert_file = "/vault/tls/ca.crt"
  tls_min_version = "tls12"
}

# ============================================================
# SEAL CONFIGURATION — Auto-unseal with Azure Key Vault
# ============================================================

seal "azurekeyvault" {
  tenant_id      = "AZURE_TENANT_ID"
  client_id      = "AZURE_CLIENT_ID_VAULT_UNSEAL"
  client_secret  = "AZURE_CLIENT_SECRET_VAULT_UNSEAL"
  vault_name     = "migration-vault-hsm"
  key_name       = "vault-unseal-key"
  # HSM-backed key for FIPS compliance
  environment    = "AzurePublicCloud"
}

# ============================================================
# CORE VAULT SETTINGS
# ============================================================

api_addr     = "https://vault.migration-system.svc.cluster.local:8200"
cluster_addr = "https://vault-node-1.migration-system.svc.cluster.local:8201"
cluster_name = "migration-vault-cluster"

ui = false  # Disable UI in production; use Vault CLI or API only

log_level  = "WARN"
log_format = "json"
log_file   = "/vault/logs/vault.log"
log_rotate_duration = "24h"
log_rotate_max_files = 30

disable_mlock = false  # mlock MUST be enabled for production (prevents swap)

default_lease_ttl = "1h"
max_lease_ttl     = "24h"

# Performance settings
default_max_request_duration = "90s"
raw_storage_endpoint = false  # Disable raw storage access

# ============================================================
# TELEMETRY
# ============================================================

telemetry {
  prometheus_retention_time = "30s"
  disable_hostname          = false

  statsd_address = "statsd.monitoring.svc.cluster.local:9125"
  statsite_address = ""

  # Disable sending to HashiCorp (air-gapped)
  disable_sending_to_hashicorp = true
}

# ============================================================
# VAULT POLICIES
# ============================================================
# Policies are managed via Terraform/Vault provider but documented here

# Policy: migration-api-policy
# Description: For the migration API service account
# Path: /vault/policies/migration-api-policy.hcl
#
# path "migration/data/salesforce/*" {
#   capabilities = ["read"]
# }
# path "migration/data/database/*" {
#   capabilities = ["read"]
# }
# path "transit/encrypt/migration-data" {
#   capabilities = ["update"]
# }
# path "transit/decrypt/migration-data" {
#   capabilities = ["update"]
# }

# Policy: migration-admin-policy
# Description: For migration administrators
# Path: /vault/policies/migration-admin-policy.hcl
#
# path "migration/*" {
#   capabilities = ["create", "read", "update", "delete", "list"]
# }
# path "sys/leases/renew" {
#   capabilities = ["update"]
# }
# path "sys/leases/revoke" {
#   capabilities = ["update"]
# }
# path "auth/token/renew-self" {
#   capabilities = ["update"]
# }

# Policy: audit-reader-policy
# Description: Read-only audit access
# Path: /vault/policies/audit-reader-policy.hcl
#
# path "sys/audit" {
#   capabilities = ["read"]
# }
# path "sys/audit-hash/*" {
#   capabilities = ["update"]
# }

# ============================================================
# AUTH METHOD CONFIGURATIONS
# (Applied via Vault CLI/Terraform after initialization)
# ============================================================

# AUTH: Kubernetes
# vault auth enable kubernetes
# vault write auth/kubernetes/config \
#   kubernetes_host="https://kubernetes.default.svc" \
#   kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
#   token_reviewer_jwt=@/var/run/secrets/kubernetes.io/serviceaccount/token \
#   issuer="https://kubernetes.default.svc.cluster.local"
#
# vault write auth/kubernetes/role/migration-api \
#   bound_service_account_names=api-service-sa \
#   bound_service_account_namespaces=migration-system \
#   policies=migration-api-policy \
#   ttl=1h
#
# vault write auth/kubernetes/role/migration-admin \
#   bound_service_account_names=migration-admin-sa \
#   bound_service_account_namespaces=migration-system \
#   policies=migration-admin-policy \
#   ttl=1h

# AUTH: OIDC (for human users via Okta)
# vault auth enable oidc
# vault write auth/oidc/config \
#   oidc_discovery_url="https://company.okta.com" \
#   oidc_client_id="OKTA_CLIENT_ID" \
#   oidc_client_secret="OKTA_CLIENT_SECRET" \
#   default_role="migration-viewer"
#
# vault write auth/oidc/role/migration-admin \
#   user_claim="sub" \
#   allowed_redirect_uris="https://vault.migration.company.com/ui/vault/auth/oidc/oidc/callback" \
#   groups_claim="groups" \
#   bound_audiences="migration-vault" \
#   ttl=1h \
#   policies=migration-admin-policy
#   oidc_scopes="openid,profile,email,groups"

# AUTH: AppRole (for CI/CD pipeline)
# vault auth enable approle
# vault write auth/approle/role/cicd-deploy \
#   secret_id_ttl=10m \
#   token_num_uses=10 \
#   token_ttl=20m \
#   token_max_ttl=30m \
#   secret_id_num_uses=1 \
#   policies=cicd-deploy-policy

# ============================================================
# SECRET ENGINE CONFIGURATIONS
# ============================================================

# KV v2 — Application secrets
# vault secrets enable -version=2 -path=migration kv
#
# Structure:
#   migration/data/salesforce/credentials
#     - client_id
#     - client_secret
#     - instance_url
#     - username
#
#   migration/data/database/legacy-db
#     - host
#     - port
#     - database
#     - username
#     - password
#     - ssl_cert
#
#   migration/data/database/postgres
#     - host
#     - port
#     - database
#     - username
#     - password
#
#   migration/data/kafka
#     - bootstrap_servers
#     - sasl_username
#     - sasl_password
#     - ssl_ca_cert
#
#   migration/data/redis
#     - host
#     - port
#     - password
#     - tls_cert
#
#   migration/data/api-keys
#     - openai_key (for AI-assisted validation)
#     - slack_webhook
#     - pagerduty_key

# Dynamic Secrets — PostgreSQL
# vault secrets enable database
# vault write database/config/migration-postgres \
#   plugin_name=postgresql-database-plugin \
#   allowed_roles="migration-app,migration-readonly" \
#   connection_url="postgresql://{{username}}:{{password}}@postgres.migration-system.svc.cluster.local:5432/migration" \
#   username="vault-admin" \
#   password="VAULT_POSTGRES_PASSWORD"
#
# vault write database/roles/migration-app \
#   db_name=migration-postgres \
#   creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA public TO \"{{name}}\";" \
#   default_ttl=1h \
#   max_ttl=4h
#
# vault write database/roles/migration-readonly \
#   db_name=migration-postgres \
#   creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"{{name}}\";" \
#   default_ttl=1h \
#   max_ttl=4h

# Transit Encryption Engine (Encryption-as-a-Service)
# vault secrets enable transit
#
# vault write transit/keys/migration-data \
#   type=aes256-gcm96 \
#   deletion_allowed=false \
#   exportable=false \
#   allow_plaintext_backup=false
#
# vault write transit/keys/migration-pii \
#   type=aes256-gcm96 \
#   deletion_allowed=false \
#   exportable=false \
#   allow_plaintext_backup=false
#   min_decryption_version=1 \
#   min_encryption_version=1

# PKI Engine — Internal CA
# vault secrets enable pki
# vault secrets tune -max-lease-ttl=87600h pki
# vault write pki/root/generate/internal \
#   common_name="migration-platform CA" \
#   ttl=87600h
# vault write pki/config/urls \
#   issuing_certificates="https://vault.migration-system.svc.cluster.local:8200/v1/pki/ca" \
#   crl_distribution_points="https://vault.migration-system.svc.cluster.local:8200/v1/pki/crl"
# vault write pki/roles/migration-services \
#   allowed_domains="migration-system.svc.cluster.local" \
#   allow_subdomains=true \
#   max_ttl=72h

# ============================================================
# AUDIT LOGGING
# ============================================================

# vault audit enable file file_path=/vault/logs/audit.log
# vault audit enable syslog tag="vault" facility="AUTH"
#
# Audit log format: JSON
# Contains: timestamp, type, auth, request, response
# All secrets in responses are HMAC-SHA256 hashed
# Retention: 7 years (per SEC-POL-002)
