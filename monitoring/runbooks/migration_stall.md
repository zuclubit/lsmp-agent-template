# Runbook: MIGRATION_STALL

**Alert Name:** `MigrationStall` / `MIGRATION_STALL`
**Runbook Version:** 1.4.0
**Severity:** P2 (15–30 min stall) → P1 (> 30 min with no automated recovery)
**Team:** Migration Platform On-Call
**Owner:** SRE Team
**Last Updated:** 2025-12-01
**Estimated Resolution Time:** 10–45 minutes

---

## Alert Definition

```yaml
- alert: MigrationStall
  expr: |
    (migration_job_status == 1)  # job is RUNNING
    and
    (time() - migration_last_record_processed_timestamp > 300)  # no progress for 5 min
  for: 5m
  labels:
    severity: high
    team: migration-platform
  annotations:
    summary: "Migration job {{ $labels.job_id }} has stalled"
    description: "No records processed in last {{ $value | humanizeDuration }}"
```

**Threshold:** No records processed for 5+ minutes while job is in RUNNING state
**SLA Impact:** Stall extends migration window; risks missing maintenance window

---

## Detection

### Check if it's truly stalled:
```bash
# Verify migration is RUNNING but not progressing
curl -s "https://api.migration.internal/api/v1/migrations/$JOB_ID" \
  -H "Authorization: Bearer $API_TOKEN" | jq '{
    status, progress_percentage, processed_records,
    total_records, last_activity: .updated_at
  }'

# Check worker logs for the last 10 minutes
kubectl logs -n migration -l app=migration-worker --since=10m | tail -100 | \
  grep -E '"level":"(info|warn|error)"' | jq -r '.message' | head -30
```

---

## Investigation Steps

### 1. Check Worker Pod Health
```bash
kubectl get pods -n migration -l app=migration-worker -o wide
kubectl describe pod -n migration -l app=migration-worker | grep -A 10 "Events:"
```

### 2. Check for Deadlock or Blocking Query
```bash
kubectl exec -n migration deployment/migration-worker -- \
  python -c "
from sqlalchemy import create_engine, text
from config.settings import get_settings
engine = create_engine(get_settings().database.url)
with engine.connect() as conn:
    # Check for blocking queries (PostgreSQL)
    result = conn.execute(text('''
      SELECT pid, wait_event_type, wait_event, query, query_start
      FROM pg_stat_activity
      WHERE state != 'idle' AND query_start < NOW() - INTERVAL '2 minutes'
    '''))
    for row in result:
        print(row)
"
```

### 3. Check Kafka Consumer Lag
```bash
# Check if event consumer is lagging
kubectl exec -n migration deployment/kafka-consumer -- \
  kafka-consumer-groups.sh --bootstrap-server kafka:9092 \
  --group migration-platform-consumers \
  --describe | grep -v "^$"
```

### 4. Check Salesforce Bulk Job Status
```bash
# Find the current bulk job ID from logs
BULK_JOB_ID=$(kubectl logs -n migration -l app=migration-worker --since=30m | \
  grep "bulk_job_id" | tail -1 | jq -r '.bulk_job_id')

# Check its status
python scripts/validate_salesforce_connection.py --check-bulk-job $BULK_JOB_ID
```

### 5. Check Memory and CPU
```bash
kubectl top pods -n migration
kubectl exec -n migration deployment/migration-worker -- \
  python -c "
import psutil
print(f'CPU: {psutil.cpu_percent()}%')
print(f'Memory: {psutil.virtual_memory().percent}%')
print(f'Open files: {len(psutil.Process().open_files())}')
"
```

---

## Common Causes and Fixes

### Cause 1: Worker OOM Killed
**Symptom:** Pod restarts, `OOMKilled` in events
```bash
kubectl describe pod -n migration <pod-name> | grep -A 5 "OOM"
# Fix: Restart with increased memory limit
kubectl set resources deployment/migration-worker \
  -n migration --limits=memory=4Gi
```

### Cause 2: Database Connection Pool Exhausted
**Symptom:** Log entries `acquiring connection timed out`
```bash
# Fix: Restart connection pool
kubectl rollout restart deployment/migration-worker -n migration
# Long-term: adjust pool_size in config
```

### Cause 3: Salesforce Bulk Job Stuck in InProgress
**Symptom:** Bulk job ID present, status never changes from `InProgress`
```bash
# Abort the stuck bulk job
curl -X PATCH \
  "https://myorg.salesforce.com/services/data/v59.0/jobs/ingest/$BULK_JOB_ID" \
  -H "Authorization: Bearer $SF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"state": "Aborted"}'

# Resume migration (will create a new bulk job from checkpoint)
curl -X POST "https://api.migration.internal/api/v1/migrations/$JOB_ID/resume" \
  -H "Authorization: Bearer $API_TOKEN"
```

### Cause 4: Kafka Message Processing Blocked
**Symptom:** Consumer lag growing, no message ACKs
```bash
# Restart consumer
kubectl rollout restart deployment/kafka-consumer -n migration
```

