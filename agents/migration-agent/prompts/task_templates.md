# Migration Agent – Task Prompt Templates

Use these templates to invoke the Migration Agent for specific scenarios.
Replace `{{placeholders}}` with actual values before sending.

---

## 1. Routine Health Check

```
Perform a routine health check on migration run {{run_id}}.

Report:
- Current status and progress (% complete)
- Error rate over the last 100 batches
- Salesforce API limits headroom
- Any anomalies or risks that require attention

Take corrective action only if error rate exceeds 10% or API limits are below 20%.
```

---

## 2. High Error Rate Alert

```
Migration run {{run_id}} has triggered an alert for elevated error rate.

Alert details:
- Reported error rate: {{error_rate}}%
- Total failed records so far: {{failed_count}}
- Object type: {{object_type}}
- Alert triggered at: {{timestamp}}

Please investigate the root cause, take appropriate remediation actions,
and provide a full incident summary.
```

---

## 3. Salesforce API Limit Warning

```
The Salesforce API limit monitor has triggered a warning for the organisation.

Current state:
- Daily API requests remaining: {{remaining}} / {{total}} ({{pct}}%)
- Active migration runs: {{active_runs}}

Please assess the situation, identify which runs are the highest consumers,
reduce throughput as needed (batch size reduction or pausing lower-priority runs),
and ensure the remaining limit is sufficient to complete priority run {{priority_run_id}}.
```

---

## 4. Post-Batch Validation Failure

```
Data validation failed for batch {{batch_id}} in migration run {{run_id}}.

Validation summary:
- Total records in batch: {{batch_size}}
- Records with validation errors: {{validation_error_count}}
- Records with warnings only: {{warning_count}}
- Top failing fields: {{top_failing_fields}}

Determine whether these failures are:
a) Systematic (data quality issue in the source system requiring a fix)
b) Isolated (a subset of records that can be skipped or manually corrected)
c) Configuration (field mapping issue requiring a deployment)

Take appropriate action and provide remediation recommendations.
```

---

## 5. Bulk Job Failure

```
A Salesforce Bulk API 2.0 job has failed.

Job details:
- Bulk Job ID: {{bulk_job_id}}
- Migration Run ID: {{run_id}}
- Object type: {{object_type}}
- Operation: {{operation}} (insert/update/upsert)
- Records processed before failure: {{processed}}
- Error: {{error_message}}

Retrieve the error report, determine if the job can be safely retried,
and either retry or escalate appropriately. If retrying, recommend whether
to use the same batch size or reduce it.
```

---

## 6. Dependency Outage During Migration

```
A dependency health check has failed during migration run {{run_id}}.

Failed dependency: {{dependency_name}}
Status: {{dependency_status}}
Error: {{error_message}}
Active migration runs affected: {{affected_runs}}

Immediately assess the impact on in-flight migrations, pause any runs
that cannot safely continue without the dependency, and create an
appropriately-severity incident.
```

---

## 7. Reconciliation Discrepancy

```
Post-migration reconciliation has found a discrepancy for run {{run_id}}.

Reconciliation report:
- Legacy source record count: {{source_count}}
- Salesforce target record count: {{target_count}}
- Discrepancy: {{discrepancy_count}} records ({{discrepancy_pct}}%)
- Object type: {{object_type}}

Investigate the following possibilities:
1. Records skipped due to validation errors (check error report)
2. Records that failed silently (check Bulk API failed results)
3. Duplicate detection deleting records (check for duplicate rules)

Provide a definitive root cause and a plan to close the gap.
```

---

## 8. Performance Degradation

```
Migration run {{run_id}} throughput has dropped significantly.

Baseline throughput: {{baseline_records_per_hour}} records/hour
Current throughput:  {{current_records_per_hour}} records/hour
Throughput reduction: {{reduction_pct}}%

Estimated completion at current rate: {{new_eta}}
Original estimated completion: {{original_eta}}
Deadline: {{deadline}}

Diagnose the cause of the slowdown (SF API response times, Kafka lag,
database load, batch failures requiring retries) and take steps to
restore throughput. If the deadline is at risk, escalate.
```

---

## 9. Full Migration Run Kickoff

```
Start a new migration run for the following configuration:

Object types to migrate: {{object_types}}
Source system: {{source_system}}
Batch size: {{batch_size}}
Dry run: {{dry_run}}
Priority: {{priority}}
Target completion: {{target_completion}}

Before starting:
1. Check Salesforce API limits to ensure sufficient headroom
2. Verify all dependency health checks are passing
3. Confirm no other high-priority runs are consuming significant API quota

Start the run only if all pre-conditions are met. Otherwise report
what needs to be resolved first.
```

---

## 10. End-of-Run Summary Request

```
Migration run {{run_id}} has completed (or been marked complete by the orchestrator).

Please generate a comprehensive end-of-run summary that includes:
- Overall success rate and record counts by outcome
- Top 5 error categories and their resolutions
- Salesforce API usage consumed by this run
- Duration and throughput analysis (records/hour, peak vs trough)
- Any manual follow-up items (records requiring human review)
- Recommendations for future runs of the same object type

Format the summary for inclusion in the post-migration report distributed
to the project steering committee.
```
