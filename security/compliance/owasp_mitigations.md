# OWASP Top 10 Mitigation Mapping
## Legacy to Salesforce Migration Platform

**Document ID:** SEC-COMP-002
**OWASP Reference:** OWASP Top 10 2021
**Version:** 1.0.0
**Last Updated:** 2026-03-16

---

## A01:2021 — Broken Access Control

**Risk Level for this project:** HIGH (handles Confidential/Restricted data)

### Threats
- Unauthorized users accessing migration jobs or data
- Horizontal privilege escalation (accessing other users' migration data)
- Vertical privilege escalation (viewer gaining admin access)
- IDOR (Insecure Direct Object Reference) on migration job IDs
- Forced browsing to undiscovered admin endpoints
- JWT manipulation to elevate role claims

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| RBAC enforcement | rbac_config.py — @require_permission decorator on every endpoint | DONE |
| JWT validation | RS256 algorithm; public key verification; no 'none' algorithm | DONE |
| Resource ownership validation | Migration jobs scoped to tenant/org; cross-tenant access impossible | DONE |
| URL path authorization | API gateway denies unknown paths; no directory browsing | DONE |
| Rate limiting | 100 req/min per user, 1000 req/min per IP at API gateway | DONE |
| Audit logging | All access decisions logged with actor, resource, outcome | DONE |
| CORS policy | Allowlist of approved origins; no wildcard origin | DONE |
| Semgrep rules | semgrep_rules.yaml: missing-auth-decorator, jwt-none-algorithm | DONE |

### Testing
- DAST (OWASP ZAP) tests all endpoints for auth bypass
- Manual pen test includes IDOR testing on all resource types
- Unit tests in tests/unit/test_rbac.py cover all role/permission combinations

---

## A02:2021 — Cryptographic Failures

**Risk Level for this project:** CRITICAL (processes SSN, financial data, health records)

### Threats
- PII transmitted in cleartext
- Weak encryption algorithms (DES, 3DES, RC4)
- Hardcoded cryptographic keys
- Improper key management
- TLS misconfiguration (SSLv3, TLS 1.0/1.1)
- Sensitive data in logs or error messages

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| AES-256-GCM for data at rest | encryption_service.py — AESGCMCipher | DONE |
| Field-level encryption for PII | encryption_service.py — encrypt_field() for SSN, financial data | DONE |
| Envelope encryption | DEK wrapped by master key via HSM | DONE |
| TLS 1.3 minimum | All listeners configured; TLS 1.0/1.1 disabled | DONE |
| HSTS enforcement | max-age=31536000; includeSubDomains; preload | DONE |
| Certificate management | cert-manager with Let's Encrypt (public) / internal CA | DONE |
| Vault for key management | vault_config.hcl — Transit engine, auto-unseal with Azure HSM | DONE |
| Key rotation | Data keys rotate every 90 days; automated | DONE |
| Banned algorithms | Semgrep: weak-hash-for-security, aes-ecb-mode rules | DONE |
| Secrets manager | secrets_manager.py — no hardcoded credentials | DONE |
| PBKDF2 for password KDF | 600,000 iterations per NIST SP 800-132 | DONE |

---

## A03:2021 — Injection

**Risk Level for this project:** HIGH (constructs SOQL queries and SQL queries)

### Threats
- SQL injection in legacy database queries
- SOQL injection in Salesforce API calls
- Command injection in migration orchestration
- Log injection (CRLF injection into log files)
- Template injection in transformation templates

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| Parameterized SQL queries | All database queries use SQLAlchemy ORM or parameterized queries | DONE |
| SOQL parameterization | Salesforce queries use Bulk API with typed parameters; no string concatenation | DONE |
| Input validation | Pydantic models on all API inputs; strict type validation | DONE |
| Output encoding | All API responses use JSON serialization; no raw string interpolation | DONE |
| Command injection prevention | subprocess calls use shell=False with explicit argument lists | DONE |
| Semgrep rules | sql-string-concatenation, salesforce-soql-injection, command-injection-via-shell | DONE |
| Salesforce ID validation | Regex validation of 15/18-char SF IDs before use | DONE |
| Log sanitization | sanitize_for_audit() in audit_logger.py strips injection chars | DONE |

---

## A04:2021 — Insecure Design

**Risk Level for this project:** MEDIUM

### Threats
- Missing threat model for new migration phases
- Over-permissive data access patterns
- Lack of rate limiting on sensitive operations
- No separation between environments

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| Threat modeling | STRIDE analysis for each migration phase before development | DONE |
| Secure design patterns | Zero-trust architecture; defense-in-depth | DONE |
| Environment separation | Separate VPCs, separate secrets, separate Salesforce orgs per env | DONE |
| Rate limiting | Per-operation rate limits (bulk migration: 1 job/user/5min) | DONE |
| Data minimization | Only required fields extracted; classification-based access | DONE |
| Approval workflow | Production migrations require explicit approval (migration:jobs:approve) | DONE |
| Security design review | Required for all components handling Confidential+ data | DONE |

---

## A05:2021 — Security Misconfiguration

**Risk Level for this project:** HIGH (complex Kubernetes/cloud infrastructure)

### Threats
- Default credentials not changed
- Unnecessary features enabled
- Missing security headers
- Verbose error messages exposing stack traces
- Insecure Kubernetes configurations
- Open cloud storage buckets

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| Security headers | X-Frame-Options, X-Content-Type-Options, CSP, HSTS on all responses | DONE |
| Error handling | Generic error messages to clients; details in structured logs only | DONE |
| CIS K8s Benchmark | Trivy scans K8s manifests against CIS benchmark | DONE |
| Pod security standards | Restricted Pod Security Standard enforced in migration namespace | DONE |
| No default credentials | All secrets generated; no defaults; enforced via Bandit | DONE |
| Minimal container images | Distroless base images; no shell, no package manager | DONE |
| Cloud storage ACLs | S3/Azure Storage — private by default; public-read requires approval | DONE |
| UI disabled in Vault | vault_config.hcl: ui=false | DONE |
| Trivy IaC scanning | Detects K8s/Terraform misconfigurations in CI | DONE |

---

## A06:2021 — Vulnerable and Outdated Components

**Risk Level for this project:** MEDIUM

### Threats
- Dependencies with known CVEs
- Unpatched container base images
- Outdated Salesforce API versions
- End-of-life Python runtimes

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| Dependency scanning | Dependabot + Snyk: weekly scans, auto-PRs for patches | DONE |
| Container scanning | Trivy scans every image build; fails on HIGH/CRITICAL | DONE |
| SCA in CI | pip-audit run in CI pipeline | DONE |
| Pinned dependencies | All dependencies pinned in requirements.txt with hashes | DONE |
| Base image updates | Automated PRs for base image updates weekly | DONE |
| EOL tracking | Python version tracked; upgrade before EOL -6 months | DONE |
| CVE SLA | Critical: 24h, High: 7d, Medium: 30d (see security_policy.md 6.3) | DONE |

---

## A07:2021 — Identification and Authentication Failures

**Risk Level for this project:** HIGH

### Threats
- Brute-force attacks on admin accounts
- Credential stuffing
- Session fixation
- Weak session tokens
- JWT algorithm confusion attacks

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| MFA enforcement | FIDO2/TOTP required for admin/operator roles | DONE |
| Account lockout | 5 failures → 30-min lockout; progressive | DONE |
| Secure session management | JWT with RS256; short expiry (60 min admin, 8h standard) | DONE |
| Secure token generation | secrets module for all token generation; no random.random() | DONE |
| JWT algorithm whitelist | Only RS256/ES256 accepted; 'none' rejected | DONE |
| Credential breach monitoring | Okta integration with HaveIBeenPwned for password checks | DONE |
| Session invalidation | Token revocation on logout/role change | DONE |
| No SMS MFA | FIDO2 and TOTP only; SMS prohibited (SIM swap risk) | DONE |

---

## A08:2021 — Software and Data Integrity Failures

**Risk Level for this project:** HIGH (CI/CD pipeline, migration data)

### Threats
- Compromised CI/CD pipeline (supply chain attack)
- Malicious package substitution
- Unsigned container images deployed
- Migration data tampered in transit

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| Signed container images | Cosign image signing; admission webhook rejects unsigned | DONE |
| Dependency hash verification | pip install --require-hashes; locked requirements.txt | DONE |
| CI/CD pipeline security | GitHub Actions with pinned action versions (SHA hash) | DONE |
| GitOps deployment | ArgoCD with Git as single source of truth; no manual deploys | DONE |
| Audit log tamper-evidence | HMAC chain in TamperEvidentChain (audit_logger.py) | DONE |
| SLSA Level 2 | Provenance generated for all releases | IN PROGRESS |
| Migration data checksums | SHA-256 hash of each batch verified at load stage | DONE |
| SBOM generation | Trivy generates CycloneDX SBOM on every release | DONE |

---

## A09:2021 — Security Logging and Monitoring Failures

**Risk Level for this project:** HIGH (incident detection and compliance)

### Threats
- Insufficient logging of security events
- PII in log files
- Log injection attacks
- No alerting on anomalies
- Logs not retained long enough for investigations

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| Centralized audit logging | audit_logger.py with multiple sinks (file, Elasticsearch) | DONE |
| Structured JSON logs | All logs are structured JSON with consistent schema | DONE |
| PII sanitization | sanitize_for_audit() in audit_logger.py; semgrep rule pii-in-log | DONE |
| Log retention | 7 years per data classification policy | DONE |
| Real-time SIEM | All logs forwarded to Splunk/ELK; 24/7 monitoring | DONE |
| Alert rules | Prometheus alertmanager rules for all critical events | DONE |
| Failed auth alerting | 5+ failures from same IP → PagerDuty alert | DONE |
| Anomaly detection | UEBA in SIEM flags unusual access patterns | DONE |
| Log immutability | WORM storage; Elasticsearch ILM with write-lock | DONE |

---

## A10:2021 — Server-Side Request Forgery (SSRF)

**Risk Level for this project:** MEDIUM (API calls to Salesforce and external services)

### Threats
- SSRF via migration webhook URLs
- SSRF via document upload URLs
- Metadata service access via SSRF (cloud credentials)
- Internal service enumeration

### Mitigations Implemented

| Control | Implementation | Status |
|---------|---------------|--------|
| URL allowlist | Only approved Salesforce endpoints and configured URLs | DONE |
| URL validation | Validate scheme (https only), hostname against allowlist | DONE |
| Metadata service blocking | IMDS blocked at network level (NACLs, NSGs) | DONE |
| Egress filtering | All outbound traffic via egress proxy; unapproved destinations blocked | DONE |
| Redirect following disabled | httpx/requests: follow_redirects=False for webhook calls | DONE |
| IP range blocking | Private RFC1918 ranges rejected for external URL parameters | DONE |

---

## Summary

| OWASP Category | Risk Level | Mitigations | Status |
|---------------|-----------|-------------|--------|
| A01 Broken Access Control | HIGH | 8 controls | FULLY MITIGATED |
| A02 Cryptographic Failures | CRITICAL | 11 controls | FULLY MITIGATED |
| A03 Injection | HIGH | 8 controls | FULLY MITIGATED |
| A04 Insecure Design | MEDIUM | 7 controls | FULLY MITIGATED |
| A05 Security Misconfiguration | HIGH | 9 controls | FULLY MITIGATED |
| A06 Vulnerable Components | MEDIUM | 7 controls | FULLY MITIGATED |
| A07 Auth Failures | HIGH | 8 controls | FULLY MITIGATED |
| A08 Data Integrity Failures | HIGH | 8 controls | MOSTLY MITIGATED (SLSA in progress) |
| A09 Logging & Monitoring | HIGH | 9 controls | FULLY MITIGATED |
| A10 SSRF | MEDIUM | 6 controls | FULLY MITIGATED |

**Overall OWASP Coverage:** 9/10 categories fully mitigated, 1/10 partially (SLSA Level 2 in progress)
