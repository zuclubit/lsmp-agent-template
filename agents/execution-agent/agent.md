# Execution Agent Specification

**Version**: 2.0.0
**Model**: `claude-sonnet-4-6` (override: `ANTHROPIC_MODEL` env var)
**Temperature**: 0.0 (deterministic)
**Last Updated**: 2026-03

---

## Purpose

The Execution Agent executes **exactly one** step from a MigrationPlan per invocation. It is a single-step executor — it does NOT decide what step to run next, does NOT iterate through a plan, and does NOT manage orchestration. The orchestrator calls this agent once per step.

The agent is responsible for:
1. **Idempotency check** — verify the step has not already been completed before executing
2. **Step execution** — invoke the migration API to run the step
3. **Checkpoint persistence** — save a checkpoint after each successful execution
4. **Dry-run support** — log all intended actions without making state changes
5. **Operator authorization** — destructive steps require an `operator_id`

---

## Design Decisions

### Single-Step Architecture

The previous execution agent was a multi-step orchestrator that maintained in-process `CheckpointStore`, looped over a plan, and held file-backed state. This caused:
- Non-idempotent re-runs on crash recovery
- Unclear responsibility boundaries with the orchestrator
- Difficult-to-test internal state management

The redesigned agent receives ONE `PlanStep`, executes it, and returns a `StepResult`. The orchestrator manages sequencing.

### Default FAILED Posture

`StepResult.status` defaults to `StepStatus.FAILED`. The agent must prove success via tool results. This prevents silent failures where an incomplete agentic loop returns a misleading status.

### Idempotency-First Workflow

The agent ALWAYS calls `check_step_idempotency` before `execute_migration_step`. If `already_completed=True`, it returns `ALREADY_COMPLETED` without re-executing. This makes the agent safe to call multiple times for the same step (e.g., on retry after orchestrator crash).

---

## Input Schema (`ExecutionContext`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plan_step.step_id` | string | Yes | Unique step identifier |
| `plan_step.step_type` | enum | Yes | One of the allowed step types |
| `plan_step.entity_name` | string | Yes | Salesforce/source entity (e.g. `Account`) |
| `plan_step.phase` | string | Yes | Migration phase |
| `plan_step.sequence_number` | int (>=1) | Yes | Order within the plan |
| `plan_step.config` | object | No | Step-specific configuration |
| `plan_step.depends_on` | string[] | No | step_ids this step depends on |
| `plan_step.is_destructive` | boolean | No | Whether step modifies or deletes data (default: false) |
| `migration_id` | string | Yes | Migration run identifier |
| `tenant_id` | string | Yes | Multi-tenant identifier |
| `operator_id` | string | Conditional | Required when step_type is destructive |
| `dry_run` | boolean | No | Simulate execution without state changes (default: false) |
| `correlation_id` | UUID | No | Auto-generated request correlation ID for tracing |

### Allowed Step Types

| Step Type | Phase | Destructive | Requires operator_id |
|-----------|-------|-------------|----------------------|
| `extract` | extraction | No | No |
| `transform` | transformation | No | No |
| `load` | loading | No | No |
| `validate` | validation | No | No |
| `notify` | any | No | No |
| `checkpoint` | any | No | No |
| `bulk_delete` | any | Yes | **Yes** |
| `truncate_staging` | any | Yes | **Yes** |
| `rollback` | any | Yes | **Yes** |
| `archive` | any | Yes | **Yes** |
| `deactivate_records` | any | Yes | **Yes** |

---

## Output Schema (`StepResult`)

| Field | Type | Description |
|-------|------|-------------|
| `step_id` | string | Echoed from input |
| `status` | enum | `COMPLETED` / `FAILED` / `SKIPPED` / `BLOCKED` / `ALREADY_COMPLETED` |
| `records_processed` | int | Number of records processed (0 on failure or dry run) |
| `duration_ms` | int | Wall-clock execution time in milliseconds |
| `checkpoint_id` | UUID | Checkpoint created after successful execution (null on failure) |
| `error` | string | Error message if status is FAILED or BLOCKED |
| `dry_run` | bool | Whether this was a dry-run invocation |
| `completed_at` | ISO 8601 | Timestamp when step completed (null on failure) |
| `metadata` | object | Additional context: idempotency status, checkpoint status, error list |

### Status Values

| Status | Meaning |
|--------|---------|
| `COMPLETED` | Step executed successfully and checkpoint created |
| `FAILED` | Step execution failed — see `error` field |
| `SKIPPED` | Step was skipped by the agent |
| `BLOCKED` | Execution refused — destructive step without operator_id |
| `ALREADY_COMPLETED` | Idempotency check confirmed step already ran — not re-executed |

---

## Execution Workflow

```
ExecutionContext received
        |
        v
1. [Guard] Is step_type destructive AND operator_id missing?
        |  YES -> return StepResult(status=BLOCKED)
        |  NO   |
        v
2. check_step_idempotency(step_id, migration_id)
        |  already_completed=True -> return StepResult(status=ALREADY_COMPLETED)
        |  already_completed=False |
        v
3. [Optional] get_migration_phase_status(migration_id, phase)
        |  Verify phase prerequisites are met
        v
4. execute_migration_step(step_id, migration_id, tenant_id, dry_run)
        |  FAILED -> [Optional] pause_migration if critical
        |         -> return StepResult(status=FAILED)
        |  COMPLETED / DRY_RUN_SIMULATED |
        v
5. create_checkpoint(step_id, migration_id, state)
        v
6. return StepResult(status=COMPLETED, checkpoint_id=...)
```

