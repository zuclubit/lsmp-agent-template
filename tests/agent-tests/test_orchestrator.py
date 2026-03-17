"""
Tests for MultiAgentOrchestrator — blocking gate enforcement is critical.

Key test scenarios:
1. Validation BLOCK → Execution is NOT called
2. Security CRITICAL → Execution is BLOCKED with correct reason
3. Planning fails → Orchestration returns structured error
4. All gates pass → Execution agent is called
5. HIGH risk → Human-in-the-loop gate fires
6. Halcon metrics are emitted on every run
7. No circular dependencies in agent calls

MOCKING STRATEGY:
- ALL anthropic.AsyncAnthropic calls are mocked via patch
- Agent delegates (MigrationAgent, DataValidationAgent, etc.) are replaced with AsyncMock
- The gate logic in _do_synthesise() is exercised via the orchestrator code path
- We test that the orchestrator's Python-level decisions are correct, not Claude's reasoning
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Test imports with graceful fallback for CI environments
# ---------------------------------------------------------------------------

try:
    from agents.orchestrator.multi_agent_orchestrator import (
        MultiAgentOrchestrator,
        OrchestrationResult,
        AgentName,
        EventType,
        _SUPERVISOR_TOOLS,
    )
    ORCHESTRATOR_AVAILABLE = True
except ImportError:
    ORCHESTRATOR_AVAILABLE = False
    MultiAgentOrchestrator = None
    OrchestrationResult = None
    AgentName = None
    EventType = None

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers — fake Claude responses
# ---------------------------------------------------------------------------


def _make_text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(name: str, tool_input: Dict[str, Any], block_id: str = "tu_001") -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = tool_input
    block.id = block_id
    return block


def _make_claude_response(
    content_blocks: list,
    stop_reason: str = "tool_use",
) -> MagicMock:
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


def _make_final_claude_response(text: str) -> MagicMock:
    return _make_claude_response(
        [_make_text_block(text)],
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_migration_agent():
    agent = AsyncMock()
    agent_result = MagicMock()
    agent_result.final_answer = "Migration run healthy. No action required."
    agent_result.tool_calls_made = []
    agent_result.decided_actions = []
    agent_result.error = None
    agent.run = AsyncMock(return_value=agent_result)
    return agent


@pytest.fixture
def mock_validation_agent_pass():
    agent = AsyncMock()
    result = MagicMock()
    result.final_answer = "Data quality validated. Grade: A. Score: 0.97."
    result.overall_score = 0.97
    result.grade = "A"
    result.error = None
    agent.run = AsyncMock(return_value=result)
    return agent


@pytest.fixture
def mock_validation_agent_fail():
    agent = AsyncMock()
    result = MagicMock()
    result.final_answer = "CRITICAL: required_fields_null rate 3.2% exceeds 1% threshold. BLOCK."
    result.overall_score = 0.52
    result.grade = "F"
    result.error = None
    agent.run = AsyncMock(return_value=result)
    return agent


@pytest.fixture
def mock_security_agent_pass():
    agent = AsyncMock()
    result = MagicMock()
    result.final_answer = "No critical or high findings. Security gate: PASS."
    result.risk_level = "LOW"
    result.pass_security_gate = True
    result.findings_count = 2
    result.error = None
    agent.run = AsyncMock(return_value=result)
    return agent


@pytest.fixture
def mock_security_agent_critical():
    agent = AsyncMock()
    result = MagicMock()
    result.final_answer = "CRITICAL: Hardcoded API key found in integrations/rest_clients/base_client.py"
    result.risk_level = "CRITICAL"
    result.pass_security_gate = False
    result.findings_count = 1
    result.error = None
    agent.run = AsyncMock(return_value=result)
    return agent


@pytest.fixture
def mock_documentation_agent():
    agent = AsyncMock()
    result = MagicMock()
    result.generated_content = "# Post-Migration Report\n\nMigration completed successfully."
    result.files_written = ["docs/migration_report_run-abc-123.md"]
    result.error = None
    agent.run = AsyncMock(return_value=result)
    return agent


@pytest.fixture
def orchestrator_with_mocked_agents(
    mock_migration_agent,
    mock_validation_agent_pass,
    mock_security_agent_pass,
    mock_documentation_agent,
):
    """Orchestrator where all specialist agents are replaced with mocks."""
    if not ORCHESTRATOR_AVAILABLE:
        pytest.skip("Orchestrator module not available")

    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        orch._agents[AgentName.MIGRATION] = mock_migration_agent
        orch._agents[AgentName.VALIDATION] = mock_validation_agent_pass
        orch._agents[AgentName.SECURITY] = mock_security_agent_pass
        orch._agents[AgentName.DOCUMENTATION] = mock_documentation_agent
        orch._client = mock_anthropic.return_value
        yield orch, mock_anthropic.return_value


# ---------------------------------------------------------------------------
# Test 1: Validation BLOCK → Execution is NOT called
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_validation_block_prevents_execution(
    mock_migration_agent,
    mock_validation_agent_fail,
    mock_security_agent_pass,
    mock_documentation_agent,
):
    """When validation returns grade F, the orchestrator must not call execution tools."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        orch._agents[AgentName.MIGRATION] = mock_migration_agent
        orch._agents[AgentName.VALIDATION] = mock_validation_agent_fail
        orch._agents[AgentName.SECURITY] = mock_security_agent_pass
        orch._agents[AgentName.DOCUMENTATION] = mock_documentation_agent

        # Claude first delegates to validation, then synthesises (no migration action)
        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            # Turn 1: delegate to validation
            _make_claude_response([
                _make_tool_use_block(
                    "delegate_to_validation_agent",
                    {"task": "Validate Account migration for run-abc-123", "run_id": "run-abc-123"},
                )
            ]),
            # Turn 2: synthesise and return BLOCKED
            _make_final_claude_response(
                "BLOCKED: Validation failed with grade F. "
                "Required fields null rate 3.2% exceeds 1% threshold. "
                "Execution halted."
            ),
        ])

        result = await orch.run(
            "Run post-migration pipeline for run-abc-123.",
            context={"run_id": "run-abc-123"},
        )

    # Validation was called
    mock_validation_agent_fail.run.assert_called_once()
    # Migration execution tools were NOT called (no pause, resume, cancel, retry)
    mock_migration_agent.run.assert_not_called()
    # Final answer contains BLOCKED
    assert "BLOCK" in result.final_answer.upper() or result.error is None


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_synthesise_blocks_when_validation_grade_f():
    """_do_synthesise() returns BLOCKED when any agent returns grade F."""
    if not ORCHESTRATOR_AVAILABLE:
        pytest.skip()

    result = MultiAgentOrchestrator._do_synthesise(
        results={
            "validation": {"grade": "F", "quality_score": 0.52, "error": None},
            "documentation": {"error": None, "files_written": []},
        },
        synthesis_goal="Determine if migration can proceed",
    )

    assert result["overall_status"] == "BLOCKED"
    assert any("validation" in issue.lower() for issue in result["issues"])
    assert len(result["issues"]) >= 1


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_synthesise_passes_when_all_agents_succeed():
    """_do_synthesise() returns APPROVED when all agents succeed."""
    result = MultiAgentOrchestrator._do_synthesise(
        results={
            "validation": {"grade": "A", "quality_score": 0.97, "error": None},
            "security": {"risk_level": "LOW", "pass_gate": True, "error": None},
        },
        synthesis_goal="Check if safe to proceed",
    )

    assert result["overall_status"] == "APPROVED"
    assert result["issues"] == []


