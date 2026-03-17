# FedRAMP Moderate Control Mapping
## Legacy to Salesforce Migration Platform

**Document ID:** SEC-COMP-001
**FedRAMP Baseline:** Moderate (NIST SP 800-53 Rev 5)
**Authorization Type:** Agency ATO
**Version:** 1.0.0
**Last Updated:** 2026-03-16
**Status:** In Progress — Pre-Authorization

---

## Control Families Summary

| Family | Controls | Implemented | Partially | Planned | N/A |
|--------|----------|-------------|-----------|---------|-----|
| AC — Access Control | 25 | 20 | 4 | 1 | 0 |
| AU — Audit and Accountability | 12 | 10 | 2 | 0 | 0 |
| CA — Assessment, Authorization | 9 | 6 | 3 | 0 | 0 |
| CM — Configuration Management | 11 | 8 | 2 | 1 | 0 |
| CP — Contingency Planning | 13 | 9 | 3 | 1 | 0 |
| IA — Identification and Authentication | 12 | 11 | 1 | 0 | 0 |
| IR — Incident Response | 10 | 8 | 2 | 0 | 0 |
| MA — Maintenance | 6 | 4 | 1 | 0 | 1 |
| MP — Media Protection | 8 | 6 | 2 | 0 | 0 |
| PE — Physical and Environmental | 20 | 0 | 0 | 0 | 20 |
| PL — Planning | 9 | 7 | 2 | 0 | 0 |
| PS — Personnel Security | 9 | 7 | 1 | 1 | 0 |
| RA — Risk Assessment | 6 | 4 | 2 | 0 | 0 |
| SA — System and Services Acquisition | 23 | 15 | 5 | 3 | 0 |
| SC — System and Communications Protection | 44 | 30 | 8 | 4 | 2 |
| SI — System and Information Integrity | 17 | 12 | 4 | 1 | 0 |
| SR — Supply Chain Risk Management | 12 | 7 | 3 | 2 | 0 |

---

## Access Control (AC)

### AC-1 — Policy and Procedures
**Status:** IMPLEMENTED
**Implementation:** Security Policy (SEC-POL-001) documents access control policy and procedures. Reviewed quarterly; current version 2.1.0 dated 2026-03-16.
**Evidence:** security/policies/security_policy.md, SharePoint policy repository.

### AC-2 — Account Management
**Status:** IMPLEMENTED
**Implementation:**
- Accounts provisioned via Okta with mandatory manager approval
- Service accounts registered in CMDB
- Quarterly access reviews using automated tooling
- Accounts deprovisioned within 1 business day of termination
- Shared accounts prohibited

**Evidence:** Okta provisioning logs, CMDB records, access review reports.

### AC-3 — Access Enforcement
**Status:** IMPLEMENTED
**Implementation:** RBAC enforced at API layer (rbac_config.py), Kubernetes layer (roles.yaml), and Salesforce Connected App (org-level permissions). All access decisions logged to audit log.

**Evidence:** security/rbac/rbac_config.py, security/rbac/roles.yaml.

### AC-4 — Information Flow Enforcement
**Status:** IMPLEMENTED
**Implementation:**
- Network policies enforce traffic flow between migration tiers
- Istio service mesh enforces mTLS between services
- Egress filtering via network policy
- Data classification labels enforced by DLP

**Evidence:** K8s NetworkPolicy manifests, Istio AuthorizationPolicy.

### AC-5 — Separation of Duties
**Status:** IMPLEMENTED
**Implementation:**
- Migration approval and execution roles are separate (migration-admin vs migration-operator)
- Production deployments require 2 approvers in CI/CD
- Security team separate from operations team
- Four-eyes principle enforced in ArgoCD

**Evidence:** roles.yaml role matrix, CI/CD pipeline configuration.

### AC-6 — Least Privilege
**Status:** IMPLEMENTED
**Implementation:**
- Service accounts have minimum required permissions (api-service role)
- JIT access for production administrative actions (4h max)
- Database users created with dynamic credentials via Vault
- Kubernetes service accounts have automountServiceAccountToken=false

**Evidence:** security/rbac/roles.yaml, vault_config.hcl database roles.

### AC-7 — Unsuccessful Logon Attempts
**Status:** IMPLEMENTED
**Implementation:**
- 5 failed attempts trigger 30-minute lockout
- Progressive lockout up to 24 hours
- CAPTCHA after 3 failures
- Alerts sent to security team on 5+ failures from same IP

**Evidence:** Okta authentication policy, WAF rate limiting rules.

