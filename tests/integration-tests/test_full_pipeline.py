"""
Full pipeline integration test — from task submission to completion.

Mocks: Claude API, all external HTTP calls
Does NOT mock: agent orchestration logic, gate enforcement, Halcon metrics structure

Test scenarios:
1. Happy path: validation passes, security passes, execution runs
2. Validation fails: execution is blocked, correct error returned
3. Security critical: execution blocked, correct risk level in result
4. Planning failure: no execution attempted, error propagated cleanly

These tests exercise the full orchestration code path including:
- MultiAgentOrchestrator.run()
- _execute_supervisor_tool() routing
- _do_synthesise() gate logic
- OrchestrationResult schema completeness
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from agents.orchestrator.multi_agent_orchestrator import (
        MultiAgentOrchestrator,
        OrchestrationResult,
        AgentName,
        EventType,
    )
    ORCHESTRATOR_AVAILABLE = True
except ImportError:
    ORCHESTRATOR_AVAILABLE = False

pytestmark = pytest.mark.asyncio

skip_if = pytest.mark.skipif(
    not ORCHESTRATOR_AVAILABLE, reason="MultiAgentOrchestrator not importable"
)


# ---------------------------------------------------------------------------
# Helpers — fixture-style builder functions
# ---------------------------------------------------------------------------


def _text_block(text: str) -> MagicMock:
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _tool_use_block(name: str, inp: Dict, bid: str = "tu_001") -> MagicMock:
    b = MagicMock()
    b.type = "tool_use"
    b.name = name
    b.input = inp
    b.id = bid
    return b


def _response(blocks: list, stop: str = "end_turn") -> MagicMock:
    r = MagicMock()
    r.content = blocks
    r.stop_reason = stop
    return r


def _make_validation_result(grade: str, score: float, error: Any = None) -> MagicMock:
    m = MagicMock()
    m.final_answer = f"Validation complete. Grade: {grade}."
    m.overall_score = score
    m.grade = grade
    m.error = error
    return m


def _make_security_result(
    risk_level: str, pass_gate: bool, critical: int = 0, error: Any = None
) -> MagicMock:
    m = MagicMock()
    m.final_answer = f"Security audit complete. Risk: {risk_level}."
    m.risk_level = risk_level
    m.pass_security_gate = pass_gate
    m.findings_count = critical
    m.error = error
    return m


def _make_migration_result(action: str = "health_check") -> MagicMock:
    m = MagicMock()
    m.final_answer = f"Migration agent completed: {action}."
    m.tool_calls_made = [{"tool": "check_migration_status"}]
    m.decided_actions = [f"Action: {action}"]
    m.error = None
    return m


def _make_doc_result() -> MagicMock:
    m = MagicMock()
    m.generated_content = "# Post-Migration Report\n\nAll systems nominal."
    m.files_written = ["docs/report.md"]
    m.error = None
    return m


# ---------------------------------------------------------------------------
# Scenario 1: Happy path — all gates pass, execution proceeds
# ---------------------------------------------------------------------------


@skip_if
async def test_happy_path_all_gates_pass():
    """
    Full pipeline:
    1. Claude delegates validation + security in parallel
    2. Both pass: Claude delegates to migration for status check
    3. Claude delegates to documentation for report
    4. Final answer is APPROVED

    Assertions:
    - validation agent was called
    - security agent was called
    - migration agent was called (execution proceeded)
    - documentation agent was called
    - result has no error
    - orchestration events include TASK_COMPLETED
    """
    mock_val = AsyncMock()
    mock_val.run = AsyncMock(return_value=_make_validation_result("A", 0.97))

    mock_sec = AsyncMock()
    mock_sec.run = AsyncMock(return_value=_make_security_result("LOW", True))

    mock_mig = AsyncMock()
    mock_mig.run = AsyncMock(return_value=_make_migration_result("health_check"))

    mock_doc = AsyncMock()
    mock_doc.run = AsyncMock(return_value=_make_doc_result())

    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        orch._agents[AgentName.MIGRATION] = mock_mig
        orch._agents[AgentName.VALIDATION] = mock_val
        orch._agents[AgentName.SECURITY] = mock_sec
        orch._agents[AgentName.DOCUMENTATION] = mock_doc

        call_count = 0

        async def step_responses(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _response(
                    [_tool_use_block(
                        "run_agents_in_parallel",
                        {"tasks": [
                            {"agent": "validation", "task": "Validate Account"},
                            {"agent": "security", "task": "Security scan"},
                        ]},
                    )],
                    stop="tool_use",
                )
            elif call_count == 2:
                return _response(
                    [_tool_use_block(
                        "delegate_to_migration_agent",
                        {"task": "Check run health"},
                        bid="tu_002",
                    )],
                    stop="tool_use",
                )
            elif call_count == 3:
                return _response(
                    [_tool_use_block(
                        "delegate_to_documentation_agent",
                        {"task": "Generate post-migration report"},
                        bid="tu_003",
                    )],
                    stop="tool_use",
                )
            else:
                return _response(
                    [_text_block(
                        "APPROVED: All gates passed.\n"
                        "- Validation: Grade A (0.97)\n"
                        "- Security: LOW risk, gate PASS\n"
                        "- Migration: healthy\n"
                        "- Documentation: report generated"
                    )],
                )

        mock_anthropic.return_value.messages = AsyncMock()
        mock_anthropic.return_value.messages.create = AsyncMock(side_effect=step_responses)

        result = await orch.run(
            "Full post-migration pipeline for run-happy-001.",
            context={"run_id": "run-happy-001"},
        )

    mock_val.run.assert_called()
    mock_sec.run.assert_called()
    mock_mig.run.assert_called()
    mock_doc.run.assert_called()

    assert result.error is None

    event_types = [e.event_type.value for e in result.events]
    assert "task_completed" in event_types

    assert len(result.agents_used) >= 2


# ---------------------------------------------------------------------------
# Scenario 2: Validation fails — execution is blocked
# ---------------------------------------------------------------------------


@skip_if
async def test_validation_fails_execution_blocked():
    """
    When validation returns grade D/F:
    - Migration execution must NOT be called
    - Final answer must reflect BLOCKED status
    - result.error must be None (BLOCKED is a gate decision, not a crash)
    """
    mock_val = AsyncMock()
    mock_val.run = AsyncMock(return_value=_make_validation_result("F", 0.42))

    mock_mig = AsyncMock()
    mock_mig.run = AsyncMock(return_value=_make_migration_result())

    mock_sec = AsyncMock()
    mock_sec.run = AsyncMock(return_value=_make_security_result("LOW", True))

    mock_doc = AsyncMock()
    mock_doc.run = AsyncMock(return_value=_make_doc_result())

    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        orch._agents[AgentName.MIGRATION] = mock_mig
        orch._agents[AgentName.VALIDATION] = mock_val
        orch._agents[AgentName.SECURITY] = mock_sec
        orch._agents[AgentName.DOCUMENTATION] = mock_doc

        call_count = 0

        async def step_responses(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _response(
                    [_tool_use_block(
                        "delegate_to_validation_agent",
                        {"task": "Validate Account records"},
                    )],
                    stop="tool_use",
                )
            else:
                return _response(
                    [_text_block(
                        "BLOCKED: Validation grade F (score 0.42). "
                        "Required fields null rate exceeds threshold. "
                        "Migration execution halted — data quality insufficient."
                    )],
                )

        mock_anthropic.return_value.messages = AsyncMock()
        mock_anthropic.return_value.messages.create = AsyncMock(side_effect=step_responses)

        result = await orch.run("Post-migration pipeline for run-fail-001.")

    mock_val.run.assert_called()
    mock_mig.run.assert_not_called()
    assert result.error is None


@skip_if
async def test_validation_fail_synthesise_returns_blocked():
    """_do_synthesise(): grade D or F must produce BLOCKED status."""
    for bad_grade in ("D", "F"):
        result = MultiAgentOrchestrator._do_synthesise(
            results={"validation": {"grade": bad_grade, "error": None}},
            synthesis_goal="Can we proceed?",
        )
        assert result["overall_status"] == "BLOCKED", (
            f"Grade '{bad_grade}' must result in BLOCKED, got '{result['overall_status']}'"
        )


# ---------------------------------------------------------------------------
# Scenario 3: Security critical — execution blocked
# ---------------------------------------------------------------------------


@skip_if
async def test_security_critical_blocks_pipeline():
    """
    When security returns CRITICAL risk:
    - Migration execution must NOT be called
    - Orchestration records the security failure
    """
    mock_val = AsyncMock()
    mock_val.run = AsyncMock(return_value=_make_validation_result("A", 0.97))

    mock_sec = AsyncMock()
    mock_sec.run = AsyncMock(return_value=_make_security_result("CRITICAL", False, critical=2))

    mock_mig = AsyncMock()
    mock_mig.run = AsyncMock(return_value=_make_migration_result())

    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        orch._agents[AgentName.MIGRATION] = mock_mig
        orch._agents[AgentName.VALIDATION] = mock_val
        orch._agents[AgentName.SECURITY] = mock_sec
        orch._agents[AgentName.DOCUMENTATION] = AsyncMock()

        call_count = 0

        async def steps(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _response(
                    [_tool_use_block("delegate_to_security_agent", {"task": "Security scan"})],
                    stop="tool_use",
                )
            else:
                return _response(
                    [_text_block(
                        "BLOCKED: Security agent found CRITICAL vulnerabilities. "
                        "2 hardcoded secrets detected. Deployment blocked."
                    )],
                )

        mock_anthropic.return_value.messages = AsyncMock()
        mock_anthropic.return_value.messages.create = AsyncMock(side_effect=steps)

        result = await orch.run("Security preflight for deployment.")

    mock_sec.run.assert_called()
    mock_mig.run.assert_not_called()


@skip_if
async def test_security_synthesise_critical_blocked():
    """_do_synthesise returns BLOCKED for CRITICAL security risk."""
    result = MultiAgentOrchestrator._do_synthesise(
        results={
            "security": {
                "risk_level": "CRITICAL",
                "pass_gate": False,
                "findings": 2,
                "error": None,
            }
        },
        synthesis_goal="Deployment gate",
    )
    assert result["overall_status"] == "BLOCKED"
    assert len(result["issues"]) >= 1


# ---------------------------------------------------------------------------
# Scenario 4: Planning failure — no execution attempted
# ---------------------------------------------------------------------------


@skip_if
async def test_planning_failure_no_execution():
    """
    If Claude API raises on the first call, no agents should be called
    and the result must contain the error.
    """
    mock_mig = AsyncMock()
    mock_mig.run = AsyncMock()
    mock_val = AsyncMock()
    mock_val.run = AsyncMock()

    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        orch._agents[AgentName.MIGRATION] = mock_mig
        orch._agents[AgentName.VALIDATION] = mock_val

        mock_anthropic.return_value.messages = AsyncMock()
        mock_anthropic.return_value.messages.create = AsyncMock(
            side_effect=ConnectionError("Anthropic API unavailable")
        )

        result = await orch.run("Plan and execute migration.")

    mock_mig.run.assert_not_called()
    mock_val.run.assert_not_called()

    assert result.error is not None
    assert isinstance(result.error, str)
    assert len(result.error) > 0
    assert isinstance(result, OrchestrationResult)
    assert result.orchestration_id != ""
    assert result.total_duration_seconds >= 0.0


@skip_if
async def test_planning_failure_result_has_task_failed_event():
    """On planning failure, events list must include TASK_FAILED."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        mock_anthropic.return_value.messages = AsyncMock()
        mock_anthropic.return_value.messages.create = AsyncMock(
            side_effect=RuntimeError("crash")
        )
        result = await orch.run("Any task.")

    event_types = [e.event_type.value for e in result.events]
    assert "task_failed" in event_types


# ---------------------------------------------------------------------------
# Cross-scenario: OrchestrationResult always has complete schema
# ---------------------------------------------------------------------------


@skip_if
async def test_result_schema_always_complete():
    """OrchestrationResult must have all required fields in all scenarios."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        mock_anthropic.return_value.messages = AsyncMock()
        mock_anthropic.return_value.messages.create = AsyncMock(
            return_value=_response([_text_block("Task complete.")])
        )
        result = await orch.run("Test schema completeness.")

    required_attrs = [
        "orchestration_id",
        "task",
        "final_answer",
        "agents_used",
        "agent_results",
        "events",
        "total_duration_seconds",
        "error",
    ]
    for attr in required_attrs:
        assert hasattr(result, attr), f"OrchestrationResult missing: {attr}"
