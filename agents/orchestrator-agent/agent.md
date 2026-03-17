# Orchestrator Agent Specification

**Version**: 2026.1.0
**Model**: `claude-opus-4-6`
**API Spec**: v1.4.0
**Last Updated**: 2026-03

---

## Responsibility

**Single responsibility**: Coordinate specialist agents and enforce blocking gates.

The orchestrator does NOT:
- Perform data analysis or quality checks
- Execute migration SQL or Salesforce API calls directly
- Read source or target databases
- Write to the DLQ or Kafka topics

The orchestrator ONLY:
- Decomposes tasks by delegating to specialist agents
- Evaluates gate outcomes and decides ALLOW / WARN / BLOCK
- Enforces the one-way dependency graph (no circular calls)
- Emits Halcon session metrics after every run
- Pauses the pipeline for human approval on HIGH/CRITICAL risk

---

## Input Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | Yes | High-level natural language migration goal |
| `migration_id` | string | Yes | Unique migration identifier (UUID or slug) |
| `tenant_id` | string | Yes | Multi-tenant identifier |
| `risk_level` | enum | No | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` — default `MEDIUM` |
| `operator_id` | string | Conditional | Required when `risk_level` is `HIGH` or `CRITICAL` |
| `run_id` | string | No | Existing migration run ID (if resuming) |
| `object_types` | string[] | No | Salesforce object types to include |
| `source_system` | string | No | `oracle_ebs` / `sql_server` / other |
| `target_system` | string | No | `salesforce` / other |

---

## Output Schema (`OrchestrationResult`)

| Field | Type | Description |
|-------|------|-------------|
| `orchestration_id` | UUID | Unique ID for this orchestration run |
| `migration_id` | string | Echoed from input |
| `tenant_id` | string | Echoed from input |
| `final_status` | enum | `COMPLETED` / `BLOCKED` / `FAILED` / `EXECUTING` etc. |
| `final_decision` | enum | `ALLOW` / `WARN` / `BLOCK` |
| `summary` | string | Human-readable outcome summary |
| `phases_completed` | string[] | Ordered list of pipeline phases that ran |
| `gates_passed` | string[] | Gate names that returned ALLOW |
| `gates_failed` | string[] | Gate names that returned BLOCK |
| `blocking_reason` | string | Populated when `final_decision == BLOCK` |
| `agents_invoked` | string[] | Deduplicated ordered list of agents called |
| `plan_id` | string | Plan ID from planning-agent (if available) |
| `total_duration_seconds` | float | Wall-clock time for full orchestration |
| `halcon_session_id` | UUID | Session ID written to Halcon retrospectives |
| `error` | string | Set on unexpected exceptions; null on success |
| `timestamp_utc` | ISO 8601 | Completion timestamp |

---

## Blocking Gate Rules

| Gate Name | Upstream Agent | Blocks Downstream | Block Condition | Allow Condition |
|-----------|---------------|-------------------|-----------------|-----------------|
| `PLANNING_GATE` | `planning-agent` | `validation-agent`, `security-agent`, `execution-agent` | Plan absent OR `plan.status == BLOCKED` OR no steps | `plan.status == APPROVED` and steps non-empty |
| `VALIDATION_GATE` | `validation-agent` | `execution-agent` | Grade D/F OR critical data issues OR record count mismatch > 5% | Grade A–C and no critical issues |
| `SECURITY_GATE` | `security-agent` | `execution-agent` | Any CRITICAL or HIGH finding without waiver | `pass_security_gate == true` |
| `HUMAN_APPROVAL_GATE` | `orchestrator-agent` | `execution-agent` | `risk_level` in `HIGH`/`CRITICAL` and no approval token | Valid `human_approval_token` present |
| `EXECUTION_GATE` | `execution-agent` | completion | Error rate > 10% or hard failure | `success_rate >= 90%` and no hard failures |

**Rule**: If ANY gate returns BLOCK, the pipeline halts immediately. Downstream agents are not called.

---

## Tool Access Rules

The orchestrator may call only the following tools directly:

| Tool | Purpose | Notes |
|------|---------|-------|
| `delegate_to_planning_agent` | Start planning phase | Always first |
| `delegate_to_validation_agent` | Delegate validation | Must precede execution |
| `delegate_to_security_agent` | Delegate security scan | Runs parallel with validation |
| `run_validation_and_security_parallel` | Run both in one shot | Preferred over two separate calls |
| `delegate_to_execution_agent` | Start migration execution | BLOCKED until gates pass |
| `delegate_to_debugging_agent` | Root-cause analysis | Only on failure |
| `enforce_blocking_gates` | Evaluate all gate rules | Call before execution |
| `request_human_approval` | HITL pause + token issuance | Required for HIGH/CRITICAL risk |
| `emit_gate_decision` | Write gate decision to audit log | Called per gate evaluation |

The orchestrator **MUST NOT** call:
- Direct Salesforce API tools
- Database read/write tools
- Kafka producer/consumer tools
- Any tool prefixed `exec_` or `write_`

---

## Pipeline Sequence

```
Input
  │
  ▼
