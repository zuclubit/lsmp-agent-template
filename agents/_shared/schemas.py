"""Pydantic v2 schemas shared across all agents in the s-agent system."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class AgentRole(str, Enum):
    """Identifies the functional role of an agent within the system."""

    ORCHESTRATOR = "orchestrator"
    PLANNING = "planning"
    VALIDATION = "validation"
    SECURITY = "security"
    EXECUTION = "execution"
    DEBUGGING = "debugging"


class GateDecision(str, Enum):
    """Three-way outcome returned by validation and security gates."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


class AgentPriority(str, Enum):
    """Execution priority hint passed to scheduling infrastructure."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Core context / input / result models
# ---------------------------------------------------------------------------


class RequestContext(BaseModel):
    """Immutable request envelope propagated through every agent hop."""

    model_config = {"frozen": True}

    request_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Globally unique identifier for this top-level request.",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Tenant (org) that originated the request.",
    )
    job_id: str = Field(
        ...,
        min_length=1,
        description="Migration or batch job this request belongs to.",
    )
    trace_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Distributed-tracing span identifier.",
    )
    initiated_by: str = Field(
        ...,
        min_length=1,
        description="User, service account, or system component that triggered the request.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC wall-clock time at which the request was created.",
    )
    max_budget_usd: float = Field(
        default=1.0,
        ge=0.0,
        description="Maximum allowable spend in USD for this request across all agents.",
    )

    @field_validator("max_budget_usd")
    @classmethod
    def budget_must_be_finite(cls, v: float) -> float:
        import math

        if math.isinf(v) or math.isnan(v):
            raise ValueError("max_budget_usd must be a finite number.")
        return v


class AgentInput(BaseModel):
    """Structured input payload delivered to any agent's execute() method."""

    model_config = {"frozen": True}

    context: RequestContext = Field(
        ...,
        description="Request envelope providing tracing and budget context.",
    )
    task: str = Field(
        ...,
        min_length=1,
        description="Free-text description of the work to be performed.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Agent-specific key/value configuration for this invocation.",
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="Explicit allow-list of tool names the agent may invoke.",
    )


class AgentResult(BaseModel):
    """Structured result returned by any agent after execution completes."""

    request_id: str = Field(
        ...,
        description="Echoes RequestContext.request_id for correlation.",
    )
    agent_role: AgentRole = Field(
        ...,
        description="Role of the agent that produced this result.",
    )
    success: bool = Field(
        ...,
        description="True when the agent completed its task without a hard failure.",
    )
    output: dict[str, Any] = Field(
        default_factory=dict,
        description="Agent-specific structured output payload.",
    )
    error: Optional[str] = Field(
        default=None,
        description="Human-readable error description when success is False.",
    )
    duration_ms: int = Field(
        default=0,
        ge=0,
        description="Wall-clock execution time in milliseconds.",
    )
    tokens_used: int = Field(
        default=0,
        ge=0,
        description="Total tokens consumed across all LLM calls during this execution.",
    )
    gate_decision: Optional[GateDecision] = Field(
        default=None,
        description="Populated by gate agents (VALIDATION, SECURITY) with their final verdict.",
    )
    halcon_metrics: dict[str, Any] = Field(
        default_factory=dict,
        description="Serialised HalconMetrics payload for observability.",
    )

    @model_validator(mode="after")
    def error_required_on_failure(self) -> "AgentResult":
        if not self.success and not self.error:
            raise ValueError("error must be provided when success is False.")
        return self


# ---------------------------------------------------------------------------
# Gate result models
# ---------------------------------------------------------------------------


class BlockingGateResult(BaseModel):
    """Outcome of a single named gate check."""

    gate_name: str = Field(..., min_length=1)
    decision: GateDecision
    reason: str = Field(..., min_length=1, description="Human-readable rationale.")
    blocking_issue: Optional[str] = Field(
        default=None,
        description="Specific issue that caused a BLOCK decision, if applicable.",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Ordered list of evidence items supporting the decision.",
    )


