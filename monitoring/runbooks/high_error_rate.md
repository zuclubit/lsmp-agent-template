# Runbook: HIGH_ERROR_RATE

**Alert Name:** `MigrationHighErrorRate` / `HIGH_ERROR_RATE`
**Runbook Version:** 1.5.0
**Severity:** Warning (0.5% threshold) → P1 Critical (2.0% threshold)
**Team:** Migration Platform On-Call
**Owner:** SRE Team
**Last Updated:** 2025-12-01
**Estimated Resolution Time:** 15–60 minutes (root cause dependent)

---

## Alert Definition

```yaml
# alert_rules.yaml
- alert: MigrationHighErrorRate
  expr: |
    rate(migration_records_failed_total[5m]) /
    rate(migration_records_processed_total[5m]) > 0.05
  for: 2m
  labels:
    severity: critical
    team: migration-platform
  annotations:
    summary: "Migration error rate above 5% threshold"
    description: "Job {{ $labels.job_id }} error rate is {{ $value | humanizePercentage }}"
    runbook_url: "https://wiki.internal/runbooks/high_error_rate"
```

**Threshold:** Error rate > 5% over 5-minute window for 2 consecutive minutes
**SLA Impact:** If sustained > 10 minutes, migration SLA is breached

---

## Impact Assessment

| Error Rate | Impact | Action |
|-----------|--------|--------|
| 5–10%     | Moderate — some records failing but migration progressing | Investigate while running |
| 10–25%    | High — significant data quality issues | Consider pausing migration |
| > 25%     | Critical — systematic failure | Pause immediately, escalate |
| > 50%     | Catastrophic — likely source/target connectivity issue | Pause + incident bridge |

---

## Immediate Response (First 5 Minutes)

### 1. Acknowledge the alert
```bash
# In PagerDuty / Alertmanager
curl -X POST https://alertmanager.internal/api/v2/alerts/acknowledge \
  -H "Authorization: Bearer $AM_TOKEN" \
  -d '{"matchers":[{"name":"alertname","value":"MigrationHighErrorRate"}]}'
```

### 2. Identify the affected job
```bash
# Check current running jobs
curl -s https://api.migration.internal/api/v1/migrations?status=RUNNING \
  -H "Authorization: Bearer $API_TOKEN" | jq '.items[] | {job_id, name, failed_records, success_rate}'
```