### Cause 5: Lock Contention on Migration Job Record
**Symptom:** DB queries blocked, `lock_timeout` errors
```bash
kubectl exec -n migration deployment/migration-worker -- \
  python -c "
from sqlalchemy import create_engine, text
from config.settings import get_settings
engine = create_engine(get_settings().database.url)
with engine.connect() as conn:
    conn.execute(text('SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE wait_event_type = \"Lock\" AND query_start < NOW() - INTERVAL \"1 minute\"'))
"
```

---

## Recovery Procedure

1. **Pause the stalled migration** (prevents checkpoint corruption):
```bash
curl -X POST "https://api.migration.internal/api/v1/migrations/$JOB_ID/pause" \
  -H "Authorization: Bearer $API_TOKEN" \
  -d '{"reason": "Stall recovery procedure"}'
```

2. **Identify and fix root cause** (see above)

3. **Resume from checkpoint:**
```bash
curl -X POST "https://api.migration.internal/api/v1/migrations/$JOB_ID/resume" \
  -H "Authorization: Bearer $API_TOKEN"
```

4. **Verify progress resumes:**
```bash
watch -n 30 'curl -s "https://api.migration.internal/api/v1/migrations/$JOB_ID" \
  -H "Authorization: Bearer $API_TOKEN" | jq "{progress_percentage, processed_records}"'
```

---

## Prevention

- Set aggressive timeouts on all external calls (SF, DB): max 30s
- Implement circuit breaker on Salesforce API client
- Configure K8s liveness probe to restart unresponsive workers
- Set Bulk API job timeout to auto-abort after 2 hours
- Monitor checkpoint advancement every 60 seconds

---

## Detection Criteria

### Alert Definition (Updated)

```yaml
# monitoring/alerts/migration-alerts.yaml
- alert: MIGRATION_STALL
  expr: |
    # Job is RUNNING (status = 2) but throughput has been 0 for 30 minutes
    (migration_job_status_info{status="RUNNING"} == 1)
    and
    (
      sum by (tenant_id, job_id) (
        increase(migration_records_loaded_total[30m])
      ) == 0
    )
    and
    (
      sum by (tenant_id, job_id) (
        increase(migration_records_transformed_total[30m])
      ) == 0
    )
  for: 5m
  labels:
    severity: high
    team: sre
    runbook: https://docs.internal/runbooks/migration_stall
  annotations:
    summary: "Migration stall detected — {{ $labels.tenant_id }}/{{ $labels.job_id }}"
    description: |
      Migration job {{ $labels.job_id }} (tenant: {{ $labels.tenant_id }}) shows RUNNING status
      but zero records processed in the last 30 minutes. Immediate investigation required.
      Last known throughput: check migration_throughput_records_per_second metric.
```

### Stall vs. Expected Pause Disambiguation

Before investigating as a stall, confirm this is NOT an intentional pause:

```bash
# Get job status — PAUSED is expected, RUNNING with no progress is a stall
migration-cli job status --job-id $JOB_ID | grep -E "status|pause_reason"

# Check if a maintenance window is in effect
migration-cli job schedule --job-id $JOB_ID
# If current time is outside maintenance_windows: confirm loading should be active

# Check if auto-pause threshold was triggered (error rate)
migration-cli job status --job-id $JOB_ID | grep "auto_paused"
```

---

## Triage by Stall Type

### Triage Step 1: Which Stage Is Stalled?

```bash
# Get per-stage throughput for the last 60 minutes
migration-cli metrics throughput \
    --job-id $JOB_ID \
    --all-stages \
    --window 60m \
    --time-series

# Example identifying stalled stage:
# Stage       | 60m ago | 30m ago | 15m ago | Now   | Status
# Extraction  | 45,000  | 47,000  | 0       | 0     | STALLED (extraction stall)
# Transform   | 43,000  | 40,000  | 40,500  | 41,200| OK
# Loading     | 4,200   | 4,100   | 4,000   | 3,800 | OK (transformation buffer)
```

### Triage Step 2: Diagnose by Stage

**Extraction Stalled — check source system connectivity:**
```bash
# Check Debezium/Kafka Connect connector status
curl -s https://kafka-connect.platform-kafka.svc.cluster.local:8083/connectors \
    | jq '.[]' | grep $TENANT_ID

# Get connector status
CONNECTOR_NAME="$TENANT_ID-$JOB_ID-source"
curl -s https://kafka-connect.platform-kafka.svc.cluster.local:8083/connectors/$CONNECTOR_NAME/status \
    | jq '.connector.state, .tasks[].state, .tasks[].trace'

# If FAILED: restart the connector
curl -X POST \
    https://kafka-connect.platform-kafka.svc.cluster.local:8083/connectors/$CONNECTOR_NAME/restart

# Check extraction service logs for the stall time
kubectl logs \
    -n tenant-${TENANT_ID}-app \
    -l app=extraction-service,job-id=$JOB_ID \
    --since=45m \
    | grep -E "(ERROR|last_batch|stalled|connection)" | tail -50
```