### AC-11 — Session Lock
**Status:** IMPLEMENTED
**Implementation:**
- Admin sessions lock after 15 minutes of inactivity
- Standard sessions lock after 30 minutes
- Session tokens expire per role (migration-admin: 60 min, others: 480 min)
- JWT expiry enforced server-side

**Evidence:** rbac_config.py session_timeout_minutes.

### AC-17 — Remote Access
**Status:** IMPLEMENTED
**Implementation:**
- All remote access requires VPN (WireGuard/AnyConnect)
- MFA required for VPN access
- Split tunneling disabled for production
- VPN sessions logged to SIEM

**Evidence:** VPN configuration, Okta MFA policy.

### AC-22 — Publicly Accessible Content
**Status:** IMPLEMENTED
**Implementation:**
- No public endpoints expose migration data
- Public status API returns only aggregate health metrics
- Content reviewed by security team before publication

---

## Audit and Accountability (AU)

### AU-2 — Event Logging
**Status:** IMPLEMENTED
**Implementation:** Comprehensive audit events defined in audit_logger.py:
- Authentication: login success/failure, MFA
- Authorization: allow/deny decisions
- Data access: read, write, delete, export
- Configuration changes
- Migration operations: start, complete, fail, approve

**Evidence:** security/audit/audit_logger.py AuditEventType enum.

### AU-3 — Content of Audit Records
**Status:** IMPLEMENTED
**Implementation:** AuditEvent dataclass captures:
- Timestamp (UTC, ISO 8601)
- Event type and severity
- Actor: user ID, username, IP address, roles
- Target: resource type, resource ID
- Outcome: success/failure with reason
- Correlation ID for distributed tracing
- Session ID

**Evidence:** security/audit/audit_logger.py AuditEvent class.

### AU-4 — Audit Log Storage Capacity
**Status:** IMPLEMENTED
**Implementation:**
- Audit logs stored in Elasticsearch cluster (3-node, 3TB per node)
- ILM policy: hot (7 days), warm (30 days), cold (6 months), frozen (7 years)
- Auto-rollover at 50GB per index
- Capacity alerts at 80% utilization

**Evidence:** Elasticsearch ILM policy, Grafana storage dashboard.

### AU-5 — Response to Audit Logging Process Failures
**Status:** IMPLEMENTED
**Implementation:**
- Async audit queue with 10,000 event buffer
- If Elasticsearch unavailable: write to local file sink
- If both fail: log to stderr and alert PagerDuty
- Never fail business operation due to audit failure

**Evidence:** audit_logger.py AuditLogger._worker() fallback logic.

### AU-9 — Protection of Audit Information
**Status:** IMPLEMENTED
**Implementation:**
- Audit logs immutable (Elasticsearch ILM with write-lock)
- Tamper-evident HMAC chain (TamperEvidentChain class)
- Audit logs encrypted at rest (AES-256)
- Access to audit logs restricted to audit-reader role
- WORM storage for compliance tier (S3 Object Lock)

**Evidence:** audit_logger.py TamperEvidentChain, Elasticsearch security config.

### AU-12 — Audit Record Generation
**Status:** IMPLEMENTED
**Implementation:**
- All components use centralized audit logger
- Audit events generated at both application and infrastructure level
- Kubernetes audit logs forwarded to SIEM
- API gateway logs forwarded to SIEM

---

## Identification and Authentication (IA)

### IA-2 — Identification and Authentication (Organizational Users)
**Status:** IMPLEMENTED
**Implementation:**
- All users authenticated via Okta OIDC
- MFA enforced for all accounts (FIDO2 preferred)
- No shared accounts
- Service accounts use Kubernetes workload identity

**Evidence:** Okta authentication policy, OIDC config in vault_config.hcl.

### IA-2(1) — Multi-Factor Authentication to Privileged Accounts
**Status:** IMPLEMENTED
**Implementation:**
- Hardware security keys (YubiKey) required for migration-admin role
- TOTP accepted for migration-operator
- MFA bypass prohibited
- MFA status captured in JWT claims and checked by RBAC

**Evidence:** rbac_config.py RoleDefinition.require_mfa.

### IA-4 — Identifier Management
**Status:** IMPLEMENTED
**Implementation:**
- User IDs provisioned by Okta; unique and non-reusable
- Service account IDs follow naming convention: {service}-sa
- Identifiers never reused after deprovisioning (90-day quarantine)
- Identifiers stored in CMDB