class ValidationGateResult(BaseModel):
    """Composite result produced by the validation agent's three-gate pipeline."""

    gate1_source_completeness: BlockingGateResult = Field(
        ...,
        description="Gate 1: verifies that the source data set is complete and readable.",
    )
    gate2_target_validity: BlockingGateResult = Field(
        ...,
        description="Gate 2: verifies the target schema accepts the transformed records.",
    )
    gate3_post_load_sample: BlockingGateResult = Field(
        ...,
        description="Gate 3: spot-checks a sample of records after they are loaded.",
    )
    overall_decision: GateDecision = Field(
        ...,
        description="Worst-case decision across all three gates.",
    )
    validated_record_count: int = Field(
        ...,
        ge=0,
        description="Number of records that passed validation.",
    )

    @model_validator(mode="after")
    def overall_decision_is_worst_case(self) -> "ValidationGateResult":
        """Ensure overall_decision reflects the strictest individual gate decision."""
        rank: dict[GateDecision, int] = {
            GateDecision.ALLOW: 0,
            GateDecision.WARN: 1,
            GateDecision.BLOCK: 2,
        }
        worst = max(
            self.gate1_source_completeness.decision,
            self.gate2_target_validity.decision,
            self.gate3_post_load_sample.decision,
            key=lambda d: rank[d],
        )
        if rank[self.overall_decision] < rank[worst]:
            raise ValueError(
                f"overall_decision '{self.overall_decision}' is less severe than "
                f"the worst individual gate decision '{worst}'."
            )
        return self


# ---------------------------------------------------------------------------
# Observability — Halcon metrics
# ---------------------------------------------------------------------------


class HalconMetrics(BaseModel):
    """Agent-level observability metrics for the Halcon monitoring framework."""

    session_id: str = Field(..., min_length=1)
    convergence_efficiency: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Ratio of useful tokens (output) to total tokens consumed. "
            "1.0 means no tokens were wasted on retries or dead ends."
        ),
    )
    decision_density: float = Field(
        ...,
        ge=0.0,
        description=(
            "Average number of tool calls made per 1 000 tokens consumed."
        ),
    )
    adaptation_utilization: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of available tools actually invoked during the session."
        ),
    )
    dominant_failure_mode: Optional[str] = Field(
        default=None,
        description="Most frequently observed failure category, if any.",
    )
    evidence_trajectory: list[str] = Field(
        default_factory=list,
        description="Chronological list of key decision / evidence events.",
    )
    final_utility: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Composite utility score (0–1) summarising overall session quality."
        ),
    )


# ---------------------------------------------------------------------------
# Migration planning models
# ---------------------------------------------------------------------------


class PlanStep(BaseModel):
    """A single step within a migration plan."""

    step_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    agent_role: AgentRole = Field(
        ...,
        description="Agent responsible for executing this step.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="step_id values that must complete before this step begins.",
    )
    estimated_duration_minutes: int = Field(
        ...,
        ge=0,
        description="Optimistic wall-clock duration estimate.",
    )
    is_blocking: bool = Field(
        ...,
        description="When True, downstream steps cannot start until this step passes.",
    )


class MigrationPlan(BaseModel):
    """Top-level migration plan produced by the planning agent."""

    plan_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique identifier for this plan version.",
    )
    job_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    steps: list[PlanStep] = Field(
        ...,
        min_length=1,
        description="Ordered list of steps; graph semantics are encoded via depends_on.",
    )
    total_estimated_minutes: int = Field(
        ...,
        ge=0,
        description="Critical-path duration for the entire plan.",
    )
    risk_level: str = Field(
        ...,
        description="Qualitative risk rating: low | medium | high | critical.",
        pattern=r"^(low|medium|high|critical)$",
    )
    requires_maintenance_window: bool = Field(
        ...,
        description="True when the plan requires the target system to be offline.",
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


# ---------------------------------------------------------------------------
# Migration platform — extended I/O schemas
# (added as part of the 2026 agent infrastructure redesign)
# ---------------------------------------------------------------------------

import re as _re
from typing import Dict, List

_UUID4_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    _re.IGNORECASE,
)
_SF_OBJECT_RE = _re.compile(r"^[A-Z][a-zA-Z0-9_]*(__c)?$")
_RUN_ID_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_]{1,127}$")


