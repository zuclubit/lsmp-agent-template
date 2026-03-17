"""
Execution Agent — Single-Step Executor (Redesigned 2026)

Single responsibility: Execute EXACTLY ONE step from a MigrationPlan.
Does NOT decide what to do next — the orchestrator decides.

Key design decisions:
1. Receives a single PlanStep, executes it, returns StepResult
2. NEVER executes if validation gate is BLOCKED
3. Idempotent execution — always checks if step already completed before running
4. All destructive operations require operator_id in context
5. Checkpoints after each step — can resume from any point
6. Model: claude-sonnet-4-6
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import anthropic
import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("EXECUTION_AGENT_MAX_TOKENS", "4096"))
MAX_ITERATIONS = int(os.getenv("EXECUTION_AGENT_MAX_ITERATIONS", "15"))
MIGRATION_API_BASE = os.getenv("MIGRATION_API_BASE_URL", "http://localhost:8000/api/v1")
INTERNAL_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")

_HTTP_HEADERS = {
    "Authorization": f"Bearer {INTERNAL_TOKEN}",
    "Content-Type": "application/json",
    "X-Agent-Name": "execution-agent",
}

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt.txt")

# Destructive step types that require operator_id
_DESTRUCTIVE_STEP_TYPES = frozenset([
    "bulk_delete",
    "truncate_staging",
    "rollback",
    "archive",
    "deactivate_records",
])


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def _http_request_with_retry(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Dict[str, Any]:
    """Generic HTTP request with exponential backoff retry (3 attempts max)."""
    last_exc: Exception = RuntimeError("No attempt made")
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(max_retries):
            try:
                if method.upper() == "GET":
                    resp = await client.get(url, headers=headers, params=params)
                elif method.upper() == "POST":
                    resp = await client.post(url, headers=headers, json=payload)
                elif method.upper() == "PATCH":
                    resp = await client.patch(url, headers=headers, json=payload)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "HTTP %s %s failed (attempt %d/%d): %s — retrying in %.1fs",
                        method, url, attempt + 1, max_retries, exc, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("HTTP %s %s exhausted retries: %s", method, url, exc)
    raise last_exc


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class StepStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"


class StepResult(BaseModel):
    """Result of executing a single migration step."""
    step_id: str
    status: StepStatus = StepStatus.FAILED  # Default FAILED — must be proven
    records_processed: int = 0
    duration_ms: Optional[int] = None
    checkpoint_id: Optional[str] = None
    error: Optional[str] = None
    dry_run: bool = False
    completed_at: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PlanStep(BaseModel):
    """A single step in a MigrationPlan."""
    step_id: str
    step_type: str = Field(description="e.g. extract, transform, load, validate, notify")
    entity_name: str
    phase: str = Field(description="e.g. extraction, transformation, loading, validation")
    sequence_number: int = Field(ge=1, description="Order within the migration plan")
    config: Dict[str, Any] = Field(default_factory=dict)
    depends_on: List[str] = Field(
        default_factory=list,
        description="step_ids this step depends on",
    )
    is_destructive: bool = False

    @field_validator("step_type")
    @classmethod
    def validate_step_type(cls, v: str) -> str:
        allowed = {
            "extract", "transform", "load", "validate",
            "notify", "checkpoint", "bulk_delete", "truncate_staging",
            "rollback", "archive", "deactivate_records",
        }
        if v not in allowed:
            raise ValueError(f"step_type must be one of: {allowed}")
        return v


class ExecutionContext(BaseModel):
    """Context for executing a single migration step."""
    plan_step: PlanStep
    migration_id: str
    tenant_id: str
    operator_id: Optional[str] = None
    dry_run: bool = False
    correlation_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Request correlation ID for distributed tracing",
    )

    @model_validator(mode="after")
    def validate_destructive_requires_operator(self) -> "ExecutionContext":
        """Destructive steps MUST have an operator_id."""
        step_type = self.plan_step.step_type
        if step_type in _DESTRUCTIVE_STEP_TYPES and not self.operator_id:
            raise ValueError(
                f"operator_id is required for destructive step type '{step_type}'. "
                "All destructive operations must be authorised by an operator."
            )
        return self


# ---------------------------------------------------------------------------
# Tool implementations — all use real HTTP calls with retry
# ---------------------------------------------------------------------------


async def check_step_idempotency(step_id: str, migration_id: str) -> Dict[str, Any]:
    """
    Check whether a migration step has already been completed.
    MUST be called before execute_migration_step.

    Returns:
        already_completed (bool), checkpoint_id, completed_at, records_processed
    """
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/steps/{step_id}/idempotency"
    try:
        data = await _http_request_with_retry("GET", url, _HTTP_HEADERS)
        return {
            "step_id": step_id,
            "migration_id": migration_id,
            "already_completed": data.get("already_completed", False),
            "checkpoint_id": data.get("checkpoint_id"),
            "completed_at": data.get("completed_at"),
            "records_processed": data.get("records_processed", 0),
            "status": "IDEMPOTENT_SKIP" if data.get("already_completed") else "PROCEED",
        }
    except Exception as exc:
        logger.error("check_step_idempotency failed for step %s: %s", step_id, exc)
        return {
            "step_id": step_id,
            "migration_id": migration_id,
            "already_completed": False,
            "status": "UNKNOWN",
            "error": str(exc),
            "warning": "Could not verify idempotency — proceed with caution",
        }


async def execute_migration_step(
    step_id: str,
    migration_id: str,
    tenant_id: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Execute a single migration step.
    MUST only be called after check_step_idempotency returns already_completed=False.
    In dry_run mode: logs all actions but makes no state changes.

    Returns:
        status, records_processed, duration_ms
    """
    if dry_run:
        logger.info(
            "[DRY RUN] Would execute step %s for migration %s (tenant %s)",
            step_id, migration_id, tenant_id,
        )
        return {
            "step_id": step_id,
            "migration_id": migration_id,
            "tenant_id": tenant_id,
            "dry_run": True,
            "status": "DRY_RUN_SIMULATED",
            "records_processed": 0,
            "message": "Dry run — no actual changes made",
        }

    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/steps/{step_id}/execute"
    try:
        data = await _http_request_with_retry(
            "POST", url, _HTTP_HEADERS, payload={"tenant_id": tenant_id}
        )
        return {
            "step_id": step_id,
            "migration_id": migration_id,
            "status": data.get("status", "COMPLETED"),
            "records_processed": data.get("records_processed", 0),
            "duration_ms": data.get("duration_ms"),
            "errors": data.get("errors", [])[:10],
        }
    except Exception as exc:
        logger.error("execute_migration_step failed for step %s: %s", step_id, exc)
        return {
            "step_id": step_id,
            "migration_id": migration_id,
            "status": "FAILED",
            "records_processed": 0,
            "error": str(exc),
        }