### IA-5 — Authenticator Management
**Status:** IMPLEMENTED
**Implementation:**
- Passwords meet complexity requirements (16+ chars)
- Service account credentials stored in Vault; rotated every 90 days
- API keys generated via secrets manager; unique per service
- Compromised credentials revoked within 1 hour of detection

**Evidence:** vault_config.hcl, secrets_manager.py.

### IA-8 — Identification and Authentication (Non-Organizational Users)
**Status:** IMPLEMENTED
**Implementation:**
- External auditors receive time-limited accounts (audit-reader role, 30-day max)
- Vendor service accounts issued via separate IdP group
- All external access requires NDA and DPA

---

## System and Communications Protection (SC)

### SC-7 — Boundary Protection
**Status:** IMPLEMENTED
**Implementation:**
- NGFW with stateful inspection at perimeter
- WAF (Cloudflare/Azure WAF) for all public-facing APIs
- DMZ separates internet-facing from internal services
- Kubernetes NetworkPolicy enforces pod-level segmentation
- Istio service mesh enforces mTLS inside cluster

### SC-8 — Transmission Confidentiality and Integrity
**Status:** IMPLEMENTED
**Implementation:**
- TLS 1.3 for all external communications
- mTLS for all inter-service communications
- HSTS headers (max-age=31536000, includeSubDomains)
- Certificate management via cert-manager (automated renewal)

**Evidence:** Istio PeerAuthentication policy, cert-manager configuration.

### SC-12 — Cryptographic Key Establishment and Management
**Status:** IMPLEMENTED
**Implementation:**
- Master keys in Azure HSM (FIPS 140-2 Level 3)
- Data keys in HashiCorp Vault (auto-sealed to HSM)
- Key rotation automated: data keys every 90 days
- Key ceremonies for master keys: 3-of-5 quorum
- Key escrow documented and tested annually

**Evidence:** vault_config.hcl, encryption_service.py.

### SC-13 — Cryptographic Protection
**Status:** IMPLEMENTED
**Implementation:**
- AES-256-GCM for data at rest (FIPS 140-2 validated)
- TLS 1.3 with ECDHE for data in transit
- SHA-256/SHA-384 for hashing
- Ed25519 for digital signatures
- PBKDF2-SHA256 (600,000 iterations) for key derivation

**Evidence:** encryption_service.py, security_policy.md Section 7.

### SC-28 — Protection of Information at Rest
**Status:** IMPLEMENTED
**Implementation:**
- All databases encrypted with AES-256 (CMK)
- Kubernetes persistent volumes encrypted (cloud-provider encryption)
- Field-level encryption for PII (SSN, financial data)
- Backups encrypted with separate key

**Evidence:** encryption_service.py, K8s StorageClass encrypted=true.

---

## System and Information Integrity (SI)

### SI-2 — Flaw Remediation
**Status:** IMPLEMENTED
**Implementation:**
- Automated vulnerability scanning: Trivy (containers), Bandit/Semgrep (code), Dependabot (deps)
- Critical CVEs patched within 24 hours
- High CVEs patched within 7 days
- POA&M tracked in JIRA SECOPS project
- Patch compliance dashboard in Grafana

**Evidence:** security/scanning/ configuration files, CI/CD pipeline.

### SI-3 — Malware Protection
**Status:** IMPLEMENTED
**Implementation:**
- Container images scanned for malware before deployment
- No shell access in production containers (distroless images)
- Runtime security via Falco (behavioral anomaly detection)
- Unsigned images rejected by admission controller

### SI-4 — System Monitoring
**Status:** IMPLEMENTED
**Implementation:**
- SIEM (Splunk/ELK) with 24/7 monitoring
- Prometheus/Grafana for metrics and alerting
- Jaeger/Tempo for distributed tracing
- Falco for runtime security monitoring
- PagerDuty integration for P0/P1 alerts

**Evidence:** monitoring/ directory configuration files.

### SI-10 — Information Input Validation
**Status:** IMPLEMENTED
**Implementation:**
- Pydantic validation on all API inputs
- SOQL injection prevention (parameterized queries)
- File upload scanning for migration data files
- Schema validation on source data before transformation

---

## Control Implementation Summary

**Controls Fully Implemented:** 136 of 205 applicable controls (66%)
**Controls Partially Implemented:** 42 (20%)
**Controls Planned:** 14 (7%)
**Controls Not Applicable (PE family):** 20 (10%) — CSP responsible per shared responsibility model

**Target ATO Date:** Q3 2026
**3PAO Assessment:** Scheduled Q2 2026
**Current POA&M Items:** 8 (tracked in SEC-JIRA project)
