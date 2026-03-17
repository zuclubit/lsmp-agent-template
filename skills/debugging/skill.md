---
name: debugging
description: Read-only diagnosis of migration pipeline failures using logs, metrics, and API status
type: skill
version: 2.0.0
agent: debugging-agent
---

# Debugging Skill

**Version**: 2.0.0
**Agent**: debugging-agent
**Last Updated**: 2026-03

---

## Purpose

The `debugging` skill diagnoses failures in the migration pipeline. Given a job identifier
and optional failure context, it correlates log entries, API status responses, and migration
step results to identify the most probable root cause, assign a confidence score, and propose
a remediation path.

This skill operates in **read-only** mode at all times. It never modifies infrastructure state,
restarts services, re-queues jobs, or alters data. All mutation actions are delegated to explicit
operator commands.

---

## Read-Only Constraint

This skill is strictly observational. It will:
- Query `GET /migrations/{id}/status` and `GET /migrations/{id}/steps/{step_id}` endpoints
- Read structured logs from the migration API log endpoint: `GET /migrations/{id}/logs`
- Read metrics from `GET /migrations/{id}/metrics`
- Access job failure details from `GET /jobs/{id}/failures`

It will **never**:
- Call any `POST`, `PUT`, `PATCH`, or `DELETE` migration API endpoints
- Restart pods, re-queue steps, or roll back migrations
- Modify configuration or secrets
- Attempt to recover the migration autonomously

Any use of this skill as a mutation vector is rejected. Automated recovery, when `auto_recoverable=True`,
is signalled to the orchestrator — which makes the final decision to act.

---

## Root Cause Taxonomy

Every diagnosis is classified into one of the following categories:

| Category | Code | Description |
|----------|------|-------------|
| Network / Connectivity | `NET_TIMEOUT` | Upstream API or database connection timed out |
| Network / Connectivity | `NET_DNS_FAILURE` | DNS resolution failed for a service endpoint |
| Rate Limiting | `RATE_LIMIT_SF` | Salesforce API returned HTTP 429 |
| Rate Limiting | `RATE_LIMIT_DOWNSTREAM` | Downstream service returned HTTP 429 |
| Authentication | `AUTH_TOKEN_EXPIRED` | OAuth token or service credential has expired |
| Authentication | `AUTH_PERMISSION_DENIED` | Credentials valid but lack required permissions |
| Data Quality | `DQ_SCHEMA_DRIFT` | Source schema changed since migration was planned |
| Data Quality | `DQ_VALIDATION_GATE_BLOCKED` | A validation gate blocked the migration |
| Data Quality | `DQ_NULL_CONSTRAINT` | Non-nullable target column received null value |
| Data Quality | `DQ_REFERENTIAL_INTEGRITY` | Orphaned foreign-key references in target |
| Infrastructure | `INFRA_OOM` | Process killed due to out-of-memory |
| Infrastructure | `INFRA_CRASHLOOP` | Service in crash loop / repeatedly failing |
| Infrastructure | `INFRA_DISK_PRESSURE` | Disk pressure causing write failures |
| Configuration | `CONFIG_MISSING_ENV` | Required environment variable is absent |
| Configuration | `CONFIG_INVALID_MAPPING` | Field mapping references non-existent column |
| Concurrency | `CONCURRENCY_DEADLOCK` | Database deadlock detected in transaction logs |
| Concurrency | `CONCURRENCY_LOCK_TIMEOUT` | Advisory or row lock acquisition timed out |
| Idempotency | `IDEMPOTENCY_DUPLICATE` | Step executed twice — checkpoint was not created on first run |
| Unknown | `UNKNOWN` | Could not determine root cause from available evidence |

---

## Confidence Scoring

Confidence is a float in `[0.0, 1.0]` representing certainty in the assigned root cause:

| Range | Label | Interpretation |
|-------|-------|----------------|
| 0.90 – 1.00 | High | Multiple corroborating sources confirm the category. Automated recovery may proceed. |
| 0.70 – 0.89 | Medium | Strong signal; secondary indicators consistent. Human review recommended. |
| 0.50 – 0.69 | Low | Partial evidence; alternative explanations plausible. Human investigation required. |
| 0.00 – 0.49 | Insufficient | Evidence ambiguous or contradictory. Category is UNKNOWN. Manual triage needed. |