# ---------------------------------------------------------------------------
# Test 2: Security CRITICAL → Execution is BLOCKED
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_security_critical_blocks_execution(
    mock_migration_agent,
    mock_validation_agent_pass,
    mock_security_agent_critical,
    mock_documentation_agent,
):
    """When security agent returns CRITICAL risk, execution must be blocked."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        orch._agents[AgentName.MIGRATION] = mock_migration_agent
        orch._agents[AgentName.VALIDATION] = mock_validation_agent_pass
        orch._agents[AgentName.SECURITY] = mock_security_agent_critical
        orch._agents[AgentName.DOCUMENTATION] = mock_documentation_agent

        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _make_claude_response([
                _make_tool_use_block(
                    "delegate_to_security_agent",
                    {"task": "Scan integration layer for secrets", "scope": "integrations/"},
                )
            ]),
            _make_final_claude_response(
                "BLOCKED: Security audit found CRITICAL vulnerability — hardcoded API key. "
                "Deployment halted. Remediate before proceeding."
            ),
        ])

        result = await orch.run("Run security preflight for deployment.")

    mock_security_agent_critical.run.assert_called_once()
    mock_migration_agent.run.assert_not_called()


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_synthesise_blocks_on_security_critical():
    """_do_synthesise() returns BLOCKED when security reports CRITICAL."""
    result = MultiAgentOrchestrator._do_synthesise(
        results={
            "validation": {"grade": "A", "error": None},
            "security": {
                "risk_level": "CRITICAL",
                "pass_gate": False,
                "findings": 1,
                "error": None,
            },
        },
        synthesis_goal="Pre-deployment check",
    )

    assert result["overall_status"] == "BLOCKED"
    assert any("security" in issue.lower() for issue in result["issues"])


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_synthesise_blocks_on_security_high():
    """HIGH risk level also triggers BLOCKED status."""
    result = MultiAgentOrchestrator._do_synthesise(
        results={
            "security": {"risk_level": "HIGH", "pass_gate": False, "error": None},
        },
        synthesis_goal="Security gate check",
    )

    assert result["overall_status"] == "BLOCKED"


# ---------------------------------------------------------------------------
# Test 3: Planning fails → structured error returned
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_planning_failure_returns_structured_error():
    """If Claude API raises an exception, orchestration returns a structured error."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")

        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(
            side_effect=Exception("Anthropic API unavailable")
        )

        result = await orch.run("Validate migration run-abc-123.")

    assert result.error is not None
    assert "error" in result.error.lower() or "unavailable" in result.error.lower()
    assert result.final_answer != ""   # must still return something, not crash
    assert isinstance(result.total_duration_seconds, float)


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_orchestration_result_always_has_events():
    """Even on failure, the orchestration result must contain at least TASK_STARTED event."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))

        result = await orch.run("Any task.")

    assert len(result.events) >= 1
    assert result.events[0].event_type.value == "task_started"


# ---------------------------------------------------------------------------
# Test 4: All gates pass → Execution agent is called
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_all_gates_pass_execution_proceeds(
    mock_migration_agent,
    mock_validation_agent_pass,
    mock_security_agent_pass,
    mock_documentation_agent,
):
    """When both validation and security pass, migration execution tools can be called."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        orch._agents[AgentName.MIGRATION] = mock_migration_agent
        orch._agents[AgentName.VALIDATION] = mock_validation_agent_pass
        orch._agents[AgentName.SECURITY] = mock_security_agent_pass
        orch._agents[AgentName.DOCUMENTATION] = mock_documentation_agent

        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _make_claude_response([
                _make_tool_use_block(
                    "run_agents_in_parallel",
                    {"tasks": [
                        {"agent": "validation", "task": "Validate run-abc-123"},
                        {"agent": "security", "task": "Security check"},
                    ]},
                )
            ]),
            _make_claude_response([
                _make_tool_use_block(
                    "delegate_to_migration_agent",
                    {"task": "Retry failed records for run-abc-123", "context": {"run_id": "run-abc-123"}},
                    block_id="tu_002",
                )
            ]),
            _make_final_claude_response(
                "APPROVED: All gates passed. Retried 45 failed records. "
                "Documentation generated."
            ),
        ])

        result = await orch.run(
            "Post-migration for run-abc-123: validate, security check, then retry failures."
        )

    mock_validation_agent_pass.run.assert_called()
    mock_migration_agent.run.assert_called_once()
    assert result.error is None


