# ADR-004: Zero Trust Security Model

**Status:** Accepted
**Date:** 2025-11-18
**Deciders:** Security Architect, CISO, Platform Architecture Team, Compliance Officer
**Supersedes:** N/A
**Superseded by:** N/A
**Tags:** `security`, `zero-trust`, `mtls`, `spiffe`, `spire`, `vault`, `opa`, `fedramp`, `nist-800-53`

---

## Table of Contents

1. [Context and Problem Statement](#1-context-and-problem-statement)
2. [Decision Drivers](#2-decision-drivers)
3. [Considered Options](#3-considered-options)
4. [Decision Outcome](#4-decision-outcome)
5. [Detailed Implementation Architecture](#5-detailed-implementation-architecture)
6. [NIST 800-53 Control Mapping](#6-nist-800-53-control-mapping)
7. [Pros and Cons of the Options](#7-pros-and-cons-of-the-options)
8. [Operational Procedures](#8-operational-procedures)
9. [Related Decisions](#9-related-decisions)

---

## 1. Context and Problem Statement

The Legacy-to-Salesforce migration platform operates in a threat landscape with elevated risk profile:

**Client Profile:**
- U.S. Federal Government agencies (DoD, DHS, civilian) operating at FISMA High impact level
- State and local government organizations with CJI (Criminal Justice Information) data
- Private enterprise clients with PCI-DSS and SOX obligations
- Healthcare organizations subject to HIPAA

**Data Sensitivity:**
- Personally Identifiable Information (PII) from millions of individuals
- Controlled Unclassified Information (CUI) per 32 CFR Part 2002
- Financial records subject to SOX audit requirements
- Legacy systems containing decades of sensitive transactional history

**Threat Model:**
The migration platform is uniquely exposed because it simultaneously accesses:
1. Legacy systems (often poorly secured, on-premises, aged OS/databases)
2. Modern cloud infrastructure (Kubernetes, Kafka, microservices)
3. Salesforce production environments (internet-accessible SaaS)

This creates a threat vector where a compromise of any single component could expose all three environments. Traditional perimeter security (VPN/firewall) cannot adequately address this because:

- The migration platform itself spans the perimeter (it connects legacy on-prem to cloud-hosted Salesforce)
- Lateral movement within the Kubernetes cluster could reach any tenant's migration data
- Service accounts with broad database access are necessary for bulk extraction, creating high-privilege targets
- Insider threat is elevated during migration projects (contractors, temporary staff)
- Supply chain attacks on migration tooling (Kafka clients, Salesforce SDK) have occurred in the industry

**Regulatory Mandates:**
- NIST SP 800-207 (Zero Trust Architecture) — explicitly required for federal systems post Executive Order 14028
- FedRAMP High baseline requires specific controls from NIST SP 800-53 Rev 5 that mandate Zero Trust principles
- DISA STIG compliance for containers (ASD STIG) requires workload identity
- OMB Memorandum M-22-09 mandates Zero Trust strategy for federal agencies

---

## 2. Decision Drivers

| Priority | Driver |
|----------|--------|
| P0 | EO 14028 / M-22-09 Zero Trust mandate for federal clients |
| P0 | NIST SP 800-207 compliance for FedRAMP High authorization |
| P0 | Prevent lateral movement in event of container compromise |
| P0 | Cryptographic workload identity — no shared secrets or static credentials |
| P1 | Mutual authentication for all service-to-service communication |
| P1 | Fine-grained authorization with attribute-based access control (ABAC) |
| P1 | Secrets management without hardcoded credentials in any configuration |
| P1 | Continuous verification — authenticated sessions must be re-validated |
| P2 | Audit log of every authorization decision |
| P2 | Integration with existing PKI and identity infrastructure |
| P3 | Developer experience — security controls should not require per-service boilerplate |

---

## 3. Considered Options

1. **Zero Trust with SPIFFE/SPIRE + HashiCorp Vault + OPA** (selected)
2. **VPN-Based Perimeter Security with Internal Trust**
3. **Service Mesh (Istio) with Internal CA Only**
4. **Cloud-Native IAM Only (AWS IAM / Azure Managed Identity)**

---

## 4. Decision Outcome

**Chosen option: Zero Trust Architecture with SPIFFE/SPIRE for workload identity, HashiCorp Vault for secrets management, and Open Policy Agent (OPA) for policy-based authorization.**

This combination implements the three pillars of NIST SP 800-207 Zero Trust:

1. **Identity:** Every workload receives a cryptographically verifiable identity (SPIFFE SVID) that is independent of network location and rotated automatically.
2. **Device/Workload:** All service-to-service communication uses mutual TLS (mTLS) with SPIFFE-issued certificates, eliminating implicit trust based on IP address or subnet.
3. **Policy Enforcement:** OPA evaluates authorization policies as code, with every decision logged. Policies are version-controlled and testable.

### Positive Consequences

- **Cryptographic workload identity**: No service uses a static username/password or API key for service-to-service auth. SPIFFE SVIDs are short-lived (1 hour by default) and automatically rotated by SPIRE agents.
- **mTLS everywhere**: All service-to-service communication is mutually authenticated. Network-layer compromise cannot impersonate a legitimate service without its private key.
- **No secrets in configuration**: HashiCorp Vault's dynamic secrets engine issues time-limited database credentials, Kafka credentials, and Salesforce OAuth tokens on demand. No credentials in environment variables, config maps, or source code.
- **Lateral movement prevention**: Even if an attacker gains code execution in one pod, the mTLS certificates are namespaced and the pod's SPIFFE SVID only authorizes it to call its specific downstream services.
- **Policy as code**: OPA Rego policies in version control define what each service can do. Security reviews are code reviews. Policy violations are caught in CI before deployment.
- **Continuous validation**: SPIRE rotates SVIDs on 1-hour TTL. Vault leases are renewed or revoked. A compromised workload that is killed loses credentials within 1 hour without manual intervention.
- **Compliance mapping**: Every authorization decision logged to immutable audit store maps to specific NIST 800-53 controls (AC-2, AC-3, AC-17, IA-3, IA-9, SC-8, SC-28).

### Negative Consequences

- **Operational complexity**: SPIRE server, Vault cluster, and OPA policy server are critical infrastructure. Failure of SPIRE server prevents SVIDs from being renewed, causing cascading service failures within 1 hour. High availability is mandatory and complex.
- **Certificate management learning curve**: Engineers must understand SPIFFE SVID format, trust domains, federation, and SVID rotation. Estimated 2-week training investment per engineer.
- **Vault operational burden**: Vault requires careful unseal management (Shamir's Secret Sharing or auto-unseal with HSM). Key ceremony for initial Vault initialization requires 3–5 key holders in a secure facility.
- **OPA policy testing discipline**: Poorly written OPA policies can be either too permissive (security gap) or too restrictive (operational outage). Policy test coverage must be maintained.
- **Latency overhead**: mTLS handshake adds ~2–5ms to first request per connection. Connection pooling and keep-alive mitigate this to negligible impact for bulk migration workloads.
- **SPIRE HA complexity**: In government on-premises deployments without cloud-native clustering primitives, SPIRE server HA requires etcd backend and careful network configuration.

---

## 5. Detailed Implementation Architecture

### 5.1 SPIFFE/SPIRE Workload Identity

**Trust Domain:** `spiffe://migration-platform.internal`

**SPIFFE ID Scheme:**
```
spiffe://migration-platform.internal/ns/{k8s_namespace}/sa/{service_account}/{role}

Examples:
spiffe://migration-platform.internal/ns/tenant-gov-dod-001/sa/extraction-service/reader
spiffe://migration-platform.internal/ns/tenant-gov-dod-001/sa/transformation-service/transformer
spiffe://migration-platform.internal/ns/tenant-gov-dod-001/sa/loading-service/writer
spiffe://migration-platform.internal/ns/platform-control-plane/sa/orchestrator/admin
```

**SPIRE Deployment:**
```yaml
# spire/server/deployment.yaml (abbreviated)
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: spire-server
  namespace: spire
spec:
  replicas: 3  # HA — requires etcd backend
  template:
    spec:
      containers:
      - name: spire-server
        image: ghcr.io/spiffe/spire-server:1.9.0
        args:
        - -config
        - /run/spire/config/server.conf
        volumeMounts:
        - name: spire-config
          mountPath: /run/spire/config
        - name: spire-data
          mountPath: /run/spire/data

# SPIRE Server Configuration
# server.conf
server {
  bind_address = "0.0.0.0"
  bind_port = "8081"
  trust_domain = "migration-platform.internal"
  data_dir = "/run/spire/data"
  log_level = "INFO"
  ca_key_type = "ec-p384"       # FIPS 140-2 approved curve
  ca_ttl = "24h"
  default_svid_ttl = "1h"       # Short-lived SVIDs
  jwt_issuer = "https://spire.migration-platform.internal"

  # FIPS mode
  experimental {
    require_fips = true
  }
}

plugins {
  DataStore "sql" {
    plugin_data {
      database_type = "postgres"
      connection_string = "host=spire-db port=5432 dbname=spire_server sslmode=require"
    }
  }

  KeyManager "disk" {
    plugin_data {
      keys_path = "/run/spire/data/keys.json"
    }
  }

  NodeAttestor "k8s_psat" {
    plugin_data {
      clusters = {
        "migration-cluster" = {
          service_account_allow_list = ["spire:spire-agent"]
        }
      }
    }
  }

  UpstreamAuthority "vault" {
    plugin_data {
      vault_addr = "https://vault.vault.svc.cluster.local:8200"
      pki_mount_path = "pki"
      cert_auth {
        cert_auth_mount_path = "cert"
        client_cert_path = "/run/spire/vault-client/tls.crt"
        client_key_path = "/run/spire/vault-client/tls.key"
      }
    }
  }
}
```

**SPIRE Agent DaemonSet** (deployed to every node):
```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: spire-agent
  namespace: spire
spec:
  template:
    spec:
      hostPID: true
      hostNetwork: true
      containers:
      - name: spire-agent
        image: ghcr.io/spiffe/spire-agent:1.9.0
        securityContext:
          allowPrivilegeEscalation: false
          runAsNonRoot: true
          readOnlyRootFilesystem: true
        volumeMounts:
        - name: spire-socket
          mountPath: /run/spire/sockets
          readOnly: false
      volumes:
      - name: spire-socket
        hostPath:
          path: /run/spire/sockets
          type: DirectoryOrCreate
```

### 5.2 HashiCorp Vault — Secrets Management

**Vault Cluster Architecture:**
```
Vault Cluster: 5 nodes (3 voters, 2 non-voters) using Integrated Storage (Raft)
Auto-Unseal: AWS KMS (GovCloud) or on-premises HSM (Thales Luna Network HSM 7)
Audit Backend: File (local) + Syslog (to SIEM)
Auth Methods: Kubernetes (primary), Cert (for SPIRE UpstreamAuthority)
```

**Secret Engines Configured:**

```bash
# Database secrets engine — dynamic Salesforce OAuth tokens
vault secrets enable -path=salesforce database

vault write salesforce/config/production \
    plugin_name="salesforce-database-plugin" \
    allowed_roles="migration-loader" \
    sf_instance_url="https://myorg.salesforce.com" \
    sf_client_id="${SF_CLIENT_ID}" \
    sf_client_secret="${SF_CLIENT_SECRET}"

vault write salesforce/roles/migration-loader \
    db_name=production \
    default_ttl="1h" \
    max_ttl="4h"

# KV v2 for static configuration (non-secrets)
vault secrets enable -path=migration-config kv-v2

# PKI for internal CA (SPIRE Upstream Authority)
vault secrets enable pki
vault secrets tune -max-lease-ttl=87600h pki
vault write pki/root/generate/internal \
    common_name="Migration Platform Root CA" \
    key_type="ec" key_bits="384" \
    ttl=87600h

# Transit secrets engine — envelope encryption for data at rest
vault secrets enable transit
vault write transit/keys/tenant-gov-dod-001 \
    type=aes256-gcm96 \
    exportable=false \
    allow_plaintext_backup=false

# Kafka credentials (dynamic)
vault secrets enable -path=kafka database
vault write kafka/config/migration-cluster \
    plugin_name="confluent-kafka-plugin" \
    allowed_roles="kafka-producer,kafka-consumer" \
    bootstrap_servers="kafka-0.kafka:9092,kafka-1.kafka:9092,kafka-2.kafka:9092"
```

**Vault Policies:**

```hcl
# policies/extraction-service.hcl
# Extraction service: read-only access to source system credentials
# and can write to Kafka extracted topic (via dynamic Kafka credentials)

path "migration-config/data/sources/+/connection" {
  capabilities = ["read"]
}

path "kafka/creds/kafka-producer" {
  capabilities = ["read", "update"]
  # Allows requesting dynamic Kafka producer credentials
}

path "transit/encrypt/tenant-+/*" {
  capabilities = ["update"]
  # Can encrypt data for any tenant (but not decrypt — enforced by OPA)
}

path "transit/decrypt/tenant-+/*" {
  capabilities = ["deny"]
  # Extraction service should NEVER decrypt — it only encrypts
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

# policies/loading-service.hcl
# Loading service: access Salesforce dynamic credentials, decrypt tenant data

path "salesforce/creds/migration-loader" {
  capabilities = ["read", "update"]
}

path "transit/decrypt/tenant-+/*" {
  capabilities = ["update"]
}

path "kafka/creds/kafka-consumer" {
  capabilities = ["read", "update"]
}
```

### 5.3 Open Policy Agent (OPA) — Policy Enforcement

OPA is deployed as a sidecar in every service pod AND as a central policy decision point for the API gateway.

**Kubernetes Admission Control (OPA Gatekeeper):**

```rego
# policies/k8s/require-spiffe-annotation.rego
package kubernetes.admission

violation[{"msg": msg}] {
    input.request.kind.kind == "Pod"
    not input.request.object.metadata.annotations["spiffe.io/spiffeid"]
    msg := sprintf(
        "Pod %v/%v must have spiffe.io/spiffeid annotation",
        [input.request.namespace, input.request.object.metadata.name]
    )
}

violation[{"msg": msg}] {
    input.request.kind.kind == "Pod"
    input.request.object.spec.containers[_].securityContext.privileged == true
    msg := "Privileged containers are not permitted in migration platform"
}

violation[{"msg": msg}] {
    input.request.kind.kind == "Pod"
    not input.request.object.spec.securityContext.runAsNonRoot == true
    msg := "All migration platform pods must run as non-root"
}
```

**Service Authorization Policy:**

```rego
# policies/authz/migration-service-authz.rego
package migration.authz

import future.keywords.in

# Default deny
default allow = false

# Allow extraction service to publish to extracted topics only
allow {
    input.principal.spiffe_id == concat("/", [
        "spiffe://migration-platform.internal/ns",
        input.resource.tenant_namespace,
        "sa/extraction-service/reader"
    ])
    input.action == "kafka:publish"
    regex.match(
        concat("", ["^", input.resource.tenant_id, "\\.migration\\..*\\.extracted$"]),
        input.resource.topic
    )
}

# Allow transformation service to consume extracted, publish transformed
allow {
    startswith(input.principal.spiffe_id,
        "spiffe://migration-platform.internal/ns/tenant-")
    endswith(input.principal.spiffe_id, "/sa/transformation-service/transformer")
    input.action in ["kafka:consume", "kafka:publish"]
    valid_transformation_topics[input.resource.topic]
}

valid_transformation_topics[topic] {
    topic = input.resource.topic
    regex.match(".*\\.migration\\..*\\.(extracted|transformed)$", topic)
}

# Deny cross-tenant access absolutely
deny[{"msg": msg}] {
    spiffe_tenant := regex.find_n(
        "spiffe://[^/]+/ns/([^/]+)/.*", input.principal.spiffe_id, 1
    )[0]
    resource_tenant := input.resource.tenant_id
    spiffe_tenant != resource_tenant
    msg := sprintf(
        "Cross-tenant access denied: principal from tenant %v attempted to access tenant %v resource",
        [spiffe_tenant, resource_tenant]
    )
}

# All decisions are logged (enforced at OPA config level via decision_logs)
```

**OPA Decision Logging Configuration:**
```yaml
# opa-config.yaml
decision_logs:
  service:
    name: audit-log-sink
    url: https://audit-service.platform.svc.cluster.local:8443/v1/decisions
    credentials:
      client_tls:
        cert: /run/spiffe/certs/opa-client.crt
        private_key: /run/spiffe/certs/opa-client.key
  reporting:
    min_delay_seconds: 1
    max_delay_seconds: 10
    upload_size_limit_bytes: 65536

status:
  service:
    name: policy-status-sink
    url: https://opa-bundle-server.platform.svc.cluster.local:8443/v1/status
```

### 5.4 mTLS Service Mesh Configuration

All service-to-service communication uses mTLS with SPIFFE SVIDs. The Envoy proxy sidecar handles TLS termination, allowing application code to communicate over plaintext locally while the network layer enforces encryption.

```yaml
# Envoy mTLS configuration (per-service sidecar)
static_resources:
  clusters:
  - name: transformation_service
    connect_timeout: 5s
    type: STRICT_DNS
    transport_socket:
      name: envoy.transport_sockets.tls
      typed_config:
        "@type": type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext
        common_tls_context:
          tls_certificate_sds_secret_configs:
          - name: "spiffe://migration-platform.internal/ns/tenant-gov-dod-001/sa/extraction-service/reader"
            sds_config:
              api_config_source:
                api_type: GRPC
                grpc_services:
                - envoy_grpc:
                    cluster_name: spire_agent
          combined_validation_context:
            default_validation_context:
              match_typed_subject_alt_names:
              - san_type: URI
                matcher:
                  prefix: "spiffe://migration-platform.internal/"
            validation_context_sds_secret_config:
              name: "spiffe://migration-platform.internal"
              sds_config:
                api_config_source:
                  api_type: GRPC
                  grpc_services:
                  - envoy_grpc:
                      cluster_name: spire_agent
```

---

## 6. NIST 800-53 Control Mapping

| NIST 800-53 Rev 5 Control | Implementation |
|---------------------------|----------------|
| AC-2 (Account Management) | SPIFFE SVIDs as workload identities; Vault dynamic credentials; no shared accounts |
| AC-3 (Access Enforcement) | OPA policies enforce ABAC; every access decision logged |
| AC-4 (Information Flow Enforcement) | mTLS between all services; Kafka ACLs restrict topic access by SPIFFE ID |
| AC-17 (Remote Access) | No implicit trust from network location; all remote calls require mTLS |
| IA-3 (Device Identification and Authentication) | SPIRE attests workloads via Kubernetes Pod Security Admission + node attestation |
| IA-9 (Service Identification and Authentication) | SPIFFE SVIDs provide cryptographic service identity |
| SC-8 (Transmission Confidentiality and Integrity) | TLS 1.3 for all communications; mTLS verifies both endpoints |
| SC-12 (Cryptographic Key Establishment) | HashiCorp Vault manages all keys; HSM-backed for FedRAMP High |
| SC-28 (Protection of Information at Rest) | Vault transit encryption (AES-256-GCM96) for all tenant data |
| SI-3 (Malicious Code Protection) | OPA admission control rejects non-compliant workloads |
| AU-2/AU-3 (Audit Events/Content) | OPA decision logs + SPIRE audit logs → SIEM |
| AU-9 (Protection of Audit Information) | Audit logs in WORM S3 bucket; OPA sidecar prevents tampering |
| CM-7 (Least Functionality) | Read-only root filesystems; no unnecessary capabilities |

---

## 7. Pros and Cons of the Options

### Option 1: Zero Trust with SPIFFE/SPIRE + HashiCorp Vault + OPA (Selected)

**Pros:**
- Directly implements NIST SP 800-207 Zero Trust requirements
- SPIFFE/SPIRE is CNCF graduated — production-proven at scale (Uber, Square, GitHub)
- HashiCorp Vault is the industry standard for secrets management; FedRAMP-authorized version available
- OPA is CNCF graduated; 100% policy coverage with testable Rego units
- No static credentials anywhere in the system
- Automatic SVID rotation limits blast radius of compromise to 1-hour window
- Policy-as-code enables security audit via standard code review process
- Works in on-premises, GovCloud, and multi-cloud environments

**Cons:**
- Highest operational complexity of all options
- SPIRE server HA requires careful configuration; failure means no SVID renewal
- Vault initialization key ceremony requires in-person quorum (compliance requirement)
- OPA policy complexity can grow; Rego is a non-standard policy language
- Developer experience: services must use SPIFFE Workload API or SVID helper libraries
- Initial setup time: 6–8 weeks for full production deployment and validation

**Verdict:** Complexity is required by compliance mandates. Non-negotiable for FedRAMP High.

---

### Option 2: VPN-Based Perimeter Security with Internal Trust

**Architecture:** All services deployed behind firewall/VPN. Services trust any request from within the VPN subnet. Vault used only for external-facing secrets.

**Pros:**
- Familiar operational model for traditional IT organizations
- Lower initial implementation complexity
- No per-service certificate management
- Works with legacy tools and monitoring systems

**Cons:**
- Violates NIST SP 800-207 Zero Trust principle — implicitly trusts based on network location
- Non-compliant with EO 14028 and M-22-09 (federal clients cannot use this)
- Single VPN breach gives lateral access to all services
- No workload-level authorization — any compromised service can call any other service
- Secrets still required in configuration (API keys, database passwords)
- Does not prevent insider threat (any authorized VPN user can reach all services)
- FedRAMP High authorization would require Plan of Action & Milestones (POA&M) for this gap

**Verdict:** Rejected. Non-compliant with federal mandates. Insufficient for the threat model.

---

### Option 3: Service Mesh (Istio) with Internal CA Only

**Architecture:** Istio service mesh with auto-generated mTLS certificates from Istio's internal CA. No SPIRE/SPIFFE. No OPA. HashiCorp Vault for application secrets.

**Pros:**
- Istio provides mTLS transparently without application changes
- Istio Authorization Policies provide L7 access control
- Good observability (Kiali, Jaeger integration)
- Managed certificate rotation within Istio
- Less operational overhead than full SPIFFE/SPIRE

**Cons:**
- Istio's internal CA is not FIPS 140-2 compliant by default
- Istio certificates are not SPIFFE-compliant SVIDs — cannot federate with external systems
- Cannot extend identity outside the Kubernetes cluster (e.g., to Kafka brokers outside mesh, legacy systems)
- Authorization Policies are less expressive than OPA Rego for complex ABAC rules
- Istio has had CVEs that exposed mTLS bypass vulnerabilities
- Does not satisfy NIST SP 800-207 requirement for "continuous validation of trust" without additional tooling
- Istio's resource overhead (~50MB RAM per sidecar) is significant in large deployments

**Verdict:** Rejected. Does not satisfy FIPS requirements or cross-cluster identity federation. Could be used as a complement to SPIFFE/SPIRE in future iterations.

---

### Option 4: Cloud-Native IAM Only (AWS IAM / Azure Managed Identity)

**Architecture:** Rely entirely on cloud provider IAM (AWS IAM Roles for Service Accounts, Azure Workload Identity). No SPIFFE, no Vault, no OPA.

**Pros:**
- Zero operational overhead for identity infrastructure
- Native cloud integration (IAM roles grant access to cloud resources directly)
- No certificate rotation to manage
- Well-understood by cloud engineers

**Cons:**
- Hard lock-in to single cloud provider — incompatible with multi-cloud and on-premises requirements
- Cannot be used for Kafka authentication, legacy system access, or cross-cloud calls
- No policy-as-code for authorization decisions
- Cloud IAM policies can be difficult to audit across large organizations
- Does not extend to on-premises components (SPIRE does via join tokens)
- Not portable across government cloud environments (GovCloud AWS vs Azure Government)
- Cannot satisfy NIST SP 800-207 for cross-boundary trust

**Verdict:** Rejected. Cloud lock-in is incompatible with government client requirements. Cannot cover the full service graph.

---

## 8. Operational Procedures

### 8.1 Initial Vault Key Ceremony

The Vault initialization key ceremony must be performed in person with designated key holders:

```bash
# Initialize Vault with 5 key shares, 3 required to unseal
# This is performed ONCE at cluster initialization in a secure facility

vault operator init \
    -key-shares=5 \
    -key-threshold=3 \
    -pgp-keys="keybase:alice,keybase:bob,keybase:charlie,keybase:david,keybase:eve" \
    -root-token-pgp-key="keybase:vault-admin"

# Each key holder receives ONE encrypted key share
# Root token is encrypted to vault-admin keybase key
# All key shares must be stored in separate secure locations (e.g., separate safes)
# Document the ceremony per NIST SP 800-57 key management procedures
```

### 8.2 SVID Rotation Monitoring

```bash
# Check SVID expiration across all workloads
kubectl exec -n spire deployment/spire-server -- \
    spire-server agent list | \
    awk '{print $1, $2}' | \
    while read id expiry; do
        expire_epoch=$(date -d "$expiry" +%s)
        now=$(date +%s)
        remaining=$((expire_epoch - now))
        if [ $remaining -lt 3600 ]; then
            echo "WARNING: SVID $id expires in ${remaining}s"
        fi
    done

# Force SVID rotation for a specific workload (emergency)
kubectl exec -n spire deployment/spire-server -- \
    spire-server entry delete \
    -entryID <entry-id>

# Then re-register with same entry — SPIRE agent will fetch new SVID
```

### 8.3 Emergency Credential Revocation

```bash
# Revoke a compromised Vault token immediately
vault token revoke -accessor <accessor-id>

# Revoke all tokens for a specific role (e.g., if loading service pod compromised)
vault token revoke -mode=path auth/kubernetes/role/loading-service

# Revoke a dynamic database credential
vault lease revoke database/creds/migration-loader/<lease-id>

# Revoke all leases for a compromised tenant (nuclear option)
vault lease revoke -prefix salesforce/creds/tenant-gov-dod-001/

# Delete a SPIRE entry to immediately invalidate a workload's identity
kubectl exec -n spire deployment/spire-server -- \
    spire-server entry delete \
    -entryID <entry-id>
```

### 8.4 OPA Policy Deployment

```bash
# Test OPA policies before deployment
opa test policies/ -v

# Build and sign OPA policy bundle
opa build -b policies/ -o bundle.tar.gz
cosign sign --key cosign.key bundle.tar.gz

# Deploy to OPA bundle server
kubectl create configmap opa-bundle \
    --from-file=bundle.tar.gz \
    -n platform-control-plane \
    --dry-run=client -o yaml | kubectl apply -f -

# OPA agents will hot-reload within 10 seconds
```

---

## 9. Related Decisions

- [ADR-003: Event-Driven Architecture](./ADR-003-event-driven-architecture.md) — Kafka clients use SPIFFE SVIDs for mTLS authentication
- [ADR-005: Data Transformation Strategy](./ADR-005-data-transformation-strategy.md) — Transformation rules loaded from Vault KV; schema registry access controlled by OPA
- [ADR-006: Multi-Tenant Deployment](./ADR-006-multi-tenant-deployment.md) — Kubernetes namespace boundaries align with SPIFFE trust domain namespacing
- [ADR-007: AI Agent Orchestration](./ADR-007-ai-agent-orchestration.md) — AI agents operate under constrained SPIFFE identities with OPA policies limiting data access

---

*Last reviewed: 2025-11-18*
*Next review due: 2026-05-18 (semi-annual; or after any NIST 800-53 revision)*
*Document owner: Security Architect*
*Classification: SENSITIVE — contains architecture details relevant to security posture*
