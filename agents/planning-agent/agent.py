"""
Planning Agent — Deterministic Migration Plan Generator

Single responsibility: Accept migration job parameters and produce a fully
ordered, deterministic MigrationPlan. No tool calling; pure structured
reasoning via the Anthropic Messages API.

Key design decisions:
1. Output is DETERMINISTIC — same task always produces same plan structure
2. risk_level is calculated in Python before the LLM call — never overrideable by the model
3. Steps always in order: EXTRACT → VALIDATE_SOURCE → TRANSFORM → VALIDATE_TARGET → LOAD → VALIDATE_POST_LOAD
4. SOX scope adds extra steps: PRE_MIGRATION_COMPLIANCE_CHECK, POST_MIGRATION_RECONCILIATION
5. Model: claude-sonnet-4-6 (structured output, not creative reasoning)
6. No tool calling — pure reasoning to generate plan
7. Returns structured JSON matching MigrationPlan schema
8. Logs plan_id and step count at INFO level

API Spec: v1.2.0  |  Multi-tenant  |  SOX-aware
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLANNING_AGENT_MODEL = os.getenv("PLANNING_AGENT_MODEL", "claude-sonnet-4-6")
PLANNING_AGENT_MAX_TOKENS = int(os.getenv("PLANNING_AGENT_MAX_TOKENS", "4096"))
_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt.txt"


def _load_system_prompt() -> str:
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("prompt.txt not found — using inline fallback")
        return (
            "You are a Migration Planning Specialist. "
            "Always output valid JSON matching the MigrationPlan schema. "
            "Follow the step ordering constraints exactly."
        )


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class SourceType(str, Enum):
    LEGACY_CRM = "LEGACY_CRM"
    ORACLE_EBS = "ORACLE_EBS"


class StepType(str, Enum):
    PRE_MIGRATION_COMPLIANCE_CHECK = "PRE_MIGRATION_COMPLIANCE_CHECK"
    EXTRACT = "EXTRACT"
    VALIDATE_SOURCE = "VALIDATE_SOURCE"
    TRANSFORM = "TRANSFORM"
    VALIDATE_TARGET = "VALIDATE_TARGET"
    LOAD = "LOAD"
    VALIDATE_POST_LOAD = "VALIDATE_POST_LOAD"
    POST_MIGRATION_RECONCILIATION = "POST_MIGRATION_RECONCILIATION"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class MaintenanceWindow(BaseModel):
    start_utc: str = Field(..., description="ISO 8601 UTC start time of the maintenance window.")
    end_utc: str = Field(..., description="ISO 8601 UTC end time of the maintenance window.")
    timezone_label: str = Field(default="UTC", description="Human-readable timezone label.")


class PlanStep(BaseModel):
    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    step_type: StepType = Field(..., description="Canonical step type from the ordered enum.")
    description: str = Field(..., min_length=1)
    depends_on: list[str] = Field(
        default_factory=list,
        description="step_id values that must complete before this step begins.",
    )
    estimated_duration_minutes: int = Field(..., ge=0)
    is_blocking: bool = Field(
        default=True,
        description="When True, downstream steps cannot start until this step passes.",
    )
    agent_role: str = Field(default="execution", description="Agent responsible for this step.")


class MigrationPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    source_type: SourceType
    target_org: str = Field(..., min_length=1)
    steps: list[PlanStep] = Field(..., min_length=1)
    total_estimated_minutes: int = Field(..., ge=0)
    risk_level: RiskLevel
    requires_maintenance_window: bool
    maintenance_window: Optional[MaintenanceWindow] = None
    has_sox_scope: bool = Field(default=False)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @model_validator(mode="after")
    def total_minutes_not_less_than_longest_step(self) -> "MigrationPlan":
        if self.steps:
            max_step = max(s.estimated_duration_minutes for s in self.steps)
            if self.total_estimated_minutes < max_step:
                raise ValueError(
                    f"total_estimated_minutes ({self.total_estimated_minutes}) cannot be "
                    f"less than the longest individual step ({max_step} min)."
                )
        return self


class PlanningAgentInput(BaseModel):
    job_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    source_type: SourceType
    target_org: str = Field(..., min_length=1)
    record_counts: dict[str, int] = Field(
        ...,
        description="Map of object_type -> expected record count.",
    )
    has_sox_scope: bool = Field(default=False)
    maintenance_window: Optional[dict[str, Any]] = Field(
        default=None,
        description="Maintenance window parameters; None if online migration.",
    )


class PlanningAgentResult(BaseModel):
    plan_id: str
    job_id: str
    tenant_id: str
    success: bool
    plan: Optional[MigrationPlan] = None
    error: Optional[str] = None
    step_count: int = Field(default=0)
    duration_ms: int = Field(default=0)
    tokens_used: int = Field(default=0)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @model_validator(mode="after")
    def error_required_on_failure(self) -> "PlanningAgentResult":
        if not self.success and not self.error:
            raise ValueError("error must be provided when success is False.")
        return self


# ---------------------------------------------------------------------------
# Risk level calculator (deterministic — no LLM involved)
# ---------------------------------------------------------------------------

_RECORD_THRESHOLD_MEDIUM = 50_000
_RECORD_THRESHOLD_HIGH = 500_000
_RECORD_THRESHOLD_CRITICAL = 2_000_000


def _calculate_risk_level(record_counts: dict[str, int], has_sox_scope: bool) -> RiskLevel:
    """
    Deterministic risk calculation:
      - SOX scope always elevates to at least HIGH
      - Total record count drives the base risk tier:
        < 50,000         -> LOW
        50,000-500,000   -> MEDIUM
        500,000-2,000,000 -> HIGH
        > 2,000,000      -> CRITICAL
    """
    total = sum(record_counts.values())

    if total < _RECORD_THRESHOLD_MEDIUM:
        base = RiskLevel.LOW
    elif total < _RECORD_THRESHOLD_HIGH:
        base = RiskLevel.MEDIUM
    elif total < _RECORD_THRESHOLD_CRITICAL:
        base = RiskLevel.HIGH
    else:
        base = RiskLevel.CRITICAL

    if has_sox_scope:
        _risk_rank: dict[RiskLevel, int] = {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
            RiskLevel.CRITICAL: 3,
        }
        sox_minimum = RiskLevel.HIGH
        if _risk_rank[base] < _risk_rank[sox_minimum]:
            return sox_minimum

    return base


# ---------------------------------------------------------------------------
# Step duration estimator (deterministic)
# ---------------------------------------------------------------------------

_FLAT_DURATIONS: dict[StepType, int] = {
    StepType.PRE_MIGRATION_COMPLIANCE_CHECK: 15,
    StepType.POST_MIGRATION_RECONCILIATION: 20,
}

_DURATION_PER_10K_RECORDS: dict[StepType, float] = {
    StepType.EXTRACT: 2.0,
    StepType.VALIDATE_SOURCE: 1.5,
    StepType.TRANSFORM: 3.0,
    StepType.VALIDATE_TARGET: 1.5,
    StepType.LOAD: 2.5,
    StepType.VALIDATE_POST_LOAD: 1.0,
}

_ORACLE_EBS_TRANSFORM_MULTIPLIER = 1.2  # +20% for complex joins


def _estimate_step_duration(
    step_type: StepType,
    total_records: int,
    source_type: SourceType,
) -> int:
    if step_type in _FLAT_DURATIONS:
        return _FLAT_DURATIONS[step_type]
    per_10k = _DURATION_PER_10K_RECORDS[step_type]
    base = max(5, int((total_records / 10_000) * per_10k) + 5)
    if step_type == StepType.TRANSFORM and source_type == SourceType.ORACLE_EBS:
        base = max(5, int(base * _ORACLE_EBS_TRANSFORM_MULTIPLIER))
    return base


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_STEP_AGENT_ROLES: dict[StepType, str] = {
    StepType.PRE_MIGRATION_COMPLIANCE_CHECK: "security",
    StepType.EXTRACT: "execution",
    StepType.VALIDATE_SOURCE: "validation",
    StepType.TRANSFORM: "execution",
    StepType.VALIDATE_TARGET: "validation",
    StepType.LOAD: "execution",
    StepType.VALIDATE_POST_LOAD: "validation",
    StepType.POST_MIGRATION_RECONCILIATION: "security",
}


def _build_user_prompt(inp: PlanningAgentInput) -> str:
    total_records = sum(inp.record_counts.values())
    risk_level = _calculate_risk_level(inp.record_counts, inp.has_sox_scope)

    mw_info = "No maintenance window defined (online migration)."
    if inp.maintenance_window:
        mw_info = (
            f"Maintenance window: start={inp.maintenance_window.get('start_utc', 'unknown')} "
            f"end={inp.maintenance_window.get('end_utc', 'unknown')} "
            f"tz={inp.maintenance_window.get('timezone_label', 'UTC')}"
        )

    object_breakdown = "\n".join(
        f"  - {obj}: {count:,} records" for obj, count in inp.record_counts.items()
    )

    step_order = (
        "PRE_MIGRATION_COMPLIANCE_CHECK → EXTRACT → VALIDATE_SOURCE → TRANSFORM → "
        "VALIDATE_TARGET → LOAD → VALIDATE_POST_LOAD → POST_MIGRATION_RECONCILIATION"
        if inp.has_sox_scope
        else "EXTRACT → VALIDATE_SOURCE → TRANSFORM → VALIDATE_TARGET → LOAD → VALIDATE_POST_LOAD"
    )

    return f"""Generate a complete MigrationPlan for the following job.