### 3. Check current error rate in Grafana
Navigate to: [Migration Dashboard → Error Rate Panel](https://grafana.internal/d/migration-platform)

Key panels to check:
- `migration_records_failed_total` by `error_code`
- `migration_batch_duration_seconds` (slowdown = resource pressure)
- `salesforce_api_error_rate` (SF-side errors vs. our errors)

---

## Investigation Steps

### Step 1: Identify Error Type
```bash
# Query Prometheus for error breakdown
curl -G 'https://prometheus.internal/api/v1/query' \
  --data-urlencode 'query=topk(10, sum by (error_code) (rate(migration_records_failed_total[5m])))'

# Check application logs
kubectl logs -n migration -l app=migration-worker --since=10m | \
  grep -E '"level":"error"' | \
  jq -r '.error_code' | sort | uniq -c | sort -rn | head -20
```

### Step 2: Check Salesforce API Health
```bash
# Validate Salesforce connection
python scripts/validate_salesforce_connection.py --org production

# Check SF API limits
curl -s https://myorg.salesforce.com/services/data/v59.0/limits \
  -H "Authorization: Bearer $SF_TOKEN" | jq '{
    DailyApiRequests: .DailyApiRequests,
    DailySobjectCreates: .DailySobjectCreates,
    ConcurrentAsyncGetReportInstances: .ConcurrentAsyncGetReportInstances
  }'
```

### Step 3: Inspect Recent Error Records
```bash
# Get failed records from the validation report
curl -s "https://api.migration.internal/api/v1/migrations/$JOB_ID/report" \
  -H "Authorization: Bearer $API_TOKEN" | \
  jq '.errors[:20]'

# Check quarantine queue
kubectl exec -n migration deployment/migration-worker -- \
  python -c "
from migration.validation.data_validator import DataValidator
v = DataValidator()
print(v.get_quarantine_summary('$JOB_ID'))
"
```

### Step 4: Check Source Data Quality
```bash
# Run data quality check on the current batch
kubectl exec -n migration deployment/migration-worker -- \
  python scripts/validate_salesforce_connection.py --check-source-batch --batch-id $CURRENT_BATCH_ID
```

### Step 5: Check Infrastructure Health
```bash
# Pod health
kubectl get pods -n migration -l app=migration-worker
kubectl top pods -n migration

# Database connections
kubectl exec -n migration deployment/migration-worker -- \
  python -c "
from config.settings import get_settings
from sqlalchemy import create_engine, text
engine = create_engine(get_settings().database.url)
with engine.connect() as conn:
    result = conn.execute(text('SELECT COUNT(*) FROM pg_stat_activity'))
    print('DB connections:', result.scalar())
"
```

---

## Common Causes and Fixes

### Cause 1: Salesforce Validation Errors (REQUIRED_FIELD_MISSING)
**Symptom:** `error_code: REQUIRED_FIELD_MISSING`, errors on `Name` or picklist fields
**Fix:**
```bash
# Check transformation rules for missing field mapping
grep -r "REQUIRED_FIELD_MISSING" /var/log/migration/ | tail -50 | jq -r '.field' | sort | uniq -c

# Re-run transformer with debug
python -c "
from migration.data_transformations.account_transformer import AccountTransformer
t = AccountTransformer()
t.transform({'id': 'LEG-SAMPLE', 'name': ''})  # Reproduce the error
"
```

### Cause 2: Salesforce Governor Limits
**Symptom:** `error_code: REQUEST_LIMIT_EXCEEDED` or bulk job failures
**Fix:**
```bash
# Reduce batch size
curl -X PATCH "https://api.migration.internal/api/v1/migrations/$JOB_ID/config" \
  -H "Authorization: Bearer $API_TOKEN" \
  -d '{"config": {"batch_size": 500}}'

# Check if we need to pause and resume after SF limit resets (midnight PT)
```

### Cause 3: Duplicate Detection
**Symptom:** `error_code: DUPLICATE_VALUE` affecting > 1% of records
**Fix:**
```bash
# Check dedup strategy
kubectl exec -n migration deployment/migration-worker -- \
  python -c "
from migration.validation.data_validator import DataValidator
v = DataValidator()
print(v.get_duplicate_summary('$JOB_ID'))
"

# Update upsert key if needed (via feature flag)
curl -X PATCH "https://api.migration.internal/api/v1/migrations/$JOB_ID/config" \
  -d '{"config": {"external_id_field": "Legacy_ID__c"}}'
```

### Cause 4: Data Type Mismatch
**Symptom:** `error_code: INVALID_TYPE` on numeric or date fields
**Fix:**
```bash
# Check specific failing record
curl "https://api.migration.internal/api/v1/migrations/$JOB_ID/validation" \
  -H "Authorization: Bearer $API_TOKEN" | \
  jq '.errors[] | select(.rule == "TYPE_VALIDATION")'
```

### Cause 5: Network/Timeout Issues
**Symptom:** Errors spike correlate with Salesforce response time increase
**Fix:**
```bash
# Check SF response time trends
curl -G 'https://prometheus.internal/api/v1/query' \
  --data-urlencode 'query=histogram_quantile(0.99, rate(salesforce_request_duration_seconds_bucket[5m]))'

# Restart workers if connection pool is exhausted
kubectl rollout restart deployment/migration-worker -n migration
```

---

## Decision Tree: Pause or Continue?

```
Error rate > 5%?
├── YES: Is it Salesforce Governor Limits?
│   ├── YES → Pause migration, wait for limit reset (midnight PT), resume
│   └── NO: Is it Data Quality (REQUIRED_FIELD_MISSING, INVALID_TYPE)?
│       ├── < 10% of records → Continue, quarantine bad records, fix post-migration
│       └── > 10% of records → Pause, fix transformation rules, re-run
└── (Shouldn't reach here)
```

---

## Escalation Path

| Time Without Resolution | Action |
|-------------------------|--------|
| 0–5 min | On-call investigates |
| 5–15 min | Alert Migration Lead |
| 15–30 min | Open incident bridge, notify client stakeholders |
| > 30 min | Pause migration, escalate to Engineering Manager |

**Contacts:**
- Migration Platform On-Call: PagerDuty policy `migration-platform-oncall`
- Migration Lead: `@migration-lead` in `#migration-ops` Slack channel
- Salesforce Admin: `@sf-admin` in `#salesforce-admin`

---

## Post-Incident

After resolution, document in the incident ticket:
1. Root cause
2. Time to detect / Time to mitigate
3. Records affected and remediation status
4. Prevention measures added
5. Update this runbook if new scenarios discovered

---

## Extended Investigation: DLQ Deep Dive

When AI triage results are available, review them critically before acting:

```bash
# Get full AI triage analysis with reasoning trace
migration-cli dlq triage-results \
    --job-id $JOB_ID \
    --show-reasoning-trace \
    --format detailed

# If AI confidence is LOW: do not use AI-recommended fix without independent verification
# If AI confidence is MEDIUM: verify the proposed root cause before approving
# If AI confidence is HIGH: still verify at least 5 sample records manually

# Approve AI-recommended remediation (minimum 90 seconds review enforced by UI)
migration-cli dlq approve-remediation \
    --job-id $JOB_ID \
    --remediation-id $REMEDIATION_ID \
    --operator-id your@email.com \
    --reason "Verified: AI recommendation correct — 5 sample records confirmed pattern"
```

### Prometheus Queries for Root Cause Analysis

```promql
# Error rate by error code — identify the dominant failure type
topk(10, sum by (error_code) (
    rate(migration_records_failed_total{job_id="$JOB_ID"}[5m])
)) * 60

# Error rate trend over 2 hours — is it getting worse or better?
sum(rate(migration_records_failed_total{job_id="$JOB_ID"}[5m])) /
sum(rate(migration_records_extracted_total{job_id="$JOB_ID"}[5m])) * 100

# DLQ depth trend — accumulating or stable?
migration_dlq_depth{job_id="$JOB_ID"}

# Salesforce API limit correlation — is the error rate tied to API exhaustion?
salesforce_api_daily_limit_remaining{tenant_id="$TENANT_ID"}

# Loading throughput — did it drop when errors started?
migration_throughput_records_per_second{job_id="$JOB_ID", stage="LOADING"}

# Consumer lag — transformation still running while loading has errors?
kafka_consumer_lag_records{
    consumer_group="migration-loader-$JOB_ID"
}
```

### Mandatory Documentation for SOX-Scoped Tenants

If the affected tenant is SOX-scoped (financial data):
- Error rate > 2% triggers automatic notification to SOX Compliance Officer
- Document: total records affected, error categories, resolution time, records confirmed-correct vs. excluded
- Reconciliation report must be re-run and signed after DLQ resolution
- If any financial field value was incorrect: escalate to Data Corruption incident procedure

### Escalation Path

| Condition | Escalate To | Method |
|-----------|------------|--------|
| Error rate > 5% and rising | Engineering Lead | PagerDuty P1 |
| Root cause unknown after 30 minutes | Senior SRE or Engineering Lead | Slack + direct call |
| Data corruption suspected | CISO + Engineering Lead | P0 bridge call |
| SOX-scoped tenant > 2% error | SOX Compliance Officer | Email + Slack within 1 hour |
| PII observed in error logs | Security Team | Secure channel only, within 30 minutes |

### Resolution Verification

```bash
# Confirm error rate declining
watch -n 60 'migration-cli metrics error-rate --job-id $JOB_ID --window 10m'
# Target: < 0.5% within 10 minutes of fix applied

# Confirm DLQ depth decreasing
watch -n 60 'migration-cli dlq depth --job-id $JOB_ID'

# Confirm migration throughput recovering
migration-cli metrics throughput --job-id $JOB_ID --all-stages --window 5m
```
