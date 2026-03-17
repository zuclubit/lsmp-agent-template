# ADR-001: Salesforce Government Cloud Plus as Target CRM Platform

**Status:** Accepted
**Date:** 2025-08-14
**Deciders:** CIO, Program Director, Data Architect, Enterprise Architecture Board
**Reviewed By:** ISSO, Procurement Officer, Legal Counsel
**Classification:** Internal — Restricted

---

## Context

The agency operates three legacy CRM/case management platforms that have reached end-of-life and are creating significant operational risk:

1. **Oracle Siebel CRM 8.1** — On-premises deployment. Oracle extended support ended December 2024. No security patches available after that date. Hosting 2.1M Account records and 1.1M Opportunity records. Annual license + maintenance cost: $2.1M. Hardware refresh estimated at $4.8M due 2026.

2. **SAP CRM 7.0 (EHP3)** — On-premises deployment integrated with SAP ERP. SAP has announced mainstream maintenance end for CRM 7.0 in 2027. Running on aging Solaris hardware. Hosting 8.2M Case records. Annual license + support cost: $3.4M.

3. **Custom PostgreSQL Case Management Application** — Built in-house in 2014. Primary developer retired in 2022. No documentation. 3.9M archived Case records. Hosting infrastructure is reaching capacity. Annual support + infrastructure cost: $890K.

The combined annual cost of these platforms is $6.39M. The platforms do not interoperate — agents must switch between three UIs to manage a single customer's complete record. This causes data inconsistency and reduces agent productivity (estimated 18 min/case in redundant data entry).

The agency requires a consolidated, FedRAMP-authorized, modern CRM platform that can:
- Consolidate all customer data into a single source of truth
- Support 2,400 concurrent agency users
- Comply with FedRAMP High, FISMA, NIST SP 800-53 Rev 5
- Handle 340,000 external constituent records with PII/PHI protections
- Reduce annual platform costs over a 5-year horizon
- Support agency-specific extensions without custom software development

### Options Evaluated

A formal market analysis was conducted by the Enterprise Architecture office between March and June 2025. The following platforms were evaluated:

| Platform | FedRAMP Status | Deployment | 5-Year TCO | Key Concerns |
|---|---|---|---|---|
| **Salesforce Government Cloud+** | FedRAMP High Authorized | SaaS (AWS GovCloud) | $8.2M | Vendor lock-in; customization costs |
| **Microsoft Dynamics 365 Government** | FedRAMP High Authorized | SaaS (Azure Government) | $9.1M | Integration complexity; limited APEX equivalent |
| **ServiceNow CSM (Government)** | FedRAMP High Authorized | SaaS | $11.4M | Primary strength is ITSM, not CRM; higher cost |
| **Pegasystems CRM (FedRAMP)** | FedRAMP Moderate (not High) | SaaS/PaaS | $10.8M | FedRAMP High gap; highest cost |
| **Custom Development (AWS GovCloud)** | Inherited | PaaS | $14.2M | High development risk; long timeline; ongoing maintenance burden |
| **Oracle CX Government** | FedRAMP Moderate (not High) | SaaS | $9.7M | FedRAMP High gap; familiar to Siebel team but modern platform migration still required |

### Key Evaluation Criteria

| Criterion | Weight | Basis |
|---|---|---|
| FedRAMP High Authorization | Mandatory | Agency policy: all SaaS platforms must be FedRAMP High |
| PII/PHI data protection (Shield Platform Encryption) | High | 340K constituent records with PII; subset with PHI |
| Configurability without code (declarative) | High | Reduce ongoing maintenance cost |
| Bulk data API for migration | High | 23M+ records must be loaded via API |
| 5-year Total Cost of Ownership | Medium | Within approved program budget ceiling |
| Vendor stability and federal track record | Medium | Risk mitigation |
| Existing agency expertise | Low | Can be built; not a blocker |

---

## Decision

**Salesforce Government Cloud Plus (GC+) is selected as the target CRM platform.**

The migration will use Salesforce Bulk API 2.0 for initial data loading and the LSMP (Legacy-to-Salesforce Migration Platform) as the custom ETL orchestration layer.

Salesforce GC+ specifically (not standard Salesforce Government Cloud) is required to access:
- Shield Platform Encryption (AES-256 for PII/PHI fields)
- Advanced encryption key management (HSM-backed)
- Event Monitoring (field-level audit for PHI access)
- Bring Your Own Key (BYOK) capability

