# Information Security Policy
## Legacy to Salesforce Migration Project

**Document ID:** SEC-POL-001
**Version:** 2.1.0
**Classification:** Internal
**Owner:** Chief Information Security Officer (CISO)
**Last Reviewed:** 2026-03-16
**Next Review:** 2026-09-16
**Status:** APPROVED

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Regulatory Framework](#2-regulatory-framework)
3. [Access Control Policy](#3-access-control-policy)
4. [Data Handling Policy](#4-data-handling-policy)
5. [Network Security](#5-network-security)
6. [Application Security](#6-application-security)
7. [Cryptography Standards](#7-cryptography-standards)
8. [Incident Response](#8-incident-response)
9. [Acceptable Use Policy](#9-acceptable-use-policy)
10. [Third-Party and Vendor Management](#10-third-party-and-vendor-management)
11. [Business Continuity](#11-business-continuity)
12. [Compliance and Audit](#12-compliance-and-audit)
13. [Roles and Responsibilities](#13-roles-and-responsibilities)
14. [Policy Exceptions](#14-policy-exceptions)
15. [Enforcement and Violations](#15-enforcement-and-violations)

---

## 1. Purpose and Scope

### 1.1 Purpose

This Information Security Policy establishes the security requirements, controls, and procedures for the Legacy to Salesforce Migration project. It defines the standards to protect the confidentiality, integrity, and availability (CIA triad) of all data assets processed, transmitted, or stored during the migration lifecycle.

### 1.2 Scope

This policy applies to:

- All personnel (employees, contractors, consultants, vendors) with access to migration systems
- All data assets: source legacy system data, transformation artifacts, Salesforce target data
- All environments: development, staging, pre-production, production
- All system components: APIs, databases, message queues, orchestration platforms, CI/CD pipelines
- Cloud infrastructure: Azure (primary), AWS (DR), GCP (analytics workloads)
- Network components: VPNs, firewalls, load balancers, service meshes

### 1.3 Out of Scope

- Salesforce platform-internal security controls (governed by Salesforce Trust)
- End-user workstation security (governed by Corporate IT Security Policy SEC-POL-000)

---

## 2. Regulatory Framework

### 2.1 Applicable Standards and Frameworks

| Framework | Applicability | Compliance Level |
|-----------|--------------|-----------------|
| FedRAMP Moderate | Federal data processing | Required - Full ATO |
| SOC 2 Type II | Service organization controls | Required - Annual audit |
| ISO/IEC 27001:2022 | ISMS framework | Required - Certification |
| NIST SP 800-53 Rev 5 | Security controls | Reference standard |
| NIST SP 800-171 | CUI protection | Required for federal contracts |
| OWASP ASVS 4.0 | Application security verification | Level 2 minimum |
| CIS Controls v8 | Security benchmarks | Implementation Group 2 |
| GDPR | EU personal data | Required - Data processor |
| CCPA | California residents | Required - Service provider |
| HIPAA | Health data (if applicable) | Conditional |
| PCI DSS v4.0 | Payment card data | Conditional |

### 2.2 FedRAMP Specific Requirements

All systems processing federal data must:

1. Implement FIPS 140-2 validated cryptographic modules
2. Maintain a System Security Plan (SSP) updated quarterly
3. Complete annual security assessments by a 3PAO
4. Implement continuous monitoring per FedRAMP ConMon requirements
5. Report security incidents within 1 hour to the AO and US-CERT
6. Maintain POA&M for all identified vulnerabilities

### 2.3 SOC 2 Trust Service Criteria

The following Trust Service Criteria are in scope:

- **CC1:** Control Environment
- **CC2:** Communication and Information
- **CC3:** Risk Assessment
- **CC4:** Monitoring Activities
- **CC5:** Control Activities
- **CC6:** Logical and Physical Access Controls (all sub-criteria)
- **CC7:** System Operations
- **CC8:** Change Management
- **CC9:** Risk Mitigation
- **A1:** Availability
- **C1:** Confidentiality

---

## 3. Access Control Policy

### 3.1 Principles

**Principle of Least Privilege (PoLP):** All users, services, and processes must be granted only the minimum permissions required to perform their designated functions.

**Zero Trust Architecture:** No implicit trust. Every access request must be authenticated, authorized, and continuously validated regardless of network location.

**Need-to-Know:** Access to sensitive data is restricted to individuals with a demonstrated operational need.

**Separation of Duties:** Critical functions (e.g., migration approval + execution) must be separated between distinct roles.

### 3.2 Identity and Authentication

#### 3.2.1 Multi-Factor Authentication (MFA)

MFA is mandatory for:

- All production system access
- All administrative interfaces
- All CI/CD pipeline management
- All VPN connections
- All Salesforce org access

Accepted MFA methods (in order of preference):
1. FIDO2/WebAuthn hardware security keys (YubiKey 5 series minimum)
2. TOTP authenticator apps (Google Authenticator, Authy)
3. Push notifications (Okta Verify, Microsoft Authenticator)

SMS-based MFA is **PROHIBITED** due to SIM-swapping vulnerabilities.

#### 3.2.2 Password Policy

| Requirement | Minimum Standard |
|-------------|-----------------|
| Minimum length | 16 characters |
| Complexity | Uppercase + lowercase + digit + special char |
| Maximum age | 90 days (service accounts: 365 days) |
| History | Last 24 passwords cannot be reused |
| Lockout threshold | 5 failed attempts |
| Lockout duration | 30 minutes (progressive) |
| Brute-force protection | CAPTCHA after 3 failures |

#### 3.2.3 Service Account Management

- All service accounts must be documented in the Service Account Registry
- Service accounts must not be shared between services
- Service accounts must use API keys or certificates, not passwords where possible
- Workload Identity (OIDC) is preferred over static credentials
- Service account credentials must be rotated every 90 days
- Unused service accounts must be deactivated within 30 days

### 3.3 Role-Based Access Control (RBAC)

#### 3.3.1 Role Definitions

| Role | Description | Permissions |
|------|-------------|-------------|
| migration-admin | Full migration management | Create/read/update/delete all migration resources |
| migration-operator | Day-to-day operations | Read/update migration jobs, no delete |
| migration-viewer | Read-only monitoring | Read migration status, logs, metrics |
| api-service | Service-to-service | Scoped to specific API endpoints |
| audit-reader | Compliance/audit access | Read audit logs only |
| security-analyst | Security operations | Read security logs, manage alerts |

#### 3.3.2 Access Review

- Quarterly access reviews for all privileged accounts
- Annual access reviews for standard accounts
- Immediate review triggered by role change or termination
- Access reviews documented and signed by data owners

### 3.4 Privileged Access Management (PAM)

- All privileged access (admin/root/SA) must route through a PAM solution (CyberArk preferred)
- Just-In-Time (JIT) access for production environments (maximum 4-hour sessions)
- All privileged sessions must be recorded
- Break-glass accounts require dual approval and post-use review
- Privileged credentials stored exclusively in approved secrets management systems

### 3.5 Network Access Control

- Production networks segmented from development via separate VPCs/VNets
- All inter-service communication over mTLS
- External API access requires valid JWT + API key
- IP allowlisting enforced for administrative interfaces
- All outbound traffic proxied through egress controllers

---

## 4. Data Handling Policy

### 4.1 Data Classification

See companion document: `data_classification.md`

### 4.2 Data at Rest

| Classification | Encryption Requirement | Key Management |
|---------------|----------------------|----------------|
| Public | Optional | N/A |
| Internal | AES-256 | Platform-managed keys |
| Confidential | AES-256-GCM | Customer-managed keys (CMK) |
| Restricted | AES-256-GCM + HSM | Dedicated HSM, dual-control |

### 4.3 Data in Transit

- All data transmission uses TLS 1.3 minimum (TLS 1.2 conditionally allowed for legacy compatibility, TLS 1.0/1.1 **PROHIBITED**)
- Certificate validation is mandatory; self-signed certificates permitted only in development
- Perfect Forward Secrecy (PFS) required: ECDHE or DHE cipher suites
- HSTS headers required for all HTTPS endpoints (max-age minimum 31536000)
- Certificate pinning for mobile/native clients communicating with migration APIs

### 4.4 Data Retention and Disposal

| Data Type | Retention Period | Disposal Method |
|-----------|-----------------|-----------------|
| Migration logs | 7 years | Cryptographic erasure |
| Audit logs | 7 years | Cryptographic erasure |
| Temp transformation data | 30 days post-migration | DoD 5220.22-M wipe + verification |
| Error records | 1 year | Cryptographic erasure |
| Backup data | Per DR policy | Cryptographic erasure |

### 4.5 Data Loss Prevention (DLP)

- DLP policies enforced on all endpoints with access to Confidential/Restricted data
- Automated scanning of outbound data for PII, PCI, PHI patterns
- USB/removable media blocked on systems processing Restricted data
- Copy-paste of Restricted data from secure environments prohibited
- Screen capture disabled for sessions accessing Restricted data

---

## 5. Network Security

### 5.1 Perimeter Security

- Next-Generation Firewall (NGFW) with deep packet inspection
- Web Application Firewall (WAF) for all public-facing APIs (OWASP Core Rule Set)
- DDoS protection (Cloudflare/Azure DDoS Protection Standard)
- Intrusion Detection/Prevention System (IDS/IPS)

### 5.2 Network Segmentation

```
Internet
    |
  [WAF/CDN]
    |
  [DMZ]  — Public-facing APIs
    |
  [Application Tier] — Migration services, API gateway
    |
  [Data Tier] — Databases, message queues
    |
  [Management Tier] — Monitoring, PAM, secrets management
```

- Micro-segmentation via Kubernetes Network Policies
- Service mesh (Istio) enforces mTLS and authorization policies
- No lateral movement between migration phases without explicit policy

### 5.3 VPN and Remote Access

- All remote access requires VPN (WireGuard or Cisco AnyConnect)
- Split tunneling disabled for production access
- VPN sessions limited to 8 hours with re-authentication
- Device posture assessment required before VPN establishment

---

## 6. Application Security

### 6.1 Secure Development Lifecycle (SDL)

#### 6.1.1 Design Phase
- Threat modeling (STRIDE methodology) required for all new components
- Security requirements defined in user stories
- Architecture review by Security team for Confidential/Restricted data systems

#### 6.1.2 Development Phase
- Developers must complete annual secure coding training
- Pre-commit hooks enforce linting and basic security checks
- No hardcoded secrets (enforced via git-secrets, detect-secrets)
- Dependency pinning and automated vulnerability scanning (Dependabot, Snyk)

#### 6.1.3 Testing Phase
- SAST (Bandit, Semgrep) on every PR
- DAST (OWASP ZAP) on staging deployments
- Container scanning (Trivy) on every image build
- SCA (Software Composition Analysis) on every dependency update
- Penetration testing before production launch and annually thereafter

#### 6.1.4 Deployment Phase
- All deployments via CI/CD pipeline (no manual production deployments)
- Signed container images (cosign/Notary)
- GitOps deployment model (ArgoCD)
- Four-eyes principle for production deployments

### 6.2 API Security

- All APIs authenticated via OAuth 2.0 / OIDC
- Rate limiting enforced at API gateway (per-client and global)
- Input validation on all parameters
- Output encoding to prevent injection
- API versioning enforced; deprecated versions have 6-month sunset
- OpenAPI specification maintained and validated

### 6.3 Dependency Management

- All dependencies scanned weekly for CVEs
- Critical CVEs patched within 24 hours
- High CVEs patched within 7 days
- Medium CVEs patched within 30 days
- Low CVEs reviewed in next sprint cycle
- No dependencies with licenses incompatible with commercial use

---

## 7. Cryptography Standards

### 7.1 Approved Algorithms

| Use Case | Algorithm | Key Size | Notes |
|----------|-----------|----------|-------|
| Symmetric encryption | AES-GCM | 256-bit | Preferred |
| Asymmetric encryption | RSA-OAEP | 4096-bit | Legacy interop only |
| Asymmetric encryption | ECDSA/ECDH | P-384 | Preferred |
| Key derivation | PBKDF2-SHA256 | 256-bit, 600000 iter | Per NIST SP 800-132 |
| Key derivation | Argon2id | 256-bit | For passwords |
| Hashing | SHA-256/SHA-384 | — | Minimum SHA-256 |
| Digital signatures | Ed25519 | 256-bit | Preferred |
| TLS | TLS 1.3 | — | 1.2 min |
| Key exchange | ECDHE | P-256/P-384 | With PFS |

### 7.2 Prohibited Algorithms

The following are **STRICTLY PROHIBITED**:
- MD5, SHA-1 (except legacy compatibility with documented exception)
- DES, 3DES, RC4, RC2
- RSA with key size below 2048 bits
- Elliptic curves below P-256
- ECB mode for any block cipher
- Random number generation not from CSPRNG

### 7.3 Key Management

- All cryptographic keys managed in HashiCorp Vault (primary) or cloud HSM (backup)
- Key rotation schedules enforced automatically
- Key escrow procedures documented and tested annually
- Key ceremony procedures for master keys require quorum (3-of-5)

---

## 8. Incident Response

### 8.1 Incident Classification

| Severity | Definition | Response Time | Escalation |
|----------|-----------|---------------|------------|
| P0 - Critical | Data breach, ransomware, production down | 15 minutes | CISO, CEO, Legal |
| P1 - High | Unauthorized access detected, major service degradation | 1 hour | CISO, Engineering VP |
| P2 - Medium | Suspicious activity, minor data exposure | 4 hours | Security Manager |
| P3 - Low | Policy violation, minor vulnerability | 24 hours | Security Analyst |
| P4 - Informational | Security event, no immediate impact | 72 hours | Security Analyst |

### 8.2 Incident Response Process

#### Phase 1: Detection and Analysis (0-1 hour)
1. Automated detection via SIEM/monitoring
2. Security analyst triage and severity classification
3. Incident ticket created in incident management system
4. Initial notification sent to incident commander
5. Evidence preservation initiated (log snapshots, memory dumps)

#### Phase 2: Containment (1-4 hours for P0/P1)
1. Isolate affected systems (network quarantine)
2. Revoke compromised credentials immediately
3. Block suspicious IP addresses at perimeter
4. Preserve forensic evidence before remediation
5. Activate incident response team war room

#### Phase 3: Eradication (P0: 4-24 hours)
1. Identify and eliminate root cause
2. Remove malicious artifacts
3. Patch exploited vulnerabilities
4. Validate system integrity

#### Phase 4: Recovery
1. Restore from clean backups if needed
2. Progressive re-enablement with enhanced monitoring
3. Validate functionality before full restoration
4. Post-incident monitoring for 72 hours minimum

#### Phase 5: Post-Incident Review
1. Root cause analysis (RCA) within 5 business days
2. Lessons learned documented and distributed
3. Control improvements implemented within 30 days
4. Update threat models and runbooks

### 8.3 Breach Notification Requirements

| Regulation | Notification Requirement | Recipients |
|-----------|--------------------------|------------|
| FedRAMP | 1 hour | AO, US-CERT |
| GDPR | 72 hours | Supervisory Authority, if high risk: affected individuals |
| CCPA | Expedient | California AG, affected residents |
| State laws | Per state | Varies by state |
| Contractual | Per SLA | Customers as defined in MSA |

### 8.4 Contact Directory

| Role | Contact | Escalation |
|------|---------|-----------|
| Incident Commander | security-oncall@company.com | PagerDuty P0 runbook |
| CISO | ciso@company.com | Mobile: on file in PAM |
| Legal/Privacy | legal@company.com | Via general counsel |
| External IR firm | [IR Retainer] | Engagement letter on file |

---

## 9. Acceptable Use Policy

### 9.1 Permitted Uses

Migration system resources may be used only for:

- Authorized migration activities as defined in the project charter
- Testing and validation in designated non-production environments
- Security monitoring and investigation
- Approved training and certification activities

### 9.2 Prohibited Activities

The following are strictly prohibited:

- Accessing production data in development/staging environments
- Copying production data to local workstations
- Using migration infrastructure for personal computing
- Installing unauthorized software on migration systems
- Circumventing security controls or monitoring
- Sharing credentials with other individuals
- Accessing systems outside your role authorization
- Using public Wi-Fi to access migration systems without VPN
- Taking screenshots/photos of sensitive data
- Discussing sensitive migration data in unsecured channels (Slack DMs, personal email)

### 9.3 Monitoring Notice

**Employees are advised that activity on all migration systems is monitored and logged for security, compliance, and performance purposes. Use of these systems constitutes consent to monitoring.**

---

## 10. Third-Party and Vendor Management

### 10.1 Vendor Security Assessment

All vendors with access to migration data must:

1. Complete security questionnaire (SIG Lite or full SIG)
2. Provide evidence of SOC 2 Type II or ISO 27001 certification
3. Sign Data Processing Agreement (DPA)
4. Complete penetration test summary review
5. Undergo annual re-assessment

### 10.2 Salesforce as a Vendor

Salesforce holds FedRAMP Moderate authorization for Government Cloud. Trust status: trust.salesforce.com

- Monitor Salesforce trust page for incidents
- Subscribe to Salesforce security advisories
- Review Salesforce Government Cloud Shared Responsibility Matrix

### 10.3 Fourth-Party Risk

- Vendors must disclose all sub-processors handling migration data
- Critical sub-processors require same assessment as primary vendors
- Supply chain security review required for open-source dependencies

---

## 11. Business Continuity

### 11.1 Recovery Objectives

| Tier | Systems | RTO | RPO |
|------|---------|-----|-----|
| Tier 1 | Migration API, Salesforce sync | 1 hour | 15 minutes |
| Tier 2 | Monitoring, logging | 4 hours | 1 hour |
| Tier 3 | Dev/staging environments | 24 hours | 4 hours |

### 11.2 Backup Requirements

- Production databases: continuous replication + hourly snapshots
- Backup encryption: AES-256 with separate key management
- Backup testing: monthly restore test with documented results
- Offsite backups: geo-redundant (minimum 300 miles separation)
- Backup immutability: 30-day write-lock on backup storage

---

## 12. Compliance and Audit

### 12.1 Continuous Monitoring

- Security information and event management (SIEM): 24/7 monitoring
- Vulnerability scanning: Weekly infrastructure, daily code
- Configuration compliance: Daily CIS Benchmark checks
- Access review automation: Quarterly reports generated automatically
- Security metrics reported to leadership monthly

### 12.2 Audit Log Requirements

Audit logs must capture:

- Who: User ID, service account, IP address
- What: Action taken, resource accessed
- When: Timestamp (UTC, ISO 8601)
- Where: Source system, target system
- Result: Success/failure + error details
- Context: Correlation ID, session ID

Audit logs must be:
- Immutable (write-once storage)
- Encrypted at rest
- Retained for 7 years
- Available for review within 24 hours of request
- Sent to centralized SIEM in real-time

### 12.3 Internal Audit

- Quarterly internal security reviews
- Semi-annual penetration testing
- Annual comprehensive security assessment
- Findings tracked in GRC platform with SLA-based remediation

---

## 13. Roles and Responsibilities

| Role | Responsibilities |
|------|-----------------|
| CISO | Policy ownership, risk acceptance, audit oversight |
| Security Manager | Day-to-day security operations, incident command |
| Security Analyst | Monitoring, incident response, vulnerability management |
| Engineering Lead | Secure design, developer security training, SDL enforcement |
| DevSecOps Engineer | Security toolchain, CI/CD security gates, container security |
| Data Owner | Data classification, access approval, retention decisions |
| Migration Admin | Operational access management, PAM system |
| All Personnel | Policy compliance, incident reporting, security training |

---

## 14. Policy Exceptions

### 14.1 Exception Process

Security policy exceptions must:

1. Be submitted via the Exception Request Form (SEC-FORM-001)
2. Include: business justification, risk assessment, compensating controls
3. Be approved by: Security Manager + Data Owner (Confidential), CISO (Restricted)
4. Have a defined expiration date (maximum 90 days, renewable)
5. Be tracked in the exception registry
6. Be reviewed at expiration for continuation or remediation

### 14.2 Emergency Exceptions

Emergency exceptions (P0/P1 incidents):
- Verbal approval from CISO or delegate sufficient
- Written documentation within 24 hours
- Maximum 7-day duration

---

## 15. Enforcement and Violations

### 15.1 Violation Classification

| Severity | Examples | Consequence |
|----------|---------|-------------|
| Minor | Unintentional policy oversight | Training, verbal warning |
| Moderate | Sharing credentials, unauthorized data access | Written warning, additional training, access review |
| Major | Deliberate policy circumvention, data exfiltration | Termination, legal action |
| Critical | Malicious insider activity, sabotage | Immediate termination, law enforcement referral |

### 15.2 Reporting Violations

- Security violations: security@company.com
- Anonymous reporting: [Ethics Hotline Number]
- Retaliation against good-faith reporters is strictly prohibited

---

## Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0.0 | 2025-01-15 | Security Team | Initial release |
| 1.5.0 | 2025-06-01 | Security Team | Added FedRAMP controls |
| 2.0.0 | 2025-11-01 | CISO | Major revision: Zero Trust, SOC2 alignment |
| 2.1.0 | 2026-03-16 | Security Manager | Quarterly review updates |

**Approved by:** _______________________ Date: _______________
**CISO Signature**

**Approved by:** _______________________ Date: _______________
**VP Engineering**
