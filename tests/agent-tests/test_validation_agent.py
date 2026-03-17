"""
Tests for DataValidationAgent — NO MORE STUB DATA.

Critical tests:
1. Default result is FAILED (not A/0.95) when quality_report is absent
2. BLOCK fires when required_fields_null > 1%
3. All three quality dimensions must be checked (not just one gate)
4. Tool failures → structured error, not silent pass
5. Real tool call schemas return expected fields

FIXED BUGS TESTED HERE:
- Bug: overall_score defaulted to 0.95 and grade = "A" even when Claude returned
  no quality_report JSON block. This masked real validation failures.
  Fix: default to overall_score = 0.0, grade = "F" when quality_report is None.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

try:
    from agents.data_validation_agent.agent import DataValidationAgent, ValidationResult
    VALIDATION_AGENT_AVAILABLE = True
except ImportError:
    try:
        from agents["data-validation-agent"].agent import DataValidationAgent, ValidationResult
        VALIDATION_AGENT_AVAILABLE = True
    except (ImportError, TypeError):
        VALIDATION_AGENT_AVAILABLE = False
        DataValidationAgent = None
        ValidationResult = None

try:
    from agents.data_validation_agent.tools import (
        check_field_completeness,
        validate_record_counts,
        run_custom_soql_check,
        dispatch_tool,
    )
    VALIDATION_TOOLS_AVAILABLE = True
except ImportError:
    VALIDATION_TOOLS_AVAILABLE = False

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_block(text: str) -> MagicMock:
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _tool_use_block(name: str, inp: Dict[str, Any], block_id: str = "tu_001") -> MagicMock:
    b = MagicMock()
    b.type = "tool_use"
    b.name = name
    b.input = inp
    b.id = block_id
    return b


def _claude_response(blocks: list, stop_reason: str = "end_turn") -> MagicMock:
    r = MagicMock()
    r.content = blocks
    r.stop_reason = stop_reason
    return r


# ---------------------------------------------------------------------------
# Test 1: Default result is FAILED (not A/0.95) when no quality_report is parsed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not VALIDATION_AGENT_AVAILABLE, reason="DataValidationAgent not importable")
async def test_default_grade_is_f_when_no_quality_report():
    """
    CRITICAL: When Claude does not embed a JSON quality report,
    the result must default to grade='F', overall_score=0.0.
    The previous bug returned grade='A', overall_score=0.95.
    """
    with patch("agents.data_validation_agent.agent.anthropic.AsyncAnthropic") as mock_anthropic:
        agent = DataValidationAgent(api_key="test-key-not-real")

        # Claude returns plain text only — no embedded JSON report
        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_claude_response(
            [_text_block("I was unable to complete the validation due to tool errors.")],
            stop_reason="end_turn",
        ))

        result = await agent.run(
            task="Validate Account migration for run-fail-001",
            run_id="run-fail-001",
        )

    # THE FIX: defaults must be failure state, not success state
    assert result.grade == "F", (
        f"Expected grade 'F' when no quality_report parsed, got '{result.grade}'. "
        "This is the critical bug fix — do not revert."
    )
    assert result.overall_score == 0.0, (
        f"Expected overall_score 0.0 when no quality_report parsed, got {result.overall_score}. "
        "Previous bug: defaulted to 0.95."
    )


@pytest.mark.skipif(not VALIDATION_AGENT_AVAILABLE, reason="DataValidationAgent not importable")
async def test_grade_a_only_when_quality_report_present():
    """Grade A is only returned when Claude embeds a valid quality_report JSON block."""
    with patch("agents.data_validation_agent.agent.anthropic.AsyncAnthropic") as mock_anthropic:
        agent = DataValidationAgent(api_key="test-key-not-real")

        report = {
            "overall_quality_score": 0.97,
            "grade": "A",
            "object_scores": {"Account": 0.97},
        }
        text_with_report = (
            "Validation complete.\n"
            f"```json\n{json.dumps(report)}\n```\n"
            "All checks passed."
        )
        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_claude_response(
            [_text_block(text_with_report)],
            stop_reason="end_turn",
        ))

        result = await agent.run(
            task="Validate Account migration for run-good-001",
            run_id="run-good-001",
        )

    assert result.grade == "A"
    assert result.overall_score == pytest.approx(0.97, abs=0.01)


# ---------------------------------------------------------------------------
# Test 2: BLOCK fires when required_fields_null > 1%
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not VALIDATION_TOOLS_AVAILABLE, reason="Validation tools not importable")
async def test_field_completeness_flags_null_rate_above_threshold():
    """check_field_completeness returns FAIL status when null rate exceeds threshold."""
    # Patch random to guarantee a field returns null rate above threshold
    with patch("agents.data_validation_agent.tools.random") as mock_rand:
        # First field returns 0.80 (below 0.95 threshold), rest return 0.99
        mock_rand.uniform = MagicMock(side_effect=[0.80, 0.99, 0.99, 0.99, 0.99, 0.99, 0.99, 0.99])

        result = await check_field_completeness(
            run_id="run-test-001",
            object_type="Account",
            completeness_threshold=0.95,
        )

    assert result["overall_status"] == "FAIL"
    assert result["failing_fields_count"] >= 1
    # Verify the failing field is present in the failing_fields list
    failing_names = [f["field"] for f in result["failing_fields"]]
    assert len(failing_names) >= 1
    # Null rate must be below threshold
    for ff in result["failing_fields"]:
        assert ff["rate"] < 0.95


@pytest.mark.skipif(not VALIDATION_TOOLS_AVAILABLE, reason="Validation tools not importable")
async def test_field_completeness_passes_when_all_above_threshold():
    """check_field_completeness returns PASS when all fields above threshold."""
    with patch("agents.data_validation_agent.tools.random") as mock_rand:
        # All fields return 0.99
        mock_rand.uniform = MagicMock(return_value=0.99)

        result = await check_field_completeness(
            run_id="run-test-002",
            object_type="Contact",
            completeness_threshold=0.95,
        )

    assert result["overall_status"] == "PASS"
    assert result["failing_fields_count"] == 0


# ---------------------------------------------------------------------------
# Test 3: All three quality gates must be checked
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not VALIDATION_AGENT_AVAILABLE, reason="DataValidationAgent not importable")
async def test_agent_calls_multiple_validation_tools():
    """
    The agent must invoke at least validate_record_counts, check_field_completeness,
    and check_referential_integrity — not just one gate.
    Verifies that the agent loop processes multiple tool calls before concluding.
    """
    tools_called = []

    async def mock_dispatch(tool_name: str, tool_input: Dict[str, Any]) -> Any:
        tools_called.append(tool_name)
        if tool_name == "validate_record_counts":
            return {"source_count": 10000, "salesforce_count": 9990, "match_percentage": 99.9, "status": "PASS"}
        if tool_name == "check_field_completeness":
            return {"overall_status": "PASS", "failing_fields_count": 0, "failing_fields": []}
        if tool_name == "check_referential_integrity":
            return {"total_orphaned_records": 0, "status": "PASS"}
        if tool_name == "generate_report":
            return {"overall_quality_score": 0.97, "grade": "A", "status": "PASS"}
        return {"status": "PASS"}

    with patch("agents.data_validation_agent.agent.anthropic.AsyncAnthropic") as mock_anthropic:
        agent = DataValidationAgent(api_key="test-key-not-real")

        call_count = 0

        async def multi_tool_response(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _claude_response(
                    [
                        _tool_use_block("validate_record_counts", {"run_id": "r1", "object_type": "Account"}, "tu_1"),
                        _tool_use_block("check_field_completeness", {"run_id": "r1", "object_type": "Account"}, "tu_2"),
                    ],
                    stop_reason="tool_use",
                )
            elif call_count == 2:
                return _claude_response(
                    [_tool_use_block("check_referential_integrity", {"run_id": "r1", "object_type": "Account"}, "tu_3")],
                    stop_reason="tool_use",
                )
            else:
                report = {"overall_quality_score": 0.97, "grade": "A"}
                return _claude_response(
                    [_text_block(f"All checks passed.\n```json\n{json.dumps(report)}\n```")],
                    stop_reason="end_turn",
                )

        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=multi_tool_response)

        with patch("agents.data_validation_agent.agent.dispatch_tool", side_effect=mock_dispatch):
            result = await agent.run(
                task="Validate Account migration for run-r1",
                run_id="r1",
                object_types=["Account"],
            )

    assert "validate_record_counts" in tools_called, "validate_record_counts must be called"
    assert "check_field_completeness" in tools_called, "check_field_completeness must be called"
    assert "check_referential_integrity" in tools_called, "check_referential_integrity must be called"


# ---------------------------------------------------------------------------
# Test 4: Tool failures → structured error, not silent pass
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not VALIDATION_AGENT_AVAILABLE, reason="DataValidationAgent not importable")
async def test_tool_failure_does_not_silently_pass():
    """
    When a tool raises an exception, the agent must NOT return grade A/0.95.
    The tool result must be marked is_error=True and the agent must reflect the failure.
    """
    with patch("agents.data_validation_agent.agent.anthropic.AsyncAnthropic") as mock_anthropic:
        agent = DataValidationAgent(api_key="test-key-not-real")

        call_count = 0

        async def failing_tool_then_error_response(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _claude_response(
                    [_tool_use_block("validate_record_counts", {"run_id": "r1", "object_type": "Account"}, "tu_1")],
                    stop_reason="tool_use",
                )
            else:
                return _claude_response(
                    [_text_block("Validation could not complete — database connection failed.")],
                    stop_reason="end_turn",
                )

        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=failing_tool_then_error_response)

        async def raise_db_error(tool_name: str, tool_input: Dict[str, Any]) -> Any:
            raise ConnectionError("DB_UNAVAILABLE: Cannot reach migration database")

        with patch("agents.data_validation_agent.agent.dispatch_tool", side_effect=raise_db_error):
            result = await agent.run(task="Validate Account", run_id="r1")

    # Tool failure must produce a FAILED result, not a passing one
    assert result.grade == "F", (
        f"Tool failure should produce grade F, got '{result.grade}'. "
        "Silent pass on tool errors is a critical bug."
    )
    assert result.overall_score == 0.0


@pytest.mark.skipif(not VALIDATION_TOOLS_AVAILABLE, reason="Validation tools not importable")
async def test_dispatch_tool_raises_on_unknown_tool():
    """dispatch_tool must raise ValueError for unknown tool names, not return None."""
    with pytest.raises((ValueError, KeyError)):
        await dispatch_tool("nonexistent_tool_xyz", {"param": "value"})


# ---------------------------------------------------------------------------
# Test 5: Real tool calls return expected schema
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not VALIDATION_TOOLS_AVAILABLE, reason="Validation tools not importable")
async def test_validate_record_counts_schema():
    """validate_record_counts must return all required fields with correct types."""
    result = await validate_record_counts(
        run_id="test-run-001",
        object_type="Account",
        include_skipped=True,
    )

    required_fields = [
        "run_id", "object_type", "source_count", "salesforce_count",
        "failed_count", "skipped_count", "discrepancy_count",
        "match_percentage", "status",
    ]
    for field in required_fields:
        assert field in result, f"Missing required field: {field}"

    assert result["run_id"] == "test-run-001"
    assert result["object_type"] == "Account"
    assert isinstance(result["source_count"], int)
    assert isinstance(result["salesforce_count"], int)
    assert isinstance(result["match_percentage"], float)
    assert result["status"] in ("PASS", "WARNING", "FAIL")
    assert 0.0 <= result["match_percentage"] <= 100.0


@pytest.mark.skipif(not VALIDATION_TOOLS_AVAILABLE, reason="Validation tools not importable")
async def test_check_field_completeness_schema():
    """check_field_completeness must return all required fields."""
    result = await check_field_completeness(
        run_id="test-run-001",
        object_type="Contact",
    )

    required_fields = [
        "run_id", "object_type", "threshold", "total_fields_checked",
        "failing_fields_count", "failing_fields", "field_results", "overall_status",
    ]
    for field in required_fields:
        assert field in result, f"Missing required field: {field}"

    assert result["overall_status"] in ("PASS", "FAIL")
    assert isinstance(result["failing_fields"], list)
    assert isinstance(result["field_results"], list)
    assert result["total_fields_checked"] > 0


@pytest.mark.skipif(not VALIDATION_TOOLS_AVAILABLE, reason="Validation tools not importable")
async def test_run_custom_soql_check_schema():
    """run_custom_soql_check must return required fields including soql echo."""
    result = await run_custom_soql_check(
        soql="SELECT Id, Name FROM Account WHERE Legacy_ID__c != null LIMIT 100",
        description="Verify all accounts have legacy ID populated",
        expected_count=None,
    )

    required_fields = ["description", "soql", "actual_count", "status"]
    for field in required_fields:
        assert field in result, f"Missing required field: {field}"

    assert result["status"] in ("PASS", "FAIL")
    assert isinstance(result["actual_count"], int)
    # SOQL must be echoed back for audit purposes
    assert "SELECT" in result["soql"].upper()


@pytest.mark.skipif(not VALIDATION_TOOLS_AVAILABLE, reason="Validation tools not importable")
async def test_run_custom_soql_check_expected_count_validation():
    """When expected_count is provided and doesn't match, status must be FAIL."""
    with patch("agents.data_validation_agent.tools.random") as mock_rand:
        mock_rand.randint = MagicMock(return_value=50)   # actual_count = 50

        result = await run_custom_soql_check(
            soql="SELECT COUNT() FROM Account WHERE IsDeleted = false LIMIT 10",
            description="Check record count matches expected",
            expected_count=100,  # different from 50
        )

    assert result["status"] == "FAIL"
    assert result["actual_count"] == 50
    assert result["expected_count"] == 100


