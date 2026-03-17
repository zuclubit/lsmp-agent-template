# SOX Compliance Controls for Enterprise Deployments

**Document Version:** 1.3.0
**Last Updated:** 2025-12-01
**Owner:** Compliance Officer / Internal Audit
**Reviewed By:** CFO, CISO, External Auditors (PwC — reference engagement SOX-2025-EXT)
**Classification:** CONFIDENTIAL — Do Not Distribute Externally Without Approval
**Applicable Regulation:** Sarbanes-Oxley Act of 2002 (Public Law 107-204)

---

## Table of Contents

1. [SOX Applicability to the Migration Platform](#1-sox-applicability-to-the-migration-platform)
2. [Change Management Controls (ITGC)](#2-change-management-controls-itgc)
3. [Access Controls and Segregation of Duties](#3-access-controls-and-segregation-of-duties)
4. [Audit Trail Requirements](#4-audit-trail-requirements)
5. [Data Integrity Controls](#5-data-integrity-controls)
6. [System Availability Requirements](#6-system-availability-requirements)
7. [Financial Data Migration Controls](#7-financial-data-migration-controls)
8. [Vendor Management (Third-Party Risk)](#8-vendor-management-third-party-risk)
9. [Evidence Collection for External Auditors](#9-evidence-collection-for-external-auditors)
10. [Control Testing Matrix](#10-control-testing-matrix)

---

## 1. SOX Applicability to the Migration Platform

### 1.1 When SOX Controls Apply

SOX Section 404 requires management of publicly traded companies (and their service providers) to assess and report on the effectiveness of internal control over financial reporting (ICFR). The migration platform is subject to SOX controls when:

1. **The migration client is a public company** (registered with the SEC) or a subsidiary/affiliate of a public company
2. **The migrated data includes financial information** that flows into or supports the financial statements:
   - Customer accounts (Accounts Receivable)
   - Revenue records (Opportunities, Orders, Contracts in Salesforce CPQ)
   - Billing and payment history
   - Accounts Payable / Vendor records
   - Asset registers
   - Employee compensation data (HR migrations feeding payroll)

3. **The platform processes data for a client's Salesforce instance** that is designated as a "financially significant system" in the client's SOX scope

**Key Determination:** If the Salesforce org being migrated to is listed in the client's SOX-scoped systems inventory, full SOX controls apply to the migration engagement.

### 1.2 Control Framework Reference

Controls in this document are mapped to:
- **COSO 2013 Framework** (Committee of Sponsoring Organizations)
- **COBIT 2019** (Control Objectives for Information and Related Technologies)
- **PCAOB AS 2201** (Auditing Standard for ICFR)

SOX Section 302: CEO/CFO certification of financial statement accuracy — requires reliable IT systems
SOX Section 404: Management's annual assessment of internal controls — includes IT General Controls (ITGCs)
SOX Section 906: Criminal liability for certifying false financial statements

### 1.3 IT General Controls (ITGCs) In Scope

Four categories of ITGCs apply to the migration platform:
1. **Change Management** — How code and configuration changes are made and reviewed
2. **Logical Access** — Who can access what systems, data, and functions
3. **Computer Operations** — How the system is monitored and how incidents are managed
4. **System Development / Acquisition** — How new systems or features are built and validated

---

## 2. Change Management Controls (ITGC)

### 2.1 Control Objective

All changes to the migration platform (code, configuration, infrastructure, transformation rules) that could affect financially significant data must be:
1. Formally requested and documented
2. Reviewed and approved by an independent party
3. Tested before deployment to production
4. Deployed in a controlled manner with rollback capability
5. Post-implementation verified

### 2.2 Change Classification

| Change Type | Examples | Approval Required | Testing Required | Lead Time |
|-------------|---------|-------------------|------------------|-----------|
| Emergency | P0 incident fix, security patch | CISO + CTO (retroactive CAB) | Minimal documented testing | Immediate |
| Standard | Routine dependency updates, minor bug fixes | Engineering Lead | Unit + integration tests | 2 business days |
| Normal | New features, transformation rule changes, API changes | Change Advisory Board (CAB) | Full test suite + UAT | 5 business days |
| Major | Architecture changes, new client onboarding, schema changes | CAB + External Audit notification | Full regression + parallel run | 10 business days |

### 2.3 Change Management Workflow

```
DEVELOPER submits Pull Request
  │
  ▼
AUTOMATED CHECKS (CI/CD pipeline — must all pass before human review):
  - Unit tests: 100% pass required
  - Integration tests: 100% pass required
  - SAST (Semgrep, Bandit): No HIGH/CRITICAL findings
  - DAST (OWASP ZAP): No HIGH/CRITICAL findings
  - OPA policy tests: 100% pass required
  - Transformation rule validation: Schema compatibility check
  │
  ▼
PEER REVIEW (required for all production changes):
  - 2 engineer approvals required (neither may be the author)
  - Security-labeled PRs require Security Engineer approval
  - Database migration PRs require DBA approval
  - Financial-data-scope PRs require SOX Officer review
  │
  ▼
STAGING DEPLOYMENT (automated via CD pipeline):
  - Deployed to staging environment identical to production
  - Smoke tests run automatically
  - For Normal/Major changes: 24-hour observation period in staging
  │
  ▼
CHANGE ADVISORY BOARD APPROVAL (Normal/Major changes only):
  - Weekly CAB meeting (Tuesdays 10:00 EST)
  - Members: Engineering Lead, Security Architect, Operations Lead, SOX Compliance Officer
  - Change presented with: business justification, risk assessment, test results, rollback plan
  - Approval recorded in ITSM tool (ServiceNow) with approver identities
  │
  ▼
PRODUCTION DEPLOYMENT (controlled window):
  - Deployments only during approved maintenance windows
  - Two-person rule: deployer + reviewer present for major changes
  - Automated deployment via GitOps (ArgoCD) — no manual kubectl apply
  - Deployment logged in ITSM change ticket
  │
  ▼
POST-IMPLEMENTATION REVIEW:
  - Production smoke tests run automatically post-deployment
  - Monitoring dashboards reviewed for 30 minutes post-deployment
  - Change ticket updated with: actual deployment time, issues encountered, validation results
```

### 2.4 Transformation Rule Change Controls

Transformation rules are governance artifacts that directly determine what financial data appears in Salesforce. Additional controls apply:

```
Rule change proposed (YAML PR submitted)
  │
  ▼
DATA STEWARD REVIEW: Business analyst confirms rule accurately reflects legacy-to-SF mapping
  │
  ▼
SOX OFFICER REVIEW: For rules touching financial fields (Amount, Revenue, Dates):
  "Does this rule produce the correct financial values in Salesforce?"
  │
  ▼
CLIENT APPROVAL: Client data steward or controller signs off on rule set
  (Documented in rule YAML as 'approved_by' and 'approved_date')
  │
  ▼
ENGINEERING REVIEW: 2 engineers confirm implementation matches approved specification
  │
  ▼
DEPLOYMENT: Rule versioned in git; effective_date set; audit trail captures version applied per record
```

### 2.5 Emergency Change Procedure

```bash
# Emergency change process (P0 incidents only):

# 1. Engineering Lead + CISO authorize verbally (recorded in incident ticket)
# 2. Change deployed with abbreviated review (1 engineer review minimum)
# 3. CAB retroactive approval within 48 hours (emergency meeting called if needed)
# 4. Post-emergency review: was the emergency warranted? Could it have been avoided?

# For SOX-scoped systems: external auditors are notified of emergency changes
# Emergency change rate is tracked as a KPI — high rate indicates control breakdown
```

### 2.6 Evidence Required for Audit

For each SOX-scoped change, the following evidence must be retained for 7 years:
- Git PR with all approval records (names, timestamps, comments)
- CI/CD pipeline run logs (showing all tests passed)
- ITSM change ticket with CAB approval record
- Deployment logs showing what was deployed, when, by whom
- Post-implementation test results
- Any rollback events and associated approvals

---

## 3. Access Controls and Segregation of Duties

### 3.1 Control Objective

Access to SOX-scoped systems and data must be:
- Limited to authorized individuals with a documented business need (Least Privilege)
- Segregated so that no single individual can initiate AND approve a financial transaction (SoD)
- Reviewed periodically to detect and remove stale access
- Revoked immediately upon role change or termination

### 3.2 Critical Segregation of Duties Requirements

The following combinations must NEVER be assigned to the same individual:

| Function A | Function B | Risk if Combined | Enforcement |
|-----------|-----------|-----------------|-------------|
| Can write transformation rules | Can deploy transformation rules to production | Could unilaterally alter how financial data is mapped | Separate GitHub teams with CODEOWNERS |
| Can initiate a migration job | Can approve the migration job | Could self-approve unauthorized data changes | Separate operator and approver roles in platform |
| Has Salesforce org admin access | Has migration platform admin access | Could cover tracks of unauthorized data modification | Separate identity providers for Salesforce and platform |
| Can write to Vault secrets | Can read production database credentials | Could self-escalate to full DB access | Vault policy review — write and read are separate policies |
| Can execute rollback | Can approve rollback | Could initiate and conceal unauthorized data deletion | Two-person rule enforced in platform API |

### 3.3 Role Definitions

| Role | Description | Permissions | SoD Restriction |
|------|-------------|-------------|-----------------|
| Migration Engineer | Executes migrations, monitors progress | Start/pause/resume migrations; view all migration data | Cannot approve own migration jobs |
| Migration Approver | Reviews and approves migration jobs | Approve/reject migration jobs; view reports | Cannot be the engineer who created the job |
| Transformation Rule Author | Creates transformation rules | Write rule YAML files; submit PRs | Cannot approve own rules; cannot deploy to production |
| Platform Admin | Manages platform configuration | All platform operations except financial-scoped Salesforce access | Cannot have Salesforce production admin access |
| SOX Compliance Officer | Monitors compliance controls | Read-only to all audit logs; can flag violations | Cannot write to any system |
| Security Auditor | External/Internal audit | Read-only to audit logs, change records, access logs | No write access to any system |
| Break-Glass Admin | Emergency access — last resort | All access | All break-glass usage logged, reviewed within 24h |

### 3.4 Access Review Schedule

| Review Type | Frequency | Scope | Reviewer | Documentation |
|-------------|-----------|-------|----------|---------------|
| User access review | Quarterly | All platform roles | Engineering Lead + CISO | Access review report in ITSM |
| Privileged access review | Monthly | Admin, break-glass | CISO | PRA report |
| Service account review | Quarterly | All service accounts, API keys | Security Architect | SA inventory in CMDB |
| Terminated employee review | Immediate | Access for departed employees | HR notification → automatic deprovisioning | Deprovisioning record |
| Contractor access review | At contract end | Contractor accounts | Account Manager | Contract closure checklist |

### 3.5 Access Provisioning and Deprovisioning

```bash
# Access Provisioning Request Process:
# 1. Manager submits access request in ITSM (ServiceNow ticket)
# 2. Access request includes: justification, required role, duration
# 3. Security reviews and approves/denies within 2 business days
# 4. Platform admin provisions access; updates CMDB
# 5. User receives access notification with security responsibilities reminder

# Deprovisioning (Automated via HR system integration):
# Trigger: HR system marks employee as terminated
# Action within 4 hours: Disable SSO account → cascades to all platform access
# Action within 24 hours: Revoke all API keys, Vault tokens, service account associations
# Action within 48 hours: Security review confirms all access removed; document in ITSM

# Verify deprovisioning:
vault token lookup {user_vault_token}  # Should return error
kubectl get rolebinding -A | grep {username}  # Should return nothing
kafka-acls.sh --list --principal User:{username}  # Should return nothing
```

### 3.6 Privileged Access Management

Break-glass (emergency privileged access) procedure:
```bash
# Break-glass access requires:
# 1. Two-person authorization (requestor + CISO or Engineering Lead)
# 2. Time-bounded access (max 4 hours, auto-revoked)
# 3. Session recording (all commands logged to immutable store)
# 4. Post-use review within 24 hours — was the access necessary?

# Enable break-glass (CISO or Engineering Lead must authorize):
vault token create \
    -policy=break-glass \
    -ttl=4h \
    -display-name="break-glass-${USER}-$(date +%Y%m%d-%H%M)" \
    -metadata="authorized_by=${AUTHORIZER}" \
    -metadata="reason=${REASON}" \
    -metadata="incident_ticket=${INCIDENT_ID}"

# All break-glass sessions appear in Vault audit log and trigger SIEM alert
```

---

## 4. Audit Trail Requirements

### 4.1 SOX Audit Trail Principles

SOX requires an audit trail that:
- Is immutable (cannot be modified or deleted)
- Has sufficient detail to reconstruct transactions
- Is protected from unauthorized modification
- Is retained for minimum 7 years (SOX Section 802 / Rule 13b2-2)
- Is available to external auditors upon request

### 4.2 Events That Must Be Logged

| Event Category | Specific Events | Retention | Immutability |
|----------------|----------------|-----------|-------------|
| Migration lifecycle | Job created, started, paused, resumed, completed, failed, rolled back | 7 years | Kafka + WORM S3 |
| Data transformation | Rule version applied to each batch; rejection decisions | 7 years | Kafka + WORM S3 |
| Data loading | Records loaded to Salesforce with before/after values for financial fields | 7 years | Kafka + WORM S3 |
| Access events | Login, logout, privilege escalation, API key usage | 7 years | SIEM |
| Configuration changes | Rule deployments, platform config changes | 7 years | Git history + ITSM |
| Authorization decisions | Every OPA allow/deny decision for financial operations | 7 years | OPA decision log + SIEM |
| Data erasure | GDPR erasure events (fact of erasure, not content) | 7 years | Immutable audit DB |
| Approval events | CAB approvals, migration job approvals, rollback approvals | 7 years | ITSM |

### 4.3 Audit Log Format (Financial Records)

Financial record audit events must include before and after values for key financial fields:

```json
{
  "audit_event_id": "ae-20251201-00001234",
  "event_type": "RECORD_LOADED_TO_SALESFORCE",
  "timestamp": "2025-12-01T22:14:37.234Z",
  "job_id": "mig-20251201-a3f8b2c1",
  "tenant_id": "ent-acme-corp",
  "rule_set_id": "oracle-ebs-to-sf-account-v3",
  "rule_set_version": "3.2.1",
  "source_system": "oracle-ebs-prod",
  "source_record_id": "12345678",
  "salesforce_object": "Account",
  "salesforce_record_id": "0015f000001XXXXX",
  "operation": "UPSERT_CREATE",
  "financial_fields_snapshot": {
    "AnnualRevenue": {
      "source_value": "5000000",
      "loaded_value": "5000000",
      "match": true
    },
    "NumberOfEmployees": {
      "source_value": "250",
      "loaded_value": "250",
      "match": true
    }
  },
  "checksum": "sha256:a3f8b2c1...",
  "operator_id": "data-engineer@acme.com",
  "kafka_offset": 1000042,
  "kafka_partition": 3
}
```

### 4.4 Audit Log Protection

```
Audit logs flow:
  Platform Events
       │
       ▼
  Kafka Audit Topic
  (retention: 7 years, replication: 3)
       │
       ├──▶ SIEM (Splunk Enterprise) — real-time monitoring
       │
       └──▶ S3 WORM Bucket (Object Lock: COMPLIANCE mode, 7 years)
               │
               └──▶ AWS Glacier (cost-optimized long-term storage after 1 year)

WORM protection configuration:
aws s3api put-object-lock-configuration \
    --bucket migration-audit-logs-worm \
    --object-lock-configuration \
    '{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"COMPLIANCE","Years":7}}}'

# COMPLIANCE mode: cannot be deleted or overridden by ANY user, including root
# Provides forensic integrity for regulatory and legal proceedings
```

---

## 5. Data Integrity Controls

### 5.1 Control Objective

Financial data migrated to Salesforce must be complete, accurate, and consistent with the source system records. No unauthorized modifications may occur during migration.

### 5.2 Record Count Reconciliation

After each migration run, a formal reconciliation is required:

```bash
# Automated reconciliation report (runs at migration completion)
migration-cli reconcile \
    --job-id {JOB_ID} \
    --tolerance 0.0  # Zero tolerance for financial records

# Manual verification (required for SOX-scoped migrations):
# 1. Source system record count (from source DB directly, not via migration platform)
# 2. Migration platform total extracted count
# 3. Migration platform total loaded count (= extracted - rejected)
# 4. Salesforce record count (queried directly)
# 5. Three-way reconciliation: Source = Loaded + Rejected; Salesforce = Loaded

# Document reconciliation in the migration completion checklist
# Signed by: Migration Engineer + SOX Compliance Officer
```

**Reconciliation Tolerance Policy:**
- Financial record count variance: 0% (zero tolerance — every financial record must be accounted for as either loaded, rejected with documented reason, or excluded with documented approval)
- Non-financial record count variance: 0.1% maximum
- Financial field value variance: 0% (no rounding or conversion errors permitted)

### 5.3 Financial Field Accuracy Validation

For financial fields (Amount, AnnualRevenue, Tax amounts, currency values), the post-load validation includes:

```python
# Validation query run against Salesforce after migration
def validate_financial_field_accuracy(
    job_id: str,
    sample_size: int = 1000  # 1000-record sample minimum for SOX
) -> ValidationResult:
    """
    Queries Salesforce for a sample of migrated financial records and
    compares against source system values. Zero tolerance for discrepancy.
    """
    sample = get_random_sample(job_id, sample_size, financial_records_only=True)

    discrepancies = []
    for record in sample:
        sf_value = salesforce_client.get_field(
            record.salesforce_id, "Amount"
        )
        source_value = source_db.get_field(
            record.source_id, "TRANSACTION_AMOUNT"
        )
        if Decimal(sf_value) != Decimal(source_value):
            discrepancies.append({
                "source_id": record.source_id,
                "sf_id": record.salesforce_id,
                "source_amount": source_value,
                "sf_amount": sf_value,
                "discrepancy": Decimal(sf_value) - Decimal(source_value)
            })

    if discrepancies:
        # SOX: ANY financial discrepancy is a finding
        raise FinancialDiscrepancyError(
            f"Financial field discrepancies found in {len(discrepancies)} records. "
            f"Migration cannot be certified until all discrepancies resolved."
        )
```

### 5.4 Completeness Check (All Records Accounted For)

Every source record must be accounted for in exactly one of:
1. **Loaded to Salesforce** — Record present with matching External ID
2. **Rejected with documented reason** — In the rejection report with reason code
3. **Excluded by approved filter** — Filter expression documented and approved in the migration job configuration

Unaccounted records = audit finding. Zero tolerance.

### 5.5 Hash Chain Integrity

For high-assurance migrations (SOX financial systems), a cryptographic hash chain is maintained:

```python
# Each batch of loaded records has a hash chain:
# BATCH_N_HASH = SHA256(BATCH_(N-1)_HASH + BATCH_N_RECORD_HASHES + BATCH_N_METADATA)

# This ensures that any tampering with historical batch records (e.g., inserting or
# removing records retroactively) is detectable by hash chain verification.

# Verify hash chain integrity:
migration-cli verify-hash-chain --job-id {JOB_ID}
# Expected: "Hash chain VALID — 8,947 batches verified"
# If "INVALID": potential evidence of tampering — escalate to CISO immediately
```

---

## 6. System Availability Requirements

### 6.1 SLA Requirements for SOX-Scoped Deployments

| Requirement | Standard | Government/Public Company | Measurement Period |
|-------------|---------|--------------------------|-------------------|
| System availability | 99.5% | 99.9% | Monthly |
| Planned downtime window | 4 hours/month max | 2 hours/month max | Monthly |
| RTO (Recovery Time Objective) | 4 hours | 2 hours | Per incident |
| RPO (Recovery Point Objective) | 15 minutes | 5 minutes | Per incident |
| Migration job completion SLA | Best effort | Contractual window + 10% | Per engagement |

### 6.2 High Availability Architecture

For SOX-scoped deployments, minimum HA configuration:

```
Kubernetes: 3-node control plane, minimum 6 worker nodes across 3 AZs
Kafka: 6 brokers, 3 KRaft controllers, replication factor 3, min ISR 2
Vault: 5-node Raft cluster, HSM-backed unsealing, auto-unseal configured
PostgreSQL: Primary + 2 replicas (1 synchronous), automated failover (Patroni)
Schema Registry: 3 instances behind load balancer
```

### 6.3 Disaster Recovery

```
DR Objective: RPO 5 minutes, RTO 2 hours

DR Mechanisms:
- Kafka MirrorMaker 2 replicating to DR region (secondary cluster)
- PostgreSQL streaming replication to DR region
- Vault Enterprise Replication (Performance Replication) to DR cluster
- Kubernetes cluster manifests in Git (GitOps) — DR cluster deployable in <30 min

DR Test Schedule: Quarterly full DR failover test
DR Test Evidence: Required for SOX audit — test report with RTO/RPO achieved vs. target

Most Recent DR Test: 2025-09-15 | Result: RTO 1h 47m (target: 2h) PASSED | RPO 3m (target: 5m) PASSED
Next DR Test: 2025-12-15
```

---

## 7. Financial Data Migration Controls

### 7.1 Additional Controls Specific to Financial Data

When migrating financial data (Opportunities, Orders, Revenue, Accounts Receivable), these additional controls apply beyond the standard migration controls:

**Pre-Migration:**
- [ ] Obtain written authorization from CFO or Finance Controller that migration may proceed
- [ ] Confirm financial period is closed (or migration window does not span period close)
- [ ] Obtain point-in-time source system export as rollback reference (not just Kafka replay)
- [ ] Confirm revenue recognition rules in Salesforce are configured before loading Opportunities
- [ ] Confirm currency exchange rates in Salesforce match source system rates for multi-currency migrations
- [ ] Obtain external auditor concurrence if migration occurs within 60 days of fiscal year end

**During Migration:**
- [ ] Financial records are not loaded during active quarter close (T-5 business days to quarter end)
- [ ] Loading throughput limited during business hours for financial systems (avoid overwhelming audit trail writers)
- [ ] Real-time monitoring by Finance team representative during migration window
- [ ] Immediate stop authority: CFO or Finance Controller can halt migration via documented process

**Post-Migration:**
- [ ] Three-way reconciliation signed by: Migration Engineer + Finance Controller + SOX Compliance Officer
- [ ] Variance analysis: any difference between source and loaded amounts requires documented explanation
- [ ] Finance team performs substantive testing on random sample (minimum 5% of Opportunity amounts)
- [ ] External auditor walkthrough of migration controls (for annual SOX audit)
- [ ] Issue certificate of migration completion filed in audit evidence repository

### 7.2 Revenue Recognition Specific Controls

If migrating Opportunity or revenue data that feeds revenue recognition:
- Document the accounting periods that migrated Opportunities fall into
- Confirm Salesforce Revenue Cloud / CPQ configurations are in place before loading
- Obtain Finance sign-off that migrated Opportunity stages/amounts are correct
- Do NOT load Opportunities with `StageName = 'Closed Won'` during active quarter without CFO approval (they may affect recognized revenue)

---

## 8. Vendor Management (Third-Party Risk)

### 8.1 SOX-Relevant Third Parties

| Vendor | Service | SOX Relevance | SOC 2 Report | Reviewed |
|--------|---------|---------------|-------------|---------|
| Salesforce Inc. | Target CRM platform | High — financially significant system | SOC 2 Type II | Quarterly |
| Confluent / Apache Kafka | Event streaming | Medium — audit trail infrastructure | SOC 2 Type II | Annual |
| HashiCorp / HCP Vault | Secrets management | Medium — access control dependency | SOC 2 Type II | Annual |
| Anthropic PBC | AI API (PII-masked only) | Low — not in financial data path | No SOC 2 available; contractual controls only | Annual |
| Amazon Web Services | Cloud infrastructure | High — hosting financially significant systems | SOC 1 + SOC 2 Type II | Annual |

### 8.2 Vendor SOC Report Review Process

1. Obtain vendor's most recent SOC 2 Type II report (or SOC 1 if applicable)
2. Review "bridge letter" from vendor for period not covered by SOC report
3. Identify any qualified opinions, exceptions, or subservice organizations
4. Map vendor control exceptions to our own compensating controls
5. Document review in Vendor Management System
6. Flag significant exceptions to external auditors

**Salesforce SOC 1 Report:** Critical for SOX. Must be obtained and reviewed annually. Key sections:
- Change management controls (does Salesforce have adequate controls on their platform changes?)
- Access management (are Salesforce platform admin access controls adequate?)
- Data backup and recovery (can we recover from Salesforce data loss?)

---

## 9. Evidence Collection for External Auditors

### 9.1 Standard Audit Evidence Package

For each SOX-scoped migration engagement, prepare the following evidence package for external auditors:

**Change Management Evidence:**
- [ ] Git history of transformation rules with reviewer names and timestamps
- [ ] CI/CD pipeline run logs for each production deployment
- [ ] CAB meeting minutes with approval records
- [ ] Change tickets for all production changes during the engagement period

**Access Control Evidence:**
- [ ] User access list with role assignments (point-in-time export)
- [ ] Quarterly access review reports (with any removals documented)
- [ ] SoD conflict report (confirmation that no conflicts existed)
- [ ] Service account inventory
- [ ] Privileged access usage log (all break-glass usage)

**Audit Trail Evidence:**
- [ ] Kafka audit log excerpt (sample of audit events)
- [ ] Audit log integrity verification (hash chain verification output)
- [ ] SIEM query results for the migration period
- [ ] Evidence that audit logs are in WORM storage (S3 Object Lock configuration screenshot)

**Data Integrity Evidence:**
- [ ] Three-way reconciliation report (signed)
- [ ] Financial field accuracy validation report
- [ ] Migration completion certificate (signed by Finance Controller and SOX Officer)
- [ ] DLQ resolution log (all rejected records documented with disposition)

### 9.2 Auditor Access Procedure

External auditors require read-only access to audit evidence. Provisioning:

```bash
# Create time-bounded read-only auditor role
vault policy write external-auditor policies/external-auditor-readonly.hcl

# Grant 30-day time-bounded access for audit engagement
vault token create \
    -policy=external-auditor \
    -ttl=720h \
    -display-name="audit-engagement-2025-pwc" \
    -metadata="purpose=annual_sox_audit" \
    -metadata="auditor_firm=PwC" \
    -metadata="engagement_id=SOX-2025-EXT"

# policies/external-auditor-readonly.hcl
# path "audit-evidence/*" { capabilities = ["read", "list"] }
# path "migration-reports/*" { capabilities = ["read", "list"] }
# All paths: deny by default; explicitly allow only evidence paths
```

**Auditor Access Log:** All auditor access to the platform is logged separately and included in the audit evidence package as demonstration that audit access itself was controlled.

---

## 10. Control Testing Matrix

External auditors and internal audit use this matrix to determine which controls to test and how:

| Control ID | Control Description | Test Type | Test Frequency | Last Test Result | Owner |
|------------|---------------------|-----------|---------------|-----------------|-------|
| CM-01 | All production changes require 2-engineer PR approval | Inspection | Quarterly | PASSED (2025-09-15) | Engineering Lead |
| CM-02 | CAB approval documented for Normal/Major changes | Inspection | Quarterly | PASSED (2025-09-15) | Compliance Officer |
| CM-03 | Transformation rule changes require SOX Officer review | Walkthrough | Annual | PASSED (2025-10-01) | SOX Officer |
| CM-04 | Emergency changes have retroactive CAB approval | Inquiry + inspection | Quarterly | PASSED - 2 emergency changes in Q3, both retroactively approved | Engineering Lead |
| AC-01 | SoD matrix enforced — no individual has conflicting roles | Inspection | Quarterly | PASSED (2025-09-30) | CISO |
| AC-02 | Access reviewed quarterly | Inspection | Quarterly | PASSED (2025-09-30) | CISO |
| AC-03 | Terminated employee access revoked within 4 hours | Test (sample) | Annual | PASSED — 3 terminations tested, avg 1.8h (2025-10-15) | HR + Security |
| AC-04 | Break-glass access logged and reviewed within 24h | Inspection | Quarterly | PASSED (2025-09-15) | CISO |
| AT-01 | Audit logs are immutable (WORM protection) | Technical test | Annual | PASSED — S3 Object Lock COMPLIANCE mode verified (2025-09-01) | Security Architect |
| AT-02 | Audit log hash chain is valid | Technical test | Quarterly | PASSED (2025-09-15) | Security Architect |
| AT-03 | Audit logs retained 7 years | Technical test | Annual | PASSED — Lifecycle policy verified (2025-09-01) | Operations |
| DI-01 | Financial record count reconciliation performed post-migration | Inspection | Per migration | PASSED — Last migration 2025-11-30 (2025-12-01) | Migration Engineer + Finance |
| DI-02 | Financial field accuracy validated (zero tolerance) | Walkthrough + reperformance | Annual | PASSED (2025-10-15) | Migration Engineer |
| DI-03 | All rejected records have documented disposition | Inspection | Per migration | PASSED — Last migration 2025-11-30 | Migration Engineer |
| AV-01 | DR test conducted quarterly | Inspection | Annual | PASSED — Last test 2025-09-15 (RTO: 1h47m vs 2h target) | Operations |
| AV-02 | System availability meets SLA | Performance metrics review | Quarterly | PASSED — 99.92% availability Q3 2025 | SRE |
| TP-01 | Vendor SOC 2 reports reviewed annually | Inspection | Annual | PASSED — All 4 vendors reviewed (2025-09-01) | Compliance Officer |

**Control Test Legend:**
- **Inspection:** Auditor examines evidence (logs, reports, tickets) to confirm control operated as designed
- **Inquiry:** Auditor interviews responsible personnel
- **Walkthrough:** Auditor traces a transaction from initiation through recording
- **Reperformance:** Auditor independently executes the control to verify it works correctly
- **Technical test:** Auditor uses tools/commands to directly verify a technical control

---

*Document Version: 1.3.0 | Reviewed: 2025-12-01 | Next Review: 2026-03-01 (pre-annual SOX audit preparation)*
*Owner: Compliance Officer | External Auditor Contact: SOX-2025-EXT@pwc.com*
*Audit Evidence Repository: https://audit.internal/sox/2025 (access controlled — auditors only)*