---

## Tools

### `check_step_idempotency`
- **Always called first** before any execution.
- Queries `GET /migrations/{migration_id}/steps/{step_id}/idempotency`.
- Returns `already_completed`, `checkpoint_id`, `completed_at`, `records_processed`.
- On HTTP failure: returns `status=UNKNOWN` with a warning — agent proceeds cautiously.

### `execute_migration_step`
- Calls `POST /migrations/{migration_id}/steps/{step_id}/execute`.
- In `dry_run=True` mode: returns `DRY_RUN_SIMULATED` without calling the execution endpoint.
- Returns `status`, `records_processed`, `duration_ms`, `errors` (first 10).

### `create_checkpoint`
- Calls `POST /migrations/{migration_id}/checkpoints`.
- Stores `{step_id, state, created_at}`.
- Must be called after every successful execution.
- On failure: logged but does not cause step failure (best-effort).

### `pause_migration`
- Calls `POST /migrations/{migration_id}/pause`.
- Requires `operator_id` — rejected without it.
- Used when step fails critically and the migration should not auto-continue.

### `get_migration_phase_status`
- Calls `GET /migrations/{migration_id}/phases/{phase}`.
- Returns `steps_total`, `steps_completed`, `steps_failed`, `steps_skipped`.
- Used to verify phase prerequisites before executing a step.

---

## HTTP Retry Policy

All tool HTTP calls use exponential backoff:
- **Max retries**: 3
- **Delays**: 1s -> 2s -> 4s
- **Timeout**: 60 seconds per request
- **Errors retried**: `httpx.HTTPStatusError`, `httpx.RequestError`

---

## Dry-Run Mode

When `dry_run=True`:
- The agent calls `execute_migration_step` with `dry_run=True`
- The tool logs the intended action but makes no HTTP call to the execution endpoint
- Returns `status=DRY_RUN_SIMULATED` and `records_processed=0`
- A checkpoint is still created to record the dry-run event
- `StepResult.dry_run=True` is propagated to the caller

Use dry-run for:
- Pre-flight validation of a migration plan
- Operator review before committing destructive steps
- Integration testing without side effects

---

## Operator Authorization

Destructive step types require `operator_id` in `ExecutionContext`:

```python
context = ExecutionContext(
    plan_step=PlanStep(step_type="bulk_delete", ...),
    migration_id="mig-001",
    tenant_id="tenant-abc",
    operator_id="op-jane-doe",  # Required for destructive steps
)
```

If `operator_id` is absent for a destructive step, the Pydantic `model_validator` raises `ValueError` before the agent is invoked.

`pause_migration` also requires `operator_id` regardless of step type.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Anthropic model ID |
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `EXECUTION_AGENT_MAX_TOKENS` | `4096` | Maximum tokens per LLM response |
| `EXECUTION_AGENT_MAX_ITERATIONS` | `15` | Maximum agentic loop iterations |
| `MIGRATION_API_BASE_URL` | `http://localhost:8000/api/v1` | Migration platform API base URL |
| `INTERNAL_SERVICE_TOKEN` | `""` | Bearer token for internal API calls |

---

## Example Invocations

### Standard Extract Step

```python
from agents.execution_agent.agent import execute_single_step, PlanStep

result = await execute_single_step(
    plan_step=PlanStep(
        step_id="mig-001-extract-account-001",
        step_type="extract",
        entity_name="Account",
        phase="extraction",
        sequence_number=1,
        config={"batch_size": 2000, "source_table": "dbo.Accounts"},
    ),
    migration_id="mig-001",
    tenant_id="tenant-acme",
)

if result.status.value == "COMPLETED":
    print(f"Extracted {result.records_processed} records (checkpoint: {result.checkpoint_id})")
else:
    print(f"Step failed: {result.error}")
```

### Destructive Step with Operator Authorization

```python
result = await execute_single_step(
    plan_step=PlanStep(
        step_id="mig-001-truncate-001",
        step_type="truncate_staging",
        entity_name="staging_accounts",
        phase="loading",
        sequence_number=5,
        is_destructive=True,
    ),
    migration_id="mig-001",
    tenant_id="tenant-acme",
    operator_id="op-john-smith",  # Required
)
```

### Dry Run

```python
result = await execute_single_step(
    plan_step=step,
    migration_id="mig-001",
    tenant_id="tenant-acme",
    dry_run=True,
)
assert result.dry_run is True
```

---

## What the Execution Agent Does NOT Do

- **Does NOT orchestrate** — does not call itself recursively or loop over plan steps
- **Does NOT validate security** — Security Agent must run first as a blocking gate
- **Does NOT validate data quality** — that is the Validation Agent's responsibility
- **Does NOT decide the next step** — the orchestrator calls this agent per step
- **Does NOT modify the migration plan** — reads the plan, does not alter it
- **Does NOT store state in-process** — all state is persisted via API checkpoints

---

## Limitations

1. **API availability**: All tools depend on `MIGRATION_API_BASE_URL`. If the API is unreachable after 3 retries, tools return error state.
2. **Max iterations**: If the agentic loop exceeds `EXECUTION_AGENT_MAX_ITERATIONS` (default 15), the loop terminates and `_build_step_result` derives status from accumulated tool results.
3. **Checkpoint best-effort**: If `create_checkpoint` fails, the step is still marked COMPLETED. Monitor checkpoint creation separately for critical migrations.
4. **Idempotency on UNKNOWN**: If the idempotency check returns `status=UNKNOWN` (API unavailable), the agent proceeds with execution. Monitor for duplicate executions in this scenario.
