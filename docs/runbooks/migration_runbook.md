# Migration Execution Runbook

**Version:** 2.4.1
**Last Updated:** 2025-12-01
**Owner:** Migration Operations Team
**Audience:** Migration Engineers, Senior Operations Staff
**Classification:** INTERNAL — Contains operational procedures. Do not distribute to clients.

---

## Table of Contents

1. [Runbook Overview](#1-runbook-overview)
2. [SLA Definitions](#2-sla-definitions)
3. [Pre-Migration Checklist](#3-pre-migration-checklist)
4. [Migration Execution Steps](#4-migration-execution-steps)
5. [Monitoring During Migration](#5-monitoring-during-migration)
6. [Pause and Resume Procedures](#6-pause-and-resume-procedures)
7. [Rollback Procedures](#7-rollback-procedures)
8. [Common Failure Scenarios and Remediation](#8-common-failure-scenarios-and-remediation)
9. [Communication Templates](#9-communication-templates)
10. [Post-Migration Validation](#10-post-migration-validation)

---

## 1. Runbook Overview

This runbook covers the end-to-end execution of a Legacy-to-Salesforce data migration using the migration platform. It is designed for use by migration engineers with at least 3 months of platform experience.

**Prerequisite Reading:**
- [ADR-003: Event-Driven Architecture](../../architecture/decisions/ADR-003-event-driven-architecture.md)
- [ADR-005: Data Transformation Strategy](../../architecture/decisions/ADR-005-data-transformation-strategy.md)
- Platform Access Guide (internal wiki)

**Tools Required:**
- `kubectl` configured for the production cluster
- `migration-cli` installed and authenticated (`migration-cli auth login`)
- PagerDuty account with migration-team access
- Salesforce Workbench access (read-only, for validation queries)
- VPN connection if accessing on-premises legacy systems

**Estimated Duration by Migration Size:**

| Record Count | Estimated Duration | Recommended Window |
|-------------|-------------------|-------------------|
| < 1M | 2–6 hours | Business hours |
| 1M–10M | 6–24 hours | Overnight or weekend |
| 10M–50M | 1–5 days | Extended maintenance window |
| > 50M | 5–30 days | Phased migration approach |

---

## 2. SLA Definitions

| SLA | Definition | Target |
|-----|-----------|--------|
| Migration Start | Time from scheduled start to first record extracted | < 15 minutes |
| Throughput (Extract) | Records extracted per minute at steady state | > 50,000 rec/min |
| Throughput (Load) | Records loaded to Salesforce per minute | > 3,000 rec/min (Bulk API limit-dependent) |
| DLQ Response | AI triage begins after DLQ threshold breach | < 5 minutes |
| Human DLQ Review | Engineer reviews AI remediation and approves/rejects | < 2 hours |
| Incident Response (P1) | Engineer engaged and investigating | < 15 minutes |
| Migration Completion Report | Delivered after completion | < 4 hours |
| Rollback Initiation | From decision to rollback to first rollback action | < 30 minutes |
| Rollback Completion | All Salesforce changes reverted | < 2× migration duration |
| Data Retention (Audit) | Kafka extraction events retained | 7 years |

---

## 3. Pre-Migration Checklist

Complete all items before clicking "Start Migration". Each item should be checked off with initials and timestamp. This checklist must be saved in the migration job ticket.

**The checklist is divided into sections. All CRITICAL items are blocking — migration cannot start until resolved.**

### 3.1 Access and Authorization (D-5 to D-1)

- [ ] **[CRITICAL]** Migration job created in platform: `migration-cli job create --config migration-job.yaml` and status is `READY`
- [ ] **[CRITICAL]** Client data processing agreement (DPA/BAA) signed and filed in contract management system
- [ ] **[CRITICAL]** Change management ticket approved by client change advisory board (CAB) with maintenance window confirmed
- [ ] **[CRITICAL]** Salesforce org sandbox validation completed — all transformation rules tested against sandbox data
- [ ] **[CRITICAL]** Rollback plan reviewed and approved by client technical lead (documented in Section 7 of this runbook)
- [ ] **[CRITICAL]** On-call engineer assigned for migration duration; PagerDuty rotation updated
- [ ] Legacy system read credentials verified: `migration-cli validate-source --job-id {JOB_ID}`
- [ ] Salesforce connected app OAuth credentials verified: `migration-cli validate-target --job-id {JOB_ID}`
- [ ] Platform team has Salesforce org admin access for emergency operations (temporary, time-bounded access)

### 3.2 Infrastructure Readiness (D-2 to D-1)

- [ ] **[CRITICAL]** Kafka cluster health verified: all brokers in ISR, no under-replicated partitions
  ```bash
  kubectl exec -n platform-kafka kafka-0 -- \
      kafka-topics.sh --bootstrap-server localhost:9092 \
      --describe --topic prod.{tenant_id}.migration.{job_id}.extracted 2>/dev/null || \
      echo "Topics not yet created — will be created at migration start"

  # Check broker health
  kubectl exec -n platform-kafka kafka-0 -- \
      kafka-broker-api-versions.sh --bootstrap-server localhost:9092 | head -5
  ```
- [ ] **[CRITICAL]** Vault is unsealed and healthy: `curl -s https://vault.vault.svc.cluster.local:8200/v1/sys/health | jq '.sealed'` returns `false`
- [ ] **[CRITICAL]** SPIRE server healthy, all agent SVIDs current: `kubectl exec -n spire spire-server-0 -- spire-server healthcheck`
- [ ] Tenant namespace verified: `kubectl get namespace tenant-{tenant_id}-app`
- [ ] ResourceQuota headroom verified — migration pods will fit:
  ```bash
  kubectl describe resourcequota -n tenant-{tenant_id}-app | grep -A2 "requests.cpu\|requests.memory"
  ```
- [ ] Schema Registry connectivity from transformation namespace:
  ```bash
  kubectl run schema-test --rm -it --image=curlimages/curl:8.5.0 \
      -n tenant-{tenant_id}-app --restart=Never -- \
      curl -s https://schema-registry.platform-kafka.svc.cluster.local:8081/subjects | head -c 200
  ```
- [ ] Salesforce API limits checked — not within 20% of daily limit:
  ```bash
  migration-cli salesforce api-limits --job-id {JOB_ID}
  # Output should show: Daily API Calls: XXXX/50000 (XX%)
  # Alert if > 80% consumed before migration start
  ```
- [ ] Disk space on Kafka brokers: minimum 200GB free per broker
  ```bash
  for i in 0 1 2 3 4 5; do
      echo "kafka-$i:"; kubectl exec -n platform-kafka kafka-$i -- df -h /var/kafka-data | tail -1
  done
  ```

### 3.3 Data Preparation (D-5 to D-1)

- [ ] **[CRITICAL]** Source data profiling report reviewed and accepted by client: anomaly counts reviewed, expected rejection rates agreed
- [ ] **[CRITICAL]** Transformation rules approved: rule set version matches what was tested in sandbox
  ```bash
  migration-cli rules verify --job-id {JOB_ID}
  # Expected output: Rule set oracle-ebs-to-sf-account-v3 v3.2.1 — APPROVED ✓
  ```
- [ ] **[CRITICAL]** External ID field exists in Salesforce target org: `Legacy_ID__c` field present on Account, Contact, etc.
- [ ] **[CRITICAL]** Salesforce picklist values verified — all legacy enum values have a mapping:
  ```bash
  migration-cli validate-lookups --job-id {JOB_ID} --strict
  # Any UNMAPPED values must be resolved before migration
  ```
- [ ] Duplicate detection pre-check: confirm no existing Salesforce records match the External IDs we will migrate:
  ```bash
  migration-cli duplicate-scan \
      --job-id {JOB_ID} \
      --sample-size 10000 \
      --fail-on-duplicates
  ```
- [ ] Referential integrity scan: quantify orphan records, confirm disposition (migrate orphans? exclude? remediate?):
  ```bash
  migration-cli referential-scan \
      --job-id {JOB_ID} \
      --output orphan-report.json
  cat orphan-report.json | jq '.total_orphans, .by_entity'
  ```
- [ ] Large object (LOB) fields assessed: binary/BLOB columns handled separately or excluded from migration scope confirmation
- [ ] Record count baseline documented (for completeness validation post-migration):
  ```bash
  migration-cli record-counts \
      --job-id {JOB_ID} \
      --save-baseline baseline-counts-$(date +%Y%m%d).json
  ```

### 3.4 Monitoring Setup (D-1)

- [ ] Grafana migration dashboard loaded and bookmarked: `https://grafana.internal/d/migration-pipeline/`
- [ ] PagerDuty alert routing verified — migration job ID in alert context:
  ```bash
  migration-cli configure-alerts \
      --job-id {JOB_ID} \
      --pagerduty-service-key {PD_KEY} \
      --oncall-engineer {ENGINEER_EMAIL}
  ```
- [ ] Alert thresholds configured:
  ```bash
  migration-cli set-thresholds \
      --job-id {JOB_ID} \
      --error-rate-warn 0.5 \
      --error-rate-crit 2.0 \
      --dlq-depth-warn 100 \
      --dlq-depth-crit 1000 \
      --throughput-warn 1000 \
      --stall-timeout-minutes 30
  ```
- [ ] Client technical contact confirmed as available during migration window
- [ ] Slack channel `#migration-{job_id}` created with client technical lead, on-call engineer, and escalation contacts

### 3.5 Client Communication (D-1)

- [ ] Migration start confirmation sent to client (use Template A in Section 9)
- [ ] Client has confirmed Salesforce org is in maintenance mode or users notified of read-only period (if applicable)
- [ ] Salesforce system administrator confirmed as available during migration window
- [ ] Emergency stop procedure reviewed with client technical lead (they may request a stop at any time via `#migration-{job_id}` Slack or on-call phone)

---

## 4. Migration Execution Steps

### Step 1: Start Migration Job

```bash
# Verify job is in READY state
migration-cli job status --job-id {JOB_ID}
# Expected: status: READY, config_validated: true, pre_checks: PASSED

# Start the migration
migration-cli job start \
    --job-id {JOB_ID} \
    --operator-id {YOUR_EMAIL} \
    --confirm

# Expected output:
# Migration job mig-20251201-abc started at 2025-12-01T22:00:00Z
# Extraction phase beginning...
# Monitor at: https://grafana.internal/d/migration-pipeline/?jobId=mig-20251201-abc
```

### Step 2: Verify Extraction Phase Started

Within 5 minutes of starting, confirm extraction is producing records:

```bash
# Check extraction topic for messages
migration-cli kafka offset \
    --job-id {JOB_ID} \
    --topic extracted \
    --output summary

# Expected: offset advancing, records > 0

# Check extraction service logs
kubectl logs \
    -n tenant-{tenant_id}-app \
    -l app=extraction-service,job-id={JOB_ID} \
    --since=5m | tail -50

# Look for: "Extraction started", "Batch X extracted: N records"
# Red flags: "Connection refused", "ORA-12154", "Authentication failed"
```

### Step 3: Verify Transformation Phase

After extraction begins (usually within 2 minutes), confirm transformation is consuming and producing:

```bash
# Check consumer group lag for transformation
migration-cli kafka lag \
    --job-id {JOB_ID} \
    --consumer-group migration-transformer

# Healthy: lag < 100,000 and decreasing or stable
# Concern: lag > 500,000 or continuously growing

# Check transformation throughput
migration-cli metrics throughput \
    --job-id {JOB_ID} \
    --stage transformation \
    --window 5m

# Expected: > 10,000 records/min
```

### Step 4: Verify Loading Phase

After transformation builds a buffer (usually 5–10 minutes after extraction starts):

```bash
# Check Salesforce loading is active
migration-cli metrics throughput \
    --job-id {JOB_ID} \
    --stage loading \
    --window 5m

# Expected: > 1,000 records/min (Salesforce rate limits apply)

# Verify records appearing in Salesforce (spot check)
migration-cli salesforce spot-check \
    --job-id {JOB_ID} \
    --sample-size 10 \
    --object Account

# Expected: 10 records found in Salesforce matching expected field values
```

### Step 5: Monitor Steady State

Once all three phases are running, monitor via dashboard and check in every 30 minutes:

```bash
# Comprehensive job status summary
migration-cli job status \
    --job-id {JOB_ID} \
    --verbose

# Example healthy output:
# Job ID: mig-20251201-abc
# Status: RUNNING
# Duration: 2h 14m
# Phase: LOADING (extraction 100%, transformation 98%, loading 71%)
# Records Extracted: 2,847,391 / 2,847,391 (100%)
# Records Transformed: 2,789,044 / 2,847,391 (97.9%) — 58,347 in DLQ
# Records Loaded: 2,023,847 / 2,789,044 (72.6%)
# DLQ Depth: 847 (AI triage: 3 categories identified, 1 pending human review)
# ETA: 1h 42m
# Error Rate: 0.21% (within threshold)
# Throughput: 4,212 records/min (loading)
```

### Step 6: Handle DLQ as Needed

See [Common Failure Scenarios](#8-common-failure-scenarios-and-remediation) for specific DLQ patterns.

```bash
# Review DLQ triage results
migration-cli dlq triage-results \
    --job-id {JOB_ID} \
    --pending-approval

# Approve a batch remediation
migration-cli dlq approve-remediation \
    --job-id {JOB_ID} \
    --remediation-id {REMEDIATION_ID} \
    --operator-id {YOUR_EMAIL} \
    --reason "Verified: 892 duplicate records are pre-existing SF records from manual data entry"

# Reject and escalate a remediation
migration-cli dlq reject-remediation \
    --job-id {JOB_ID} \
    --remediation-id {REMEDIATION_ID} \
    --reason "AI recommendation incorrect — consulting client on correct disposition"
```

### Step 7: Migration Completion

When extraction, transformation, and loading are all 100%, the job moves to `COMPLETING` state:

```bash
# Wait for completion
migration-cli job wait-complete \
    --job-id {JOB_ID} \
    --timeout 4h

# Run final completeness check
migration-cli validate-completion \
    --job-id {JOB_ID} \
    --baseline-file baseline-counts-{DATE}.json

# Expected output: all object counts within 0.1% tolerance (accounting for expected rejections)
```

### Step 8: Generate Migration Report

```bash
# Trigger report generation (AI Documentation Agent)
migration-cli report generate \
    --job-id {JOB_ID} \
    --recipient-emails "client@org.gov,your-email@company.com" \
    --format pdf,html

# Report will be available within 10 minutes at:
# https://reports.migration-platform.internal/jobs/{JOB_ID}/completion-report.pdf
```

---

## 5. Monitoring During Migration

### 5.1 Key Metrics to Watch

| Metric | Healthy Range | Action If Outside Range |
|--------|--------------|------------------------|
| Extraction throughput | 30,000–100,000 rec/min | See [Scenario 3](#scenario-3-extraction-performance-degradation) |
| Loading throughput | 1,000–5,000 rec/min | See [Scenario 4](#scenario-4-salesforce-api-rate-limiting) |
| Consumer group lag (transformation) | < 500,000 records | See [Scenario 5](#scenario-5-transformation-lag-building) |
| DLQ depth | < 1,000 records | Review AI triage, approve remediation |
| Error rate | < 2% | If > 2%: alert fires; > 5%: consider pause |
| Kafka broker CPU | < 70% | > 80% sustained: notify Kafka admin |
| Kafka disk usage | < 70% per broker | > 80%: reduce retention or add disk |

### 5.2 Monitoring Commands (Quick Reference)

```bash
# Real-time log stream for all migration services
kubectl logs \
    -n tenant-{tenant_id}-app \
    -l job-id={JOB_ID} \
    --follow \
    --prefix=true \
    2>&1 | grep -E "(ERROR|WARN|BATCH_COMPLETE|RATE_LIMIT)"

# Kafka consumer lag across all groups
kafka-consumer-groups.sh \
    --bootstrap-server kafka.platform-kafka.svc.cluster.local:9092 \
    --describe \
    --group migration-transformer-{JOB_ID} \
    --group migration-loader-{JOB_ID}

# Salesforce API usage (check we're not hitting governor limits)
watch -n 60 'migration-cli salesforce api-limits --job-id {JOB_ID}'

# Pod resource usage
kubectl top pods \
    -n tenant-{tenant_id}-app \
    --sort-by=memory \
    | grep -E "(extraction|transformation|loading)"

# Recent Vault token renewals (confirms Dynamic credentials working)
vault audit list -detailed 2>/dev/null | grep "migration" | tail -20

# SPIRE SVID expiry check
kubectl exec -n spire spire-server-0 -- \
    spire-server entry show \
    -spiffeID "spiffe://migration-platform.internal/ns/tenant-{tenant_id}-app/sa/loading-service/writer" \
    2>/dev/null | grep "Expiry"
```

### 5.3 Grafana Dashboard Panels

Key panels on the migration dashboard (https://grafana.internal/d/migration-pipeline/):

1. **Migration Progress** — Stacked bar: extracted / transformed / loaded / rejected as % of total
2. **Pipeline Throughput** — Lines for extraction, transformation, loading rec/sec
3. **Consumer Lag** — Time series of Kafka consumer group lag per stage
4. **DLQ Depth** — Time series with threshold lines
5. **Salesforce API Usage** — Gauge showing % of daily API limit consumed
6. **Error Rate by Category** — Stacked area chart by error code
7. **End-to-End Latency** — P50/P95/P99 latency from extraction to Salesforce confirmation

---

## 6. Pause and Resume Procedures

### 6.1 Planned Pause (e.g., Salesforce maintenance window)

```bash
# Pause loading only (extraction and transformation continue, buffering in Kafka)
migration-cli job pause-loading \
    --job-id {JOB_ID} \
    --reason "Salesforce scheduled maintenance 02:00-04:00 UTC" \
    --operator-id {YOUR_EMAIL}

# Resume loading after maintenance window
migration-cli job resume-loading \
    --job-id {JOB_ID} \
    --operator-id {YOUR_EMAIL}

# Pause all stages (complete pause — Kafka retains position)
migration-cli job pause \
    --job-id {JOB_ID} \
    --reason "Client requested pause for business review" \
    --operator-id {YOUR_EMAIL}

# Resume all stages
migration-cli job resume \
    --job-id {JOB_ID} \
    --operator-id {YOUR_EMAIL}
```

### 6.2 Emergency Pause

If an issue is detected that requires immediate stop:

```bash
# Emergency stop — fastest possible halt
migration-cli job emergency-stop \
    --job-id {JOB_ID} \
    --operator-id {YOUR_EMAIL} \
    --reason "Describe reason here"

# This immediately:
# 1. Scales loading-service replicas to 0
# 2. Scales transformation-service replicas to 0
# 3. Scales extraction-service replicas to 0
# 4. All Kafka offsets are preserved — no data loss
# 5. Current position is checkpointed

# After resolving the issue, resume:
migration-cli job resume \
    --job-id {JOB_ID} \
    --operator-id {YOUR_EMAIL} \
    --confirm-issue-resolved "Describe resolution"
```

### 6.3 Verifying Pause State

```bash
# Confirm all pods scaled to 0
kubectl get pods -n tenant-{tenant_id}-app -l job-id={JOB_ID}
# Expected: No resources found

# Confirm Kafka consumer offsets are not advancing (consumers are gone)
migration-cli kafka lag --job-id {JOB_ID} --consumer-group migration-loader
# Expected: lag = N (static, not changing)
```

---

## 7. Rollback Procedures

**Important:** Rollback of a migration means deleting records from Salesforce that were created or updated during the migration. This is a destructive operation and cannot recover data that was overwritten (if records pre-existed in Salesforce and were updated). Rollback must be explicitly authorized by the client and documented in the change ticket.

### 7.1 Pre-Rollback Assessment

Before initiating rollback, assess:

```bash
# How many records were loaded? What is the rollback scope?
migration-cli rollback assess \
    --job-id {JOB_ID} \
    --object Account,Contact,Opportunity

# Expected output:
# Rollback scope for mig-20251201-abc:
# Account: 892,341 records created (can be deleted)
# Account: 12,847 records updated (CANNOT ROLL BACK — previous values not captured)
# Contact: 1,247,891 records created (can be deleted)
# Warning: 12,847 Account records were updates; pre-migration values are not available.
# Pre-migration backup available: NO (not configured for this job)
```

**Critical Warning:** If Salesforce records were UPDATED (not created), rollback cannot restore the pre-migration values unless a Salesforce export/backup was taken before migration start. This is why the pre-migration backup step is in the checklist.

### 7.2 Rollback Execution

```bash
# Initiate rollback (requires dual authorization for government clients)
migration-cli job rollback \
    --job-id {JOB_ID} \
    --operator-id {YOUR_EMAIL} \
    --approver-id {SECOND_APPROVER_EMAIL} \  # Required for Tier 1/2 clients
    --confirm \
    --reason "Rollback requested by client due to data quality concerns"

# Rollback proceeds by:
# 1. Pausing any remaining loading
# 2. Reading all .loaded Kafka events for this job (the record of what was created)
# 3. Issuing Salesforce Bulk API delete for all created records
# 4. Issuing Salesforce Bulk API restore for all updated records (from backup if available)

# Monitor rollback progress
migration-cli rollback status \
    --job-id {JOB_ID} \
    --watch

# Example output:
# Rollback Status: RUNNING
# Records to delete: 2,140,232
# Records deleted: 847,291 (39.6%)
# Records to restore: 12,847
# Records restored: 12,847 (100%)
# ETA: 47 minutes
```

### 7.3 Post-Rollback Validation

```bash
# Verify rollback completeness
migration-cli rollback verify \
    --job-id {JOB_ID}

# This queries Salesforce for any remaining records with Legacy_ID__c values
# from the migration job. A clean rollback returns 0 records.

# If records remain (incomplete rollback):
migration-cli rollback resume \
    --job-id {JOB_ID} \
    --skip-completed
```

---

## 8. Common Failure Scenarios and Remediation

### Scenario 1: Salesforce Duplicate Value Error

**Symptom:** DLQ accumulating `SF_DUPLICATE_VALUE` errors; AI triage categorizes as duplicate External ID.

**Root Cause:** Records with the same `Legacy_ID__c` already exist in Salesforce from a previous partial migration or manual data entry.

**Remediation:**
```bash
# Investigate the duplicates
migration-cli dlq investigate \
    --job-id {JOB_ID} \
    --error-code SF_DUPLICATE_VALUE \
    --sample 20

# Option A: Skip these records (they already exist in SF)
migration-cli dlq resolve \
    --job-id {JOB_ID} \
    --error-code SF_DUPLICATE_VALUE \
    --action SKIP \
    --reason "Records already exist in Salesforce from previous partial load" \
    --operator-id {YOUR_EMAIL}

# Option B: Overwrite with migrated data (use with caution!)
# This changes the job's upsert to OVERWRITE mode for these records
migration-cli dlq resolve \
    --job-id {JOB_ID} \
    --error-code SF_DUPLICATE_VALUE \
    --action UPSERT_OVERWRITE \
    --reason "Client confirmed: overwrite Salesforce records with migrated values" \
    --operator-id {YOUR_EMAIL}
    --requires-client-approval
```

### Scenario 2: Salesforce Validation Rule Rejection

**Symptom:** `FIELD_CUSTOM_VALIDATION_EXCEPTION` errors in DLQ.

**Root Cause:** A Salesforce custom validation rule is rejecting records that pass schema validation but fail business logic.

**Investigation:**
```bash
# Get the full Salesforce error message
migration-cli dlq sample \
    --job-id {JOB_ID} \
    --error-code FIELD_CUSTOM_VALIDATION_EXCEPTION \
    --show-sf-error-message

# The Salesforce error message will indicate WHICH validation rule failed.
# Example: "Government_Contract_Number__c is required when Account Type is Government"

# Find the affected transformation rule
migration-cli rules lookup \
    --field Account.Type \
    --job-id {JOB_ID}
```

**Remediation Options:**
1. Add the missing field to the transformation rules (requires rule update + approval)
2. Temporarily disable the Salesforce validation rule during migration (requires client SA approval)
3. Exclude affected records and migrate them manually after rule remediation

```bash
# If updating transformation rules mid-migration (requires replay):
migration-cli rules update \
    --job-id {JOB_ID} \
    --rule-set oracle-ebs-to-sf-account-v3 \
    --new-version 3.2.2 \
    --changes "Added Government_Contract_Number__c mapping from LEGACY_CONTRACT_REF field"

# This will:
# 1. Replay DLQ records through the new rule version
# 2. New extraction events go through new rules automatically
# 3. Does NOT re-extract from legacy system (uses Kafka replay)
```

### Scenario 3: Extraction Performance Degradation

**Symptom:** Extraction throughput drops below 5,000 rec/min; consumer lag on transformation increasing rapidly.

**Investigation:**
```bash
# Check extraction service logs for slow queries
kubectl logs -n tenant-{tenant_id}-app \
    -l app=extraction-service,job-id={JOB_ID} \
    --since=10m | grep -E "(SLOW_QUERY|timeout|ORA-|deadlock)"

# Check Oracle (or source DB) query performance
# (Requires DBA access to source system)
# Connect to Oracle and run:
SELECT sql_id, elapsed_time/executions/1000 as avg_ms, executions, sql_text
FROM v$sql
WHERE sql_text LIKE '%HZ_PARTIES%'
AND elapsed_time/executions/1000 > 1000
ORDER BY elapsed_time/executions DESC
FETCH FIRST 10 ROWS ONLY;
```

**Common Causes and Fixes:**

| Cause | Fix |
|-------|-----|
| Legacy DB query plan change after stats update | Work with DBA to pin query plan or add hints |
| Legacy DB under load from other batch jobs | Reduce extraction parallelism: `migration-cli job set-parallelism --extraction 2` |
| Network bandwidth saturation | Check network utilization; move extraction to off-peak hours |
| Kafka broker disk I/O saturated | Check Kafka broker disk metrics; increase broker count |
| Debezium CDC lag (for real-time sources) | Check Debezium connector status; increase tasks.max |

```bash
# Reduce extraction parallelism immediately to reduce DB load
migration-cli job set-parallelism \
    --job-id {JOB_ID} \
    --extraction 2 \
    --reason "Source DB under load — DBA requested throttling"
```

### Scenario 4: Salesforce API Rate Limiting

**Symptom:** Loading throughput drops to 0 or near-0; errors: `REQUEST_LIMIT_EXCEEDED` or `TXN_SECURITY_NO_ACCESS`.

**Investigation:**
```bash
# Check Salesforce API limit status
migration-cli salesforce api-limits --job-id {JOB_ID}
# If DailyApiRequests is > 95%: this is the cause

# Check Bulk API batch status
migration-cli salesforce bulk-status --job-id {JOB_ID} | tail -20
```

**Remediation:**
```bash
# If hitting 24-hour API limit: pause loading, wait for limit reset (midnight Pacific)
migration-cli job pause-loading \
    --job-id {JOB_ID} \
    --reason "SF API daily limit reached; resuming at limit reset" \
    --resume-at "2025-12-02T08:00:00Z"  # UTC midnight Pacific = 08:00 UTC

# If hitting Bulk API concurrent batch limit:
# Reduce batch concurrency (Salesforce allows 15 concurrent bulk jobs by default)
migration-cli job set-parallelism \
    --job-id {JOB_ID} \
    --loading-concurrency 5  # Reduce from 15 to 5 concurrent bulk batches
```

### Scenario 5: Transformation Lag Building

**Symptom:** Transformation consumer lag exceeds 1M records and continues growing; extraction is outpacing transformation.

**Investigation:**
```bash
# Check transformation service resource usage
kubectl top pods -n tenant-{tenant_id}-app -l app=transformation-service,job-id={JOB_ID}
# If CPU is at 100% limit: need to scale up or increase limits

# Check for slow transformations
kubectl logs -n tenant-{tenant_id}-app \
    -l app=transformation-service,job-id={JOB_ID} \
    --since=5m | grep "SLOW_TRANSFORM\|batch_duration_ms"
```

**Remediation:**
```bash
# Scale transformation service replicas
kubectl scale deployment \
    -n tenant-{tenant_id}-app \
    transformation-service-{JOB_ID} \
    --replicas=6  # Increase from 3 to 6

# Note: Cannot exceed Kafka partition count
# Max useful replicas = partition count (default 12)
# Check partition count:
migration-cli kafka partitions --job-id {JOB_ID} --topic extracted
```

### Scenario 6: Migration Job Appears Stalled

**Symptom:** No progress for > 30 minutes; alert: `MIGRATION_STALL`.

See dedicated runbook: [monitoring/runbooks/migration_stall.md](../../monitoring/runbooks/migration_stall.md)

---

## 9. Communication Templates

### Template A: Migration Start Notification

**To:** Client Technical Lead, Client Project Manager
**Subject:** [Migration Platform] Migration Job {JOB_ID} Starting — {MIGRATION_NAME}
**Channel:** `#migration-{JOB_ID}` Slack + Email

```
Subject: Data Migration Starting — {MIGRATION_NAME} [{JOB_ID}]

{CLIENT_NAME} Team,

Your data migration from {SOURCE_SYSTEM} to Salesforce is beginning now.

Migration Details:
- Job ID: {JOB_ID}
- Estimated Records: {RECORD_COUNT}
- Estimated Duration: {DURATION}
- Migration Window: {START_TIME} – {END_TIME} ({TIMEZONE})
- On-Call Engineer: {ENGINEER_NAME} ({ENGINEER_PHONE})

What to Expect:
- Phase 1 (Extraction): Data is read from {SOURCE_SYSTEM}. No changes to legacy data.
- Phase 2 (Transformation): Records are translated to Salesforce format.
- Phase 3 (Loading): Records appear in Salesforce. This phase may take the majority of the window.

Your Action Items:
- During migration: Inform users that Salesforce {AFFECTED_OBJECTS} records may be
  partially visible. Complete records will appear when migration finishes.
- Salesforce administrator {SF_ADMIN_NAME} should be available at {SF_ADMIN_PHONE}
  if we need to make emergency configuration changes.

We will send updates at: start, 50% completion, 100% completion, and upon any issues.

Migration Control: {MIGRATION_DASHBOARD_URL}

{ENGINEER_NAME}
Migration Platform — {COMPANY_NAME}
```

### Template B: Migration Progress Update (50%)

```
Subject: [UPDATE] Data Migration {JOB_ID} — 50% Complete

{CLIENT_NAME} Team,

Quick update on your migration:

Status as of {TIMESTAMP}:
✓ Records Extracted: {EXTRACTED_COUNT} / {TOTAL_COUNT} ({EXTRACT_PCT}%)
✓ Records Transformed: {TRANSFORMED_COUNT} ({TRANSFORM_PCT}%)
● Records in Salesforce: {LOADED_COUNT} ({LOAD_PCT}%)
△ Records Requiring Review: {DLQ_COUNT} ({DLQ_PCT}% — within expected range)

Estimated Completion: {ETA}
Current Status: ON TRACK

No action required from your team at this time.

{ENGINEER_NAME}
```

### Template C: Issue Notification

```
Subject: [ACTION REQUIRED] Data Migration {JOB_ID} — Issue Detected

{CLIENT_NAME} Team,

We have detected an issue with your migration that requires your input.

Issue Description:
{ISSUE_DESCRIPTION}

Impact:
- {IMPACT_DESCRIPTION}
- Migration is currently: {PAUSED | CONTINUING AT REDUCED RATE | STOPPED}

What We Are Doing:
{REMEDIATION_STEPS_IN_PROGRESS}

What We Need From You:
{SPECIFIC_CLIENT_ACTION_REQUIRED}
Required Response By: {RESPONSE_DEADLINE}

If we do not receive a response by {RESPONSE_DEADLINE}, we will
{DEFAULT_ACTION_IF_NO_RESPONSE}.

Please respond to this email or message in #migration-{JOB_ID}.
For urgent issues, call: {ENGINEER_PHONE}

{ENGINEER_NAME}
```

### Template D: Migration Completion

```
Subject: [COMPLETE] Data Migration {JOB_ID} — {MIGRATION_NAME} Successfully Completed

{CLIENT_NAME} Team,

Your data migration has been successfully completed!

Migration Summary:
- Completed: {COMPLETION_TIMESTAMP}
- Duration: {DURATION}
- Records Successfully Migrated to Salesforce: {LOADED_COUNT} ({SUCCESS_PCT}%)
- Records Requiring Manual Review: {DLQ_FINAL_COUNT} ({DLQ_FINAL_PCT}%)
- Records Excluded (below quality threshold): {REJECTED_COUNT}

Next Steps:
1. Please review the attached Migration Completion Report for full details.
2. Your Salesforce administrator should validate a sample of migrated records.
3. Excluded records ({REJECTED_COUNT}) are documented in the report with
   remediation recommendations.
4. Migration data is retained for 7 years per compliance requirements.

Validation Resources:
- Full Report: {REPORT_URL}
- Data Quality Dashboard: {QUALITY_DASHBOARD_URL}
- Excluded Records List: {EXCLUDED_RECORDS_URL} (secure download, 30-day link)

Migration audit trail is available upon request for compliance purposes.

Thank you for your partnership on this migration.

{ENGINEER_NAME}
{COMPANY_NAME}
```

---

## 10. Post-Migration Validation

### 10.1 Automated Validation (Runs Automatically)

```bash
# These run automatically but can be re-triggered:
migration-cli validate \
    --job-id {JOB_ID} \
    --gate post-load-final

# Checks performed:
# - Record count reconciliation (migrated vs. source baseline)
# - Sample field value spot-check (1% random sample)
# - Referential integrity in Salesforce (Contacts without Accounts, etc.)
# - Duplicate External ID check (should be 0)
```

### 10.2 Manual Validation Steps

1. **Record Count Reconciliation:**
   ```bash
   migration-cli validate counts \
       --job-id {JOB_ID} \
       --tolerance 0.1  # Fail if > 0.1% discrepancy beyond expected rejections
   ```

2. **Field Value Spot Check (Salesforce Query):**
   ```soql
   -- Run in Salesforce Developer Console or Workbench
   SELECT Legacy_ID__c, Name, BillingStreet, Phone, Type, CreatedDate_Legacy__c
   FROM Account
   WHERE Legacy_ID__c IN ('12345', '67890', '11111')
   -- Compare against source system values manually
   ```

3. **Relationship Integrity:**
   ```soql
   SELECT COUNT(Id), AccountId
   FROM Contact
   WHERE AccountId = null
   AND CreatedDate >= {MIGRATION_START_DATE}
   -- Should return 0 if all Contacts have parent Accounts
   ```

4. **Client Sign-Off:** Send validation results to client technical lead for formal sign-off. Use Template D only after sign-off received.

---

*Document Version: 2.4.1 | Reviewed: 2025-12-01 | Next Review: 2026-03-01*
*Maintained by: Migration Operations Team | Feedback: #migration-runbook-feedback*
