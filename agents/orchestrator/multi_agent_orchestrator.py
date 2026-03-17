"""
Multi-Agent Orchestrator for the Salesforce Migration Platform.

This orchestrator coordinates all specialised agents, manages inter-agent
communication, implements the agent handoff protocol, and maintains a unified
view of the migration platform's state.

Architecture
------------
The orchestrator implements a supervisor pattern with Claude as the router:

  Orchestrator (supervisor)
  ├─ MigrationAgent        – migration run control
  ├─ DataValidationAgent   – data quality analysis
  ├─ DocumentationAgent    – auto-documentation
  └─ SecurityAuditAgent    – security scanning

Coordination Patterns
  1. Sequential Pipeline: Extract → Validate → Migrate → Document
  2. Parallel Monitoring: Run migration + validation simultaneously
  3. Reactive Escalation: Detect anomaly → escalate to migration agent
  4. Agent Handoff: Supervisor delegates to specialist → receives result → routes next

The orchestrator uses Anthropic Claude as its own reasoning engine to decide
which agents to invoke, in what order, and how to synthesise their outputs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import anthropic

# Import specialised agents
from agents.migration_agent.agent import MigrationAgent
from agents.data_validation_agent.agent import DataValidationAgent
from agents.documentation_agent.agent import DocumentationAgent
from agents.security_audit_agent.agent import SecurityAuditAgent

logger = logging.getLogger(__name__)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")
MAX_TOKENS = int(os.getenv("ORCHESTRATOR_MAX_TOKENS", "4096"))


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------


class AgentName(str, Enum):
    MIGRATION = "migration"
    VALIDATION = "validation"
    DOCUMENTATION = "documentation"
    SECURITY = "security"
    ORCHESTRATOR = "orchestrator"


@dataclass
class AgentCapability:
    name: AgentName
    description: str
    best_for: List[str]
    typical_duration_seconds: int


AGENT_REGISTRY: Dict[AgentName, AgentCapability] = {
    AgentName.MIGRATION: AgentCapability(
        name=AgentName.MIGRATION,
        description=(
            "Controls migration run lifecycle: start, pause, resume, cancel. "
            "Analyses error rates, adjusts batch sizes, opens incidents."
        ),
        best_for=[
            "migration run control", "error rate investigation",
            "batch size tuning", "incident management", "SF API limit management",
        ],
        typical_duration_seconds=30,
    ),
    AgentName.VALIDATION: AgentCapability(
        name=AgentName.VALIDATION,
        description=(
            "Data quality validation: record counts, field completeness, "
            "anomaly detection, referential integrity, duplicate detection."
        ),
        best_for=[
            "data quality assessment", "pre-migration validation",
            "post-migration reconciliation", "field completeness analysis",
        ],
        typical_duration_seconds=45,
    ),
    AgentName.DOCUMENTATION: AgentCapability(
        name=AgentName.DOCUMENTATION,
        description=(
            "Auto-generates and updates documentation: field mappings, "
            "runbooks, API docs, post-migration reports."
        ),
        best_for=[
            "documentation generation", "post-run reports",
            "changelog creation", "field mapping tables",
        ],
        typical_duration_seconds=20,
    ),
    AgentName.SECURITY: AgentCapability(
        name=AgentName.SECURITY,
        description=(
            "Security scanning: OWASP checks, secrets detection, "
            "dependency CVEs, Salesforce permission audit."
        ),
        best_for=[
            "security audit", "secrets scanning", "CVE detection",
            "SF permission review", "compliance check",
        ],
        typical_duration_seconds=60,
    ),
}


# ---------------------------------------------------------------------------
# Orchestration event types
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    TASK_STARTED = "task_started"
    AGENT_DELEGATED = "agent_delegated"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    HANDOFF = "handoff"
    ESCALATION = "escalation"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"


@dataclass
class OrchestrationEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType = EventType.TASK_STARTED
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    orchestration_id: str = ""
    source_agent: Optional[AgentName] = None
    target_agent: Optional[AgentName] = None
    task_summary: str = ""
    result_summary: Optional[str] = None
    duration_seconds: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Orchestration result
# ---------------------------------------------------------------------------


@dataclass
class OrchestrationResult:
    orchestration_id: str
    task: str
    final_answer: str
    agents_used: List[AgentName]
    agent_results: Dict[str, Any]
    events: List[OrchestrationEvent]
    total_duration_seconds: float
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Supervisor tool schemas
# ---------------------------------------------------------------------------

_SUPERVISOR_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "delegate_to_migration_agent",
        "description": (
            "Delegate a migration control task to the Migration Agent. "
            "Use for: checking/changing migration run status, investigating errors, "
            "adjusting batch sizes, creating incidents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task for the Migration Agent."},
                "context": {"type": "object", "description": "Key-value context (run_id, etc.)"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "delegate_to_validation_agent",
        "description": (
            "Delegate a data validation task to the Data Validation Agent. "
            "Use for: checking data quality, field completeness, record counts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "run_id": {"type": "string"},
                "object_types": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task"],
        },
    },
    {
        "name": "delegate_to_documentation_agent",
        "description": (
            "Delegate a documentation task to the Documentation Agent. "
            "Use for: generating reports, updating runbooks, creating field maps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "context": {"type": "object"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "delegate_to_security_agent",
        "description": (
            "Delegate a security audit task to the Security Agent. "
            "Use for: scanning code for vulnerabilities, checking secrets, reviewing permissions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "scope": {"type": "string", "description": "Directory or file path to audit."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "run_agents_in_parallel",
        "description": (
            "Run multiple agent tasks simultaneously. "
            "Use when tasks are independent and can be executed concurrently."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string", "enum": ["migration", "validation", "documentation", "security"]},
                            "task": {"type": "string"},
                            "context": {"type": "object"},
                        },
                        "required": ["agent", "task"],
                    },
                },
            },
            "required": ["tasks"],
        },
    },
    {
        "name": "synthesise_results",
        "description": (
            "Synthesise results from multiple agents into a unified summary. "
            "Call this after gathering results from 2+ agents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "results": {"type": "object", "description": "Dict of agent_name → result_summary"},
                "synthesis_goal": {"type": "string", "description": "What the synthesis should achieve."},
            },
            "required": ["results", "synthesis_goal"],
        },
    },
]


# ---------------------------------------------------------------------------
# Multi-Agent Orchestrator
# ---------------------------------------------------------------------------


class MultiAgentOrchestrator:
    """
    Supervisor-pattern multi-agent orchestrator for the migration platform.

    Uses Claude as the reasoning engine to route tasks, coordinate agents,
    and synthesise their outputs into coherent responses.

    Usage::

        orchestrator = MultiAgentOrchestrator()

        # Coordinate a full post-migration pipeline
        result = await orchestrator.run(
            "Migration run abc-123 just completed. "
            "Validate the data, generate a post-run report, and perform a "
            "security scan of any code changed in the last deployment."
        )
        print(result.final_answer)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL,
        max_tokens: int = MAX_TOKENS,
        agent_api_key: Optional[str] = None,
    ) -> None:
        _api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        _agent_key = agent_api_key or _api_key

        self._client = anthropic.AsyncAnthropic(api_key=_api_key)
        self._model = model
        self._max_tokens = max_tokens

        # Instantiate all specialist agents
        self._agents: Dict[AgentName, Any] = {
            AgentName.MIGRATION: MigrationAgent(api_key=_agent_key),
            AgentName.VALIDATION: DataValidationAgent(api_key=_agent_key),
            AgentName.DOCUMENTATION: DocumentationAgent(api_key=_agent_key),
            AgentName.SECURITY: SecurityAuditAgent(api_key=_agent_key),
        }

        self._event_log: List[OrchestrationEvent] = []
        self._orchestration_id: str = ""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        max_iterations: int = 10,
    ) -> OrchestrationResult:
        """
        Execute a multi-agent orchestration task.

        The supervisor uses Claude to decompose the task, delegate to the
        appropriate specialist agents, and synthesise a final answer.

        Args:
            task:            Natural language description of the goal.
            context:         Optional structured context (run_id, object_types, etc.)
            max_iterations:  Maximum supervisor loop iterations.

        Returns:
            :class:`OrchestrationResult` with the final answer and full audit trail.
        """
        self._orchestration_id = str(uuid.uuid4())
        self._event_log = []
        start_ts = time.perf_counter()
        agents_used: List[AgentName] = []
        agent_results: Dict[str, Any] = {}

        self._emit_event(EventType.TASK_STARTED, task_summary=task[:200])
        logger.info(
            "Orchestration started id=%s task=%s",
            self._orchestration_id,
            task[:100],
        )

        system_prompt = self._build_supervisor_prompt()
        user_message = self._build_user_message(task, context)
        messages: List[Dict[str, Any]] = [{"role": "user", "content": user_message}]

        final_answer = ""
        error: Optional[str] = None

        try:
            for iteration in range(1, max_iterations + 1):
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=system_prompt,
                    tools=_SUPERVISOR_TOOLS,
                    messages=messages,
                    temperature=0.1,
                )

                tool_blocks = [b for b in response.content if b.type == "tool_use"]
                for block in response.content:
                    if block.type == "text" and block.text:
                        final_answer = block.text

                if not tool_blocks or response.stop_reason == "end_turn":
                    break

                messages.append({"role": "assistant", "content": response.content})

                # Execute all supervisor tool calls
                tool_results = []
                for block in tool_blocks:
                    result, used_agents = await self._execute_supervisor_tool(
                        block.name, block.input or {}
                    )
                    agents_used.extend(used_agents)
                    for name in used_agents:
                        agent_results[name.value] = result

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                        "is_error": isinstance(result, dict) and "error" in result,
                    })
                messages.append({"role": "user", "content": tool_results})

        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            logger.error("Orchestrator error: %s", exc, exc_info=True)
            final_answer = f"Orchestration error: {error}"

        duration = time.perf_counter() - start_ts
        self._emit_event(
            EventType.TASK_COMPLETED if not error else EventType.TASK_FAILED,
            task_summary=task[:200],
            result_summary=final_answer[:200],
            duration_seconds=duration,
        )

        logger.info(
            "Orchestration completed id=%s duration=%.2fs agents=%s",
            self._orchestration_id,
            duration,
            [a.value for a in set(agents_used)],
        )

        return OrchestrationResult(
            orchestration_id=self._orchestration_id,
            task=task,
            final_answer=final_answer,
            agents_used=list(set(agents_used)),
            agent_results=agent_results,
            events=list(self._event_log),
            total_duration_seconds=round(duration, 2),
            error=error,
        )

    # ------------------------------------------------------------------
    # Supervisor tool execution
    # ------------------------------------------------------------------

    async def _execute_supervisor_tool(
        self, tool_name: str, tool_input: Dict[str, Any]
    ) -> Tuple[Any, List[AgentName]]:
        """Execute a supervisor tool call and return (result, agents_used)."""

        if tool_name == "delegate_to_migration_agent":
            return await self._delegate(AgentName.MIGRATION, tool_input)

        elif tool_name == "delegate_to_validation_agent":
            return await self._delegate(AgentName.VALIDATION, tool_input)

        elif tool_name == "delegate_to_documentation_agent":
            return await self._delegate(AgentName.DOCUMENTATION, tool_input)

        elif tool_name == "delegate_to_security_agent":
            return await self._delegate(AgentName.SECURITY, tool_input)

        elif tool_name == "run_agents_in_parallel":
            return await self._run_parallel(tool_input.get("tasks", []))

        elif tool_name == "synthesise_results":
            summary = self._do_synthesise(
                tool_input.get("results", {}),
                tool_input.get("synthesis_goal", ""),
            )
            return summary, []

        else:
            return {"error": f"Unknown supervisor tool: {tool_name}"}, []

    async def _delegate(
        self, agent_name: AgentName, tool_input: Dict[str, Any]
    ) -> Tuple[Any, List[AgentName]]:
        """Delegate a task to a specific agent."""
        agent = self._agents[agent_name]
        task = tool_input.get("task", "")
        start_ts = time.perf_counter()

        self._emit_event(EventType.AGENT_DELEGATED, target_agent=agent_name, task_summary=task[:100])

        try:
            if agent_name == AgentName.MIGRATION:
                result = await agent.run(
                    task=task,
                    context=tool_input.get("context"),
                )
                summary = {
                    "agent": agent_name.value,
                    "final_answer": result.final_answer,
                    "tool_calls": len(result.tool_calls_made),
                    "decided_actions": result.decided_actions,
                    "error": result.error,
                }

            elif agent_name == AgentName.VALIDATION:
                result = await agent.run(
                    task=task,
                    run_id=tool_input.get("run_id"),
                    object_types=tool_input.get("object_types"),
                )
                summary = {
                    "agent": agent_name.value,
                    "final_answer": result.final_answer,
                    "quality_score": result.overall_score,
                    "grade": result.grade,
                    "error": result.error,
                }

            elif agent_name == AgentName.DOCUMENTATION:
                result = await agent.run(
                    task=task,
                    files=tool_input.get("files"),
                    context=tool_input.get("context"),
                )
                summary = {
                    "agent": agent_name.value,
                    "final_answer": result.generated_content[:500],
                    "files_written": result.files_written,
                    "error": result.error,
                }

            elif agent_name == AgentName.SECURITY:
                result = await agent.run(task=task, scope=tool_input.get("scope"))
                summary = {
                    "agent": agent_name.value,
                    "final_answer": result.final_answer,
                    "risk_level": result.risk_level,
                    "pass_gate": result.pass_security_gate,
                    "findings": result.findings_count,
                    "error": result.error,
                }
            else:
                summary = {"agent": agent_name.value, "error": "Unknown agent"}

            duration = time.perf_counter() - start_ts
            self._emit_event(
                EventType.AGENT_COMPLETED,
                source_agent=agent_name,
                result_summary=str(summary)[:200],
                duration_seconds=duration,
            )
            return summary, [agent_name]

        except Exception as exc:  # noqa: BLE001
            duration = time.perf_counter() - start_ts
            error_result = {"agent": agent_name.value, "error": str(exc)}
            self._emit_event(
                EventType.AGENT_FAILED,
                source_agent=agent_name,
                result_summary=str(exc)[:200],
                duration_seconds=duration,
            )
            logger.error("Agent %s failed: %s", agent_name.value, exc, exc_info=True)
            return error_result, [agent_name]

    async def _run_parallel(
        self, tasks: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, Any], List[AgentName]]:
        """Run multiple agent tasks concurrently."""
        coroutines = []
        for task_def in tasks:
            agent_name = AgentName(task_def["agent"])
            tool_input = {
                "task": task_def["task"],
                **{k: v for k, v in task_def.items() if k not in ("agent", "task")},
            }
            coroutines.append(self._delegate(agent_name, tool_input))

        results = await asyncio.gather(*coroutines, return_exceptions=True)

        combined: Dict[str, Any] = {}
        all_agents: List[AgentName] = []
        for i, (result, agents) in enumerate(results):
            if isinstance(result, Exception):
                combined[f"task_{i}"] = {"error": str(result)}
            else:
                combined[f"task_{i}"] = result
                all_agents.extend(agents)

        return combined, all_agents

    @staticmethod
    def _do_synthesise(
        results: Dict[str, Any], synthesis_goal: str
    ) -> Dict[str, Any]:
        """Combine multiple agent results into a unified summary."""
        issues = []
        positives = []

        for agent, result in results.items():
            if isinstance(result, dict):
                if result.get("error"):
                    issues.append(f"{agent}: ERROR – {result['error']}")
                elif result.get("grade") in ("D", "F"):
                    issues.append(f"{agent}: Quality grade {result['grade']}")
                elif result.get("risk_level") in ("CRITICAL", "HIGH"):
                    issues.append(f"{agent}: Security risk {result['risk_level']}")
                else:
                    positives.append(f"{agent}: completed successfully")

        return {
            "synthesis_goal": synthesis_goal,
            "agents_synthesised": list(results.keys()),
            "issues": issues,
            "positives": positives,
            "overall_status": "BLOCKED" if issues else "APPROVED",
            "summary": (
                f"{'BLOCKED: ' + '; '.join(issues) if issues else 'All agents completed successfully'}"
            ),
        }

    # ------------------------------------------------------------------
    # Supervisor system prompt
    # ------------------------------------------------------------------

    def _build_supervisor_prompt(self) -> str:
        capabilities = "\n".join(
            f"- **{cap.name.value}**: {cap.description}"
            for cap in AGENT_REGISTRY.values()
        )
        return f"""You are the **Migration Platform Orchestrator** – the top-level supervisor
that coordinates a team of specialised AI agents for a Salesforce migration platform.

## Your Team

{capabilities}

## Orchestration Principles

1. **Decompose** complex tasks into agent-specific sub-tasks
2. **Parallelise** independent sub-tasks using `run_agents_in_parallel`
3. **Sequence** dependent tasks (e.g. validate BEFORE documenting results)
4. **Synthesise** results from multiple agents into a coherent response
5. **Escalate** – if any agent reports a critical issue, prioritise it

## Decision Framework

- Use `delegate_to_migration_agent` for anything involving run control or SF API
- Use `delegate_to_validation_agent` for data quality concerns
- Use `delegate_to_documentation_agent` for reports and doc updates
- Use `delegate_to_security_agent` for audits and compliance checks
- Use `run_agents_in_parallel` when 2+ agents can work independently
- Use `synthesise_results` after collecting 2+ agent results to produce a unified view

## Response Format

Always end with a clear, structured summary:
1. **Overall Status** – what happened
2. **Agent Findings** – one bullet per agent
3. **Actions Taken** – concrete changes made
4. **Recommendations** – next steps
"""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_user_message(
        self, task: str, context: Optional[Dict[str, Any]]
    ) -> str:
        if not context:
            return task
        ctx = "\n".join(f"  {k}: {v}" for k, v in context.items())
        return f"{task}\n\nContext:\n{ctx}"

    def _emit_event(
        self,
        event_type: EventType,
        source_agent: Optional[AgentName] = None,
        target_agent: Optional[AgentName] = None,
        task_summary: str = "",
        result_summary: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = OrchestrationEvent(
            event_type=event_type,
            orchestration_id=self._orchestration_id,
            source_agent=source_agent,
            target_agent=target_agent,
            task_summary=task_summary,
            result_summary=result_summary,
            duration_seconds=duration_seconds,
            metadata=metadata or {},
        )
        self._event_log.append(event)
        logger.debug(
            "Orchestration event type=%s src=%s tgt=%s",
            event_type.value,
            source_agent,
            target_agent,
        )


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


async def run_post_migration_pipeline(
    run_id: str,
    object_types: List[str],
    api_key: Optional[str] = None,
) -> OrchestrationResult:
    """
    Standard post-migration pipeline:
    1. Validate data quality (validation agent)
    2. Generate migration report (documentation agent)
    Both run in parallel for efficiency.
    """
    orchestrator = MultiAgentOrchestrator(api_key=api_key)
    return await orchestrator.run(
        task=(
            f"Post-migration pipeline for run {run_id}. "
            f"Object types: {', '.join(object_types)}. "
            "Run validation and documentation generation in parallel. "
            "If validation grade is D or F, also check the migration agent for error details."
        ),
        context={
            "run_id": run_id,
            "object_types": object_types,
            "pipeline": "post_migration",
        },
    )


async def run_security_preflight(
    directories: List[str],
    api_key: Optional[str] = None,
) -> OrchestrationResult:
    """
    Pre-deployment security check: scan specified directories for vulnerabilities.
    Blocks deployment if CRITICAL or HIGH findings are present.
    """
    orchestrator = MultiAgentOrchestrator(api_key=api_key)
    return await orchestrator.run(
        task=(
            f"Pre-deployment security audit for: {', '.join(directories)}. "
            "Scan for secrets, CVEs, auth issues, and OWASP violations. "
            "Provide a clear PASS/FAIL gate decision."
        ),
        context={"scope": directories, "pipeline": "security_preflight"},
    )


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    async def _main() -> None:
        orchestrator = MultiAgentOrchestrator()
        task = " ".join(sys.argv[1:]) or (
            "Perform a complete health check of the migration platform: "
            "check if any active runs have issues, validate data quality, "
            "and do a quick security scan of the integration layer."
        )
        result = await orchestrator.run(task)
        print(f"\nOrchestration Result\n{'='*60}")
        print(f"ID: {result.orchestration_id}")
        print(f"Duration: {result.total_duration_seconds}s")
        print(f"Agents used: {[a.value for a in result.agents_used]}")
        print(f"Events: {len(result.events)}")
        if result.error:
            print(f"Error: {result.error}")
        print(f"\nFINAL ANSWER:\n{result.final_answer}")

    asyncio.run(_main())
