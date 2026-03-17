"""
Debugging Agent — Root Cause Analysis for Migration Failures

Single responsibility: Analyze migration failures using READ-ONLY diagnostic
tools and produce a structured RootCauseAnalysis. Never modifies state.

Key design decisions:
1. READ-ONLY tools only — never writes, patches, or restarts anything
2. Evidence-based: every conclusion must cite specific log lines or data
3. Maps root cause to runbook references (migration_stall.md, high_error_rate.md)
4. If auto_recoverable AND confidence > 0.85: signals orchestrator with recovery action
5. Model: claude-sonnet-4-6
6. All tool dispatch validates read-only policy at runtime

Root cause categories:
  OOM / DB_DEADLOCK / SF_BULK_STALL / KAFKA_LAG / NETWORK_TIMEOUT / DATA_QUALITY / UNKNOWN

API Spec: v1.1.0  |  Read-only  |  Evidence-based
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
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEBUGGING_AGENT_MODEL = os.getenv("DEBUGGING_AGENT_MODEL", "claude-sonnet-4-6")
DEBUGGING_AGENT_MAX_TOKENS = int(os.getenv("DEBUGGING_AGENT_MAX_TOKENS", "8192"))
DEBUGGING_AGENT_MAX_ITERATIONS = int(os.getenv("DEBUGGING_AGENT_MAX_ITERATIONS", "20"))

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt.txt"
_HALCON_PATH = Path(
    os.getenv(
        "HALCON_SESSIONS_PATH",
        str(Path(__file__).parent.parent.parent / ".halcon" / "retrospectives" / "sessions.jsonl"),
    )
)

# Confidence threshold for auto-recovery signalling
_AUTO_RECOVERY_CONFIDENCE_THRESHOLD = 0.85

# Runbook reference mapping by root cause category
_RUNBOOK_REFERENCES: dict[str, str] = {
    "OOM": "migration_stall.md#oom-recovery",
    "DB_DEADLOCK": "migration_stall.md#deadlock-resolution",
    "SF_BULK_STALL": "migration_stall.md#bulk-api-stall",
    "KAFKA_LAG": "high_error_rate.md#kafka-consumer-lag",
    "NETWORK_TIMEOUT": "migration_stall.md#network-timeout",
    "DATA_QUALITY": "high_error_rate.md#data-quality-failures",
    "UNKNOWN": "migration_stall.md#unknown-failure",
}

# Tools this agent is NEVER allowed to call — enforced at dispatch layer
_WRITE_TOOLS: frozenset[str] = frozenset({
    "pause_migration",
    "resume_migration",
    "cancel_migration",
    "retry_failed_records",
    "scale_batch_size",
    "write_records",
    "upsert_records",
    "delete_records",
    "execute_migration",
    "restart_service",
})


def _load_system_prompt() -> str:
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("prompt.txt not found — using inline fallback")
        return (
            "You are the Migration Debugging Agent. "
            "Analyze failures using only the provided diagnostic tools (read-only). "
            "Cite specific log lines and data. Never assume without evidence. "
            "Never modify state — only read and analyze."
        )


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RootCauseCategory(str, Enum):
    OOM = "OOM"
    DB_DEADLOCK = "DB_DEADLOCK"
    SF_BULK_STALL = "SF_BULK_STALL"
    KAFKA_LAG = "KAFKA_LAG"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    DATA_QUALITY = "DATA_QUALITY"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RootCauseAnalysis(BaseModel):
    """Structured root cause analysis produced by the debugging agent."""

    analysis_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    root_cause_category: RootCauseCategory = Field(
        ...,
        description="Canonical root cause category from the allowed enum.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in the root cause determination (0.0–1.0). "
            "Must be > 0.85 for auto_recoverable=True to trigger auto-recovery signalling."
        ),
    )
    evidence: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Ordered list of specific evidence items supporting the root cause determination. "
            "Each item MUST cite the source tool and specific observed value. "
            "No assumptions without evidence."
        ),
    )
    recommended_fix: str = Field(
        ...,
        min_length=1,
        description="Specific, actionable remediation steps.",
    )
    auto_recoverable: bool = Field(
        ...,
        description=(
            "True when the issue can be resolved automatically without human intervention. "
            "When True AND confidence > 0.85: orchestrator will execute recovery_action."
        ),
    )
    recovery_action: Optional[str] = Field(
        default=None,
        description=(
            "Specific recovery action to signal to the orchestrator. "
            "MUST be set when auto_recoverable=True."
        ),
    )
    estimated_recovery_minutes: int = Field(
        ...,
        ge=0,
        description="Estimated time for the recovery action to complete in minutes.",
    )
    runbook_reference: str = Field(
        ...,
        description="Reference to the relevant runbook document and section.",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @field_validator("confidence")
    @classmethod
    def confidence_precision(cls, v: float) -> float:
        return round(v, 3)

    @model_validator(mode="after")
    def recovery_action_required_when_auto_recoverable(self) -> "RootCauseAnalysis":
        if self.auto_recoverable and not self.recovery_action:
            raise ValueError("recovery_action must be set when auto_recoverable is True.")
        return self


class DebuggingAgentInput(BaseModel):
    job_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    failed_step_id: str = Field(..., min_length=1)
    failed_step_type: str = Field(..., min_length=1)
    failure_report: dict[str, Any] = Field(
        ...,
        description="FailureReport dict from the execution-agent.",
    )
    namespace: str = Field(
        default="migration",
        description="Kubernetes namespace for pod diagnostics.",
    )
    service_names: list[str] = Field(
        default_factory=list,
        description="Service names to collect logs from.",
    )
    kafka_consumer_groups: list[str] = Field(
        default_factory=list,
        description="Kafka consumer group IDs to check for lag.",
    )
    salesforce_bulk_job_id: Optional[str] = Field(
        default=None,
        description="Salesforce Bulk API job ID to inspect (for SF_BULK_STALL diagnosis).",
    )
    since_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="Lookback window in minutes for log collection.",
    )


class DebuggingAgentResult(BaseModel):
    analysis_id: str
    job_id: str
    tenant_id: str
    success: bool
    analysis: Optional[RootCauseAnalysis] = None
    auto_recovery_signalled: bool = Field(default=False)
    recovery_action: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int = Field(default=0)
    tokens_used: int = Field(default=0)
    halcon_metrics: dict[str, Any] = Field(default_factory=dict)
    completed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @model_validator(mode="after")
    def error_required_on_failure(self) -> "DebuggingAgentResult":
        if not self.success and not self.error:
            raise ValueError("error must be provided when success is False.")
        return self


# ---------------------------------------------------------------------------
# Tool definitions (READ-ONLY)
# ---------------------------------------------------------------------------

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_logs",
        "description": (
            "Read recent log entries from a service. READ-ONLY. "
            "Returns structured log lines with timestamps, levels, and messages. "
            "Always specify since_minutes to bound the query. "
            "Use search_pattern to filter for relevant errors."
        ),
        "input_schema": {
            "type": "object",
            "required": ["service", "since_minutes"],
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name (e.g. 'migration-worker', 'salesforce-bulk-loader')",
                },
                "since_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1440,
                    "description": "Lookback window in minutes.",
                },
                "level_filter": {
                    "type": "string",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                    "description": "Minimum log level to include.",
                },
                "search_pattern": {
                    "type": "string",
                    "description": "Optional regex pattern to filter log lines.",
                },
            },
        },
    },
    {
        "name": "get_pod_status",
        "description": (
            "Get Kubernetes pod status in a namespace. READ-ONLY. "
            "Returns pod names, states, restart counts, resource usage, and OOMKilled events. "
            "Critical for detecting OOM root cause (look for OOMKilled reason in pod status)."
        ),
        "input_schema": {
            "type": "object",
            "required": ["namespace"],
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace to inspect.",
                },
                "label_selector": {
                    "type": "string",
                    "description": "Label selector to filter pods (e.g. 'app=migration-worker').",
                },
            },
        },
    },
    {
        "name": "get_db_blocking_queries",
        "description": (
            "Query the database for currently blocking queries and deadlocks. READ-ONLY. "
            "Returns blocking query text, wait times, and lock chain. "
            "Use to detect DB_DEADLOCK root cause."
        ),
        "input_schema": {
            "type": "object",
            "required": [],
            "properties": {
                "min_wait_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 10,
                    "description": "Minimum lock wait time in seconds to include.",
                },
                "include_query_text": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to include the blocking query SQL text.",
                },
            },
        },
    },
    {
        "name": "get_kafka_consumer_lag",
        "description": (
            "Get consumer group lag metrics for a Kafka consumer group. READ-ONLY. "
            "Returns per-partition lag, total lag, group state, and last offset timestamp. "
            "Use to detect KAFKA_LAG root cause."
        ),
        "input_schema": {
            "type": "object",
            "required": ["group_id"],
            "properties": {
                "group_id": {
                    "type": "string",
                    "description": "Kafka consumer group ID to check.",
                },
                "topic_filter": {
                    "type": "string",
                    "description": "Optional topic name to filter results.",
                },
            },
        },
    },
    {
        "name": "get_salesforce_bulk_job_status",
        "description": (
            "Get the status of a Salesforce Bulk API 2.0 job. READ-ONLY. "
            "Returns job state, records processed, records failed, and error details. "
            "Use to detect SF_BULK_STALL root cause."
        ),
        "input_schema": {
            "type": "object",
            "required": ["bulk_job_id"],
            "properties": {
                "bulk_job_id": {
                    "type": "string",
                    "description": "Salesforce Bulk API 2.0 job ID.",
                },
                "include_failed_records": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to fetch sample failed record details.",
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Read-only tool handler implementations
# ---------------------------------------------------------------------------


class ReadOnlyToolHandler:
    """
    Read-only diagnostic tool implementations.

    All methods make read-only HTTP calls to observability infrastructure.
    None of these methods modify any system state.
    The dispatch method enforces the write-tool blocklist at runtime.
    """

    def __init__(self, job_id: str, tenant_id: str) -> None:
        self._job_id = job_id
        self._tenant_id = tenant_id
        self._api_base = os.getenv("MIGRATION_API_BASE_URL", "http://localhost:8000/api/v1")
        self._token = os.getenv("INTERNAL_SERVICE_TOKEN", "")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "X-Tenant-ID": self._tenant_id,
            "X-Job-ID": self._job_id,
        }

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Make a read-only GET request to the diagnostics API."""
        import httpx

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(
                    f"{self._api_base}{path}",
                    headers=self._headers(),
                    params=params,
                )
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]
        except Exception as exc:  # noqa: BLE001
            logger.warning("diagnostic_api.unavailable path=%s error=%s", path, exc)
            return {"status": "unavailable", "path": path, "error": str(exc)}

    def read_logs(
        self,
        service: str,
        since_minutes: int,
        level_filter: str = "WARNING",
        search_pattern: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "service": service,
            "since_minutes": since_minutes,
            "level": level_filter,
        }
        if search_pattern:
            params["pattern"] = search_pattern
        return self._get("/diagnostics/logs", params)

    def get_pod_status(
        self,
        namespace: str,
        label_selector: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"namespace": namespace}
        if label_selector:
            params["selector"] = label_selector
        return self._get("/diagnostics/pods", params)

    def get_db_blocking_queries(
        self,
        min_wait_seconds: int = 10,
        include_query_text: bool = True,
    ) -> dict[str, Any]:
        return self._get(
            "/diagnostics/db/blocking",
            {"min_wait_seconds": min_wait_seconds, "include_query": include_query_text},
        )

    def get_kafka_consumer_lag(
        self,
        group_id: str,
        topic_filter: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"group_id": group_id}
        if topic_filter:
            params["topic"] = topic_filter
        return self._get("/diagnostics/kafka/consumer-lag", params)

    def get_salesforce_bulk_job_status(
        self,
        bulk_job_id: str,
        include_failed_records: bool = False,
    ) -> dict[str, Any]:
        return self._get(
            f"/integrations/salesforce/bulk-jobs/{bulk_job_id}",
            {"include_failed_records": include_failed_records},
        )

    def dispatch(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        """
        Route a tool call to the appropriate handler.
        Enforces write-tool blocklist at runtime — forbidden tools raise ValueError.
        """
        if tool_name in _WRITE_TOOLS:
            raise ValueError(
                f"FORBIDDEN: Debugging agent cannot call write tool {tool_name!r}. "
                "This agent is read-only."
            )

        if tool_name == "read_logs":
            return self.read_logs(
                service=tool_input["service"],
                since_minutes=tool_input["since_minutes"],
                level_filter=tool_input.get("level_filter", "WARNING"),
                search_pattern=tool_input.get("search_pattern"),
            )
        if tool_name == "get_pod_status":
            return self.get_pod_status(
                namespace=tool_input["namespace"],
                label_selector=tool_input.get("label_selector"),
            )
        if tool_name == "get_db_blocking_queries":
            return self.get_db_blocking_queries(
                min_wait_seconds=tool_input.get("min_wait_seconds", 10),
                include_query_text=tool_input.get("include_query_text", True),
            )
        if tool_name == "get_kafka_consumer_lag":
            return self.get_kafka_consumer_lag(
                group_id=tool_input["group_id"],
                topic_filter=tool_input.get("topic_filter"),
            )
        if tool_name == "get_salesforce_bulk_job_status":
            return self.get_salesforce_bulk_job_status(
                bulk_job_id=tool_input["bulk_job_id"],
                include_failed_records=tool_input.get("include_failed_records", False),
            )
        raise ValueError(f"Unknown tool: {tool_name!r}")


# ---------------------------------------------------------------------------
# Halcon metrics emitter
# ---------------------------------------------------------------------------


def _emit_halcon_metrics(result: DebuggingAgentResult) -> dict[str, Any]:
    category = "UNKNOWN"
    confidence = 0.0
    if result.analysis:
        category = result.analysis.root_cause_category.value
        confidence = result.analysis.confidence

    metrics = {
        "session_id": result.analysis_id,
        "job_id": result.job_id,
        "tenant_id": result.tenant_id,
        "agent": "debugging-agent",
        "success": result.success,
        "root_cause_category": category,
        "confidence": confidence,
        "auto_recovery_signalled": result.auto_recovery_signalled,
        "recovery_action": result.recovery_action,
        "duration_ms": result.duration_ms,
        "tokens_used": result.tokens_used,
        "final_utility": confidence if result.success else 0.0,
        "convergence_efficiency": min(1.0, confidence),
        "completed_at": result.completed_at,
    }

    try:
        _HALCON_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _HALCON_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(metrics) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("halcon.write_error", extra={"error": str(exc)})

    return metrics


# ---------------------------------------------------------------------------
# Debugging Agent
# ---------------------------------------------------------------------------


class DebuggingAgent:
    """
    Root cause analysis agent for migration failures.

    Uses READ-ONLY diagnostic tools to gather evidence, then produces a
    structured RootCauseAnalysis with:
      - root_cause_category (OOM / DB_DEADLOCK / SF_BULK_STALL / KAFKA_LAG /
                             NETWORK_TIMEOUT / DATA_QUALITY / UNKNOWN)
      - confidence (0.0–1.0)
      - evidence list with specific cited observations
      - recommended_fix
      - auto_recoverable + recovery_action (when confidence > 0.85)

    If auto_recoverable=True AND confidence > 0.85:
      - Sets auto_recovery_signalled=True in the result
      - Populates recovery_action for the orchestrator to execute
      - Orchestrator is responsible for executing the recovery (with dual auth if needed)

    NEVER modifies state — only reads and analyzes.
    """

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._model = DEBUGGING_AGENT_MODEL
        self._system_prompt = _load_system_prompt()

    def run(self, inp: DebuggingAgentInput) -> DebuggingAgentResult:
        """
        Execute root cause analysis on a migration failure.

        Args:
            inp: DebuggingAgentInput with failure context and diagnostic parameters.

        Returns:
            DebuggingAgentResult with RootCauseAnalysis and optional recovery signal.
        """
        start_ms = int(time.monotonic() * 1000)
        analysis_id = str(uuid.uuid4())

        logger.info(
            "debugging_agent.start",
            extra={
                "analysis_id": analysis_id,
                "job_id": inp.job_id,
                "tenant_id": inp.tenant_id,
                "failed_step_id": inp.failed_step_id,
                "failed_step_type": inp.failed_step_type,
            },
        )

        tool_handler = ReadOnlyToolHandler(
            job_id=inp.job_id,
            tenant_id=inp.tenant_id,
        )

        user_message = self._build_user_message(inp)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        total_tokens = 0
        analysis: Optional[RootCauseAnalysis] = None

        iteration = 0
        while iteration < DEBUGGING_AGENT_MAX_ITERATIONS:
            iteration += 1

            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=DEBUGGING_AGENT_MAX_TOKENS,
                    system=self._system_prompt,
                    tools=_TOOLS,  # type: ignore[arg-type]
                    messages=messages,
                )
            except anthropic.APIError as exc:
                error_msg = f"Anthropic API error: {exc}"
                logger.error("debugging_agent.api_error", extra={"error": error_msg}, exc_info=True)
                result = DebuggingAgentResult(
                    analysis_id=analysis_id,
                    job_id=inp.job_id,
                    tenant_id=inp.tenant_id,
                    success=False,
                    error=error_msg,
                    duration_ms=int(time.monotonic() * 1000) - start_ms,
                    tokens_used=total_tokens,
                )
                result.halcon_metrics = _emit_halcon_metrics(result)
                return result

            total_tokens += response.usage.input_tokens + response.usage.output_tokens
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                # Extract structured RootCauseAnalysis from the final text response
                for block in response.content:
                    if block.type == "text":
                        analysis = self._parse_analysis(
                            block.text, inp.job_id, inp.tenant_id
                        )
                        if analysis:
                            break
                break

            if response.stop_reason != "tool_use":
                logger.warning(
                    "debugging_agent.unexpected_stop_reason",
                    extra={"stop_reason": response.stop_reason, "iteration": iteration},
                )
                break

            # Process tool calls
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name = block.name
                tool_input = block.input or {}
                tool_use_id = block.id

                logger.info(
                    "debugging_agent.tool_call",
                    extra={
                        "tool": tool_name,
                        "job_id": inp.job_id,
                        "iteration": iteration,
                    },
                )

                try:
                    tool_output = tool_handler.dispatch(tool_name, tool_input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": json.dumps(tool_output, default=str),
                        }
                    )
                except ValueError as exc:
                    # Forbidden tool attempted
                    logger.error(
                        "debugging_agent.forbidden_tool",
                        extra={"tool": tool_name, "error": str(exc)},
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": json.dumps({"error": str(exc), "policy": "read_only"}),
                            "is_error": True,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    error_msg = f"Tool '{tool_name}' error: {type(exc).__name__}: {exc}"
                    logger.error(
                        "debugging_agent.tool_error",
                        extra={"tool": tool_name, "error": error_msg},
                        exc_info=True,
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": json.dumps({"error": error_msg}),
                            "is_error": True,
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

        # Fallback UNKNOWN analysis if LLM didn't produce structured output
        if analysis is None:
            analysis = RootCauseAnalysis(
                analysis_id=analysis_id,
                job_id=inp.job_id,
                tenant_id=inp.tenant_id,
                root_cause_category=RootCauseCategory.UNKNOWN,
                confidence=0.1,
                evidence=[
                    f"Failed step: {inp.failed_step_type} (step_id={inp.failed_step_id})",
                    f"Error from failure report: {inp.failure_report.get('error_message', 'not provided')}",
                    "Insufficient diagnostic data collected to determine root cause with confidence.",
                ],
                recommended_fix=(
                    "Manual investigation required. "
                    f"Refer to runbook: {_RUNBOOK_REFERENCES['UNKNOWN']}"
                ),
                auto_recoverable=False,
                estimated_recovery_minutes=60,
                runbook_reference=_RUNBOOK_REFERENCES["UNKNOWN"],
            )

        # Determine whether auto-recovery should be signalled
        auto_recovery_signalled = (
            analysis.auto_recoverable
            and analysis.confidence > _AUTO_RECOVERY_CONFIDENCE_THRESHOLD
        )

        duration_ms = int(time.monotonic() * 1000) - start_ms

        result = DebuggingAgentResult(
            analysis_id=analysis_id,
            job_id=inp.job_id,
            tenant_id=inp.tenant_id,
            success=True,
            analysis=analysis,
            auto_recovery_signalled=auto_recovery_signalled,
            recovery_action=analysis.recovery_action if auto_recovery_signalled else None,
            duration_ms=duration_ms,
            tokens_used=total_tokens,
        )

        result.halcon_metrics = _emit_halcon_metrics(result)

        logger.info(
            "debugging_agent.complete",
            extra={
                "analysis_id": analysis_id,
                "job_id": inp.job_id,
                "root_cause": analysis.root_cause_category.value,
                "confidence": analysis.confidence,
                "auto_recoverable": analysis.auto_recoverable,
                "auto_recovery_signalled": auto_recovery_signalled,
                "duration_ms": duration_ms,
                "tokens_used": total_tokens,
            },
        )

        return result

    def _build_user_message(self, inp: DebuggingAgentInput) -> str:
        services = inp.service_names or ["migration-worker", "salesforce-bulk-loader"]
        kafka_groups = inp.kafka_consumer_groups or []
        bulk_job = inp.salesforce_bulk_job_id or "none"
        failure_json = json.dumps(inp.failure_report, indent=2, default=str)

        return f"""Perform root cause analysis on the following migration failure.

FAILURE CONTEXT:
  job_id           : {inp.job_id}
  tenant_id        : {inp.tenant_id}
  failed_step_id   : {inp.failed_step_id}
  failed_step_type : {inp.failed_step_type}
  since_minutes    : {inp.since_minutes}
  namespace        : {inp.namespace}
  bulk_job_id      : {bulk_job}

FAILURE REPORT:
{failure_json}

DIAGNOSTIC SCOPE:
  services to check : {services}
  kafka groups      : {kafka_groups if kafka_groups else "none"}

INVESTIGATION PROTOCOL:
1. Call read_logs for each service in the diagnostic scope. Use search_pattern for errors.
2. Call get_pod_status to check for OOM kills (look for OOMKilled reason).
3. Call get_db_blocking_queries to check for active deadlocks.
4. If kafka_groups are specified: call get_kafka_consumer_lag for each.
5. If bulk_job_id is not "none": call get_salesforce_bulk_job_status.
6. After gathering evidence from all relevant tools, produce your RootCauseAnalysis.

EVIDENCE REQUIREMENT:
Every item in the evidence list MUST:
  - Name the specific tool call that provided it: e.g. "read_logs(migration-worker)"
  - Cite the specific observed value: e.g. "OOMKilled=true, restarts=3"
  - NOT include phrases like "likely", "may be", "could be" without a cited observation

ROOT CAUSE CATEGORIES (use exactly one):
  OOM             — pod killed by OOM killer (look for OOMKilled in pod status)
  DB_DEADLOCK     — database deadlock detected (look for blocking queries)
  SF_BULK_STALL   — Salesforce Bulk API job stalled (look for JobComplete=false + timeout)
  KAFKA_LAG       — Kafka consumer lag exceeds threshold (look for total_lag > 10000)
  NETWORK_TIMEOUT — connection timeout or reset observed in logs
  DATA_QUALITY    — high error rate in records due to data issues
  UNKNOWN         — insufficient evidence to determine root cause

AUTO-RECOVERY CRITERIA:
Set auto_recoverable=true ONLY when:
  - You can identify a specific, safe, reversible recovery action
  - Confidence > 0.85
  - The action does NOT require rollback or data deletion

OUTPUT:
Respond with a JSON object matching the RootCauseAnalysis schema. Raw JSON only — no markdown.
Required fields: analysis_id (use provided), job_id, tenant_id, root_cause_category,
confidence, evidence (list), recommended_fix, auto_recoverable, estimated_recovery_minutes,
runbook_reference.
If auto_recoverable=true: also include recovery_action.
"""

    def _parse_analysis(
        self,
        text: str,
        job_id: str,
        tenant_id: str,
    ) -> Optional[RootCauseAnalysis]:
        """Extract and validate RootCauseAnalysis from LLM response text."""
        raw = text.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(line for line in lines if not line.startswith("```")).strip()

        try:
            data = json.loads(raw)

            # Inject authoritative values
            data["job_id"] = job_id
            data["tenant_id"] = tenant_id

            # Inject runbook reference from category
            category = data.get("root_cause_category", "UNKNOWN")
            data["runbook_reference"] = _RUNBOOK_REFERENCES.get(
                category, _RUNBOOK_REFERENCES["UNKNOWN"]
            )

            return RootCauseAnalysis.model_validate(data)

        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "debugging_agent.parse_analysis_failed",
                extra={"error": str(exc), "text_preview": text[:200]},
            )
            return None


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def run_debugging_agent(
    job_id: str,
    tenant_id: str,
    failed_step_id: str,
    failed_step_type: str,
    failure_report: dict[str, Any],
    namespace: str = "migration",
    service_names: Optional[list[str]] = None,
    kafka_consumer_groups: Optional[list[str]] = None,
    salesforce_bulk_job_id: Optional[str] = None,
    since_minutes: int = 30,
) -> DebuggingAgentResult:
    """
    Convenience wrapper around DebuggingAgent.run().

    Args:
        job_id: Migration job identifier.
        tenant_id: Tenant identifier.
        failed_step_id: UUID of the step that failed.
        failed_step_type: Step type (e.g. "LOAD", "EXTRACT").
        failure_report: FailureReport dict from the execution-agent.
        namespace: Kubernetes namespace for pod diagnostics.
        service_names: Services to collect logs from.
        kafka_consumer_groups: Kafka consumer group IDs to check.
        salesforce_bulk_job_id: Salesforce Bulk API job ID (for SF_BULK_STALL).
        since_minutes: Log lookback window in minutes.

    Returns:
        DebuggingAgentResult with RootCauseAnalysis and optional recovery signal.
    """
    agent = DebuggingAgent()
    inp = DebuggingAgentInput(
        job_id=job_id,
        tenant_id=tenant_id,
        failed_step_id=failed_step_id,
        failed_step_type=failed_step_type,
        failure_report=failure_report,
        namespace=namespace,
        service_names=service_names or [],
        kafka_consumer_groups=kafka_consumer_groups or [],
        salesforce_bulk_job_id=salesforce_bulk_job_id,
        since_minutes=since_minutes,
    )
    return agent.run(inp)
