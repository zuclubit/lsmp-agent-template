# ADR-006: Multi-Tenant Deployment Architecture

**Status:** Proposed
**Date:** 2025-11-28
**Deciders:** Platform Architecture Team, DevOps Lead, CISO, Business Development
**Review Required By:** Engineering Lead, Compliance Officer
**Tags:** `kubernetes`, `multi-tenancy`, `deployment`, `isolation`, `namespace`, `government`

---

## Table of Contents

1. [Context and Problem Statement](#1-context-and-problem-statement)
2. [Decision Drivers](#2-decision-drivers)
3. [Considered Options](#3-considered-options)
4. [Proposed Decision](#4-proposed-decision)
5. [Tenant Isolation Model](#5-tenant-isolation-model)
6. [Shared vs Dedicated Infrastructure](#6-shared-vs-dedicated-infrastructure)
7. [Tenant Onboarding Procedure](#7-tenant-onboarding-procedure)
8. [Pros and Cons of Options](#8-pros-and-cons-of-options)
9. [Open Questions Requiring Resolution](#9-open-questions-requiring-resolution)
10. [Related Decisions](#10-related-decisions)

---

## 1. Context and Problem Statement

The migration platform serves two fundamentally different client categories with conflicting infrastructure requirements:

**Government Sector Clients:**
- U.S. Federal agencies and DoD components operating under FedRAMP High baseline
- Strict data residency: government client data cannot co-reside on compute with non-government data
- Audit requirements mandate complete separation of audit logs and access control
- Some clients require dedicated encryption keys (not shared HSM partitions) per NIST SP 800-57
- Contract vehicles (GSA Schedule, CIO-SP3) may prohibit data commingling
- Authority to Operate (ATO) documentation must enumerate all shared infrastructure
- Some DoD clients require Impact Level 5 (IL5) isolation — no sharing of any infrastructure layer

**Private Enterprise Clients:**
- Fortune 500 corporations, mid-market companies
- Cost-sensitive — dedicated infrastructure per client is cost-prohibitive
- Migration projects are time-bounded (weeks to months) — infrastructure must be rapidly provisioned and deprovisioned
- May have their own compliance requirements (PCI-DSS, SOX, HIPAA) requiring logical separation
- Expect cloud-native SaaS economics ($0.10–$0.50 per migrated record, not $50,000/month dedicated infrastructure)

**Current State:**
The platform currently runs all clients on a single shared Kubernetes cluster with application-level tenant filtering (tenant_id in every database query). This approach has three critical problems:
1. No enforcement of data isolation at the infrastructure layer — a SQL injection in one tenant's path could access another tenant's data
2. Government clients cannot receive ATO with this architecture (data commingling)
3. A noisy neighbor (large migration job) can starve other tenants' resources

**Growth Projections:**
- Current: 3 active tenants (2 enterprise, 1 government pilot)
- 6 months: 15–20 tenants (3–5 government, 12–15 enterprise)
- 18 months: 50–80 tenants
- Infrastructure must support this growth without linear cost scaling

---

## 2. Decision Drivers

| Priority | Driver |
|----------|--------|
| P0 | Government clients require infrastructure-layer data isolation |
| P0 | FedRAMP continuous monitoring requires enumerable, bounded system boundaries |
| P0 | ATO documentation requires per-client isolation attestation |
| P1 | Resource isolation (CPU, memory, network) to prevent noisy neighbor effects |
| P1 | Independent deployment of tenant workloads (one tenant's bad deployment should not affect others) |
| P1 | Tenant self-service for migration configuration (within authorized boundaries) |
| P2 | Cost efficiency — shared control plane and infrastructure services |
| P2 | Rapid tenant onboarding (< 4 hours from contract signing to operational) |
| P2 | Independent tenant scaling (burst capacity for large migration jobs) |
| P3 | Tenant-specific monitoring dashboards without cross-tenant visibility |
| P3 | Support for bring-your-own cloud (BYOC) for large enterprise clients |

---

## 3. Considered Options

1. **Namespace-Based Multi-Tenancy with Tiered Isolation** (proposed)
2. **Dedicated Kubernetes Cluster per Tenant**
3. **Shared Application Layer with Database-Level Tenant Isolation**
4. **vCluster (Virtual Clusters) per Tenant**

---

## 4. Proposed Decision

**Proposed option: Namespace-Based Multi-Tenancy with a tiered isolation model.**

Tenants are classified into three tiers, each with a different level of infrastructure isolation. This balances cost efficiency for enterprise clients with the mandatory isolation requirements for government clients.

### Isolation Tiers

| Tier | Target Clients | Isolation Level | Relative Cost |
|------|---------------|-----------------|---------------|
| Tier 1: Dedicated Cluster | DoD IL5, FedRAMP High (strict) | Physical compute separation | $$$$ |
| Tier 2: Dedicated Namespace Pool | FedRAMP Moderate, HIPAA, PCI L1 | Logical isolation on shared cluster, dedicated nodes | $$$ |
| Tier 3: Shared Namespace Pool | Enterprise (non-regulated) | Namespace isolation, shared nodes with ResourceQuotas | $ |

### Positive Consequences (Projected)

- Government clients receive ATO-compatible isolation without requiring fully dedicated clusters (except IL5)
- Enterprise clients get cloud-economics (shared infrastructure) with logical isolation
- Tenant onboarding is automated via Terraform and Helm — target: < 4 hours per tenant
- ResourceQuotas and LimitRanges prevent noisy neighbors in shared pools
- Namespace-scoped RBAC ensures tenant operators cannot see other tenants' resources
- Network Policies provide L3/L4 isolation between tenant namespaces
- Kafka topic ACLs (ADR-003) align with Kubernetes namespace boundaries

### Negative Consequences (Projected)

- Kubernetes namespace proliferation: 80 tenants × 3 namespaces each (app, monitoring, storage) = 240 namespaces. Namespace management must be automated.
- Multi-cluster management (Tier 1) requires fleet management tooling (Argo CD fleet, Cluster API)
- Shared Kafka cluster for Tier 2/3 tenants requires careful ACL management to prevent data leakage
- Network Policy complexity grows with tenant count; misconfiguration could create security gaps
- Node affinity and taints for Tier 2 dedicated nodes require careful scheduling configuration

---

## 5. Tenant Isolation Model

### 5.1 Namespace Structure per Tenant

```
Tier 2/3 Tenant: tenant-{client-id}

Kubernetes Namespaces:
  tenant-{client-id}-app        # Migration workloads (extraction, transformation, loading)
  tenant-{client-id}-monitoring # Prometheus, Grafana (tenant-scoped)
  tenant-{client-id}-storage    # PVCs, PVs for tenant-specific data

RBAC:
  ClusterRole: none (tenants have no cluster-level access)
  Role: tenant-operator (read/write in app namespace only)
  Role: tenant-viewer (read-only in app and monitoring namespaces)
  ServiceAccount: extraction-service, transformation-service, loading-service, orchestrator
    (each with minimal permissions per ADR-004 Zero Trust)

ResourceQuota (per tenant-{client-id}-app):
  requests.cpu: 16           # 16 CPU cores maximum
  requests.memory: 64Gi      # 64GB RAM maximum
  limits.cpu: 32             # 32 CPU cores burst
  limits.memory: 128Gi       # 128GB RAM burst
  count/pods: 50             # Maximum 50 pods
  count/services: 10
  requests.storage: 500Gi

LimitRange (per namespace):
  Container default request: 100m CPU, 256Mi RAM
  Container default limit: 500m CPU, 1Gi RAM
  Container max: 4 CPU, 16Gi RAM
  PVC max: 100Gi
```

### 5.2 Network Policy (Tenant Isolation)

```yaml
# Per-tenant default deny — applied to each tenant namespace on creation
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: tenant-gov-dod-001-app
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress
---
# Allow intra-namespace communication
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-intra-namespace
  namespace: tenant-gov-dod-001-app
spec:
  podSelector: {}
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: tenant-gov-dod-001-app
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: tenant-gov-dod-001-app
---
# Allow egress to shared services (Kafka, Schema Registry, Vault)
# Only from service accounts that require it
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-shared-services
  namespace: tenant-gov-dod-001-app
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/component: migration-workload
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: platform-kafka
    ports:
    - protocol: TCP
      port: 9092  # Kafka (mTLS enforced at Kafka layer)
  - to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: platform-vault
    ports:
    - protocol: TCP
      port: 8200  # Vault
  - to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except:
        - 10.0.0.0/8
        - 172.16.0.0/12
        - 192.168.0.0/16
    ports:
    - protocol: TCP
      port: 443   # Salesforce API (HTTPS only)
```

### 5.3 Node Isolation (Tier 2)

```yaml
# Node taint for government-dedicated nodes
kubectl taint nodes gov-node-1 gov-node-2 gov-node-3 \
    dedicated=government:NoSchedule

# Toleration in tenant workloads (Tier 2 government tenants only)
tolerations:
- key: "dedicated"
  operator: "Equal"
  value: "government"
  effect: "NoSchedule"

# Node affinity — government workloads prefer government nodes
affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      preference:
        matchExpressions:
        - key: node-classification
          operator: In
          values:
          - government
```

---

## 6. Shared vs Dedicated Infrastructure

### 6.1 Always Shared (All Tiers)

| Component | Sharing Model | Isolation Mechanism |
|-----------|--------------|---------------------|
| Kubernetes Control Plane | Shared | RBAC, Admission Controllers |
| Ingress Controller | Shared | TLS SNI routing, separate certificates per tenant |
| Cert-Manager | Shared | Per-tenant certificate namespaces |
| External DNS | Shared | Per-tenant DNS zones |
| Cluster Autoscaler | Shared | Node pool taints for Tier 2 |
| Container Image Registry | Shared | Read-only; images are not tenant-specific |

### 6.2 Tier-Dependent Sharing

| Component | Tier 1 | Tier 2 | Tier 3 |
|-----------|--------|--------|--------|
| Kubernetes Cluster | Dedicated | Shared (dedicated nodes) | Shared |
| Kafka Cluster | Dedicated | Shared (dedicated ACLs) | Shared |
| Schema Registry | Dedicated | Shared (per-tenant subject prefixes) | Shared |
| HashiCorp Vault | Dedicated | Shared (per-tenant namespaces) | Shared |
| PostgreSQL (audit DB) | Dedicated | Shared (per-tenant schemas) | Shared |
| Prometheus | Dedicated | Shared (per-tenant scrape configs) | Shared |
| Encryption Keys | Dedicated HSM partition | Vault per-tenant transit key | Vault per-tenant transit key |

### 6.3 Cost Model

```
Tier 1 (Dedicated Cluster):
  Fixed cost: ~$25,000–$45,000/month (on-premises) or ~$15,000–$25,000/month (GovCloud)
  Variable cost: $0.15/record migrated
  Minimum engagement: 3 months
  Target: DoD, IC, large federal agencies

Tier 2 (Shared Cluster, Dedicated Nodes):
  Fixed cost: ~$3,000–$8,000/month per tenant (dedicated node reservation)
  Variable cost: $0.08/record migrated
  Minimum engagement: 1 month
  Target: FedRAMP Moderate agencies, HIPAA covered entities, PCI L1 merchants

Tier 3 (Fully Shared):
  Fixed cost: $500–$2,000/month (based on ResourceQuota utilization)
  Variable cost: $0.04/record migrated
  No minimum engagement
  Target: Commercial enterprise clients
```

---

## 7. Tenant Onboarding Procedure

### 7.1 Automated Onboarding Pipeline

```bash
# Step 1: Render tenant configuration from template
cat > tenant-config.yaml << EOF
tenant_id: "ent-acme-corp"
tenant_name: "Acme Corporation"
tier: 3
environment: production
salesforce_org_id: "00D5f000000XXXXX"
data_residency_region: "us-east-1"
contacts:
  technical: "admin@acme.com"
  security: "security@acme.com"
resource_quota:
  cpu_requests: "8"
  memory_requests: "32Gi"
  storage: "200Gi"
EOF

# Step 2: Apply Terraform to create namespaces, RBAC, NetworkPolicies
terraform -chdir=infrastructure/tenants apply \
    -var-file="tenant-config.yaml" \
    -var="tenant_id=ent-acme-corp"

# Step 3: Initialize Vault namespace and policies for tenant
vault namespace create ent-acme-corp
vault policy write -namespace=ent-acme-corp \
    extraction-service policies/extraction-service.hcl
vault write -namespace=ent-acme-corp \
    transit/keys/ent-acme-corp type=aes256-gcm96

# Step 4: Register Kafka ACLs for tenant topics
kafka-acls --bootstrap-server kafka:9092 \
    --add \
    --allow-principal "User:CN=ent-acme-corp-extraction" \
    --operation Write \
    --topic "prod.ent-acme-corp.migration.*" \
    --resource-pattern-type prefixed

# Step 5: Create SPIRE registration entries
spire-server entry create \
    -spiffeID spiffe://migration-platform.internal/ns/tenant-ent-acme-corp-app/sa/extraction-service/reader \
    -parentID spiffe://migration-platform.internal/ns/spire/sa/spire-agent \
    -selector k8s:ns:tenant-ent-acme-corp-app \
    -selector k8s:sa:extraction-service \
    -ttl 3600

# Step 6: Deploy tenant workloads via Helm
helm upgrade --install \
    migration-tenant-ent-acme-corp \
    charts/migration-tenant \
    --namespace tenant-ent-acme-corp-app \
    --values tenant-config.yaml \
    --wait

# Step 7: Validate tenant health
kubectl run tenant-onboard-test \
    --namespace tenant-ent-acme-corp-app \
    --image migration-platform/health-check:latest \
    --env="TENANT_ID=ent-acme-corp" \
    --restart=Never \
    --rm -it
```

---

## 8. Pros and Cons of Options

### Option 2: Dedicated Kubernetes Cluster per Tenant

**Pros:** Maximum isolation; simplest security model; independent upgrade cycles; true noisy neighbor elimination.

**Cons:** At 80 tenants, 80 clusters = massive operational overhead; control plane cost dominates for small migration projects; cluster provisioning takes 20–45 minutes (not 4-hour SLA); Tier 3 economics impossible ($3,000+/month just for cluster overhead); fleet management tooling (Cluster API) required regardless.

**Verdict:** Retained as Tier 1 for DoD IL5. Rejected as general model.

---

### Option 3: Shared Application Layer with Database-Level Tenant Isolation

**Pros:** Simple deployment; no Kubernetes complexity; PostgreSQL Row-Level Security provides data isolation.

**Cons:** Application bugs can expose cross-tenant data (SQL injection risk); no resource isolation (noisy neighbor); RLS bypass vulnerabilities have existed in PostgreSQL; government clients cannot receive ATO with this model; does not scale to Kafka multi-tenancy; no network isolation between tenant workloads.

**Verdict:** Rejected. Application-layer isolation is insufficient for government compliance. Current state — being replaced.

---

### Option 4: vCluster (Virtual Clusters) per Tenant

**Pros:** Each tenant gets a dedicated virtual Kubernetes API server; stronger isolation than namespaces; tenants can have different Kubernetes versions; fully isolated RBAC; simpler than full dedicated clusters.

**Cons:** vCluster is relatively new technology (1.0 released 2024); additional operational complexity over namespace approach; host cluster still shared for compute; vCluster networking adds latency; limited production case studies at 80+ tenant scale; resource overhead per vCluster (API server pod); tenant kubectl access to virtual cluster complicates security model.

**Verdict:** Rejected for this release. Promising for future consideration when Tier 2 isolation requirements tighten. Could replace namespace-based isolation for Tier 2 in 18-month horizon.

---

## 9. Open Questions Requiring Resolution

Before this ADR can move from **Proposed** to **Accepted**, the following questions must be answered:

1. **IL5 Cluster Count**: How many DoD IL5 clients are projected in the next 12 months? If > 5, a shared IL5 cluster with dedicated nodes may be more cost-effective than per-client clusters. Requires input from Business Development.

2. **Shared Kafka for Government Tier 2**: FedRAMP auditors may require physical data separation even for Tier 2. Is a government-only Kafka cluster (shared among all Tier 2 government clients, isolated from Tier 3 enterprise clients) acceptable? Requires compliance review.

3. **Resource Quota Enforcement**: The proposed ResourceQuotas (16 CPU, 64GB RAM) were sized for typical migration jobs. Large clients migrating 500M records may require 4× this capacity for acceptable duration. Does the quota model support burst allowances, and what is the pricing model for burst?

4. **Tenant Kubernetes Access**: Should Tier 2/3 clients have kubectl access to their namespace? This enables self-service debugging but increases attack surface. Current proposal: no direct kubectl access; all operations via migration platform API. This needs client input.

5. **Vault Enterprise Namespaces vs OSS**: HashiCorp Vault Enterprise namespaces provide stronger tenant isolation than OSS namespace mount paths. The cost delta (Vault Enterprise at $50K+/year) vs the isolation improvement needs evaluation against compliance requirements.

---

## 10. Related Decisions

- [ADR-003: Event-Driven Architecture](./ADR-003-event-driven-architecture.md) — Kafka topic naming and ACL scheme aligns with namespace boundaries
- [ADR-004: Zero Trust Security Model](./ADR-004-zero-trust-security-model.md) — SPIFFE namespacing maps to Kubernetes namespace boundaries; Vault namespaces per tier
- [ADR-005: Data Transformation Strategy](./ADR-005-data-transformation-strategy.md) — Rule sets are per-tenant and stored in Vault KV under tenant namespace

---

*Last reviewed: 2025-11-28 (Proposed — awaiting approval)*
*Decision required by: 2026-01-15 (before Q1 government client onboarding)*
*Document owner: Platform Architecture Team + DevOps Lead*
*Review stakeholders: CISO, Compliance Officer, Business Development, Engineering Lead*