def _score(v: Any, name: str = "score") -> float:
    if not isinstance(v, (int, float)):
        raise ValueError(f"{name} must be numeric")
    f = float(v)
    if not (0.0 <= f <= 1.0):
        raise ValueError(f"{name} must be 0.0–1.0, got {f}")
    return f


# --- Gate decision ---


class BlockingGate(str, Enum):
    """Pipeline gate signal: ALLOW | WARN | BLOCK."""
    ALLOW = "ALLOW"
    WARN = "WARN"
    BLOCK = "BLOCK"


# --- Data quality ---


class DataQualityGrade(str, Enum):
    """Letter grade derived deterministically from an overall quality score."""
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"

    @classmethod
    def from_score(cls, score: float) -> "DataQualityGrade":
        if score >= 0.97:
            return cls.A
        if score >= 0.93:
            return cls.B
        if score >= 0.85:
            return cls.C
        if score >= 0.70:
            return cls.D
        return cls.F


# --- Security ---


class SecurityRiskLevel(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class IncidentSeverity(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class MigrationStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    QUEUED = "QUEUED"


# --- Agent decision ---


class AgentDecision(BaseModel):
    """Typed gate decision from an agent — drives orchestrator routing."""

    model_config = {"strict": True}

    decision_id: str
    agent: str
    rationale: str = Field(min_length=20)
    confidence: float
    gate: BlockingGate
    supporting_evidence: List[str] = Field(default_factory=list)
    recommended_next_agent: Optional[str] = None
    timestamp: datetime

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> float:
        return _score(v, "confidence")

    @model_validator(mode="after")
    def _gate_block_needs_evidence(self) -> "AgentDecision":
        if self.gate == BlockingGate.BLOCK and not self.supporting_evidence:
            raise ValueError("A BLOCK gate decision must include supporting_evidence.")
        return self


# --- Task inputs ---


class MigrationTaskInput(BaseModel):
    model_config = {"strict": True}
    trace_id: str
    job_id: str
    task_description: str = Field(min_length=10)
    run_id: Optional[str] = None
    object_type: Optional[str] = None
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")

    @field_validator("object_type", mode="before")
    @classmethod
    def _obj(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _SF_OBJECT_RE.match(v):
            raise ValueError(f"object_type {v!r} must start with a capital letter")
        return v


class ValidationTaskInput(BaseModel):
    model_config = {"strict": True}
    trace_id: str
    job_id: str
    task_description: str = Field(min_length=10)
    run_id: str
    object_types: List[str] = Field(min_length=1)
    completeness_threshold: float = 0.95
    report_format: str = Field(default="detailed", pattern=r"^(summary|detailed|executive)$")

    @field_validator("completeness_threshold", mode="before")
    @classmethod
    def _thresh(cls, v: Any) -> float:
        return _score(v, "completeness_threshold")

    @field_validator("object_types", mode="before")
    @classmethod
    def _objs(cls, v: Any) -> List[str]:
        if not isinstance(v, list) or not v:
            raise ValueError("object_types must be a non-empty list")
        for o in v:
            if not _SF_OBJECT_RE.match(o):
                raise ValueError(f"object_type {o!r} must start with a capital letter")
        return v


class SecurityTaskInput(BaseModel):
    model_config = {"strict": True}
    trace_id: str
    job_id: str
    task_description: str = Field(min_length=10)
    scope: str = Field(min_length=1)
    fail_on_severity: SecurityRiskLevel = SecurityRiskLevel.HIGH

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, v: str) -> str:
        if v.startswith("/") or ".." in v:
            raise ValueError("scope must be a relative path; '..' and absolute paths are forbidden")
        return v


class DebuggingTaskInput(BaseModel):
    model_config = {"strict": True}
    trace_id: str
    job_id: str
    task_description: str = Field(min_length=10)
    target_agent: str
    run_id: Optional[str] = None
    error_description: str = Field(min_length=10)
    include_tool_history: bool = True


# --- Task outputs ---


class TaskOutput(BaseModel):
    """Base output shared by all agents."""
    model_config = {"strict": False}
    trace_id: str
    invocation_id: str
    agent: str
    success: bool
    decision: AgentDecision
    answer: str = Field(min_length=1)
    tool_calls_made: int = Field(ge=0)
    tokens_used: int = Field(ge=0)
    duration_ms: float = Field(ge=0.0)
    errors: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent_gate(self) -> "TaskOutput":
        if self.success and self.decision.gate == BlockingGate.BLOCK:
            raise ValueError("success=True contradicts gate=BLOCK")
        return self


class MigrationTaskOutput(TaskOutput):
    run_id: Optional[str] = None
    run_status: Optional[MigrationStatus] = None
    actions_taken: List[str] = Field(default_factory=list)
    incident_ids: List[str] = Field(default_factory=list)
    current_error_rate: Optional[float] = None
    current_batch_size: Optional[int] = None

    @field_validator("current_error_rate", mode="before")
    @classmethod
    def _err(cls, v: Optional[Any]) -> Optional[float]:
        return None if v is None else _score(v, "current_error_rate")


class ValidationTaskOutput(TaskOutput):
    run_id: str
    object_types_validated: List[str]
    overall_quality_score: float
    grade: DataQualityGrade
    dimensions: Dict[str, Any] = Field(default_factory=dict)
    critical_issues_count: int = Field(default=0, ge=0)
    high_issues_count: int = Field(default=0, ge=0)
    top_recommendations: List[str] = Field(default_factory=list)

    @field_validator("overall_quality_score", mode="before")
    @classmethod
    def _qs(cls, v: Any) -> float:
        return _score(v, "overall_quality_score")

    @model_validator(mode="after")
    def _grade_matches_score(self) -> "ValidationTaskOutput":
        expected = DataQualityGrade.from_score(self.overall_quality_score)
        if self.grade != expected:
            raise ValueError(
                f"grade={self.grade!r} inconsistent with score={self.overall_quality_score} "
                f"(expected {expected!r})"
            )
        return self


class SecurityTaskOutput(TaskOutput):
    scope: str
    total_findings: int = Field(ge=0)
    critical_count: int = Field(default=0, ge=0)
    high_count: int = Field(default=0, ge=0)
    medium_count: int = Field(default=0, ge=0)
    low_count: int = Field(default=0, ge=0)
    risk_level: SecurityRiskLevel
    pass_security_gate: bool
    findings_by_category: Dict[str, int] = Field(default_factory=dict)
    report_id: Optional[str] = None

    @model_validator(mode="after")
    def _gate_vs_findings(self) -> "SecurityTaskOutput":
        if self.pass_security_gate and (self.critical_count > 0 or self.high_count > 0):
            raise ValueError(
                f"pass_security_gate=True but critical={self.critical_count}, "
                f"high={self.high_count}"
            )
        return self


# --- Halcon session metrics (migration-specific; extends base HalconMetrics) ---


class HalconSessionMetrics(BaseModel):
    """
    Extended Halcon session record for the migration platform.

    Written to .halcon/retrospectives/sessions.jsonl after each agent run.
    """
    model_config = {"strict": True}

    agent: str
    model: str
    prompt_version: str
    adaptation_utilization: float = Field(ge=0.0, le=1.0)
    convergence_efficiency: float = Field(ge=0.0, le=1.0)
    decision_density: float = Field(ge=0.0)
    dominant_failure_mode: Optional[str] = None
    evidence_trajectory: str = Field(pattern=r"^(monotonic|degraded|oscillating)$")
    final_utility: float = Field(ge=0.0, le=1.0)
    inferred_problem_class: str = Field(
        pattern=r"^(deterministic-linear|iterative-refinement|tool-heavy|unbounded-search)$"
    )
    peak_utility: float = Field(ge=0.0, le=1.0)
    structural_instability_score: float = Field(ge=0.0, le=1.0)
    wasted_rounds: int = Field(ge=0)
    duration_ms: float = Field(ge=0.0)
    timestamp_utc: datetime

    @field_validator("dominant_failure_mode", mode="before")
    @classmethod
    def _dfm(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        allowed = {"token_budget", "circuit_breaker", "input_validation", "generic_error", "api_error", "timeout"}
        if v not in allowed:
            raise ValueError(f"dominant_failure_mode {v!r} not in {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def _peak_ge_final(self) -> "HalconSessionMetrics":
        if self.peak_utility < self.final_utility:
            raise ValueError(f"peak_utility ({self.peak_utility}) must be >= final_utility ({self.final_utility})")
        return self

    def to_jsonl_record(self) -> str:
        import json as _json
        d = self.model_dump()
        d["timestamp_utc"] = self.timestamp_utc.isoformat()
        return _json.dumps(d, sort_keys=True)


# --- Tool-level input schemas (SOQL injection and path traversal guards) ---


class RunCustomSoqlCheckInput(BaseModel):
    """
    Validated input for run_custom_soql_check.
    Blocks DML keywords — fix for ISSUE-004.
    """
    model_config = {"strict": True}

    soql: str
    description: str
    expected_count: Optional[int] = None

    _DML_RE = _re.compile(
        r"\b(DELETE|UPDATE|INSERT|MERGE|UPSERT|CREATE|DROP|ALTER|TRUNCATE)\b",
        _re.IGNORECASE,
    )

    @field_validator("soql", mode="before")
    @classmethod
    def _soql(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("soql must be str")
        s = v.strip()
        if not s.upper().startswith("SELECT"):
            raise ValueError(f"SOQL must begin with SELECT, got {s[:40]!r}")
        m = RunCustomSoqlCheckInput._DML_RE.search(s)
        if m:
            raise ValueError(f"SOQL contains forbidden keyword {m.group(0)!r}")
        return s

    @field_validator("expected_count", mode="before")
    @classmethod
    def _ec(cls, v: Optional[Any]) -> Optional[int]:
        if v is None:
            return None
        if not isinstance(v, int) or v < 0:
            raise ValueError("expected_count must be a non-negative int")
        return v


class WriteDocumentationInput(BaseModel):
    """
    Validated input for write_documentation.
    Enforces path containment — fix for ISSUE-005.
    """
    model_config = {"strict": True}

    file_path: str
    content: str
    mode: str = "create"
    section_header: Optional[str] = None

    _ALLOWED = frozenset({"docs", "reports", "analysis", "documentation", "output"})
    _TRAVERSAL = _re.compile(r"\.\.|^/|^~")

    @field_validator("file_path", mode="before")
    @classmethod
    def _fp(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("file_path must be str")
        if WriteDocumentationInput._TRAVERSAL.search(v):
            raise ValueError(f"file_path {v!r} contains path traversal characters")
        top = v.split("/")[0] if "/" in v else ""
        if top not in WriteDocumentationInput._ALLOWED:
            raise ValueError(
                f"May only write to {sorted(WriteDocumentationInput._ALLOWED)}, got {top!r}"
            )
        return v

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in {"create", "append", "update_section"}:
            raise ValueError(f"mode must be create|append|update_section, got {v!r}")
        return v


# Registry: tool_name -> input model (used by BaseAgent._validate_tool_input)
TOOL_INPUT_SCHEMAS: Dict[str, type[BaseModel]] = {
    "run_custom_soql_check": RunCustomSoqlCheckInput,
    "write_documentation": WriteDocumentationInput,
}