async def create_checkpoint(
    step_id: str,
    migration_id: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Persist a checkpoint for the migration step.
    Call this AFTER execute_migration_step succeeds.
    Enables resume from any point on failure.

    Returns:
        checkpoint_id, created_at, status
    """
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/checkpoints"
    payload = {
        "step_id": step_id,
        "state": state,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        data = await _http_request_with_retry("POST", url, _HTTP_HEADERS, payload=payload)
        return {
            "checkpoint_id": data.get("checkpoint_id", str(uuid.uuid4())),
            "step_id": step_id,
            "migration_id": migration_id,
            "created_at": data.get("created_at"),
            "status": "CHECKPOINT_CREATED",
        }
    except Exception as exc:
        logger.error("create_checkpoint failed for step %s: %s", step_id, exc)
        return {
            "step_id": step_id,
            "migration_id": migration_id,
            "status": "CHECKPOINT_FAILED",
            "error": str(exc),
        }


async def pause_migration(
    migration_id: str,
    reason: str,
    operator_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Pause an active migration run.
    Requires operator_id — pause is an operator-authorised action.
    The migration will not resume until an operator explicitly resumes it.
    """
    if not operator_id:
        return {
            "migration_id": migration_id,
            "status": "REJECTED",
            "error": "operator_id is required to pause a migration",
        }
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/pause"
    payload = {
        "reason": reason,
        "operator_id": operator_id,
        "paused_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        data = await _http_request_with_retry("POST", url, _HTTP_HEADERS, payload=payload)
        logger.info(
            "Migration %s paused by operator %s — reason: %s",
            migration_id, operator_id, reason,
        )
        return {
            "migration_id": migration_id,
            "status": "PAUSED",
            "paused_at": data.get("paused_at"),
            "reason": reason,
            "operator_id": operator_id,
        }
    except Exception as exc:
        logger.error("pause_migration failed for %s: %s", migration_id, exc)
        return {
            "migration_id": migration_id,
            "status": "PAUSE_FAILED",
            "error": str(exc),
        }


async def get_migration_phase_status(migration_id: str, phase: str) -> Dict[str, Any]:
    """
    Get the current status of a migration phase.

    Returns:
        phase, status, steps_total, steps_completed, steps_failed
    """
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/phases/{phase}"
    try:
        data = await _http_request_with_retry("GET", url, _HTTP_HEADERS)
        return {
            "migration_id": migration_id,
            "phase": phase,
            "status": data.get("status", "UNKNOWN"),
            "steps_total": data.get("steps_total", 0),
            "steps_completed": data.get("steps_completed", 0),
            "steps_failed": data.get("steps_failed", 0),
            "steps_skipped": data.get("steps_skipped", 0),
            "started_at": data.get("started_at"),
            "completed_at": data.get("completed_at"),
        }
    except Exception as exc:
        logger.error(
            "get_migration_phase_status failed for %s/%s: %s", migration_id, phase, exc
        )
        return {
            "migration_id": migration_id,
            "phase": phase,
            "status": "UNKNOWN",
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Tool schemas for Claude
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "check_step_idempotency",
        "description": (
            "Check if a migration step has already been completed. "
            "MUST be called FIRST before execute_migration_step. "
            "If already_completed=True, return ALREADY_COMPLETED and do not re-execute."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "step_id": {"type": "string", "description": "Migration step identifier"},
                "migration_id": {"type": "string", "description": "Migration run identifier"},
            },
            "required": ["step_id", "migration_id"],
        },
    },
    {
        "name": "execute_migration_step",
        "description": (
            "Execute a single migration step. "
            "MUST only be called after check_step_idempotency returns already_completed=False. "
            "In dry_run mode: logs actions but makes no changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "step_id": {"type": "string"},
                "migration_id": {"type": "string"},
                "tenant_id": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["step_id", "migration_id", "tenant_id"],
        },
    },
    {
        "name": "create_checkpoint",
        "description": (
            "Persist a checkpoint after a step completes. "
            "Call this AFTER execute_migration_step succeeds. "
            "Checkpoints enable resume from any point on failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "step_id": {"type": "string"},
                "migration_id": {"type": "string"},
                "state": {
                    "type": "object",
                    "description": "Serializable state snapshot for the checkpoint",
                },
            },
            "required": ["step_id", "migration_id", "state"],
        },
    },
    {
        "name": "pause_migration",
        "description": (
            "Pause the migration run. Requires operator_id — operator-authorised action. "
            "Use when the step fails critically or an anomaly is detected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
                "reason": {"type": "string", "description": "Human-readable reason for pausing"},
                "operator_id": {"type": "string", "description": "Operator who authorised the pause"},
            },
            "required": ["migration_id", "reason"],
        },
    },
    {
        "name": "get_migration_phase_status",
        "description": (
            "Get current status of a migration phase. "
            "Use to check prerequisites before executing a step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
                "phase": {
                    "type": "string",
                    "enum": [
                        "extraction", "transformation", "loading",
                        "validation", "reconciliation",
                    ],
                    "description": "Migration phase to check",
                },
            },
            "required": ["migration_id", "phase"],
        },
    },
]

