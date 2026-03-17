# Debugging Agent Specification

**Version**: 2026.2.0
**Model**: `claude-sonnet-4-6` (override: `DEBUGGING_AGENT_MODEL` env var)
**API Spec**: v1.1.0
**Policy**: READ-ONLY — no write tools permitted
**Last Updated**: 2026-03

---

## Purpose

The Debugging Agent performs root cause analysis (RCA) on migration failures. It uses five
READ-ONLY diagnostic tools to gather evidence, then produces a structured `RootCauseAnalysis`
with a canonical root cause category, confidence score, and specific cited evidence.

The Debugging Agent does NOT:
- Modify any system state
- Restart services or pods
- Execute recovery actions (signals the orchestrator instead)
- Acknowledge or replay Kafka or DLQ messages
- Call any write API

The Debugging Agent ONLY:
- Reads logs, pod status, DB queries, Kafka lag, and Salesforce bulk job status
- Analyzes collected evidence to determine root cause
- Maps root cause to runbook references
- Signals the orchestrator with a specific recovery action when auto-recoverable AND confidence > 0.85

---

## Input Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | Yes | Migration job identifier |
| `tenant_id` | string | Yes | Multi-tenant identifier |
| `failed_step_id` | string | Yes | UUID of the step that failed |
| `failed_step_type` | string | Yes | Step type (e.g. `LOAD`, `EXTRACT`) |
| `failure_report` | object | Yes | FailureReport dict from the execution-agent |
| `namespace` | string | No | Kubernetes namespace (default: `"migration"`) |
| `service_names` | string[] | No | Services to collect logs from |
| `kafka_consumer_groups` | string[] | No | Kafka consumer groups to check |
| `salesforce_bulk_job_id` | string | No | Salesforce Bulk API 2.0 job ID |
| `since_minutes` | integer | No | Log lookback window (default: `30`, max: `1440`) |

---

## Output Schema (`DebuggingAgentResult`)

| Field | Type | Description |
|-------|------|-------------|
| `analysis_id` | UUID | Unique analysis run identifier |
| `job_id` | string | Echoed from input |
| `tenant_id` | string | Echoed from input |
| `success` | boolean | True when RCA completed successfully |
| `analysis` | RootCauseAnalysis | The analysis result; `null` on API failure |
| `auto_recovery_signalled` | boolean | True when auto-recovery conditions are met |
| `recovery_action` | string | Recovery action for orchestrator; set when `auto_recovery_signalled=true` |
| `error` | string | Error description; required when `success=false` |
| `duration_ms` | integer | Wall-clock execution time |
| `tokens_used` | integer | Total LLM tokens consumed |
| `halcon_metrics` | object | Halcon observability payload |

### RootCauseAnalysis Fields

| Field | Type | Description |
|-------|------|-------------|
| `analysis_id` | UUID | Unique analysis identifier |
| `root_cause_category` | enum | One of the canonical categories (see below) |
| `confidence` | float (0.0–1.0) | Confidence in root cause determination |
| `evidence` | string[] | Specific evidence items citing tool and observed value |
| `recommended_fix` | string | Actionable remediation steps |
| `auto_recoverable` | boolean | True when the issue can be resolved automatically |
| `recovery_action` | string | Specific orchestrator action; required when `auto_recoverable=true` |
| `estimated_recovery_minutes` | integer | Estimated recovery time |
| `runbook_reference` | string | Runbook file and section reference |

---

## Root Cause Categories

| Category | Description | auto_recoverable | Runbook |
|----------|-------------|-----------------|---------|
| `OOM` | Pod killed by OOM killer | true | `migration_stall.md#oom-recovery` |
| `DB_DEADLOCK` | Database deadlock detected | true | `migration_stall.md#deadlock-resolution` |
| `SF_BULK_STALL` | Salesforce Bulk API job stalled | false | `migration_stall.md#bulk-api-stall` |
| `KAFKA_LAG` | Kafka consumer group lag too high | true | `high_error_rate.md#kafka-consumer-lag` |
| `NETWORK_TIMEOUT` | Connection timeout observed | true | `migration_stall.md#network-timeout` |
| `DATA_QUALITY` | High error rate in record data | false | `high_error_rate.md#data-quality-failures` |
| `UNKNOWN` | Insufficient evidence | false | `migration_stall.md#unknown-failure` |

