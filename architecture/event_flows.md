# Event Flows — Key Business Process Diagrams

**Document Version:** 1.5.0
**Last Updated:** 2026-03-16
**Status:** Approved
**Owner:** Enterprise Architecture Office
**Classification:** Internal — Restricted

---

## Table of Contents

1. [Overview](#1-overview)
2. [Event Taxonomy](#2-event-taxonomy)
3. [Flow 1: Full Batch Migration (Happy Path)](#3-flow-1-full-batch-migration-happy-path)
4. [Flow 2: Incremental Delta Sync (Phase 4 Dual-Write)](#4-flow-2-incremental-delta-sync-phase-4-dual-write)
5. [Flow 3: Validation Failure & Quarantine](#5-flow-3-validation-failure--quarantine)
6. [Flow 4: Rollback Execution](#6-flow-4-rollback-execution)
7. [Flow 5: Phase Cutover](#7-flow-5-phase-cutover)
8. [Flow 6: Operator Authentication & Authorization](#8-flow-6-operator-authentication--authorization)
9. [Flow 7: Salesforce Governor Limit Backpressure](#9-flow-7-salesforce-governor-limit-backpressure)
10. [Flow 8: Orphan Record Resolution](#10-flow-8-orphan-record-resolution)
11. [Kafka Topic Map](#11-kafka-topic-map)

---

## 1. Overview

This document describes the key event-driven and request-response flows within the LSMP system using Mermaid sequence diagrams. Each flow shows the services involved, the messages exchanged, the data passed, and the conditions for success and failure.

**Notation:**
- Solid arrows (`->>`) = synchronous request or message send
- Dashed arrows (`-->>`) = response or acknowledgment
- `Note over` = state or condition at that point
- `alt`/`else` = conditional branches
- `loop` = repeated operation
- `par` = parallel operations

---

## 2. Event Taxonomy

### 2.1 Domain Events (Kafka Topic: `lsmp.audit.events`)

| Event Name | Produced By | Consumed By | Payload |
|---|---|---|---|
| `MigrationJobCreated` | Control Plane API | Audit Logger | job_id, entity_type, phase, operator_id |
| `MigrationJobStarted` | Airflow | Audit Logger | job_id, batch_id, started_at |
| `ExtractionStarted` | Extraction Service | Audit Logger | batch_id, source_system, entity_type |
| `ExtractionCompleted` | Extraction Service | Audit Logger, Airflow | batch_id, record_count, s3_prefix, manifest_checksum |
| `ExtractionFailed` | Extraction Service | Audit Logger, Airflow | batch_id, error_code, error_message, records_extracted |
| `TransformationStarted` | Spark Engine | Audit Logger | batch_id, spark_job_id |
| `TransformationCompleted` | Spark Engine | Audit Logger, Airflow | batch_id, input_count, output_count, dedup_removed, s3_prefix |
| `TransformationFailed` | Spark Engine | Audit Logger, Airflow | batch_id, spark_job_id, error_message |
| `ValidationStarted` | Validation Service | Audit Logger | batch_id, suite_name, suite_version |
| `ValidationCompleted` | Validation Service | Audit Logger, Airflow | batch_id, grade, passed, failed, warned, report_url |
| `ValidationFailed` | Validation Service | Audit Logger, Airflow | batch_id, critical_failures, report_url |
| `LoadStarted` | Load Service | Audit Logger | batch_id, entity_type, record_count |
| `RecordsBatchLoaded` | Load Service | Audit Logger | batch_id, bulk_job_id, batch_number, loaded_count, failed_count |
| `LoadCompleted` | Load Service | Audit Logger, Airflow | batch_id, total_loaded, total_failed, sf_job_ids |
| `LoadFailed` | Load Service | Audit Logger, Airflow | batch_id, bulk_job_id, error_message |
| `RollbackInitiated` | Control Plane API | Audit Logger, Airflow | batch_id, initiator, second_approver, reason |
| `RollbackCompleted` | Load Service | Audit Logger | batch_id, deleted_count, duration_seconds |
| `OrphanRecordQuarantined` | Load Service | Audit Logger | batch_id, entity_type, legacy_id, parent_type, parent_legacy_id |
| `CutoverAuthorized` | Control Plane API | Audit Logger, Airflow | phase, authorized_by, cutover_time |

### 2.2 CDC Events (Kafka Topic: `lsmp.cdc.pgdb.{table}`)

| Event Name | Produced By | Consumed By | Notes |
|---|---|---|---|
| `RowInserted` | Debezium | Extraction Service (CDC mode) | Full new row value |
| `RowUpdated` | Debezium | Extraction Service (CDC mode) | Before and after values |
| `RowDeleted` | Debezium | Extraction Service (CDC mode) | Before value only |
| `TransactionCommitted` | Debezium | Extraction Service | Flush signal |

---

## 3. Flow 1: Full Batch Migration (Happy Path)

This is the primary flow — a complete Extract, Transform, Validate, Load pipeline for a single entity batch.

```mermaid
sequenceDiagram
    actor OP as Migration Engineer
    participant CTRL as Control Plane API
    participant ORCH as Airflow Orchestrator
    participant EXT as Extraction Service
    participant S3 as S3 Staging
    participant SPARK as Transformation Engine
    participant CFG as Config Service
    participant GE as Validation Framework
    participant LOAD as Load Service
    participant SF as Salesforce GC+
    participant KAFKA as MSK Kafka
    participant AUDIT as Audit Logger
    participant SPLUNK as Splunk SIEM

    Note over OP,SPLUNK: T=01:00 ET — Migration window opens

    OP->>CTRL: POST /jobs {entity_type: "Account", phase: 2, source: "SIEBEL"}
    CTRL->>CTRL: OPA authorization check (role=migration_engineer, action=create_job)
    CTRL->>ORCH: POST /api/v1/dags/lsmp_account_migration/dagRuns
    ORCH-->>CTRL: {dag_run_id: "dr_2026031601_account"}
    CTRL-->>OP: 201 Created {job_id: "...", batch_id: "batch-2026031601-account"}
    CTRL->>KAFKA: Emit MigrationJobCreated

    Note over ORCH: DAG run begins — Task 1: Extract

    ORCH->>EXT: POST /extract {batch_id, entity_type: Account, source: SIEBEL}
    EXT->>CFG: GET /mappings/account (load source schema)
    CFG-->>EXT: Account source schema YAML
    Note over EXT: Retrieve Siebel credentials from Vault
    EXT->>EXT: Partition ROW_ID range into 16 parallel tasks
    EXT->>KAFKA: Emit ExtractionStarted {batch_id, record_count_estimate: 2104312}

    loop 16 parallel extraction partitions
        EXT->>S3: Write Parquet partition (raw/siebel/account/batch=batch-2026031601-account/part-NNNNN.parquet)
    end

    EXT->>S3: Write manifest JSON (record_count: 2104312, checksums: [...])
    EXT->>KAFKA: Emit ExtractionCompleted {batch_id, record_count: 2104312, manifest_checksum: "sha256:..."}
    EXT-->>ORCH: 200 OK {status: COMPLETED, record_count: 2104312}
    KAFKA-->>AUDIT: Consume ExtractionCompleted
    AUDIT->>SPLUNK: Forward to Splunk HEC

    Note over ORCH: Task 2: Transform

    ORCH->>SPARK: Submit EMR Serverless job {batch_id, entity_type: Account}
    SPARK->>S3: Read manifest → verify checksums
    SPARK->>CFG: GET /mappings/account (transformation rules YAML)
    CFG-->>SPARK: Account mapping YAML (TR-001 through TR-023 applied)
    SPARK->>KAFKA: Emit TransformationStarted

    Note over SPARK: Processing 2,104,312 records across 16 Spark partitions

    SPARK->>SPARK: Apply TR-001 (ID normalization)
    SPARK->>SPARK: Apply TR-002 (string normalization)
    SPARK->>SPARK: Apply TR-003 (EIN sanitization)
    SPARK->>SPARK: LOOKUP account_type_codes (picklist resolution)
    SPARK->>SPARK: Apply TR-009 (USPS address normalization — batch UDF)
    SPARK->>SPARK: Deterministic deduplication (EIN + SSN_HASH partition)
    Note over SPARK: Dedup removed 1,247 duplicate records

    SPARK->>S3: Write transformed Parquet (transformed/account/batch=batch-2026031601-account/)
    SPARK->>S3: Write updated manifest {input: 2104312, output: 2103065, dedup_removed: 1247}
    SPARK->>KAFKA: Emit TransformationCompleted {input: 2104312, output: 2103065}
    KAFKA-->>AUDIT: Consume → forward to Splunk

    Note over ORCH: Task 3: Validate

    ORCH->>GE: POST /validate {batch_id, entity_type: Account}
    GE->>S3: Read transformed Parquet
    GE->>CFG: GET /thresholds/account
    CFG-->>GE: {max_failure_pct: 0.05, critical_expectations: [...]}

    Note over GE: Running 87 expectations across 2,103,065 records

    GE->>GE: GE-ACC-001: Legacy_Account_ID__c not null — PASS (100%)
    GE->>GE: GE-ACC-002: Name not null, length valid — PASS (100%)
    GE->>GE: GE-ACC-004: EIN format valid — PASS (100% of non-null)
    GE->>GE: GE-ACC-016: BillingStreet not null — WARN (87.3% < target 85%? PASS)
    Note over GE: All 87 expectations PASS. Failure rate: 0.00%

    GE->>S3: Write HTML validation report to s3://lsmp-reports-prod/account/batch-2026031601-account.html
    GE->>KAFKA: Emit ValidationCompleted {grade: PASS, passed: 87, failed: 0, report_url: "..."}
    GE-->>ORCH: 200 OK {grade: PASS, proceed: true}

    Note over ORCH: Task 4: Load

    ORCH->>LOAD: POST /load {batch_id, entity_type: Account, operation: UPSERT}
    LOAD->>S3: Read validated Parquet → verify checksum
    LOAD->>LOAD: Check governor limits (API usage: 12% of daily limit)
    LOAD->>KAFKA: Emit LoadStarted {batch_id, record_count: 2103065}

    loop 211 batches of 10,000 records each
        LOAD->>SF: POST /services/data/v60.0/jobs/ingest (Bulk API 2.0 upsert, ExternalId=Legacy_Account_ID__c)
        SF-->>LOAD: {id: "750R00000001XYZAAB", state: "Open"}
        LOAD->>SF: PUT /services/data/v60.0/jobs/ingest/{id}/batches (10,000 records CSV)
        LOAD->>SF: PATCH /services/data/v60.0/jobs/ingest/{id} {state: "UploadComplete"}
        loop Poll every 30s
            LOAD->>SF: GET /services/data/v60.0/jobs/ingest/{id}
            SF-->>LOAD: {state: "JobComplete", numberRecordsProcessed: 10000, numberRecordsFailed: 0}
        end
        LOAD->>SF: GET /services/data/v60.0/jobs/ingest/{id}/successfulResults
        LOAD->>LOAD: Write legacy_id → sf_id mappings to id_mapping table
        LOAD->>KAFKA: Emit RecordsBatchLoaded {batch_number, loaded_count: 10000}
    end

    LOAD->>LOAD: Post-load reconciliation — SOQL count query
    LOAD->>SF: SELECT COUNT() FROM Account WHERE Legacy_Account_ID__c != null
    SF-->>LOAD: {totalSize: 2103065}
    Note over LOAD: Source count 2103065 = Salesforce count 2103065 ✓

    LOAD->>KAFKA: Emit LoadCompleted {batch_id, total_loaded: 2103065, total_failed: 0}
    LOAD-->>ORCH: 200 OK {status: COMPLETED, loaded: 2103065}
    KAFKA-->>AUDIT: Consume all events
    AUDIT->>SPLUNK: Batch forward to HEC

    Note over ORCH: DAG complete — all tasks SUCCESS

    ORCH->>KAFKA: Emit MigrationJobCompleted {batch_id, phase: 2, entity: Account, duration_min: 187}
    ORCH->>CTRL: POST /jobs/{job_id}/status {status: COMPLETED}

    Note over OP,SPLUNK: T=04:07 ET — Migration complete. Window used: 3h 7min
```

---

## 4. Flow 2: Incremental Delta Sync (Phase 4 Dual-Write)

This flow runs every 15 minutes during the Phase 4 dual-write period to keep Salesforce synchronized with new/updated active cases.

```mermaid
sequenceDiagram
    participant PG as PostgreSQL Legacy DB
    participant DEB as Debezium (Delta Tracker)
    participant KAFKA as MSK Kafka
    participant CDC_CONS as CDC Consumer (Extraction Service)
    participant S3 as S3 Staging
    participant SPARK as Transformation Engine
    participant LOAD as Load Service
    participant SF as Salesforce GC+
    participant AUDIT as Audit Logger

    Note over PG,SF: Every 15 minutes — incremental sync cycle

    PG->>DEB: WAL replication stream (new/updated case rows)
    DEB->>DEB: Decode WAL: INSERT/UPDATE/DELETE on case_header, case_detail

    loop For each changed row
        DEB->>KAFKA: Produce to lsmp.cdc.pgdb.case_header {op: "u", before: {...}, after: {...}, ts_ms: ...}
    end

    DEB->>KAFKA: Produce TransactionCommitted {lsn: "0/4B2C3D10", row_count: 847}

    Note over CDC_CONS: Polling consumer reads uncommitted batch

    CDC_CONS->>KAFKA: Consume from lsmp.cdc.pgdb.case_header (consumer group: cdc-extraction)
    CDC_CONS->>CDC_CONS: Accumulate changes until TransactionCommitted received
    Note over CDC_CONS: 847 changed case records in this sync cycle

    CDC_CONS->>S3: Write delta Parquet (raw/pgdb/case/delta/cycle=20260316T031500/)
    CDC_CONS->>KAFKA: Emit ExtractionCompleted {type: DELTA, record_count: 847, cycle: "20260316T031500"}

    SPARK->>S3: Read delta Parquet (EMR Serverless — small job, single executor)
    SPARK->>SPARK: Apply case transformation rules
    SPARK->>SPARK: Merge with full-extract records (resolve UPDATE vs INSERT based on CDC op)
    SPARK->>S3: Write transformed delta Parquet

    LOAD->>S3: Read transformed delta Parquet (847 records)
    LOAD->>SF: Bulk API 2.0 UPSERT (ExternalId=Legacy_Case_GUID__c) — 847 records in 1 batch
    SF-->>LOAD: {state: "JobComplete", numberRecordsProcessed: 847, numberRecordsFailed: 0}
    LOAD->>LOAD: Update id_mapping for any new records
    LOAD->>LOAD: Calculate delta lag = now() - max(case_created_at in this cycle)
    Note over LOAD: Delta lag = 4 minutes 32 seconds (within 15-min SLA)

    LOAD->>KAFKA: Emit DeltaSyncCompleted {cycle, records_synced: 847, lag_seconds: 272}
    KAFKA-->>AUDIT: Consume → forward to Splunk

    Note over LOAD: After 5 consecutive cycles with 0 divergences → Cutover eligible signal emitted
```

---

## 5. Flow 3: Validation Failure & Quarantine

```mermaid
sequenceDiagram
    actor DS as Data Steward
    participant ORCH as Airflow
    participant GE as Validation Framework
    participant S3 as S3 Staging
    participant CFG as Config Service
    participant CTRL as Control Plane API
    participant KAFKA as Kafka
    participant AUDIT as Audit Logger

    Note over ORCH,AUDIT: Validation task triggered after transformation

    ORCH->>GE: POST /validate {batch_id: "batch-2026031601-contact", entity_type: Contact}
    GE->>S3: Read transformed Parquet (1,847,091 Contact records)
    GE->>CFG: GET /thresholds/contact
    CFG-->>GE: {max_failure_pct: 0.05, critical: ["GE-CON-001", "GE-CON-002", "GE-CON-007"]}

    Note over GE: Running 91 expectations

    GE->>GE: GE-CON-001: Legacy_Contact_ID__c not null — PASS (100%)
    GE->>GE: GE-CON-002: LastName not null — PASS (100%)
    GE->>GE: GE-CON-007: Email RFC 5321 format — FAIL
    Note over GE: 12,847 Contacts have invalid email format (0.695% — exceeds 0.05% threshold)
    GE->>GE: GE-CON-013: Birthdate age 0-120 — FAIL
    Note over GE: 3,204 Contacts have Birthdate outside valid range (future dates from SAP date bug)
    GE->>GE: Remaining 89 expectations — PASS

    GE->>S3: Write validation report HTML (lsmp-reports-prod/contact/batch-2026031601-contact.html)
    GE->>KAFKA: Emit ValidationFailed {batch_id, grade: FAIL, critical_failures: ["GE-CON-007", "GE-CON-013"], failure_count: 16051, failure_pct: 0.869, report_url: "..."}
    GE-->>ORCH: 200 OK {grade: FAIL, proceed: false, report_url: "https://..."}

    ORCH->>ORCH: Mark task FAILED — hold pipeline
    ORCH->>KAFKA: Emit MigrationJobHeld {batch_id, reason: "VALIDATION_FAILED", report_url: "..."}

    Note over CTRL: PagerDuty alert fires: Validation failure > threshold

    KAFKA-->>CTRL: Consume MigrationJobHeld event (notification listener)
    CTRL->>CTRL: Create ServiceNow incident ticket P2
    CTRL->>DS: Send email + Slack notification {subject: "Validation FAIL — Contact batch — action required", report_url: "..."}

    DS->>CTRL: GET /jobs/{batch_id}/validation-report (view failure details)
    CTRL-->>DS: Validation report with 16,051 failing records listed by rule

    DS->>DS: Investigate GE-CON-007 failures — identifies ETL bug: email addresses from SAP CRM include trailing spaces not stripped by TR-002
    DS->>CTRL: POST /jobs/{batch_id}/remediation {action: "REPROCESS_TRANSFORM", fix_description: "Apply additional strip to email field in SAP contact adapter"}

    Note over DS,GE: Fix committed to Git → CI/CD deploys fix to staging → verified → deployed to prod
    Note over DS,GE: Data Steward triggers re-run of transformation for affected batch

    DS->>CTRL: POST /jobs {batch_id: "batch-2026031601-contact-v2", parent_batch: "batch-2026031601-contact", operation: "REPROCESS"}
    CTRL->>ORCH: Trigger new DAG run starting from Transform task

    Note over GE: Second validation run — all 91 expectations PASS
    GE->>KAFKA: Emit ValidationCompleted {grade: PASS, passed: 91, failed: 0}
    Note over ORCH: Pipeline resumes — Load task executes normally
```

---

## 6. Flow 4: Rollback Execution

```mermaid
sequenceDiagram
    actor ML as Migration Lead (Initiator)
    actor ML2 as Second Migration Lead (Approver)
    participant CTRL as Control Plane API
    participant OPA as OPA Authorization
    participant ORCH as Airflow
    participant LOAD as Load Service
    participant SF as Salesforce GC+
    participant LEGACY as Legacy System (Siebel/SAP)
    participant DBA as DBA (manual step)
    participant KAFKA as Kafka
    participant AUDIT as Audit Logger

    Note over ML,AUDIT: T=04:30 ET — Post-load reconciliation reveals 3,241 records with corrupted BillingCity field

    ML->>CTRL: POST /rollback {batch_id: "batch-2026031601-account", reason: "DATA_CORRUPTION: BillingCity field truncated", initiator: "jsmith@agency.gov"}
    CTRL->>OPA: Evaluate: can jsmith initiate_rollback?
    OPA-->>CTRL: ALLOW (role=migration_lead)
    CTRL->>CTRL: Check rollback policy: batch within 4h window? YES. Rollback already in progress? NO.
    CTRL->>CTRL: Require second approver (dual-operator authorization)

    CTRL-->>ML: 202 Accepted — awaiting second approver. Share approval token with second Migration Lead.
    CTRL->>KAFKA: Emit RollbackPendingApproval {batch_id, initiator, reason, expires_at: T+30min}

    ML->>ML2: Phone call — "Need second approval for Account rollback, token: ROLLBACK-AUTH-8F2C"
    ML2->>CTRL: POST /rollback/{batch_id}/approve {approval_token: "ROLLBACK-AUTH-8F2C", approver: "mwilliams@agency.gov"}
    CTRL->>OPA: Evaluate: can mwilliams approve_rollback? Is approver != initiator?
    OPA-->>CTRL: ALLOW (role=migration_lead, different_from_initiator=true)

    CTRL->>KAFKA: Emit RollbackInitiated {batch_id, initiator: jsmith, approver: mwilliams, timestamp}
    CTRL->>ORCH: POST /dags/lsmp_rollback/dagRuns {batch_id, entity_type: Account}

    Note over ORCH: Rollback DAG begins

    ORCH->>DBA: ALERT: Reactivate Siebel write access for Account entity (DB constraint deactivation)
    DBA-->>ORCH: Confirmed — Siebel writes re-enabled

    ORCH->>LOAD: POST /load/rollback {batch_id, entity_type: Account}
    LOAD->>LOAD: Read id_mapping table — retrieve all SF IDs loaded in this batch (2,103,065 records)
    LOAD->>KAFKA: Emit RollbackStarted {batch_id, records_to_delete: 2103065}

    loop 211 delete batches of 10,000 records each
        LOAD->>SF: POST /services/data/v60.0/jobs/ingest (Bulk API 2.0 DELETE, type: Account)
        SF-->>LOAD: {id: "750R00000001ROLLBK", state: "Open"}
        LOAD->>SF: PUT (CSV of 10,000 Account IDs to delete)
        LOAD->>SF: PATCH {state: "UploadComplete"}
        loop Poll every 30s
            LOAD->>SF: GET job status
            SF-->>LOAD: {state: "JobComplete", numberRecordsProcessed: 10000, numberRecordsFailed: 0}
        end
        LOAD->>KAFKA: Emit RollbackBatchDeleted {batch_number, deleted_count: 10000}
    end

    LOAD->>SF: SELECT COUNT() FROM Account WHERE Legacy_Account_ID__c != null
    SF-->>LOAD: {totalSize: 0}
    Note over LOAD: Salesforce Account count = 0 ✓ Rollback complete

    LOAD->>LOAD: Mark id_mapping records as ROLLED_BACK
    LOAD->>KAFKA: Emit RollbackCompleted {batch_id, deleted_count: 2103065, duration_seconds: 2847}
    KAFKA-->>AUDIT: Consume → forward to Splunk (priority forwarding)

    LOAD-->>ORCH: 200 OK {status: ROLLBACK_COMPLETED}
    ORCH->>CTRL: POST /jobs/{job_id}/status {status: ROLLED_BACK}
    CTRL-->>ML: Notification: "Rollback complete. 2,103,065 records deleted. Siebel is system of record."

    Note over ML,AUDIT: T=05:55 ET — Total rollback time: 1h 25min (within 4h RTO)
    Note over ML,AUDIT: Post-mortem scheduled for Monday 09:00 ET
```

---

## 7. Flow 5: Phase Cutover

```mermaid
sequenceDiagram
    actor ML as Migration Lead
    actor DO as Data Owner
    actor ISSO as ISSO
    participant CTRL as Control Plane API
    participant ORCH as Airflow
    participant LEGACY as Legacy System (Siebel)
    participant DBA as DBA
    participant SF as Salesforce GC+
    participant LOAD as Load Service
    participant KAFKA as Kafka
    participant COMMS as Communications Team

    Note over ML,COMMS: T-4h = Friday 22:00 ET — Go/No-Go review

    ML->>CTRL: GET /phases/2/cutover-checklist
    CTRL-->>ML: Checklist status {validation: PASS, reconciliation: PASS, rollback_tested: PASS, data_owner_approval: PENDING, open_p1_issues: 0}

    ML->>DO: "Requesting Phase 2 Data Owner approval — reconciliation report attached"
    DO->>CTRL: POST /phases/2/approve {approver: "lchen@agency.gov", approved: true, notes: "Spot-check of 500 records verified. Approved."}
    CTRL->>KAFKA: Emit PhaseApproved {phase: 2, approver: lchen}

    ML->>ISSO: "Requesting ISSO security confirmation for Phase 2 cutover"
    ISSO->>CTRL: GET /phases/2/security-posture
    CTRL-->>ISSO: {open_p1_security_issues: 0, fedramp_status: COMPLIANT, pii_encryption_verified: true}
    ISSO-->>ML: "Security confirmed. Proceed."

    Note over ML,COMMS: T=01:00 ET — Cutover window opens

    ML->>DBA: "Execute: Freeze Siebel Account writes (activate DB write constraint)"
    DBA->>LEGACY: ALTER TABLE S_ORG_EXT ADD CONSTRAINT no_writes CHECK(1=2) [disables writes]
    LEGACY-->>DBA: Constraint active — writes blocked
    DBA-->>ML: "Siebel Account writes frozen"

    ML->>CTRL: POST /phases/2/cutover/delta-extract-start
    CTRL->>ORCH: Trigger delta_extract DAG (changes since T-72h full extract)
    ORCH->>ORCH: Run delta extraction → transformation → validation
    Note over ORCH: Delta extract: 8,947 records changed in last 72h
    ORCH-->>CTRL: Delta pipeline complete {loaded: 8947, failed: 0}

    ML->>CTRL: GET /phases/2/reconciliation-final
    CTRL-->>ML: {sf_account_count: 2112009, source_account_count: 2112009, match: true, divergence: 0}
    Note over ML: Final reconciliation PASS

    ML->>DO: "Final count: 2,112,009 Accounts in Salesforce. Source count: 2,112,009. Request smoke test sign-off."
    DO->>SF: Manually verify 20 sampled Account records in Salesforce
    DO-->>ML: "20/20 records verified. Smoke test PASS."

    ML->>CTRL: POST /phases/2/cutover/complete {authorized_by: jsmith, time: "2026-03-01T03:47:00Z"}
    CTRL->>KAFKA: Emit CutoverAuthorized {phase: 2, entity: Account, authorized_by: jsmith, sf_count: 2112009}

    ML->>COMMS: "Cutover complete. Salesforce is now system of record for Accounts."
    COMMS->>COMMS: Send user email notification
    COMMS->>COMMS: Post on intranet: "Account data now in Salesforce — legacy read-only"

    Note over ML,COMMS: T=03:47 ET — Cutover complete (2h 47min into 5h window)
    Note over ML,COMMS: 72-hour hypercare monitoring period begins
```

---

## 8. Flow 6: Operator Authentication & Authorization

```mermaid
sequenceDiagram
    actor OP as Migration Engineer
    participant BROWSER as Browser (SPA)
    participant ALB as AWS ALB + WAF
    participant OKTA as Okta IdP (PIV/CAC)
    participant CTRL as Control Plane API
    participant OPA as OPA Sidecar
    participant VAULT as HashiCorp Vault

    OP->>BROWSER: Navigate to https://lsmp.agency.gov
    BROWSER->>ALB: GET /
    ALB->>CTRL: Forward request (no JWT — unauthenticated)
    CTRL-->>ALB: 302 Redirect to Okta SAML login
    ALB-->>BROWSER: 302 → Okta login page

    BROWSER->>OKTA: GET /sso/saml2/{org_id}
    OKTA-->>BROWSER: Login page (PIV/CAC prompt)

    Note over OP,OKTA: Operator inserts PIV card and enters PIN
    OP->>OKTA: PIV card authentication (X.509 cert on card)
    OKTA->>OKTA: Validate X.509 cert chain → verify against agency cert authority
    OKTA->>OKTA: Map cert subject (CN=John Smith, UID=jsmith@agency.gov) to Okta user
    OKTA->>OKTA: Check LSMP app assignment — user has role=migration_engineer
    OKTA-->>BROWSER: SAML assertion (signed, contains: email, roles, exp=T+8h)

    BROWSER->>CTRL: POST /auth/saml/callback (SAML assertion)
    CTRL->>CTRL: Validate SAML assertion signature (Okta public cert)
    CTRL->>CTRL: Extract claims: sub=jsmith@agency.gov, roles=[migration_engineer]
    CTRL->>VAULT: GET secret/lsmp/jwt-signing-key (via transit engine — sign, not read)
    VAULT-->>CTRL: (JWT signing handled by Vault transit engine)
    CTRL->>CTRL: Issue JWT {sub: jsmith, role: migration_engineer, exp: T+8h, iat: now, jti: uuid}
    CTRL-->>BROWSER: Set-Cookie: access_token=JWT (HttpOnly, Secure, SameSite=Strict)
    BROWSER-->>OP: Dashboard rendered

    Note over OP,OPA: Subsequent API call: trigger extraction job

    OP->>BROWSER: Click "Start Account Extraction"
    BROWSER->>ALB: POST /api/jobs {entity_type: Account, ...} + Cookie: access_token=JWT
    ALB->>ALB: WAF evaluation — no malicious patterns detected
    ALB->>CTRL: Forward request

    CTRL->>OPA: POST /v1/data/lsmp/authz {input: {token: JWT, action: create_job, entity_type: Account, role: migration_engineer}}
    OPA->>OPA: Verify JWT signature (Vault transit verify)
    OPA->>OPA: Check expiry — valid
    OPA->>OPA: Evaluate policy: migration_engineer CAN create_job → ALLOW
    OPA-->>CTRL: {allow: true, operator_id: jsmith, role: migration_engineer}

    CTRL->>CTRL: Process job creation request
    CTRL-->>BROWSER: 201 Created
    BROWSER-->>OP: Job created confirmation

    Note over OP,OPA: Unauthorized action attempt

    OP->>BROWSER: Click "Initiate Rollback" (requires migration_lead role)
    BROWSER->>ALB: POST /api/rollback/... + JWT
    ALB->>CTRL: Forward
    CTRL->>OPA: Evaluate: migration_engineer CAN initiate_rollback?
    OPA->>OPA: Policy check: initiate_rollback requires migration_lead → DENY
    OPA-->>CTRL: {allow: false, reason: "insufficient_role"}
    CTRL->>KAFKA: Emit AccessDenied {operator: jsmith, action: initiate_rollback, reason: insufficient_role}
    CTRL-->>BROWSER: 403 Forbidden {error: "You do not have permission to initiate rollback."}
    BROWSER-->>OP: Error message displayed
```

---

## 9. Flow 7: Salesforce Governor Limit Backpressure

```mermaid
sequenceDiagram
    participant LOAD as Load Service
    participant SF as Salesforce GC+
    participant GOV as Governor Monitor
    participant ORCH as Airflow
    participant CTRL as Control Plane API
    participant KAFKA as Kafka

    Note over LOAD,KAFKA: Load in progress — Case batch (8.2M records)

    loop Every 50 Bulk API batches
        LOAD->>SF: GET /services/data/v60.0/limits
        SF-->>LOAD: {"DailyBulkV2QueryFileStorageMB": {...}, "DailyApiRequests": {"Max": 150000000, "Remaining": 28500000}}
        LOAD->>GOV: CheckLimits {api_remaining: 28500000, api_max: 150000000, usage_pct: 81.0}
        GOV->>GOV: Evaluate: usage_pct (81%) > threshold (80%) → LIMIT_APPROACHING
    end

    GOV->>LOAD: PauseBulkLoading {reason: "API_LIMIT_APPROACHING", usage_pct: 81.0, estimated_reset_hours: 5.2}
    LOAD->>ORCH: POST /tasks/{task_id}/status {status: PAUSED, reason: API_LIMIT}
    LOAD->>KAFKA: Emit LoadPaused {batch_id, reason: API_LIMIT_APPROACHING, progress_pct: 73, records_loaded: 5989000, records_remaining: 2211000}

    CTRL->>CTRL: Create P2 alert: "Load paused — API limit approaching"
    Note over CTRL: PagerDuty alert fires to on-call engineer

    Note over LOAD,KAFKA: Wait for Salesforce daily API window reset (midnight PST = 03:00 ET)

    GOV->>GOV: Scheduled check at 03:00 ET
    GOV->>SF: GET /services/data/v60.0/limits
    SF-->>GOV: {"DailyApiRequests": {"Max": 150000000, "Remaining": 150000000}}
    Note over GOV: API counter reset! Remaining: 150,000,000 (100%)

    GOV->>LOAD: ResumeBulkLoading {reason: "API_LIMIT_RESET", api_remaining: 150000000}
    LOAD->>KAFKA: Emit LoadResumed {batch_id, records_remaining: 2211000}
    LOAD->>ORCH: POST /tasks/{task_id}/status {status: IN_PROGRESS}

    Note over LOAD,KAFKA: Load resumes from checkpoint (record 5,989,001)
    Note over LOAD,KAFKA: Migration window extended — alternate Saturday used for remainder
```

---

## 10. Flow 8: Orphan Record Resolution

```mermaid
sequenceDiagram
    actor DS as Data Steward
    participant LOAD as Load Service
    participant CTRL as Control Plane API
    participant DB as Config DB (orphan_records)
    participant SF as Salesforce GC+
    participant KAFKA as Kafka

    Note over LOAD,KAFKA: During Contact load — parent Account reference unresolvable

    LOAD->>DB: SELECT sf_id FROM id_mapping WHERE entity_type='Account' AND legacy_id='1-XYZ999'
    DB-->>LOAD: (no rows) — Account 1-XYZ999 not in Salesforce
    Note over LOAD: Account may have been deduped out, or was filtered as inactive

    LOAD->>DB: INSERT INTO orphan_records (entity_type='Contact', legacy_id='C-12345', parent_type='Account', parent_legacy_id='1-XYZ999', batch_id='batch-2026031601-contact', quarantine_reason='PARENT_NOT_FOUND')
    LOAD->>KAFKA: Emit OrphanRecordQuarantined {entity: Contact, legacy_id: C-12345, parent: 1-XYZ999}

    Note over LOAD,KAFKA: Contact is skipped from current load — written to quarantine, not to Salesforce

    Note over DS: Data Steward receives daily orphan report

    CTRL->>DS: Email: "47 Contact records quarantined in batch-2026031601-contact — review required"
    DS->>CTRL: GET /orphans?batch_id=batch-2026031601-contact
    CTRL-->>DS: [{entity: Contact, legacy_id: C-12345, parent_legacy_id: 1-XYZ999, reason: PARENT_NOT_FOUND}, ...]

    DS->>DS: Investigate: Account 1-XYZ999 was deduped and merged into Account 1-XYZ001 during Phase 2
    DS->>CTRL: POST /orphans/C-12345/resolve {action: REMAP_PARENT, new_parent_legacy_id: "1-XYZ001", notes: "Merged account — redirect to surviving account"}
    CTRL->>DB: UPDATE orphan_records SET resolved=true, resolved_by=dsteward, notes=... WHERE orphan_id=...

    CTRL->>SF: SELECT Id FROM Account WHERE Legacy_Account_ID__c = '1-XYZ001'
    SF-->>CTRL: {Id: "0010R00000ABCDE123"}

    CTRL->>CTRL: Create micro-batch load for resolved orphans (when count > 10 or daily batch)
    CTRL->>LOAD: POST /load {entity_type: Contact, records: [{legacy_id: C-12345, AccountId: "0010R00000ABCDE123", ...}], operation: UPSERT}
    LOAD->>SF: Bulk API 2.0 upsert (1 record batch)
    SF-->>LOAD: {loaded: 1, failed: 0}
    LOAD->>DB: UPDATE id_mapping (entity: Contact, legacy_id: C-12345, sf_id: "0030R00000ZYXWV987")
    LOAD->>KAFKA: Emit OrphanRecordResolved {entity: Contact, legacy_id: C-12345, sf_id: "0030R00000ZYXWV987"}
```

---

## 11. Kafka Topic Map

| Topic | Partitions | Retention | Producers | Consumers | Message Schema |
|---|---|---|---|---|---|
| `lsmp.audit.events` | 12 | 72 hours | All services | Audit Logger | `AuditEvent` JSON |
| `lsmp.job.lifecycle` | 6 | 72 hours | Airflow, Control Plane | Audit Logger, Control Plane | `JobLifecycleEvent` JSON |
| `lsmp.cdc.pgdb.case_header` | 6 | 24 hours | Debezium | Extraction Service | Debezium CDC envelope |
| `lsmp.cdc.pgdb.case_detail` | 6 | 24 hours | Debezium | Extraction Service | Debezium CDC envelope |
| `lsmp.cdc.pgdb.case_comment` | 12 | 24 hours | Debezium | Extraction Service | Debezium CDC envelope |
| `lsmp.load.commands` | 6 | 24 hours | Airflow | Load Service | `LoadCommand` JSON |
| `lsmp.audit.events.dlq` | 3 | 7 days | Audit Logger (on failure) | SRE on-call | Dead letter envelope |
| `lsmp.load.commands.dlq` | 3 | 7 days | Load Service (on failure) | SRE on-call | Dead letter envelope |

---

*Document maintained in Git at `architecture/event_flows.md`. Updated when new business flows are implemented or existing flows change significantly. Sequence diagrams are kept in sync with actual service behavior via integration tests.*