### Specific Configuration Decisions

1. **Org Type:** Single production org (not multi-org) with a hierarchical record type structure to represent the agency's program-level data separation.

2. **Custom Objects:** Minimal custom object creation. Standard Salesforce objects (Account, Contact, Case, Opportunity) are mapped to legacy entity types via external ID fields (`Legacy_Account_ID__c`, etc.).

3. **API Version:** API v60.0 (Winter '26). All integrations pinned to this version for stability. Upgrade evaluated annually.

4. **Metadata Deployment:** All Salesforce configuration managed as Salesforce DX (sfdx) metadata in Git, deployed via CI/CD. No manual org changes permitted in production.

5. **Permission Model:** Permission Sets (not Profiles) for all user access. Minimum Baseline Profile assigned to all users; capability-specific Permission Sets layered on top.

---

## Consequences

### Positive Consequences

- **FedRAMP High compliance achieved** immediately via Salesforce's existing ATO. The agency inherits 89 FedRAMP controls from Salesforce's Customer Responsibility Matrix.
- **Reduced annual platform cost:** Projected 5-year savings of $8.3M vs. staying on legacy (net of migration and Salesforce licensing costs).
- **Single source of truth:** All 2,400 agents use one platform — eliminates redundant data entry (projected 6 min/case reduction = 240 FTE-hours/month recovered).
- **Shield Platform Encryption:** Native field-level encryption for PII/PHI fields — eliminates the need for custom encryption layer.
- **Salesforce Bulk API 2.0:** Supports 150M API calls/day — sufficient for migration and ongoing operations.
- **Declarative configuration:** Platform upgrades (3x/year) do not require custom code recompilation.

### Negative Consequences

- **Vendor lock-in:** Salesforce data model and proprietary APEX/Flow customizations create dependency on Salesforce. Mitigated by: minimal customization strategy (use standard objects where possible), external ID fields maintained for portability, data export scheduled monthly to S3.
- **Salesforce Governor Limits:** Strict API and processing limits require careful architecture (Bulk API 2.0, rate limiting in Load Service, governor monitoring). Not a risk with proper design.
- **Learning curve:** Most agency staff are familiar with Siebel and SAP — Salesforce is new. Mitigated by: 90-day training program, Salesforce Admin certification track for 12 staff.
- **Migration complexity:** 23M+ records across 3 heterogeneous source systems is a significant migration undertaking. This is the rationale for the LSMP program.
- **Customization limitations:** Some legacy Siebel business rules cannot be replicated exactly in Salesforce Flow — approximately 14 custom processes require re-engineering. Risk accepted; these processes will be simplified and standardized in Salesforce.

### Risks

| Risk | Mitigation |
|---|---|
| Salesforce GC+ service outage during migration cutover | Pre-check Salesforce Trust status; cutover during lowest-risk window; rollback plan tested |
| Future Salesforce pricing increases | 5-year contract signed with price lock. Exit clause at year 3. |
| FedRAMP authorization scope changes | ISSO monitors FedRAMP PMO updates; Salesforce has contractual notification obligation |
| Custom Field limits in Salesforce (800 per object) | Field audit completed — estimated 140 custom fields needed across 4 core objects (well within limits) |

### Rejected Alternatives

**Microsoft Dynamics 365 Government:** Strong FedRAMP High authorization, but integration complexity was assessed as significantly higher (no equivalent to Salesforce Bulk API 2.0 for migration volume; migration tools less mature). 5-year TCO $900K higher.

**Custom Development:** Highest risk. Would require 24-30 months of development, dedicated ongoing engineering team, and would not inherit any FedRAMP controls. 5-year TCO nearly double.

**Oracle CX Government:** Only FedRAMP Moderate — does not meet agency's mandatory FedRAMP High requirement for systems processing PII. Waiver not available.

---

## Review Schedule

This ADR is reviewed:
- At each major phase completion (to confirm decision remains valid)
- If Salesforce FedRAMP authorization status changes
- If 5-year TCO projections deviate > 20% from original model
- At the end of Year 2 (formal post-implementation review)

**Next Review:** Phase 3 Completion (May 2026)

---

*ADR maintained in Git at `architecture/decisions/ADR-001-salesforce-as-target-platform.md`. This decision supersedes the Legacy Platform Continuation Decision (2023-11-01, archived). Any proposal to reconsider this decision requires Enterprise Architecture Board approval.*