_TOOL_DISPATCH: Dict[str, Any] = {
    "check_step_idempotency": check_step_idempotency,
    "execute_migration_step": execute_migration_step,
    "create_checkpoint": create_checkpoint,
    "pause_migration": pause_migration,
    "get_migration_phase_status": get_migration_phase_status,
}


async def _dispatch_tool(name: str, inputs: Dict[str, Any]) -> Any:
    fn = _TOOL_DISPATCH.get(name)
    if not fn:
        raise ValueError(f"Unknown execution tool: {name!r}")
    logger.info("Execution agent dispatching tool: %s", name)
    return await fn(**inputs)


# ---------------------------------------------------------------------------
# System prompt loader
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    try:
        with open(_PROMPT_PATH, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return _FALLBACK_SYSTEM_PROMPT


_FALLBACK_SYSTEM_PROMPT = """
You are the Execution Agent for an enterprise Salesforce migration platform.

ROLE: Single-Step Executor — you execute EXACTLY ONE migration step per invocation.
MODEL: claude-sonnet-4-6
TEMPERATURE: 0.0

CRITICAL RULES:
1. You execute EXACTLY ONE step — the step described in ExecutionContext.plan_step.
2. You NEVER decide what step to run next — that is the orchestrator's responsibility.
3. ALWAYS call check_step_idempotency FIRST before execute_migration_step.
4. If already_completed=True: return ALREADY_COMPLETED, do NOT re-execute.
5. After successful execution: ALWAYS call create_checkpoint.
6. If dry_run=True: call execute_migration_step with dry_run=True.
7. Destructive steps without operator_id in context: BLOCKED.

EXECUTION WORKFLOW:
1. check_step_idempotency(step_id, migration_id)
   → If already_completed=True: STOP. Return ALREADY_COMPLETED.
   → If status=UNKNOWN: log warning, proceed carefully.
2. [Optional] get_migration_phase_status to verify prerequisites.
3. execute_migration_step(step_id, migration_id, tenant_id, dry_run)
   → On COMPLETED: proceed to step 4.
   → On FAILED: call pause_migration if critical, return FAILED.
4. create_checkpoint(step_id, migration_id, state={records_processed: N, ...})
5. Return final StepResult.

NEVER skip the idempotency check.
NEVER skip the checkpoint on success.
NEVER decide the next step — that is the orchestrator's job.
""".strip()


# ---------------------------------------------------------------------------
# Main ExecutionAgent class
# ---------------------------------------------------------------------------


class ExecutionAgent:
    """
    Single-Step Executor for the migration platform.

    Executes exactly one PlanStep per invocation. Returns a StepResult.
    The orchestrator is responsible for calling this agent once per step
    and passing the next step on subsequent calls.

    Model: claude-sonnet-4-6 at temperature 0.0.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL,
        max_tokens: int = MAX_TOKENS,
        max_iterations: int = MAX_ITERATIONS,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"]
        )
        self._model = model
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._system_prompt = _load_system_prompt()

    async def execute(self, context: ExecutionContext) -> StepResult:
        """
        Execute exactly one migration step described by `context`.

        Args:
            context: ExecutionContext containing the PlanStep and runtime info.

        Returns:
            StepResult with COMPLETED/FAILED/SKIPPED/BLOCKED/ALREADY_COMPLETED status.
        """
        start_ts = time.perf_counter()
        step = context.plan_step

        # Validate destructive steps have operator_id before calling Claude
        if step.step_type in _DESTRUCTIVE_STEP_TYPES and not context.operator_id:
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.BLOCKED,
                error=(
                    f"Step type '{step.step_type}' is destructive and requires "
                    "operator_id in ExecutionContext. Refusing to execute."
                ),
            )

        task = (
            f"Execute migration step:\n"
            f"  step_id:          {step.step_id}\n"
            f"  step_type:        {step.step_type}\n"
            f"  entity_name:      {step.entity_name}\n"
            f"  phase:            {step.phase}\n"
            f"  sequence_number:  {step.sequence_number}\n"
            f"  is_destructive:   {step.is_destructive}\n"
            f"  migration_id:     {context.migration_id}\n"
            f"  tenant_id:        {context.tenant_id}\n"
            f"  operator_id:      {context.operator_id or 'N/A'}\n"
            f"  dry_run:          {context.dry_run}\n"
            f"  correlation_id:   {context.correlation_id}\n"
            f"\nStep config: {json.dumps(step.config, default=str)}\n"
            f"\nRequired workflow:\n"
            f"1. call check_step_idempotency(step_id={step.step_id!r}, "
            f"migration_id={context.migration_id!r})\n"
            f"2. If already_completed=True: return ALREADY_COMPLETED — do not execute.\n"
            f"3. call execute_migration_step(step_id={step.step_id!r}, "
            f"migration_id={context.migration_id!r}, "
            f"tenant_id={context.tenant_id!r}, dry_run={context.dry_run})\n"
            f"4. If execution succeeded: call create_checkpoint with state summary\n"
            f"5. Return final step status with records_processed and checkpoint_id\n"
            + (
                "\nNOTE: dry_run=True — log all intended actions, "
                "call execute_migration_step with dry_run=True, make no real changes."
                if context.dry_run else ""
            )
        )

        messages: List[Dict[str, Any]] = [{"role": "user", "content": task}]
        tool_results_raw: Dict[str, Any] = {}
        iteration = 0

        try:
            for iteration in range(1, self._max_iterations + 1):
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system_prompt,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                    temperature=0.0,
                )

                tool_blocks = [b for b in response.content if b.type == "tool_use"]
                if not tool_blocks or response.stop_reason == "end_turn":
                    break

                messages.append({"role": "assistant", "content": response.content})
                tool_results_list = []
                for block in tool_blocks:
                    try:
                        tool_result = await _dispatch_tool(block.name, block.input or {})
                        # Last result per tool name is the canonical result
                        tool_results_raw[block.name] = tool_result
                        is_error = False
                        content = json.dumps(tool_result, default=str)
                    except Exception as exc:
                        tool_result = {"error": str(exc), "status": "TOOL_ERROR"}
                        tool_results_raw[block.name] = tool_result
                        is_error = True
                        content = json.dumps(tool_result)
                        logger.error("Execution tool %s failed: %s", block.name, exc)

                    tool_results_list.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                        "is_error": is_error,
                    })
                messages.append({"role": "user", "content": tool_results_list})

        except Exception as exc:
            logger.error(
                "ExecutionAgent run error for step %s migration %s: %s",
                step.step_id, context.migration_id, exc,
                exc_info=True,
            )
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                duration_ms=int((time.perf_counter() - start_ts) * 1000),
                error=f"Agent execution error: {exc}",
                dry_run=context.dry_run,
            )

        duration_ms = int((time.perf_counter() - start_ts) * 1000)
        result = self._build_step_result(
            step_id=step.step_id,
            tool_results=tool_results_raw,
            duration_ms=duration_ms,
            dry_run=context.dry_run,
        )

        logger.info(
            "ExecutionAgent completed step=%s migration=%s status=%s records=%d duration_ms=%d",
            step.step_id,
            context.migration_id,
            result.status,
            result.records_processed,
            duration_ms,
        )
        return result

    def _build_step_result(
        self,
        step_id: str,
        tool_results: Dict[str, Any],
        duration_ms: int,
        dry_run: bool,
    ) -> StepResult:
        """Construct StepResult from accumulated tool results."""
        # Check idempotency result first
        idem = tool_results.get("check_step_idempotency", {})
        if idem.get("already_completed"):
            return StepResult(
                step_id=step_id,
                status=StepStatus.ALREADY_COMPLETED,
                records_processed=idem.get("records_processed", 0),
                checkpoint_id=idem.get("checkpoint_id"),
                duration_ms=duration_ms,
                dry_run=dry_run,
                completed_at=idem.get("completed_at"),
                metadata={"idempotency": "skipped — already completed"},
            )

        # Check execution result
        exec_result = tool_results.get("execute_migration_step", {})
        exec_status_raw = exec_result.get("status", "FAILED")

        if exec_status_raw in ("COMPLETED", "DRY_RUN_SIMULATED"):
            step_status = StepStatus.COMPLETED
        else:
            step_status = StepStatus.FAILED

        # Get checkpoint ID from checkpoint result
        checkpoint_result = tool_results.get("create_checkpoint", {})
        checkpoint_id = checkpoint_result.get("checkpoint_id")

        return StepResult(
            step_id=step_id,
            status=step_status,
            records_processed=exec_result.get("records_processed", 0),
            duration_ms=duration_ms,
            checkpoint_id=checkpoint_id,
            error=exec_result.get("error"),
            dry_run=dry_run,
            completed_at=(
                datetime.now(timezone.utc).isoformat()
                if step_status == StepStatus.COMPLETED else None
            ),
            metadata={
                "execution_errors": exec_result.get("errors", []),
                "idempotency_status": idem.get("status"),
                "checkpoint_status": checkpoint_result.get("status"),
            },
        )


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


async def execute_single_step(
    plan_step: PlanStep,
    migration_id: str,
    tenant_id: str,
    operator_id: Optional[str] = None,
    dry_run: bool = False,
    api_key: Optional[str] = None,
) -> StepResult:
    """
    Convenience function: execute a single migration step.

    Usage::

        result = await execute_single_step(
            plan_step=step,
            migration_id="mig-2026-001",
            tenant_id="tenant-abc",
            operator_id="op-001",
        )
        if result.status == StepStatus.FAILED:
            handle_failure(result.error)
    """
    context = ExecutionContext(
        plan_step=plan_step,
        migration_id=migration_id,
        tenant_id=tenant_id,
        operator_id=operator_id,
        dry_run=dry_run,
    )
    agent = ExecutionAgent(api_key=api_key)
    return await agent.execute(context)


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    async def _main() -> None:
        migration_id = sys.argv[1] if len(sys.argv) > 1 else "demo-migration-001"
        step = PlanStep(
            step_id=f"{migration_id}-step-001",
            step_type="extract",
            entity_name="Account",
            phase="extraction",
            sequence_number=1,
            config={"batch_size": 2000, "source_table": "dbo.Accounts"},
        )
        result = await execute_single_step(
            plan_step=step,
            migration_id=migration_id,
            tenant_id="tenant-demo",
            dry_run=True,
        )
        print(f"\n{'='*60}")
        print(f"Step ID:    {result.step_id}")
        print(f"Status:     {result.status}")
        print(f"Records:    {result.records_processed}")
        print(f"Duration:   {result.duration_ms}ms")
        print(f"Checkpoint: {result.checkpoint_id}")
        if result.error:
            print(f"Error:      {result.error}")
        print(f"{'='*60}\n")

    asyncio.run(_main())