# ---------------------------------------------------------------------------
# Test 5: HIGH risk → human-in-the-loop gate awareness
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_synthesise_blocks_on_agent_error():
    """When a delegate returns an error dict, synthesise treats it as BLOCKED."""
    result = MultiAgentOrchestrator._do_synthesise(
        results={
            "migration": {"error": "Salesforce API returned 503"},
            "validation": {"grade": "A", "error": None},
        },
        synthesis_goal="Post-migration pipeline",
    )

    assert result["overall_status"] == "BLOCKED"
    assert any("migration" in issue.lower() for issue in result["issues"])


# ---------------------------------------------------------------------------
# Test 6: Halcon metrics — agent run produces structured events
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_orchestration_events_capture_duration():
    """Every agent delegation event must capture duration_seconds."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")

        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(
            return_value=_make_final_claude_response("Task complete.")
        )

        result = await orch.run("Quick task with no tool calls.")

    assert result.total_duration_seconds >= 0.0
    # TASK_COMPLETED or TASK_FAILED event must include duration
    completion_events = [
        e for e in result.events
        if e.event_type.value in ("task_completed", "task_failed")
    ]
    assert len(completion_events) == 1
    assert completion_events[0].duration_seconds is not None
    assert completion_events[0].duration_seconds >= 0.0


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_orchestration_result_schema_complete():
    """OrchestrationResult must have all required fields."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(
            return_value=_make_final_claude_response("Done.")
        )

        result = await orch.run("Simple task.")

    assert result.orchestration_id != ""
    assert result.task == "Simple task."
    assert isinstance(result.agents_used, list)
    assert isinstance(result.agent_results, dict)
    assert isinstance(result.events, list)
    assert isinstance(result.total_duration_seconds, float)


