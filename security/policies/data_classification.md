# Data Classification Policy
## Legacy to Salesforce Migration Project

**Document ID:** SEC-POL-002
**Version:** 1.3.0
**Classification:** Internal
**Owner:** Data Governance Team / CISO
**Last Reviewed:** 2026-03-16
**Next Review:** 2026-09-16
**Status:** APPROVED

---

## 1. Purpose

This Data Classification Policy establishes a framework for identifying, categorizing, and handling data assets associated with the Legacy to Salesforce Migration project. Proper classification ensures that data receives appropriate protection commensurate with its sensitivity, value, and risk.

---

## 2. Classification Levels

The organization uses four classification levels, aligned with NIST SP 800-60 and FedRAMP data categorization:

### 2.1 Level 1 — PUBLIC

**Definition:** Information intentionally made available to the general public or that poses no harm if disclosed.

**Examples in Migration Context:**
- Published API documentation
- Public-facing migration status dashboards
- Press releases about CRM modernization
- Open-source component licenses

**Handling Requirements:**
- No special handling required
- Standard transmission acceptable
- No encryption required (though HTTPS recommended)
- Can be stored on any approved system
- No access restrictions

**Labeling:** Documents/files may be labeled `[PUBLIC]` but not required.

---

### 2.2 Level 2 — INTERNAL

**Definition:** Information intended for use within the organization only, not for public release. Unauthorized disclosure could cause minor embarrassment or competitive disadvantage.

**Examples in Migration Context:**
- Migration architecture diagrams
- Internal runbooks and procedures
- Test data using synthetic/anonymized records
- Non-sensitive configuration files
- Meeting notes and project plans
- Internal metrics and KPIs

**Handling Requirements:**

| Aspect | Requirement |
|--------|-------------|
| Storage | Organization-approved systems only |
| Transmission | Encrypted channels (TLS 1.2+) |
| Encryption at rest | Recommended (required for cloud storage) |
| Email | Permitted via corporate email only |
| Printing | Permitted; dispose in shredding bins |
| Mobile devices | Permitted on MDM-enrolled devices |
| Third parties | With NDA only |
| Retention | Per departmental policy |

**Labeling:** All documents labeled `[INTERNAL]` in header/footer.

---

### 2.3 Level 3 — CONFIDENTIAL

**Definition:** Sensitive business information that could cause significant harm if disclosed without authorization, including financial loss, regulatory penalties, or reputational damage.

**Examples in Migration Context:**
- Customer account data (names, contact info, non-financial)
- Business logic and proprietary transformation rules
- Vendor contracts and pricing
- Migration timelines with business impact analysis
- Employee records and HR data
- Partner organization data
- Non-public financial information
- System credentials and API keys (pre-production)
- Security assessment reports
- Source code containing business logic

**Handling Requirements:**

| Aspect | Requirement |
|--------|-------------|
| Storage | Encrypted storage (AES-256, CMK) |
| Transmission | TLS 1.3, encrypted email (S/MIME or PGP) |
| Encryption at rest | Mandatory |
| Email | Encrypted email only; no external forwarding |
| Printing | Need-to-know basis; immediate secure disposal |
| Mobile devices | Encrypted MDM-enrolled devices; remote wipe enabled |
| Third parties | Requires CISO approval + DPA |
| Retention | Minimum as required by regulation |
| Access logging | Mandatory |
| DLP | Active DLP monitoring |
| Screen sharing | Must blank screen when not actively presenting |

**Labeling:** `[CONFIDENTIAL]` in all document headers, footers, and watermarks. Filename prefix: `CONF_`

**Access Control:**
- Role-based access with documented approval
- Quarterly access reviews
- Shared access prohibited

---

### 2.4 Level 4 — RESTRICTED

**Definition:** Highest classification. Information whose unauthorized disclosure could cause severe harm including personal safety risks, national security implications, major regulatory violations, or catastrophic financial impact.