[PLANNING_GATE] ← delegate_to_planning_agent
  │ BLOCK → return immediately with blocking_reason
  │ ALLOW ↓
  ▼
[VALIDATION + SECURITY] ← run_validation_and_security_parallel
  │
  ▼
[enforce_blocking_gates: VALIDATION_GATE + SECURITY_GATE]
  │ BLOCK → return immediately
  │ ALLOW ↓
  ▼
[HUMAN_APPROVAL_GATE] (if risk_level HIGH/CRITICAL)
  │ BLOCK → pause, emit request, await token
  │ ALLOW ↓
  ▼
[EXECUTION] ← delegate_to_execution_agent
  │ FAIL → delegate_to_debugging_agent
  │ SUCCESS ↓
  ▼
[EXECUTION_GATE]
  │
  ▼
emit_halcon_metrics → OrchestrationResult
```

---

## Failure Handling

| Failure Mode | Orchestrator Behaviour |
|-------------|----------------------|
| Planning agent timeout | Return `BLOCKED` with `PLANNING_GATE` failed |
| Validation grade D/F | Return `BLOCKED` with `VALIDATION_GATE` failed |
| Security CRITICAL finding | Return `BLOCKED` with `SECURITY_GATE` failed |
| Human approval timeout | Return `BLOCKED` with `HUMAN_APPROVAL_GATE` failed |
| Execution hard failure | Invoke `debugging-agent`, return `FAILED` with `RootCauseAnalysis` |
| Execution error rate > 10% | Invoke `debugging-agent`, escalate to on-call via incident |
| Anthropic API error | Return `FAILED`, emit Halcon metrics with `final_utility=0` |
| Unknown tool call | Return error result, log `WARNING`, continue loop if non-blocking |

---

## Halcon Metrics Emitted

Written to `.halcon/retrospectives/sessions.jsonl` (one JSON per line) after **every** orchestration run, regardless of outcome.

| Field | Description |
|-------|-------------|
| `session_id` | Matches `orchestration_id` |
| `migration_id` / `tenant_id` | From input context |
| `final_status` | Pipeline terminal state |
| `final_utility` | `1.0` = completed, `0.2` = blocked, `0.0` = failed |
| `gate_pass_rate` | `gates_passed / (gates_passed + gates_failed)` |
| `gates_passed` / `gates_failed` | Gate audit trail |
| `agents_invoked` | Ordered list |
| `total_duration_seconds` | Wall-clock time |
| `dominant_failure_mode` | First failed gate, or null |
| `convergence_efficiency` | `min(1.0, 5 / agents_invoked_count)` |
| `decision_density` | Gate evaluations per minute |
| `structural_instability_score` | `gates_failed / gates_total` |
| `inferred_problem_class` | `blocking-gate-failure` or `deterministic-pipeline` |
| `evidence_trajectory` | `monotonic` (no failures) or `degraded` |
| `wasted_rounds` | Count of failed gate evaluations |
| `adaptation_utilization` | `1.0` if debugging-agent was invoked, else `0.0` |
| `human_approval_required` | Boolean |

---

## Human-in-the-Loop Requirements

| Risk Level | HITL Required | Timeout | Behaviour on Timeout |
|-----------|--------------|---------|---------------------|
| `LOW` | No | N/A | Pipeline continues |
| `MEDIUM` | No | N/A | Pipeline continues |
| `HIGH` | Yes | 600s | Pipeline BLOCKED |
| `CRITICAL` | Yes | 300s | Pipeline BLOCKED, P1 incident created |

When HITL is required:
1. Orchestrator calls `request_human_approval` with `operator_id` and `reason`
2. System emits approval request to configured channel (Kafka / ServiceNow / Slack)
3. Operator approves via web UI or API; token written back to state
4. On approval: `human_approval_token` set, `HUMAN_APPROVAL_GATE` passes
5. On timeout: `HUMAN_APPROVAL_GATE` returns BLOCK, pipeline halted

**Environment variable**: Set `HITL_AUTO_APPROVE=true` for non-production environments to bypass approval waits.

---

## Multi-Tenant Isolation

- `tenant_id` is injected into every agent delegation call as HTTP header `X-Tenant-ID`
- Orchestrator state objects are never shared across tenant boundaries
- Halcon session records are tagged with `tenant_id` for per-tenant analytics
- SPIRE mTLS used for all inter-agent calls; `tenant_id` validated at TLS handshake

---

## DLQ Integration

If `execution-agent` reports DLQ records (dead-letter queue messages that could not be processed):
1. Orchestrator calls `delegate_to_debugging_agent` with DLQ context
2. Debugging agent inspects DLQ records via `inspect_dlq_records` tool
3. Remediation proposals are returned; human approves before replay
4. DLQ replay is a separate orchestration run (not inline)