**Transformation Stalled — check consumer and processing:**
```bash
# Check Kafka consumer group is active
kafka-consumer-groups.sh \
    --bootstrap-server kafka.platform-kafka.svc.cluster.local:9092 \
    --describe \
    --group migration-transformer-$JOB_ID \
    | grep -v "^$" | head -20

# If CONSUMER-ID is blank: consumers are dead
kubectl get pods -n tenant-${TENANT_ID}-app \
    -l app=transformation-service,job-id=$JOB_ID

# Check for OOM kills
kubectl describe pods \
    -n tenant-${TENANT_ID}-app \
    -l app=transformation-service,job-id=$JOB_ID \
    | grep -E "(OOMKilled|Reason|Exit Code|Restart Count)"

# If OOMKilled: increase memory limits
kubectl patch deployment transformation-service-$JOB_ID \
    -n tenant-${TENANT_ID}-app \
    --patch '{"spec":{"template":{"spec":{"containers":[{"name":"transformation","resources":{"limits":{"memory":"8Gi"}}}]}}}}'
```

**Loading Stalled — check Salesforce and consumer:**
```bash
# Check if Salesforce bulk jobs are queued/stuck
migration-cli salesforce bulk-status \
    --tenant-id $TENANT_ID \
    --job-id $JOB_ID \
    | grep -E "(InProgress|Queued|Aborted|Failed)" | head -20

# If bulk job stuck InProgress > 30 minutes:
STUCK_BULK_JOB_ID=$(migration-cli salesforce bulk-status \
    --tenant-id $TENANT_ID --job-id $JOB_ID \
    | jq -r '.[] | select(.state == "InProgress" and .age_minutes > 30) | .bulk_job_id' | head -1)

if [ -n "$STUCK_BULK_JOB_ID" ]; then
    echo "Aborting stuck bulk job: $STUCK_BULK_JOB_ID"
    migration-cli salesforce bulk-abort \
        --tenant-id $TENANT_ID \
        --bulk-job-id $STUCK_BULK_JOB_ID
    # Migration will automatically retry with a new bulk job
fi

# Check if SPIFFE SVID expired (causes mTLS auth failure → silent stall)
kubectl exec \
    -n tenant-${TENANT_ID}-app \
    $(kubectl get pod -n tenant-${TENANT_ID}-app -l app=loading-service,job-id=$JOB_ID \
      -o jsonpath='{.items[0].metadata.name}') \
    -- openssl x509 -in /run/spiffe/certs/client.crt -noout -dates 2>/dev/null
# If "notAfter" is in the past: SVID expired

# Refresh SVID by restarting SPIRE agent on the node
NODE=$(kubectl get pod -n tenant-${TENANT_ID}-app \
    -l app=loading-service,job-id=$JOB_ID \
    -o jsonpath='{.items[0].spec.nodeName}')
kubectl delete pod -n spire -l app=spire-agent \
    --field-selector spec.nodeName=$NODE
```

---

## Complete Recovery Procedure

```bash
# Step 1: Identify stalled stage (above)
# Step 2: Apply the relevant fix (above)
# Step 3: Restart the stalled service if needed
kubectl rollout restart \
    deployment/${STALLED_SERVICE}-$JOB_ID \
    -n tenant-${TENANT_ID}-app

# Step 4: Watch for recovery
watch -n 15 'migration-cli metrics throughput --job-id $JOB_ID --all-stages --window 5m'
# Target: throughput > 0 within 5 minutes

# Step 5: Update estimated completion time
migration-cli job estimate-completion --job-id $JOB_ID

# Step 6: Notify client if stall exceeded 30 minutes (migration SLA impact)
# Use communication template in migration_runbook.md (Template B — Status Update)
```

---

## Prevention Measures

| Prevention | Implementation | Status |
|-----------|---------------|--------|
| SVID rotation monitoring alert | Alert fires at < 30 min remaining on SVID TTL | Implemented |
| Kafka consumer liveness check | K8s liveness probe on consumer heartbeat | Implemented |
| Bulk job timeout | Auto-abort Salesforce bulk jobs after 90 minutes | Implemented (v1.3) |
| Extraction connector auto-restart | Kafka Connect restart policy on FAILED connectors | Implemented |
| OOM headroom monitoring | Alert when pod memory > 80% of limit | Implemented |
| Stall detection granularity | Alert fires after 15-minute stall (improved from 30-min) | Planned (v1.5) |
| Pre-migration SVID TTL check | Verify all SVIDs have > 4 hours remaining before migration start | Planned (v1.5) |

---

*Runbook Version: 1.4.0 | Updated: 2025-12-01 | Owner: SRE Team*
*Alert source: `monitoring/alerts/migration-alerts.yaml`*
*Grafana dashboard: `https://grafana.internal/d/migration-pipeline/?jobId={JOB_ID}`*
*Related runbook: [high_error_rate.md](./high_error_rate.md) — often co-occurs with stall during recovery*