**Examples in Migration Context:**
- Social Security Numbers (SSN)
- Financial account numbers (bank accounts, credit cards)
- Protected Health Information (PHI) / medical records
- Government-issued ID numbers (passport, driver's license)
- Authentication credentials for production systems
- Cryptographic keys and certificates
- Biometric data
- Criminal records
- Data subject to specific government classification

**Handling Requirements:**

| Aspect | Requirement |
|--------|-------------|
| Storage | HSM-backed encryption; dedicated encrypted volumes |
| Transmission | End-to-end encryption; dedicated encrypted channels |
| Encryption at rest | Mandatory AES-256-GCM with HSM-managed keys |
| Email | PROHIBITED via standard email; use secure transfer only |
| Printing | Prohibited; exceptions require CISO written approval |
| Mobile devices | PROHIBITED on mobile devices |
| Third parties | Prohibited without Board-level approval + BAA/DPA |
| Retention | Minimum legally required; immediate disposal when permissible |
| Access logging | Real-time SIEM alerting on all access |
| DLP | Blocking DLP controls; egress filtering |
| Screen sharing | PROHIBITED |
| Remote access | Requires PAM session recording; JIT access only |
| Anonymization | Must be anonymized/tokenized except when strictly necessary |

**Labeling:** `[RESTRICTED - HANDLE WITH CARE]` in all documents. Watermarks on every page. Filename prefix: `RESTR_`

**Access Control:**
- Named individual approval by Data Owner + CISO
- Access automatically expires after 24 hours (renewable)
- Dual authorization for bulk access
- All access logged, alerted, and reviewed within 24 hours

---

## 3. Data Handling by Processing Stage

### 3.1 Data Extraction (Source Legacy System)

| Data Type | Classification | Handling |
|-----------|---------------|----------|
| Account names | Confidential | Encrypt in transit, log access |
| Contact email addresses | Confidential | Encrypt in transit, mask in logs |
| Phone numbers | Confidential | Mask last 4 digits in logs |
| SSN / Tax IDs | Restricted | Tokenize immediately at extraction, never log raw |
| Financial account numbers | Restricted | Tokenize immediately, PCI DSS controls |
| Revenue/financial data | Confidential | Encrypt in transit and at rest |
| Employee data | Confidential | Separate data store, HR approval required |
| Medical data | Restricted | HIPAA controls, BAA required |

### 3.2 Data Transformation Stage

| Activity | Requirement |
|----------|-------------|
| Transformation workloads | Run in isolated compute (no cross-tenant) |
| Intermediate files | Encrypted temp storage, deleted within 24h of migration |
| Mapping tables with PII | Classified Restricted, encrypted at rest |
| Error logs | Sanitized — no PII in error messages |
| Audit logs | Confidential; include references not raw data |

### 3.3 Data Loading (Target Salesforce)

| Activity | Requirement |
|----------|-------------|
| Salesforce API calls | Mutual TLS; log API call metadata only |
| Bulk API jobs | Monitor and log job IDs; results encrypted |
| Failed records | Re-queue encrypted; never log raw field values |
| Salesforce org access | Via Connected App with minimum required scopes |

---

## 4. Anonymization and Pseudonymization

### 4.1 When Required

Data must be anonymized or pseudonymized when:
- Used in non-production environments (dev, staging, QA)
- Shared with vendors for support purposes
- Used for analytics or reporting beyond operational necessity
- Training data for AI/ML models

### 4.2 Approved Methods

| Method | Use Case | Standard |
|--------|----------|----------|
| Tokenization | PCI data (card numbers) | PCI DSS tokenization |
| Format-preserving encryption | SSN, account numbers | NIST SP 800-38G |
| Pseudonymization | Person names, contact info | GDPR Article 4(5) |
| Data masking | Non-production environments | Deterministic masking preferred |
| Generalization | Analytics (age ranges, regions) | k-anonymity (k≥5) |
| Suppression | Outlier/rare data | Per privacy review |

### 4.3 Anonymization Validation

- Anonymized datasets must pass re-identification risk assessment
- High-risk datasets require independent privacy review
- Re-identification attacks tested quarterly

---

## 5. Data Inventory and Lineage

### 5.1 Data Catalog Requirements

All data assets must be registered in the Data Catalog with:

- Asset name and description
- Classification level
- Data owner and steward
- Source system and lineage
- Legal basis for processing (GDPR)
- Retention period
- Third-party sharing details

### 5.2 Data Lineage

The migration maintains end-to-end data lineage:

```
Legacy Source → Extraction Layer → Raw Landing Zone
    → Transformation Layer → Validated Staging
    → Salesforce Target
```

Each stage records:
- Timestamp
- Actor (service account)
- Transformation applied
- Record count
- Hash of batch (for integrity verification)

---

## 6. Special Categories of Data

### 6.1 Personally Identifiable Information (PII)

PII is automatically classified Confidential (minimum) and requires:

- Data subject rights procedures (access, deletion, portability, correction)
- Privacy impact assessment (PIA) before processing
- Legal basis documented for each processing purpose
- Data minimization: collect only what is necessary
- Purpose limitation: use only for stated purpose

### 6.2 Sensitive PII (SPII)

SPII is classified Restricted and includes:
- SSN, passport numbers, financial account numbers
- Medical/health information
- Biometric identifiers
- Race, ethnicity, religion, political views (where applicable)
- Sexual orientation or gender identity

SPII requires additional controls:
- Field-level encryption in all storage
- Tokenization for processing
- Separate access controls from general PII

### 6.3 Controlled Unclassified Information (CUI)

Federal contract data may include CUI categories:
- Privacy/PRVCY (personal information)
- Export Control/CTI
- Law Enforcement/LEINFO

CUI handling follows NIST SP 800-171 and DFARs 252.204-7012.

---

## 7. Data Owner Responsibilities

| Role | Responsibility |
|------|---------------|
| Data Owner | Classify data, approve access, define retention |
| Data Steward | Maintain catalog, enforce handling rules, quality |
| Data Custodian | Implement technical controls, backup, recovery |
| Data Processor | Process only per Owner instructions, report incidents |
| All Users | Follow classification handling requirements |

---

## 8. Compliance Mapping

| Requirement | Classification Level | Applicable Framework |
|-------------|---------------------|---------------------|
| Protect PII | Confidential+ | GDPR, CCPA, NIST |
| Protect financial data | Confidential/Restricted | PCI DSS, SOX |
| Protect health data | Restricted | HIPAA, HITECH |
| Protect federal data | Confidential/Restricted | FedRAMP, FISMA, CUI |
| Non-public information | Confidential | SEC Regulation FD |

---

## 9. Training and Awareness

- All personnel must complete Data Classification Training annually
- Training covers: classification levels, handling requirements, breach reporting
- Completion tracked in LMS; non-completion escalated to manager
- Role-specific training for Data Owners and Data Stewards

---

## Document Control

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2025-01-15 | Initial release |
| 1.1.0 | 2025-04-01 | Added CUI section |
| 1.2.0 | 2025-09-15 | Updated anonymization methods |
| 1.3.0 | 2026-03-16 | Quarterly review; added SPII section |
