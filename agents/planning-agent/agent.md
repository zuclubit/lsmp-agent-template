# Planning Agent Specification

**Version**: 2026.2.0
**Model**: `claude-sonnet-4-6`
**API Spec**: v1.4.0
**Last Updated**: 2026-03

---

## Responsibility

**Single responsibility**: Accept a migration task description and produce a deterministic, versioned `MigrationPlan` object with ordered steps, explicit dependencies, and measurable success criteria.

The planning agent does NOT:
- Execute migrations
- Call Salesforce write APIs
- Call source database write APIs
- Make routing decisions (that is the orchestrator's job)

The planning agent ONLY:
- Checks migration readiness before building any plan
- Estimates record counts and computes risk level
- Validates dependency ordering across object types
- Retrieves field mapping configurations
- Builds a `MigrationPlan` with deterministic step ordering
- Validates the plan schema before returning
- Persists the plan to the plans store

---

## Input Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | Yes | Natural language planning goal |
| `migration_id` | string | Yes | Unique migration identifier |
| `tenant_id` | string | Yes | Multi-tenant identifier |
| `object_types` | string[] | No | Salesforce object types to include — default `["Account"]` |
| `source_system` | string | No | `oracle_ebs` / `sql_server` / other |
| `target_system` | string | No | `salesforce` — default |
| `run_id` | string | No | Existing run ID when replanning an existing migration |

---

## Output Schema (`MigrationPlan`)

| Field | Type | Description |
|-------|------|-------------|
| `plan_id` | string | Globally unique plan ID (`plan-<hex>`) |
| `plan_version` | string | Semantic version, starts at `1.0.0` |
| `migration_id` | string | Echoed from input |
| `tenant_id` | string | Echoed from input |
| `status` | enum | `APPROVED` / `BLOCKED` / `DRAFT` / `SUPERSEDED` |
| `blocking_reason` | string | Populated when `status=BLOCKED` |
| `steps` | PlanStep[] | Ordered execution steps (empty if BLOCKED) |
| `readiness_checks` | ReadinessCheck[] | Pre-planning check results |
| `estimated_duration_minutes` | int | Sum of step durations |
| `estimated_record_count` | int | Total records across all objects |
| `risk_level` | enum | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` |
| `blocking_checks` | string[] | Names of checks that failed |
| `created_at` | ISO 8601 | UTC timestamp |
| `created_by` | string | `"planning-agent"` |
| `object_types` | string[] | Objects included in this plan |

### PlanStep Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `step_id` | string | Yes | Unique within plan (`step-NNN-<action>-<object>`) |
| `name` | string | Yes | Short snake_case label |
| `description` | string | Yes | One-sentence step description |
| `agent` | enum | Yes | `execution-agent` / `validation-agent` / `security-agent` |
| `action` | enum | Yes | `extract` / `transform` / `validate` / `load` / `reconcile` / `audit` |
| `depends_on` | string[] | Yes | `step_id`s this step waits for (`[]` for first step) |
| `timeout_seconds` | int | Yes | Max execution time (min 60) |
| `success_criteria` | string[] | Yes | At least 2 measurable conditions |
| `rollback_action` | string | No | Action on failure; null if irreversible |
| `estimated_duration_seconds` | int | Yes | Wall-clock estimate |
| `max_retries` | int | No | 0–5, default 2 |
| `is_idempotent` | bool | Yes | Safe to retry without side effects |
| `object_types` | string[] | No | Objects touched by this step |

---

## Canonical Step Order

| Step | Action | Agent | Depends On |
|------|--------|-------|-----------|
| `step-001-extract` | extract | execution-agent | `[]` |
| `step-002-transform` | transform | execution-agent | `step-001` |
| `step-003-validate` | validate | validation-agent | `step-002` |
| `step-NNN-load-<object>` | load | execution-agent | previous load or `step-003` |
| `step-FIN-reconcile` | reconcile | validation-agent | last load step |

SOX-scoped additions:
- `step-000-audit` (security-agent, action: audit) prepended before extract
- Duration: +20% for Oracle EBS transform steps

---

## Tool Access Rules

| Tool | Purpose | Read/Write |
|------|---------|-----------|
| `check_migration_readiness` | Pre-planning checks | Read |
| `estimate_record_counts` | Source record counts | Read |
| `check_dependency_order` | Validate load ordering | Compute |
| `validate_plan_schema` | Draft plan validation | Compute |
| `get_field_mapping_config` | Field mapping retrieval | Read |
| `check_salesforce_schema` | SF object schema | Read |
| `store_plan` | Persist finalised plan | Write (local only) |

**Forbidden tools**: Any tool prefixed `execute_`, `run_`, `start_`, `pause_`, `resume_`, `cancel_`, `write_`, `upsert_`, `delete_`, `scale_`, `retry_`

---

## Determinism Contract

The planning agent operates at `temperature=0.0`. For identical inputs:
- Plan structure is identical across runs
- Step IDs follow `step-NNN-<action>-<object>` convention
- Risk level computed deterministically from `total_records`
- Duration estimates follow fixed formulas:
  - extract: `ceil(records / 10000) * 2 + 5` minutes
  - transform: `ceil(records / 10000) * 3 + 5` minutes (x1.2 for Oracle EBS)
  - validate: `ceil(records / 10000) * 1.5 + 5` minutes
  - load: `ceil(records / 10000) * 2.5 + 5` minutes per object
  - reconcile: flat 20 minutes

---

## Risk Level Computation

| Condition | Risk Level |
|-----------|-----------|
| `total_records < 50,000` AND no SOX scope | `LOW` |
| `50,000 <= total_records <= 500,000` AND no SOX scope | `MEDIUM` |
| `total_records > 500,000` OR any SOX scope | `HIGH` |
| `total_records > 2,000,000` | `CRITICAL` |

---

## Blocking Behaviour

If `check_migration_readiness` returns any check with `passed=false` AND `blocking=true`:
1. Set `plan.status = BLOCKED`
2. Set `plan.blocking_reason` to the failing check name and detail
3. Set `plan.steps = []`
4. Return immediately — do not call further tools

---

## Object Dependency Ordering

| Child Object | Must Follow |
|-------------|-------------|
| `Contact` | `Account` |
| `Opportunity` | `Account` |
| `OpportunityLineItem` | `Opportunity`, `Product2` |
| `Case` | `Account`, `Contact` |
| `Contract` | `Account` |
| `Order` | `Account`, `Contract` |
| `OrderItem` | `Order`, `Product2` |
| `Asset` | `Account`, `Contact`, `Product2` |

---

## Plan Versioning

- First plan for a migration: `plan_version = "1.0.0"`
- Each replan increments the patch version: `1.0.1`, `1.0.2`
- Superseded plans have `status = SUPERSEDED`
- Plans are stored at `data/plans/<plan_id>.json`

---

## Failure Handling

| Failure Mode | Behaviour |
|-------------|-----------|
| Readiness check fails (blocking) | `BLOCKED` plan returned, no execution |
| `estimate_record_counts` API error | Use stub estimates, log warning |
| `store_plan` I/O error | Log warning, return plan anyway (in-memory) |
| Claude API error | Return `BLOCKED` plan with `blocking_reason = planning_error` |
| Schema validation fails | Return plan as `DRAFT` with validation errors in `blocking_checks` |