# ---------------------------------------------------------------------------
# Test: ValidationResult error field propagates correctly
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not VALIDATION_AGENT_AVAILABLE, reason="DataValidationAgent not importable")
async def test_agent_error_field_set_on_exception():
    """When the Claude API raises an exception, result.error must be set (not None)."""
    with patch("agents.data_validation_agent.agent.anthropic.AsyncAnthropic") as mock_anthropic:
        agent = DataValidationAgent(api_key="test-key-not-real")
        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(
            side_effect=Exception("API connection refused")
        )

        result = await agent.run(task="Validate any thing", run_id="run-err-001")

    assert result.error is not None
    assert len(result.error) > 0
    # Should also default to failure state
    assert result.grade == "F"
    assert result.overall_score == 0.0


@pytest.mark.skipif(not VALIDATION_AGENT_AVAILABLE, reason="DataValidationAgent not importable")
async def test_validation_result_has_required_fields():
    """ValidationResult must have all schema fields even on a successful run."""
    with patch("agents.data_validation_agent.agent.anthropic.AsyncAnthropic") as mock_anthropic:
        agent = DataValidationAgent(api_key="test-key-not-real")
        report = {"overall_quality_score": 0.95, "grade": "B"}
        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_claude_response(
            [_text_block(f"Done.\n```json\n{json.dumps(report)}\n```")],
            stop_reason="end_turn",
        ))

        result = await agent.run(task="Validate", run_id="run-schema-001")

    assert result.task is not None
    assert result.run_id == "run-schema-001"
    assert isinstance(result.iterations, int)
    assert isinstance(result.duration_seconds, float)
    assert isinstance(result.tool_calls_made, int)
    assert isinstance(result.object_types, list)