JOB PARAMETERS:
  job_id                  : {inp.job_id}
  tenant_id               : {inp.tenant_id}
  source_type             : {inp.source_type.value}
  target_org              : {inp.target_org}
  has_sox_scope           : {inp.has_sox_scope}
  total_records           : {total_records:,}
  pre_calculated_risk_level : {risk_level.value}

RECORD COUNTS BY OBJECT:
{object_breakdown}

MAINTENANCE WINDOW:
  {mw_info}

REQUIRED STEP ORDER:
  {step_order}

DURATION ESTIMATES:
  Use pre_calculated_risk_level = {risk_level.value} exactly — do not recalculate.
  Use the duration formulas from the system prompt for each step type.
  ORACLE_EBS TRANSFORM duration must include +20% multiplier.

INSTRUCTIONS:
1. Output ONLY a valid JSON object matching the MigrationPlan schema.
2. Do not include any explanation, markdown, or code fences — raw JSON only.
3. Use the pre_calculated_risk_level value exactly as provided.
4. Generate UUID v4 values for plan_id and each step_id.
5. Set depends_on as a serial chain: each step depends on its predecessor only.
6. Set total_estimated_minutes = sum of all step durations.
"""


# ---------------------------------------------------------------------------
# Planning Agent
# ---------------------------------------------------------------------------


class PlanningAgent:
    """
    Produces a deterministic MigrationPlan by invoking claude-sonnet-4-6
    with a structured system prompt and returning parsed JSON.

    No tool calling is used — the agent reasons directly to produce
    the migration plan as a single structured JSON response.

    The risk_level is calculated deterministically in Python and injected
    into the prompt — the LLM cannot override it.
    """

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._model = PLANNING_AGENT_MODEL
        self._system_prompt = _load_system_prompt()

    def run(self, inp: PlanningAgentInput) -> PlanningAgentResult:
        """
        Execute the planning agent synchronously.

        Args:
            inp: Validated PlanningAgentInput with all job parameters.

        Returns:
            PlanningAgentResult with the populated MigrationPlan on success,
            or error details on failure.
        """
        start_ms = int(time.monotonic() * 1000)
        plan_id = str(uuid.uuid4())

        logger.info(
            "planning_agent.start",
            extra={
                "plan_id": plan_id,
                "job_id": inp.job_id,
                "tenant_id": inp.tenant_id,
                "source_type": inp.source_type.value,
                "has_sox_scope": inp.has_sox_scope,
                "total_records": sum(inp.record_counts.values()),
            },
        )

        try:
            user_prompt = _build_user_prompt(inp)

            response = self._client.messages.create(
                model=self._model,
                max_tokens=PLANNING_AGENT_MAX_TOKENS,
                system=self._system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )

            raw_content = response.content[0].text.strip()

            # Strip markdown code fences if the model wraps the JSON
            if raw_content.startswith("```"):
                lines = raw_content.splitlines()
                raw_content = "\n".join(
                    line for line in lines
                    if not line.startswith("```")
                ).strip()

            plan_dict = json.loads(raw_content)

            # Inject IDs that must be authoritative from our side
            plan_dict["plan_id"] = plan_id
            plan_dict["job_id"] = inp.job_id
            plan_dict["tenant_id"] = inp.tenant_id
            plan_dict["source_type"] = inp.source_type.value
            plan_dict["target_org"] = inp.target_org
            plan_dict["has_sox_scope"] = inp.has_sox_scope

            # Enforce risk_level from the deterministically calculated value
            calculated_risk = _calculate_risk_level(inp.record_counts, inp.has_sox_scope)
            plan_dict["risk_level"] = calculated_risk.value

            # Inject maintenance window if provided
            if inp.maintenance_window:
                plan_dict["maintenance_window"] = inp.maintenance_window
                plan_dict["requires_maintenance_window"] = True
            else:
                plan_dict.setdefault("requires_maintenance_window", False)

            plan = MigrationPlan.model_validate(plan_dict)

            duration_ms = int(time.monotonic() * 1000) - start_ms
            tokens_used = response.usage.input_tokens + response.usage.output_tokens

            logger.info(
                "planning_agent.complete",
                extra={
                    "plan_id": plan_id,
                    "job_id": inp.job_id,
                    "tenant_id": inp.tenant_id,
                    "step_count": len(plan.steps),
                    "risk_level": plan.risk_level.value,
                    "total_estimated_minutes": plan.total_estimated_minutes,
                    "duration_ms": duration_ms,
                    "tokens_used": tokens_used,
                },
            )

            return PlanningAgentResult(
                plan_id=plan_id,
                job_id=inp.job_id,
                tenant_id=inp.tenant_id,
                success=True,
                plan=plan,
                step_count=len(plan.steps),
                duration_ms=duration_ms,
                tokens_used=tokens_used,
            )

        except json.JSONDecodeError as exc:
            duration_ms = int(time.monotonic() * 1000) - start_ms
            error_msg = f"LLM returned invalid JSON: {exc}"
            logger.error(
                "planning_agent.json_parse_error",
                extra={"plan_id": plan_id, "job_id": inp.job_id, "error": error_msg},
            )
            return PlanningAgentResult(
                plan_id=plan_id,
                job_id=inp.job_id,
                tenant_id=inp.tenant_id,
                success=False,
                error=error_msg,
                duration_ms=duration_ms,
            )

        except Exception as exc:  # noqa: BLE001
            duration_ms = int(time.monotonic() * 1000) - start_ms
            error_msg = f"Planning agent error: {type(exc).__name__}: {exc}"
            logger.error(
                "planning_agent.error",
                extra={
                    "plan_id": plan_id,
                    "job_id": inp.job_id,
                    "error": error_msg,
                },
                exc_info=True,
            )
            return PlanningAgentResult(
                plan_id=plan_id,
                job_id=inp.job_id,
                tenant_id=inp.tenant_id,
                success=False,
                error=error_msg,
                duration_ms=duration_ms,
            )


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def run_planning_agent(
    job_id: str,
    tenant_id: str,
    source_type: str,
    target_org: str,
    record_counts: dict[str, int],
    has_sox_scope: bool = False,
    maintenance_window: Optional[dict[str, Any]] = None,
) -> PlanningAgentResult:
    """
    Convenience wrapper around PlanningAgent.run().

    Args:
        job_id: Migration job identifier.
        tenant_id: Tenant (org) identifier.
        source_type: "LEGACY_CRM" or "ORACLE_EBS".
        target_org: Target Salesforce org identifier.
        record_counts: Dict mapping object type to expected record count.
        has_sox_scope: Whether this migration falls under SOX compliance scope.
        maintenance_window: Optional dict with start_utc/end_utc/timezone_label.

    Returns:
        PlanningAgentResult with the generated MigrationPlan.
    """
    agent = PlanningAgent()
    inp = PlanningAgentInput(
        job_id=job_id,
        tenant_id=tenant_id,
        source_type=SourceType(source_type),
        target_org=target_org,
        record_counts=record_counts,
        has_sox_scope=has_sox_scope,
        maintenance_window=maintenance_window,
    )
    return agent.run(inp)