Confidence is computed as a weighted average over evidence sources:

```
confidence = sum(source_weight * source_signal_strength) / sum(source_weight)
```

Default source weights:

| Source | Weight |
|--------|--------|
| Structured error codes from migration API | 0.45 |
| Step failure details from /steps/{id} | 0.35 |
| Heuristic pattern matching in log messages | 0.20 |

---

## Auto-Recoverable Conditions

`auto_recoverable=True` is set only when:
- `confidence >= 0.90`, AND
- The root cause category has a known safe automated recovery procedure

| Root Cause | Auto-Recoverable | Recovery Action |
|------------|-----------------|-----------------|
| `NET_TIMEOUT` | Yes (if transient) | Orchestrator retries the step after backoff |
| `RATE_LIMIT_SF` | Yes | Orchestrator pauses and retries after cooldown window |
| `RATE_LIMIT_DOWNSTREAM` | Yes | Orchestrator pauses and retries after cooldown window |
| `AUTH_TOKEN_EXPIRED` | Yes | Orchestrator refreshes the token and retries |
| All other categories | No | Requires operator investigation |

---

## HTTP Retry Policy

All diagnostic API calls use exponential backoff:
- Max retries: 3
- Delays: 1s -> 2s -> 4s
- Timeout: 30 seconds per request

---

## Input Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | Yes | Migration job identifier |
| `tenant_id` | string | Yes | Tenant owning the job |
| `failed_step_id` | string | No | Specific step ID that failed (narrows search) |
| `failure_type` | string | No | Optional hint: `timeout`, `oom`, `validation`, `rate_limit`, `auth`, `data_quality`, `config`, `concurrency`, `unknown` |
| `log_window_minutes` | integer | No | Minutes back to search in logs (default: 30, max: 1440) |

---

## Output Schema

| Field | Type | Description |
|-------|------|-------------|
| `root_cause_category` | string | One of the root cause codes from the taxonomy |
| `confidence` | float | Confidence score in [0.0, 1.0] |
| `evidence` | EvidenceItem[] | Evidence items supporting the diagnosis |
| `recommended_fix` | string | Human-readable remediation description |
| `runbook_ref` | string | Path or URL to the relevant runbook, or null |
| `auto_recoverable` | boolean | Whether safe automated recovery can proceed without human approval |
| `affected_step_id` | string | Step ID where the failure occurred, or null |

### EvidenceItem Fields

| Field | Type | Description |
|-------|------|-------------|
| `source` | string | Evidence source: `api_response`, `logs`, `metrics`, `step_status` |
| `timestamp` | ISO 8601 | Timestamp of the evidence |
| `content` | string | Redacted excerpt — secrets and PII removed |
| `relevance` | float | How strongly this item supports the diagnosis (0.0 – 1.0) |

---

## Example Invocation

```python
from agents.debugging_agent.agent import run_debugging

result = await run_debugging(
    job_id="job-acme-2026-001",
    tenant_id="tenant-acme",
    failed_step_id="step-extract-account-001",
    failure_type="timeout",
    log_window_minutes=60,
)

print(f"Root cause: {result.root_cause_category} (confidence: {result.confidence:.2f})")
print(f"Recommended fix: {result.recommended_fix}")
if result.auto_recoverable:
    print("Auto-recovery: SAFE TO PROCEED")
else:
    print("Auto-recovery: HUMAN REVIEW REQUIRED")
    if result.runbook_ref:
        print(f"Runbook: {result.runbook_ref}")
```

---

## When to Use

Use the debugging skill when:
- A migration step fails and the root cause is unknown
- The orchestrator receives a FAILED status from the execution agent
- An operator requests investigation of a migration anomaly
- Periodic health checks reveal degraded migration throughput

Do NOT use the debugging skill when:
- Performing security audits (use security-audit skill)
- Validating data quality (use validation skill)
- Making infrastructure changes — the skill is read-only

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIGRATION_API_BASE_URL` | `http://localhost:8000/api/v1` | Migration API base URL |
| `INTERNAL_SERVICE_TOKEN` | `""` | Bearer token for API calls |
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Model for diagnosis reasoning |
| `DEBUGGING_AGENT_MAX_TOKENS` | `4096` | Max tokens for diagnosis response |
