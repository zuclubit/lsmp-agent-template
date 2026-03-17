# GDPR Compliance Controls

**Document Version:** 2.1.0
**Last Updated:** 2025-12-01
**Owner:** Data Protection Officer (DPO) / Compliance Team
**Reviewed By:** Legal Counsel, CISO, Platform Engineering Lead
**Classification:** CONFIDENTIAL — Legal Privilege May Apply

---

## Table of Contents

1. [Scope and Applicability](#1-scope-and-applicability)
2. [Lawful Basis for Processing](#2-lawful-basis-for-processing)
3. [Data Subject Rights Implementation](#3-data-subject-rights-implementation)
4. [Data Retention Policies](#4-data-retention-policies)
5. [Right to Erasure in Salesforce](#5-right-to-erasure-in-salesforce)
6. [International Data Transfers (EU to US)](#6-international-data-transfers-eu-to-us)
7. [Data Processing Agreement Template](#7-data-processing-agreement-template)
8. [Privacy Impact Assessment Checklist](#8-privacy-impact-assessment-checklist)
9. [Data Breach Notification Procedures](#9-data-breach-notification-procedures)
10. [Technical and Organisational Measures (TOMs)](#10-technical-and-organisational-measures-toms)

---

## 1. Scope and Applicability

**Document Type:** Compliance Control Reference
**Regulation:** EU General Data Protection Regulation (GDPR) 2016/679
**Applicable to:** All data migration operations involving EU personal data
**Owner:** Data Protection Officer (DPO)
**Last Updated:** 2025-03-16
**Review Cycle:** Annual

---

## 1. Scope

This document covers GDPR compliance requirements for the Legacy-to-Salesforce
migration platform when processing personal data of EU data subjects, including:

- EU citizens' contact and account data in the legacy CRM
- Data transferred to Salesforce (US-based infrastructure)
- Intermediate processing during ETL pipeline execution
- Audit logs containing personal data references

---

## 2. Lawful Basis for Processing

All data processing must have a documented lawful basis under GDPR Article 6.

| Data Category | Lawful Basis | Documentation |
|---------------|-------------|---------------|
| Customer account data (B2B) | Legitimate interest (Art. 6(1)(f)) | LIA-2025-001 |
| Contact data (B2B employees) | Legitimate interest | LIA-2025-001 |
| Individual consumer data (B2C) | Contract performance (Art. 6(1)(b)) | Contract templates |
| Government sector data | Legal obligation (Art. 6(1)(c)) | Legal mandate references |

**Configuration (feature_flags.yaml):**
```yaml
gdpr_lawful_basis_check:
  enabled: true
  require_basis_per_record_type: true
  audit_processing_purpose: true
```

---

## 3. Data Subject Rights Implementation

### 3.1 Right of Access (Article 15)

Data subjects can request all personal data held. The migration platform must:

```python
# In application/use_cases/handle_data_subject_request.py

class DataSubjectAccessRequest:
    """
    Handles DSAR (Data Subject Access Request) across legacy and Salesforce.

    Timeline: Must respond within 30 days.
    """

    def execute(self, subject_email: str) -> DataSubjectReport:
        # 1. Query legacy system for all records linked to email
        legacy_records = self.legacy_repo.find_by_email(subject_email)

        # 2. Query Salesforce for all linked records
        sf_records = self.sf_client.query(
            f"SELECT Id, Name, Email__c, CreatedDate FROM Contact "
            f"WHERE Email = '{subject_email}' OR Legacy_Email__c = '{subject_email}'"
        )

        # 3. Include migration audit trail
        audit_records = self.audit_logger.find_by_subject(subject_email)

        # 4. Compile and return (never include passwords, internal keys)
        return DataSubjectReport(legacy=legacy_records, salesforce=sf_records, audit=audit_records)
```

### 3.2 Right to Erasure (Article 17) — "Right to be Forgotten"

```python
# Erasure must propagate to BOTH legacy system and Salesforce

class EraseDataSubjectUseCase:

    ERASURE_MARKER = "[GDPR_ERASED]"

    def execute(self, subject_id: str, legal_hold_check: bool = True) -> ErasureResult:
        # Check for legal hold (tax, regulatory) before erasing
        if legal_hold_check:
            if self.legal_hold_service.is_on_hold(subject_id):
                raise LegalHoldActiveError(
                    f"Subject {subject_id} is under legal hold — cannot erase"
                )

        # Pseudonymize rather than delete where deletion would corrupt referential integrity
        self.legacy_repo.pseudonymize(subject_id, marker=self.ERASURE_MARKER)

        # Delete from Salesforce (or anonymize if referenced by financial records)
        self.sf_client.anonymize_contact(subject_id)

        # Log the erasure (without PII)
        self.audit_logger.log_erasure(subject_id=subject_id, timestamp=datetime.utcnow())

        return ErasureResult(success=True, systems_cleared=["legacy-crm", "salesforce"])
```

### 3.3 Right to Portability (Article 20)

```python
# Export personal data in machine-readable format (JSON/CSV)
def export_subject_data(subject_email: str, format: str = "json") -> bytes:
    data = self.dsar_service.compile_subject_data(subject_email)
    if format == "json":
        return json.dumps(data, indent=2, default=str).encode()
    elif format == "csv":
        return generate_csv(data)
```

### 3.4 Right to Rectification (Article 16)

All data corrections in legacy system must be propagated to Salesforce during
active migration and post-migration sync. Implemented via:
- Delta sync job running every 24 hours
- Salesforce `Legacy_Last_Updated__c` field tracked for change detection

---

## 4. Data Transfer Mechanisms (EU → US)

Salesforce is a US company. EU personal data transfer to Salesforce requires a lawful
transfer mechanism under GDPR Chapter V.

**Primary Mechanism:** Standard Contractual Clauses (SCCs) — EU Commission Decision 2021/914
- Salesforce DPA (Data Processing Addendum): signed 2024-01-15
- SCC Module 1 (Controller → Processor): applicable for our use case
- Salesforce Sub-processors list: reviewed quarterly

**Salesforce-Specific Configuration:**
```yaml
# In Salesforce org settings (requires Salesforce Shield or Data Residency Option)
data_residency:
  # For EU-based customers — request EU data center
  preferred_region: eu-central-1
  pii_fields_encrypted_at_rest: true  # Requires Salesforce Shield
```

**Risk Assessment:**
- Schrems II compliance verified via Salesforce's Transfer Impact Assessment (TIA)
- Salesforce is certified under EU-US Data Privacy Framework (DPF): 2023-present

---

## 5. Data Retention Policy

| Data Type | Retention Period | System | Basis |
|-----------|-----------------|--------|-------|
| Active customer records | Duration of contract + 7 years | Salesforce | Tax/legal obligation |
| Inactive/churned customers | 3 years post-churn | Salesforce (then archive) | Legitimate interest |
| Migration audit logs | 7 years | Secure archive | Legal obligation |
| Error/quarantine records | 90 days | Migration DB | Operational necessity |
| Temporary ETL processing data | 24 hours post-migration | Migration workers (in-memory) | Minimal retention |

**Automated Retention Enforcement:**
```yaml
# config/feature_flags.yaml
gdpr_automated_retention:
  enabled: true
  run_schedule: "0 2 * * 0"  # Weekly at 2AM Sunday
  dry_run_first: true
  notify_dpo_on_deletion: true
```

---

## 6. Privacy by Design Controls

### 6.1 Data Minimization (Article 5(1)(c))

Only fields required for Salesforce business processes are migrated.
Excluded legacy fields:

```python
# migration/data_transformations/account_transformer.py

# These legacy fields are NOT migrated — they contain PII not needed in SF
EXCLUDED_FIELDS = {
    "internal_credit_score",  # Not used in SF
    "legacy_user_password_hash",  # Never migrated
    "employee_personal_phone",  # Not a business number
    "notes_with_health_data",  # Sensitive — manual review required
}
```

### 6.2 PII Field Encryption

All PII fields are encrypted at rest using AES-256-GCM:

```python
# security/encryption/encryption_service.py

PII_FIELDS_ALWAYS_ENCRYPTED = {
    "email", "phone", "billing_street", "ssn",
    "date_of_birth", "bank_account_number"
}
```

### 6.3 Pseudonymization in Non-Production

```yaml
# CI/CD: data must be pseudonymized before use in non-production environments
pseudonymization:
  enabled_for_envs: [dev, staging, test]
  strategy: tokenization  # Replace PII with reversible tokens
  token_vault: hashicorp-vault
  seed_data_only: use_synthetic_data_in_ci
```

---

## 7. Data Protection Impact Assessment (DPIA)

A DPIA was conducted per GDPR Article 35 (required because this migration involves:
- Large-scale processing of personal data
- International data transfer
- New technology (AI agents processing personal data))

**DPIA Reference:** DPIA-2025-MIG-001
**Outcome:** High risk — mitigated to acceptable level with controls documented here
**DPO Sign-off:** Required before production migration of > 10,000 EU data subjects

### AI Agent Data Privacy Controls

The migration-agent and data-validation-agent process personal data. Controls:

```yaml
# agents/migration-agent/prompts/system_prompt.md — data privacy clause
# The AI agent must:
# - Never include raw PII in reasoning traces sent to external LLM API
# - Use pseudonymized record IDs in all agent outputs
# - Not cache or store personal data beyond the current task
# - Log only aggregate statistics, never individual records
```

---

## 8. Breach Response Procedure

Under GDPR Article 33, data breaches must be reported to the supervisory authority
within **72 hours** of becoming aware.

### Breach Detection
```bash
# alert_rules.yaml — unauthorized access detection
- alert: PotentialDataBreach
  expr: security_unauthorized_access_attempts_total > 10
  for: 5m
  labels:
    severity: critical
    gdpr_relevant: "true"
```

### Breach Response Steps
1. **Contain** — Immediately revoke compromised credentials / isolate affected system
2. **Assess** — Determine what personal data was accessed, how many subjects
3. **Notify DPO** — Within 1 hour of discovery
4. **Notify Supervisory Authority** — Within 72 hours (EU DPA in member state of establishment)
5. **Notify Subjects** — If high risk to individuals (Article 34)
6. **Document** — Maintain breach register per Article 33(5)

**DPO Contact:** dpo@enterprise.org | Emergency: +1-XXX-XXX-XXXX

**Breach Register:** Maintained in the compliance management system. Every breach, regardless of notification obligation, must be documented per Art. 33(5) with:
- Nature of the breach
- Effects and likely consequences
- Remedial actions taken
- Decision on whether supervisory authority notification was required (and if not, why)

---

## 9. Data Processing Agreement (DPA) Template

```
DATA PROCESSING AGREEMENT

Controller: [Organization Name] ("Controller")
Processor: Enterprise Migration Platform Team ("Processor")

Subject matter: Processing of personal data for CRM migration to Salesforce

Duration: Duration of migration project + 90-day retention of logs

Nature: ETL processing — extract from legacy system, transform, load to Salesforce

Purpose: Business CRM migration to improve customer data management

Categories of data subjects: B2B contacts, customers, government agency contacts

Categories of personal data: Names, email addresses, phone numbers, business addresses

Technical and organisational measures: As documented in security_model.md

Sub-processors:
  - Salesforce Inc. (US) — Target CRM system
  - Microsoft Azure (EU-West) — Infrastructure
  - HashiCorp (US) — Secrets management (no personal data processed)
```

---

## 10. Right to Erasure Implementation — Salesforce Specifics

### 10.1 Challenge: Salesforce Recycle Bin

When a Salesforce record is deleted, it remains in the Recycle Bin for 15 days before permanent deletion. For GDPR erasure, the Recycle Bin must also be explicitly emptied:

```python
# After deleting a record from Salesforce:
salesforce_client.delete(object_type="Account", record_id=sf_id)
salesforce_client.emptyRecycleBin([sf_id])
# Also purge any Big Object archive entries if applicable
```

### 10.2 Pseudonymization Strategy (Where Deletion Would Break Referential Integrity)

When a Contact record has related financial records (Opportunities, Orders) that must be retained for SOX/audit, full deletion is not possible. Pseudonymize PII fields instead:

```python
CONTACT_PII_FIELDS_TO_PSEUDONYMIZE = [
    "FirstName", "LastName", "Email", "Phone",
    "MobilePhone", "HomePhone", "Title",
    "MailingStreet", "MailingCity", "MailingPostalCode",
    "Contact_DOB__c", "Contact_NationalID__c"
]

CONTACT_FIELDS_TO_RETAIN_FOR_AUDIT = [
    "Id", "AccountId",   # Relational integrity
    "Legacy_ID__c",      # Cross-reference for audit
    "CreatedDate",       # Date of record creation
]
```

### 10.3 Erasure Audit Trail

The fact of erasure must be logged without retaining the erased PII:

```json
{
  "audit_event_type": "GDPR_ERASURE",
  "timestamp": "2025-12-01T22:00:00Z",
  "erasure_request_id": "GDPR-2025-1234",
  "source_record_id_hash": "sha256:a3f8b2c1...",
  "salesforce_record_id": "0015f000001XXXXX",
  "erasure_method": "HARD_DELETE",
  "operator": "dpo@enterprise.org",
  "systems_cleared": ["salesforce", "kafka_tombstone", "application_logs"],
  "legal_hold_check_passed": true,
  "retention_obligation_check": "NO_FINANCIAL_RECORDS_LINKED"
}
```

---

## 11. International Data Transfer — Practical Checklist

Before any EU data migration to a US-based Salesforce instance:

- [ ] Confirm Salesforce DPA is signed and current version (check Salesforce Trust site)
- [ ] Confirm SCCs are attached to client DPA (Module 2 Controller-to-Processor)
- [ ] Verify Salesforce EU-US Data Privacy Framework certification is current (dataprivacyframework.gov)
- [ ] Complete Transfer Impact Assessment for client's data categories
- [ ] If special category data: verify Salesforce Hyperforce EU option or obtain explicit client approval for US transfer with documented exceptional circumstances
- [ ] Document transfer mechanism in DPIA (if DPIA required)
- [ ] Configure Kafka topics for EU data to EU region Kafka cluster only
- [ ] Verify HashiCorp Vault key management for this tenant uses EU-region Vault
- [ ] Confirm AI triage (Claude API) is disabled for EU special category data migrations, OR verify Anthropic DPA and confirm only PII-masked data is transmitted

---

## 12. Privacy Impact Assessment — DPIA Trigger Checklist

Complete before each migration engagement involving EU personal data:

**DPIA is MANDATORY if ANY of the following apply:**
- [ ] Migration involves health, biometric, genetic, criminal, or political/religious data (Art. 9/10)
- [ ] Migration involves systematic profiling that produces legal or similarly significant effects
- [ ] Migration involves personal data of children at scale
- [ ] Migration involves > 1 million EU data subjects
- [ ] Migration involves novel technology not previously assessed (e.g., new AI agent feature)
- [ ] Client is a public authority using automated decision-making

**DPIA is RECOMMENDED if 2+ of the following apply:**
- [ ] Migration involves more than 100,000 EU data subjects
- [ ] Migration crosses international boundaries (EU to US/non-adequate country)
- [ ] Migration involves sensitive personal data (financial, employment records)
- [ ] Migration involves data subjects who may not be aware their data is being transferred
- [ ] Source system has had a previous security incident

**DPIA Reference Numbers (for record):**
- DPIA-2025-MIG-001: Initial platform DPIA — completed 2025-09-15
- DPIA-2025-MIG-002: AI agent feature addition — completed 2025-11-01
- Client-specific DPIAs: Filed per engagement in compliance management system

---

*Document Version: 2.1.0 | Reviewed: 2025-12-01 | Next Review: 2026-06-01*
*Owner: Data Protection Officer | Legal Review: 2025-11-28*
*This document does not constitute legal advice. Consult qualified legal counsel for specific situations.*