---

## Read-Only Tools

| Tool | Purpose | Key Signal |
|------|---------|-----------|
| `read_logs` | Read service log entries | OOMKilled, deadlock, timeout patterns |
| `get_pod_status` | Get Kubernetes pod status | `terminationReason=OOMKilled`, restart count |
| `get_db_blocking_queries` | Get database lock chains | `total_blocking > 0` |
| `get_kafka_consumer_lag` | Get Kafka consumer lag | `total_lag > 10,000` |
| `get_salesforce_bulk_job_status` | Get Salesforce Bulk API job status | `state=Failed/Aborted` |

---

## Auto-Recovery Signalling

Auto-recovery is signalled to the orchestrator when ALL conditions are true:
1. `analysis.auto_recoverable = true`
2. `analysis.confidence > 0.85`
3. `analysis.recovery_action` is set

When auto-recovery is signalled:
- `auto_recovery_signalled = True` in the result
- `recovery_action` is populated with the specific orchestrator action
- The orchestrator executes the recovery action (with dual authorization if destructive)

---

## Evidence Format

Every evidence item MUST be a complete, specific sentence:
```
GOOD: "get_pod_status(migration): pod migration-worker-7d4b9c terminated with reason=OOMKilled, restartCount=3"
BAD:  "The pod may have run out of memory"
```

---

## When to Use

Use the Debugging Agent when:
- The execution-agent has returned `debug_signal_sent=True`
- A migration step has failed and root cause is unknown
- Orchestrator needs `auto_recovery_signalled` to determine next action

Do NOT use the Debugging Agent when:
- The failure cause is already known (use the runbook directly)
- You need to execute a recovery — the orchestrator handles that
- Monitoring for potential issues before failure (use observability dashboards)

---

## Model Rationale

`claude-sonnet-4-6` is selected because:
- RCA requires reasoning across multiple data sources — LLM reasoning is appropriate
- The agent makes several tool calls (5 tools, multiple calls per tool) — Sonnet handles this efficiently
- Confidence scoring and evidence synthesis require nuanced language understanding
- Sonnet provides lower cost than Opus for iterative tool-use loops

---

## Example Invocation

```python
from agents.debugging_agent.agent import run_debugging_agent

result = run_debugging_agent(
    job_id="job-acme-2026-001",
    tenant_id="tenant-acme",
    failed_step_id="11111111-0000-0000-0000-000000000006",
    failed_step_type="LOAD",
    failure_report={
        "error_message": "java.lang.OutOfMemoryError: Java heap space",
        "failure_category": "API_ERROR",
        "records_processed_before_failure": 450000,
    },
    namespace="migration",
    service_names=["migration-worker"],
    since_minutes=30,
)

print(f"Root cause: {result.analysis.root_cause_category.value}")
print(f"Confidence: {result.analysis.confidence:.0%}")
print(f"Auto-recovery: {result.auto_recovery_signalled}")
if result.auto_recovery_signalled:
    print(f"Recovery action: {result.recovery_action}")
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DEBUGGING_AGENT_MODEL` | `claude-sonnet-4-6` | Anthropic model ID |
| `DEBUGGING_AGENT_MAX_TOKENS` | `8192` | Maximum tokens per LLM call |
| `DEBUGGING_AGENT_MAX_ITERATIONS` | `20` | Maximum agent loop iterations |
| `MIGRATION_API_BASE_URL` | `http://localhost:8000/api/v1` | Migration platform REST API base |
| `INTERNAL_SERVICE_TOKEN` | (required) | Bearer token for API authentication |
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