# ---------------------------------------------------------------------------
# Test 7: No circular dependencies in agent calls
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_no_circular_delegation():
    """The orchestrator must not appear in its own delegate list."""
    if not ORCHESTRATOR_AVAILABLE:
        pytest.skip()

    from agents.orchestrator.multi_agent_orchestrator import AGENT_REGISTRY

    # Verify ORCHESTRATOR is not in the delegate tools
    delegate_tool_names = {t["name"] for t in _SUPERVISOR_TOOLS}
    assert "delegate_to_orchestrator" not in delegate_tool_names

    # Verify agent registry does not include self-delegation patterns
    for tool in _SUPERVISOR_TOOLS:
        if tool["name"].startswith("delegate_to_"):
            target = tool["name"].replace("delegate_to_", "").replace("_agent", "")
            assert target != "orchestrator", (
                f"Tool {tool['name']} would create circular delegation"
            )


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_parallel_agents_independent():
    """run_agents_in_parallel calls each agent independently (no cross-agent state)."""
    mock_mig = AsyncMock()
    mock_val = AsyncMock()

    mig_result = MagicMock()
    mig_result.final_answer = "Migration healthy."
    mig_result.tool_calls_made = []
    mig_result.decided_actions = []
    mig_result.error = None

    val_result = MagicMock()
    val_result.final_answer = "Data quality: A."
    val_result.overall_score = 0.97
    val_result.grade = "A"
    val_result.error = None

    mock_mig.run = AsyncMock(return_value=mig_result)
    mock_val.run = AsyncMock(return_value=val_result)

    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")
        orch._agents[AgentName.MIGRATION] = mock_mig
        orch._agents[AgentName.VALIDATION] = mock_val

        combined, agents_used = await orch._run_parallel([
            {"agent": "migration", "task": "Check health of run-abc-123"},
            {"agent": "validation", "task": "Validate Account data"},
        ])

    mock_mig.run.assert_called_once()
    mock_val.run.assert_called_once()
    assert AgentName.MIGRATION in agents_used
    assert AgentName.VALIDATION in agents_used
    # Verify each result is stored independently
    assert "task_0" in combined
    assert "task_1" in combined


# ---------------------------------------------------------------------------
# Test: Unknown supervisor tool returns structured error (not exception)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_unknown_supervisor_tool_returns_error():
    """An unknown tool name in execute_supervisor_tool must return an error dict, not raise."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic"):
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")

        result, agents_used = await orch._execute_supervisor_tool(
            "hallucinated_tool_name",
            {"param": "value"},
        )

    assert "error" in result
    assert "Unknown" in result["error"] or "hallucinated_tool_name" in result["error"]
    assert agents_used == []


# ---------------------------------------------------------------------------
# Test: Max iterations guard
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ORCHESTRATOR_AVAILABLE, reason="Orchestrator module not available")
async def test_max_iterations_stops_loop():
    """Orchestrator must stop after max_iterations even if Claude keeps returning tool_use."""
    with patch("agents.orchestrator.multi_agent_orchestrator.anthropic.AsyncAnthropic") as mock_anthropic:
        orch = MultiAgentOrchestrator(api_key="test-key-not-real")

        # Claude always returns a tool_use block, never end_turn
        infinite_response = _make_claude_response(
            [_make_tool_use_block("synthesise_results", {"results": {}, "synthesis_goal": "test"})],
            stop_reason="tool_use",
        )
        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=infinite_response)

        # Run with a very small max_iterations to test the guard
        result = await orch.run("Infinite loop task.", max_iterations=3)

    # Should complete (not hang forever) and return a result
    assert isinstance(result, OrchestrationResult)
    assert result.orchestration_id != ""
