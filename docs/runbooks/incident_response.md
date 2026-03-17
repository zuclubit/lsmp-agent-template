# Incident Response Runbook

**Version:** 1.8.0
**Last Updated:** 2025-12-01
**Owner:** Site Reliability Engineering (SRE) Team
**Audience:** On-Call Engineers, SRE, Engineering Management, CISO
**Classification:** INTERNAL — Do not distribute externally

---

## Table of Contents

1. [Incident Severity Definitions](#1-incident-severity-definitions)
2. [Response Team and Contacts](#2-response-team-and-contacts)
3. [General Incident Response Process](#3-general-incident-response-process)
4. [Runbook: Data Corruption Incident](#4-runbook-data-corruption-incident)
5. [Runbook: Salesforce API Outage](#5-runbook-salesforce-api-outage)
6. [Runbook: Migration Stall / Hang](#6-runbook-migration-stall--hang)
7. [Runbook: Security Incident (Unauthorized Access)](#7-runbook-security-incident-unauthorized-access)
8. [Runbook: Kafka Cluster Failure](#8-runbook-kafka-cluster-failure)
9. [Post-Incident Review Template](#9-post-incident-review-template)
10. [Communication During Incidents](#10-communication-during-incidents)

---

## 1. Incident Severity Definitions

### P0 — Critical (Immediate Response Required)

**Definition:** Platform-wide outage, data loss, active data corruption in production Salesforce, active security breach, or complete loss of ability to serve any client.

**Examples:**
- Active data corruption in a client's production Salesforce org (wrong data written to records)
- Confirmed unauthorized access to client migration data
- Kafka cluster total failure with potential data loss
- Migration platform API completely unavailable (all health checks failing)
- Vault cluster sealed and cannot be unsealed (all dynamic credentials invalidated)

**Response Time:** Immediate — within 5 minutes of detection
**Escalation:** On-call engineer pages Engineering Lead AND CISO immediately
**Client Communication:** Within 15 minutes, per government contract requirements within 1 hour
**Duration Expectation:** Engineers engaged continuously until resolved; escalation to VP if > 2 hours

---

### P1 — High (Urgent Response Required)

**Definition:** Active migration job impacted with significant data at risk; single-client Salesforce API unavailability; platform degraded with > 50% of active migrations affected.

**Examples:**
- Active migration stalled for > 30 minutes with no automated recovery
- Salesforce Bulk API errors causing loading to fail for a client
- Transformation service for a client crashed and not recovering
- DLQ depth > 10,000 records with AI triage unable to categorize
- SPIRE server unhealthy — SVIDs within 45 minutes of expiry

**Response Time:** 15 minutes — page on-call engineer
**Escalation:** Engineering Lead notified; client notified within 30 minutes
**Duration Expectation:** Resolution within 4 hours; escalate to P0 if > 2 hours with no progress

---

### P2 — Medium (Response Within Hours)

**Definition:** Non-critical platform functionality degraded; single migration below SLA thresholds; monitoring gaps.

**Examples:**
- Migration throughput < 50% of expected (but migration is still progressing)
- DLQ depth > 1,000 records (but AI triage is active)
- Grafana dashboards unavailable (migrations still running, just not visible)
- Schema Registry down (new schema registrations blocked, existing in-flight are fine)
- Vault Audit log writer failed (secrets still accessible, but audit gap)

**Response Time:** 2 hours
**Escalation:** Engineering Lead notified via Slack; client notified if migration is impacted
**Duration Expectation:** Resolution within 8 hours (business day); or escalate to P1

---

### P3 — Low (Response Within Business Day)

**Definition:** Cosmetic issues, non-impacting degradations, proactive improvements.

**Examples:**
- Alert firing that is a known false positive
- Minor performance degradation not impacting SLAs
- Non-critical log errors that are informational
- Documentation or configuration inconsistencies

**Response Time:** Next business day
**Escalation:** Filed as GitHub issue; engineering team triages in next sprint
**Client Communication:** Not required unless client-facing

---

## 2. Response Team and Contacts

**Note:** Replace placeholder names/contacts with actual team information. Store actual contact information in PagerDuty and the internal team wiki, NOT in this runbook.

| Role | Primary Contact | Escalation Contact | Channel |
|------|----------------|-------------------|---------|
| On-Call Engineer | PagerDuty rotation: `migration-platform-sre` | Engineering Lead | PagerDuty + #incidents |
| Engineering Lead | (See PagerDuty) | VP Engineering | PagerDuty + direct call |
| Security / CISO | (See PagerDuty policy: security-oncall) | Legal / GC | Direct call for P0 |
| Kafka Admin | (See wiki: kafka-admins) | Platform Arch | #kafka-ops |
| Salesforce Admin | Client-specific (in job config) | Platform team SA | Direct |
| Vault Admin | (See wiki: vault-admins) | Security Arch | #vault-ops |
| Client Account Manager | CRM → account record | VP Customer Success | Direct call for P0 |

**PagerDuty Escalation Policy: `migration-platform`**
```
Level 1: On-Call SRE (0 min timeout)
Level 2: Engineering Lead (15 min if unacknowledged)
Level 3: VP Engineering + CISO (30 min if unacknowledged)
Level 4: C-Suite notification (60 min for P0 only)
```

**Incident Command:** For P0 incidents, the Engineering Lead is the Incident Commander (IC). The first on-call engineer to respond is the Primary Responder and does NOT take IC role — they focus on technical resolution.

**Bridge Line / War Room:** Zoom: (link in PagerDuty) | Slack: `#incident-bridge-{YYYYMMDD}`

---

## 3. General Incident Response Process

```
DETECT (Alert fires / Client reports / Engineer observes)
    │
    ▼
ACKNOWLEDGE (On-call acknowledges in PagerDuty — starts the clock)
    │
    ▼
ASSESS severity using Section 1 definitions
    │
    ├─── P0 ──▶ Page Engineering Lead + CISO IMMEDIATELY
    │            Open Zoom bridge. Start Slack #incident-bridge-{date}
    │
    ├─── P1 ──▶ Page Engineering Lead. Notify in #incidents.
    │            Open Slack thread for coordination.
    │
    └─── P2/P3 ▶ Handle in #incidents. No bridge required.
    │
    ▼
DIAGNOSE (follow specific runbook below)
    │
    ▼
MITIGATE (stop the bleeding — may not be full resolution)
    │
    ▼
COMMUNICATE (client, stakeholders — per Section 10)
    │
    ▼
RESOLVE (full restoration of service)
    │
    ▼
DOCUMENT (update incident record with timeline)
    │
    ▼
POST-INCIDENT REVIEW (within 5 business days — use template in Section 9)
```

---

## 4. Runbook: Data Corruption Incident

**Severity:** P0
**Trigger:** Client reports incorrect data in Salesforce; automated post-load sampling detects > 0.01% field mismatch; security agent detects unauthorized transformation rule modification.

### Step 1: Immediate Containment (First 15 Minutes)

**STOP ALL LOADING IMMEDIATELY:**
```bash
# Emergency stop all loading for affected tenant
migration-cli job emergency-stop \
    --tenant-id {TENANT_ID} \
    --all-jobs \
    --operator-id {YOUR_EMAIL} \
    --reason "P0 DATA CORRUPTION INCIDENT — stopping all loading"

# Verify all loading pods are terminated
kubectl get pods -n tenant-{TENANT_ID}-app -l app=loading-service
# Must return: No resources found

# Freeze Kafka consumer groups (prevent any resumption until investigation complete)
migration-cli kafka consumer-group freeze \
    --tenant-id {TENANT_ID} \
    --all-groups
```

**Capture State Snapshot:**
```bash
# Capture the current state before anything changes
migration-cli incident snapshot \
    --tenant-id {TENANT_ID} \
    --output /tmp/incident-snapshot-$(date +%Y%m%d-%H%M%S).json

# Save Kafka offsets for all consumer groups
kafka-consumer-groups.sh \
    --bootstrap-server kafka.platform-kafka.svc.cluster.local:9092 \
    --describe --all-groups \
    2>/dev/null | grep {TENANT_ID} \
    > /tmp/kafka-offsets-snapshot-$(date +%Y%m%d-%H%M%S).txt

# Save transformation rule versions currently deployed
migration-cli rules list \
    --tenant-id {TENANT_ID} \
    --show-versions > /tmp/rule-versions-$(date +%Y%m%d-%H%M%S).txt
```

### Step 2: Scope Assessment (15–30 Minutes)

Determine: What is corrupted, how many records, which objects, what time period.

```bash
# Run post-load integrity scan against Salesforce
migration-cli validate integrity \
    --tenant-id {TENANT_ID} \
    --job-id {JOB_ID} \
    --full-scan \
    --compare-source \
    --output /tmp/integrity-report-$(date +%Y%m%d-%H%M%S).json

# Check the Kafka loaded topic — what was the transformation rule version on affected records?
migration-cli kafka analyze-loaded \
    --job-id {JOB_ID} \
    --field metadata.rule_set_version \
    --time-range "2025-12-01T20:00:00Z/2025-12-01T22:00:00Z"
# This tells us when the rule version changed (if at all)

# Query Salesforce for specific corruption pattern
# Example: if Name fields are wrong
migration-cli salesforce query \
    --tenant-id {TENANT_ID} \
    --soql "SELECT Id, Legacy_ID__c, Name, LastModifiedDate FROM Account WHERE LastModifiedDate >= {MIGRATION_START} AND (Name LIKE '%[MASKED%' OR Name = '')" \
    --output /tmp/corrupted-records-$(date +%Y%m%d-%H%M%S).csv
```

**Corruption Scope Classification:**
| Scope | Records Affected | Action |
|-------|-----------------|--------|
| Isolated | < 100 records | Manual correction in Salesforce |
| Limited | 100–10,000 records | Targeted rollback of affected records |
| Wide | 10,000–1M records | Full rollback of job |
| Total | > 1M records | Full rollback + platform freeze + P0 escalation |

### Step 3: Root Cause Investigation

```bash
# Check transformation rule deployment history
git log --since="48 hours ago" -- transformation_rules/ | head -50

# Check if any rule was deployed mid-migration
migration-cli rules history \
    --tenant-id {TENANT_ID} \
    --since 48h

# Compare what was in Kafka (extracted) vs what was loaded (transformed)
migration-cli kafka compare-stages \
    --job-id {JOB_ID} \
    --field Name \
    --source-stage extracted \
    --target-stage loaded \
    --sample 100

# Check OPA policy audit log for any policy changes
vault audit get \
    --path auth/opa-decisions \
    --start-time "48h ago" \
    | jq 'select(.request.path | contains("transformation"))'

# Check AI agent activity — was any AI-recommended remediation applied recently?
migration-cli ai-agent history \
    --tenant-id {TENANT_ID} \
    --since 24h \
    --show-approved-actions
```

### Step 4: Remediation

**Option A: Targeted Record Correction (< 10,000 records)**
```bash
# Generate correction CSV from source system re-query
migration-cli generate-corrections \
    --job-id {JOB_ID} \
    --corrupted-records-file /tmp/corrupted-records-*.csv \
    --output /tmp/corrections.csv

# Apply corrections via Salesforce Bulk API
migration-cli salesforce apply-corrections \
    --tenant-id {TENANT_ID} \
    --corrections-file /tmp/corrections.csv \
    --operator-id {YOUR_EMAIL} \
    --requires-approval

# Verify corrections applied correctly
migration-cli validate sample \
    --tenant-id {TENANT_ID} \
    --records-file /tmp/corrupted-records-*.csv \
    --expected-source
```

**Option B: Job Rollback (> 10,000 records)**
```bash
# See migration_runbook.md Section 7 for full rollback procedure
migration-cli job rollback \
    --job-id {JOB_ID} \
    --operator-id {YOUR_EMAIL} \
    --approver-id {SECOND_APPROVER} \
    --reason "P0 data corruption incident - full rollback required" \
    --confirm
```

### Step 5: Client Notification

Use Communication Template in Section 10. For government clients: notification within 1 hour of discovery is contractually required. CISO must approve the notification language for P0 incidents.

### Step 6: Post-Incident Audit

After resolution:
```bash
# Generate full incident audit report
migration-cli incident report \
    --incident-id {INCIDENT_ID} \
    --include-kafka-trace \
    --include-rule-versions \
    --include-ai-decisions \
    --output incident-report-{INCIDENT_ID}.pdf

# This report is required for:
# - Government clients: within 72 hours of resolution (FedRAMP requirement)
# - Enterprise clients: within 5 business days
```

---

## 5. Runbook: Salesforce API Outage

**Severity:** P1 (client migration halted) or P2 (client migration slowed)
**Trigger:** Alert `SALESFORCE_API_ERROR_RATE > 50%` for > 5 minutes; loading throughput = 0.

### Step 1: Determine Outage Type

```bash
# Check Salesforce status page (automated check)
migration-cli salesforce health-check --tenant-id {TENANT_ID}
# Also check: https://status.salesforce.com (Salesforce Trust Site)

# Check specific error types from loading logs
kubectl logs \
    -n tenant-{TENANT_ID}-app \
    -l app=loading-service,job-id={JOB_ID} \
    --since=15m \
    | grep -E "(ERROR|WARN)" \
    | sort | uniq -c | sort -rn | head -20
```

**Outage Type Classification:**
| Error Pattern | Type | Action |
|--------------|------|--------|
| `UNABLE_TO_LOCK_ROW`, `SERVER_UNAVAILABLE` | Salesforce platform outage | Wait for Salesforce recovery; subscribe to status page |
| `REQUEST_LIMIT_EXCEEDED` | API governor limit hit | Wait for limit reset; see below |
| `INVALID_SESSION_ID` | OAuth token expired | Refresh token; see below |
| `TXN_SECURITY_NO_ACCESS` | Security policy blocking | Contact client Salesforce admin |
| `OPERATION_TOO_LARGE` | Batch size too large | Reduce batch size; see below |
| Network timeout / connection refused | Network issue between platform and SF | Check network policy and DNS |

### Step 2: Resolve by Outage Type

**Salesforce Platform Outage:**
```bash
# Pause loading — extraction and transformation can continue, buffering in Kafka
migration-cli job pause-loading \
    --job-id {JOB_ID} \
    --reason "Salesforce platform outage - status.salesforce.com tracking"

# Set up auto-resume when Salesforce recovers
migration-cli job configure-auto-resume \
    --job-id {JOB_ID} \
    --salesforce-health-check-interval 5m

# Notify client per Template B (status update) with Salesforce incident number
```

**API Governor Limit:**
```bash
# Check current limits
migration-cli salesforce api-limits --tenant-id {TENANT_ID} --verbose

# If DailyApiRequests limit hit:
# Salesforce resets at midnight Pacific Time
# Calculate reset time in client's timezone:
RESET_UTC=$(TZ='America/Los_Angeles' date -d 'tomorrow 00:00' +"%Y-%m-%dT%H:%M:%SZ")
echo "Limit resets at: $RESET_UTC UTC"

# Set auto-resume at reset time
migration-cli job configure-auto-resume \
    --job-id {JOB_ID} \
    --resume-at "$RESET_UTC" \
    --reason "SF API daily limit; resuming at limit reset"

# Consider requesting emergency API limit increase from Salesforce (takes 24–72 hours)
# Log a Salesforce case if migration is time-sensitive
```

**OAuth Token Expired / Invalid:**
```bash
# Refresh Salesforce OAuth token (via Vault)
vault write \
    -namespace={TENANT_ID} \
    salesforce/rotate-creds/migration-loader

# Restart loading service pods to pick up new token
kubectl rollout restart \
    deployment/loading-service-{JOB_ID} \
    -n tenant-{TENANT_ID}-app

# Verify new token works
migration-cli salesforce test-auth --tenant-id {TENANT_ID}
```

**Batch Size Too Large:**
```bash
# Reduce batch size (default is 10,000 — reduce to 2,000)
migration-cli job configure \
    --job-id {JOB_ID} \
    --loading-batch-size 2000

# Restart loading service
kubectl rollout restart \
    deployment/loading-service-{JOB_ID} \
    -n tenant-{TENANT_ID}-app
```

### Step 3: Monitor Recovery

```bash
# Watch loading throughput recover
watch -n 30 'migration-cli metrics throughput \
    --job-id {JOB_ID} \
    --stage loading \
    --window 5m'

# Expected: throughput > 0 within 2 minutes of resuming
```

---

## 6. Runbook: Migration Stall / Hang

**Severity:** P1 if stalled > 30 minutes; P2 if 15–30 minutes
**Trigger:** Alert `MIGRATION_STALL` — migration running but no records processed in > 30 minutes.
**Also see:** [monitoring/runbooks/migration_stall.md](../../monitoring/runbooks/migration_stall.md) for more detail.

### Step 1: Characterize the Stall

```bash
# Get overall job status
migration-cli job status --job-id {JOB_ID} --verbose

# Check which stage is stuck
migration-cli metrics throughput \
    --job-id {JOB_ID} \
    --all-stages \
    --window 60m

# Example output:
# Stage          | 60m ago | 30m ago | 15m ago | Now   | Status
# Extraction     | 45,000  | 47,000  | 0       | 0     | STALLED (15 min)
# Transformation | 43,000  | 40,000  | 12,000  | 400   | SLOWING
# Loading        | 4,200   | 4,100   | 4,000   | 3,800 | OK
```

### Step 2: Diagnose by Stage

**Extraction Stalled:**
```bash
# Check extraction pod status
kubectl get pods -n tenant-{TENANT_ID}-app -l app=extraction-service

# Check for crash loops
kubectl describe pod \
    -n tenant-{TENANT_ID}-app \
    -l app=extraction-service,job-id={JOB_ID} \
    | grep -A 20 "Events:"

# Check extraction logs for last activity
kubectl logs \
    -n tenant-{TENANT_ID}-app \
    -l app=extraction-service,job-id={JOB_ID} \
    --since=1h | tail -100

# If using Debezium CDC: check connector status
curl -s https://kafka-connect.platform-kafka.svc.cluster.local:8083/connectors/{TENANT_ID}-{JOB_ID}-source/status \
    | jq '.connector.state, .tasks[].state'
# Expected: RUNNING for both connector and tasks

# If connector is FAILED:
curl -X POST \
    https://kafka-connect.platform-kafka.svc.cluster.local:8083/connectors/{TENANT_ID}-{JOB_ID}-source/restart \
    -H "Content-Type: application/json"
```

**Transformation Stalled:**
```bash
# Check consumer group — is it making progress?
kafka-consumer-groups.sh \
    --bootstrap-server kafka.platform-kafka.svc.cluster.local:9092 \
    --describe \
    --group migration-transformer-{JOB_ID}

# If all partitions show lag = 0 and CONSUMER-ID is empty: consumers died
kubectl get pods -n tenant-{TENANT_ID}-app -l app=transformation-service
# If pods are in CrashLoopBackOff:
kubectl logs \
    -n tenant-{TENANT_ID}-app \
    -l app=transformation-service,job-id={JOB_ID} \
    --previous | tail -50
# Look for: OOM kill, rule engine exception, schema registry connection refused

# If SVID expired (mTLS failure): check certificate expiry
kubectl exec \
    -n tenant-{TENANT_ID}-app \
    -l app=transformation-service,job-id={JOB_ID} \
    -- openssl x509 -in /run/spiffe/certs/client.crt -noout -dates

# Force SVID refresh:
kubectl delete pod \
    -n spire \
    -l app=spire-agent \
    --field-selector spec.nodeName=$(kubectl get pod \
        -n tenant-{TENANT_ID}-app \
        -l app=transformation-service \
        -o jsonpath='{.items[0].spec.nodeName}')
```

**Loading Stalled:**
```bash
# Check Bulk API batch queue in Salesforce
migration-cli salesforce bulk-status \
    --tenant-id {TENANT_ID} \
    --job-id {JOB_ID} \
    | grep -E "(Queued|InProgress|Failed)"

# If all batches in "Queued" with no InProgress: SF is under load
# Wait up to 15 minutes; if still queued, contact Salesforce support

# If loading pods are stuck processing one batch too long:
# Get the loading pod
LOADING_POD=$(kubectl get pod \
    -n tenant-{TENANT_ID}-app \
    -l app=loading-service,job-id={JOB_ID} \
    -o jsonpath='{.items[0].metadata.name}')

# Check what batch it's working on
kubectl exec -n tenant-{TENANT_ID}-app $LOADING_POD -- \
    cat /tmp/current-batch.txt

# If stuck on a single batch > 30 minutes: kill the pod (will restart and retry)
kubectl delete pod $LOADING_POD -n tenant-{TENANT_ID}-app
```

### Step 3: Recovery

```bash
# After identifying and fixing root cause, restart affected service
kubectl rollout restart \
    deployment/{SERVICE_NAME}-{JOB_ID} \
    -n tenant-{TENANT_ID}-app

# Verify migration resumes
migration-cli metrics throughput \
    --job-id {JOB_ID} \
    --all-stages \
    --window 10m
# Expected: throughput > 0 within 5 minutes

# Update estimated completion
migration-cli job estimate-completion \
    --job-id {JOB_ID}
```

---

## 7. Runbook: Security Incident (Unauthorized Access)

**Severity:** P0 — always
**Trigger:** OPA decision log shows denied cross-tenant access attempt; SIEM alert on anomalous API usage; Vault audit log shows unexpected credential access; client reports seeing another client's data.

### Step 1: Immediate Isolation

```bash
# REVOKE ALL CREDENTIALS FOR AFFECTED TENANT IMMEDIATELY
vault lease revoke -prefix -force \
    salesforce/creds/tenant-{AFFECTED_TENANT_ID}/

vault lease revoke -prefix -force \
    kafka/creds/tenant-{AFFECTED_TENANT_ID}/

# Revoke all SPIRE entries for affected namespace
kubectl exec -n spire spire-server-0 -- \
    spire-server entry list \
    -spiffeID "spiffe://migration-platform.internal/ns/tenant-{AFFECTED_TENANT_ID}-app/.*" \
    2>/dev/null | grep "Entry ID" | awk '{print $3}' | \
    xargs -I{} kubectl exec -n spire spire-server-0 -- \
        spire-server entry delete -entryID {}

# Stop all migration activity for affected tenant
migration-cli job emergency-stop \
    --tenant-id {AFFECTED_TENANT_ID} \
    --all-jobs \
    --reason "SECURITY INCIDENT - emergency isolation"
```

### Step 2: Preserve Evidence

```bash
# CRITICAL: Do not alter any logs. Capture them to secure storage.
kubectl logs \
    -n tenant-{AFFECTED_TENANT_ID}-app \
    --all-containers \
    --since 24h \
    --timestamps \
    > /tmp/security-incident-logs-$(date +%Y%m%d-%H%M%S).txt

# Export OPA decision logs for last 24 hours
curl -s https://audit-service.platform.svc.cluster.local:8443/v1/decisions?hours=24 \
    | jq 'select(.result.deny != null)' \
    > /tmp/opa-deny-decisions-$(date +%Y%m%d-%H%M%S).json

# Export Vault audit log
vault audit read \
    --since 24h \
    > /tmp/vault-audit-$(date +%Y%m%d-%H%M%S).json

# Export network flow logs (if available)
kubectl get networkpolicies -n tenant-{AFFECTED_TENANT_ID}-app -o yaml \
    > /tmp/networkpolicies-$(date +%Y%m%d-%H%M%S).yaml
```

### Step 3: Engage Security Response

- Page CISO immediately (P0 response)
- Do NOT discuss incident details in regular Slack channels — use private security channel `#security-incidents` (restricted access)
- Government clients: FedRAMP requires notification within 1 hour of confirmed breach
- Legal team engagement required if PII is confirmed exposed

---

## 8. Runbook: Kafka Cluster Failure

**Severity:** P0 if data loss is possible; P1 if brokers are down but no data loss

**Trigger:** Alert `KAFKA_BROKER_DOWN`, `KAFKA_UNDER_REPLICATED_PARTITIONS > 0`, or `KAFKA_CONTROLLER_COUNT != 1`

### Step 1: Assess

```bash
# Check broker status
kubectl get pods -n platform-kafka -l app=kafka
# Note which pods are NotReady, CrashLoopBackOff, etc.

# Check under-replicated partitions (potential data loss risk)
kubectl exec -n platform-kafka kafka-0 -- \
    kafka-topics.sh \
    --bootstrap-server localhost:9092 \
    --describe \
    --under-replicated-partitions 2>/dev/null

# Check controller election
kubectl exec -n platform-kafka kafka-0 -- \
    kafka-metadata-quorum.sh \
    --bootstrap-server localhost:9092 \
    describe 2>/dev/null | head -20
```

### Step 2: Recover Downed Broker

```bash
# If a pod is crash-looping, check logs first
kubectl logs \
    -n platform-kafka \
    kafka-{N} \
    --previous | tail -100

# Common cause: disk full
kubectl exec -n platform-kafka kafka-{N} -- df -h /var/kafka-data

# If disk full: this is CRITICAL — data may be getting dropped
# Immediate actions:
# 1. Reduce retention on non-audit topics temporarily
kafka-configs.sh \
    --bootstrap-server kafka.platform-kafka.svc.cluster.local:9092 \
    --alter \
    --entity-type topics \
    --entity-name {OPERATIONAL_TOPIC_NAME} \
    --add-config retention.ms=3600000  # Reduce to 1 hour temporarily

# 2. Delete old log segments manually (ONLY if absolutely necessary, with KAFKA ADMIN)
# This is a last resort — engage Kafka admin before proceeding

# If pod is just crashing without disk issues: delete and let it restart
kubectl delete pod kafka-{N} -n platform-kafka
# Wait for StatefulSet to recreate; will rejoin cluster and replicate
watch kubectl get pods -n platform-kafka
```

### Step 3: Verify Data Integrity Post-Recovery

```bash
# Check all partitions are fully replicated
kubectl exec -n platform-kafka kafka-0 -- \
    kafka-topics.sh \
    --bootstrap-server localhost:9092 \
    --describe \
    --under-replicated-partitions 2>/dev/null
# Must return NOTHING (no under-replicated partitions)

# Check consumer group lag hasn't grown unexpectedly (would indicate message loss)
migration-cli kafka verify-integrity \
    --tenant-id {TENANT_ID} \
    --job-id {JOB_ID}

# Resume migrations after verification
migration-cli job resume \
    --all-paused \
    --reason "Kafka cluster restored and verified" \
    --operator-id {YOUR_EMAIL}
```

---

## 9. Post-Incident Review Template

**Complete within 5 business days of incident resolution.**

```markdown
# Post-Incident Review — {INCIDENT_ID}

**Date of Incident:** {DATE}
**Date of Review:** {REVIEW_DATE}
**Severity:** P{N}
**Duration:** {DURATION_FROM_DETECT_TO_RESOLVE}
**Facilitator:** {NAME}
**Attendees:** {NAMES}

## Incident Summary

_One paragraph: what happened, what was the impact, how it was resolved._

## Timeline

| Time (UTC) | Event |
|------------|-------|
| {TIME} | Alert fired / First detection |
| {TIME} | On-call engineer acknowledged |
| {TIME} | Severity assessed as P{N} |
| {TIME} | Engineering Lead engaged (if P0/P1) |
| {TIME} | Root cause identified: {BRIEF_DESCRIPTION} |
| {TIME} | Mitigation applied: {BRIEF_DESCRIPTION} |
| {TIME} | Client notified |
| {TIME} | Service fully restored |
| {TIME} | All-clear confirmed |

## Impact

- **Clients Affected:** {COUNT} ({LIST_NAMES})
- **Records Affected:** {COUNT}
- **Data Loss:** {YES/NO — if yes, describe}
- **Downtime (client-facing):** {DURATION}
- **SLA Breach:** {YES/NO — which SLA}
- **Compliance Impact:** {FedRAMP notification required? GDPR breach? SOX audit trail gap?}

## Root Cause Analysis

_What was the actual technical cause? Use "5 Whys" technique._

**Why did the incident occur?**
Because: {REASON_1}

**Why did {REASON_1} occur?**
Because: {REASON_2}

**Why did {REASON_2} occur?**
Because: {REASON_3}

**Root Cause:** {FINAL_ROOT_CAUSE}

**Contributing Factors:**
- {FACTOR_1}
- {FACTOR_2}

## What Went Well

- {THING_1 — e.g., "Alert fired within 2 minutes of issue onset"}
- {THING_2 — e.g., "Kafka consumer offset preservation prevented data loss during stall"}
- {THING_3}

## What Went Poorly

- {THING_1 — e.g., "Runbook step 3 was unclear; caused 10-minute delay"}
- {THING_2 — e.g., "SPIRE SVID expiry warning was set too late (15 min) — should be 60 min"}
- {THING_3}

## Action Items

| ID | Action | Owner | Due Date | Priority |
|----|--------|-------|----------|----------|
| {INC-ID}-01 | {SPECIFIC_PREVENTIVE_ACTION} | {NAME} | {DATE} | P{N} |
| {INC-ID}-02 | {RUNBOOK_UPDATE} | {NAME} | {DATE} | P{N} |
| {INC-ID}-03 | {MONITORING_IMPROVEMENT} | {NAME} | {DATE} | P{N} |

## Metrics

- **MTTR (Mean Time to Resolve):** {DURATION}
- **MTTD (Mean Time to Detect):** {DURATION_FROM_ONSET_TO_ALERT}
- **MTTM (Mean Time to Mitigate):** {DURATION_FROM_DETECT_TO_MITIGATION}

## Lessons Learned

_What would we do differently? What should other teams know?_

---
_Review approved by: {ENGINEERING_LEAD} on {DATE}_
_Action items tracked in: GitHub Issues #{LIST_OF_ISSUE_NUMBERS}_
```

---

## 10. Communication During Incidents

### P0 Client Notification (Within 15 Minutes)

```
Subject: [URGENT] Migration Platform Incident Affecting Your Migration — {INCIDENT_ID}

{CLIENT_NAME} Team,

We are currently investigating an incident affecting your migration job {JOB_ID}.

Current Status: {BRIEF_STATUS}
Impact: {IMPACT_DESCRIPTION — be specific, not vague}
Action Taken: {WHAT_WE_ARE_DOING}
Next Update: In 30 minutes, or sooner if status changes.

Your migration is currently: {PAUSED | RUNNING AT REDUCED CAPACITY | STOPPED}

For urgent questions: {ENGINEER_DIRECT_PHONE}
Incident tracking: #migration-{JOB_ID}

We will provide regular updates until this is resolved.

{ENGINEER_NAME}
{COMPANY_NAME} — Migration Platform SRE
```

### P0 Government Client Notification (Additional Requirements)

For FedRAMP-authorized systems, government clients must receive:
1. Initial notification within **1 hour** of confirmed incident
2. Hourly updates during P0 incidents
3. Written incident report within **72 hours** of resolution
4. Notification must go to the client's ISSO (Information System Security Officer), not just technical lead

For incidents involving potential PII exposure, additionally notify:
- CISO (internal) immediately
- Legal/General Counsel within 1 hour
- FISMA requires US-CERT notification within **1 hour** for security incidents
- State agencies may have additional notification requirements

### P1 Status Update Cadence

| Time Since Start | Action |
|-----------------|--------|
| T+0 | Initial assessment sent to client (if migration-impacting) |
| T+30 min | Update: status, root cause theory, ETA |
| T+1 hour | Update: confirmed root cause, mitigation in progress |
| T+2 hours | Escalate to P0 if unresolved |
| T+resolution | All-clear + estimated catch-up time |

---

*Document Version: 1.8.0 | Reviewed: 2025-12-01 | Next Review: 2026-03-01*
*Maintained by: SRE Team | Feedback: #sre-docs-feedback*
*Emergency contacts: See PagerDuty policy `migration-platform`*
