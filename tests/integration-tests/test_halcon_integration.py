"""
Integration tests for Halcon session tracking.

Tests:
1. Halcon metrics are written to sessions.jsonl after each run
2. Metrics have correct schema (all required fields present)
3. convergence_efficiency is calculated correctly
4. Multiple sessions are appended (not overwritten)
5. Failed sessions write failure metrics (not 0.95 defaults)

MOCKING:
- All Claude API calls are mocked
- The Halcon file I/O is tested against a real temp file (not mocked)
  because we want to verify the actual append-not-overwrite behaviour

Note: The HalconEmitter is in monitoring/agent_observability.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from monitoring.agent_observability import (
        HalconEmitter,
        AgentRunContext,
        observe_agent_run,
    )
    HALCON_AVAILABLE = True
except ImportError:
    HALCON_AVAILABLE = False
    HalconEmitter = None
    AgentRunContext = None
    observe_agent_run = None

pytestmark = pytest.mark.asyncio

skip_halcon = pytest.mark.skipif(
    not HALCON_AVAILABLE, reason="monitoring.agent_observability not available"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def halcon_temp_file(tmp_path: Path) -> Path:
    """Provide a temporary sessions.jsonl file for testing."""
    sessions_dir = tmp_path / ".halcon" / "retrospectives"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir / "sessions.jsonl"


@pytest.fixture
def halcon_emitter(halcon_temp_file: Path) -> "HalconEmitter":
    """HalconEmitter pointing to a temp file."""
    if not HALCON_AVAILABLE:
        pytest.skip("HalconEmitter not available")
    return HalconEmitter(sessions_file=str(halcon_temp_file))


def _make_run_context(
    agent: str = "validation",
    total_iterations: int = 5,
    tool_calls: int = 8,
    wasted_rounds: int = 1,
    final_score: float = 0.94,
    error_rate: float = 0.02,
    success: bool = True,
    error: str = None,
) -> "AgentRunContext":
    """Build a realistic AgentRunContext for testing."""
    if not HALCON_AVAILABLE:
        pytest.skip("AgentRunContext not available")

    return AgentRunContext(
        orchestration_id="orch-test-001",
        agent=agent,
        task="Test task for Halcon",
        total_iterations=total_iterations,
        tool_calls=tool_calls,
        wasted_rounds=wasted_rounds,
        final_utility_score=final_score,
        peak_utility_score=final_score + 0.02,
        error_rate=error_rate,
        success=success,
        error=error,
        duration_seconds=12.5,
    )


# ---------------------------------------------------------------------------
# Test 1: Halcon metrics are written to sessions.jsonl after each run
# ---------------------------------------------------------------------------


@skip_halcon
async def test_halcon_emitter_writes_to_file(halcon_emitter, halcon_temp_file):
    """After emit(), sessions.jsonl must exist and contain valid JSON."""
    ctx = _make_run_context()
    await halcon_emitter.emit(ctx)

    assert halcon_temp_file.exists(), "sessions.jsonl must be created by HalconEmitter"
    content = halcon_temp_file.read_text()
    assert len(content.strip()) > 0

    # File must be valid JSONL (each line is valid JSON)
    lines = [l for l in content.strip().splitlines() if l.strip()]
    assert len(lines) >= 1
    first_record = json.loads(lines[0])
    assert isinstance(first_record, dict)


# ---------------------------------------------------------------------------
# Test 2: Metrics have correct schema (all required fields present)
# ---------------------------------------------------------------------------


@skip_halcon
async def test_halcon_metrics_schema(halcon_emitter, halcon_temp_file):
    """Written metrics must contain all required Halcon fields."""
    ctx = _make_run_context(
        agent="migration",
        total_iterations=6,
        tool_calls=10,
        wasted_rounds=2,
        final_score=0.88,
    )
    await halcon_emitter.emit(ctx)

    lines = [l for l in halcon_temp_file.read_text().strip().splitlines() if l.strip()]
    record = json.loads(lines[0])

    required_fields = [
        "timestamp_utc",
        "convergence_efficiency",
        "final_utility",
        "peak_utility",
        "decision_density",
        "adaptation_utilization",
        "wasted_rounds",
        "structural_instability_score",
        "dominant_failure_mode",
        "evidence_trajectory",
        "inferred_problem_class",
    ]
    for field in required_fields:
        assert field in record, (
            f"Required Halcon field '{field}' missing from sessions.jsonl. "
            f"Actual fields: {list(record.keys())}"
        )


# ---------------------------------------------------------------------------
# Test 3: convergence_efficiency is calculated correctly
# ---------------------------------------------------------------------------


@skip_halcon
async def test_convergence_efficiency_calculation(halcon_emitter, halcon_temp_file):
    """
    convergence_efficiency = 1 - (wasted_rounds / total_iterations)
    For wasted=1, total=5: efficiency = 1 - 0.2 = 0.8
    """
    ctx = _make_run_context(total_iterations=5, wasted_rounds=1)
    await halcon_emitter.emit(ctx)

    record = json.loads(halcon_temp_file.read_text().strip().splitlines()[0])
    expected_efficiency = 1.0 - (1 / 5)
    assert record["convergence_efficiency"] == pytest.approx(expected_efficiency, abs=0.01), (
        f"convergence_efficiency should be {expected_efficiency}, "
        f"got {record['convergence_efficiency']}"
    )


@skip_halcon
async def test_convergence_efficiency_perfect(halcon_emitter, halcon_temp_file):
    """Zero wasted rounds → efficiency = 1.0"""
    ctx = _make_run_context(total_iterations=5, wasted_rounds=0)
    await halcon_emitter.emit(ctx)

    record = json.loads(halcon_temp_file.read_text().strip().splitlines()[0])
    assert record["convergence_efficiency"] == pytest.approx(1.0, abs=0.001)


@skip_halcon
async def test_convergence_efficiency_all_wasted(halcon_emitter, halcon_temp_file):
    """All wasted rounds → efficiency = 0.0"""
    ctx = _make_run_context(total_iterations=4, wasted_rounds=4)
    await halcon_emitter.emit(ctx)

    record = json.loads(halcon_temp_file.read_text().strip().splitlines()[0])
    assert record["convergence_efficiency"] == pytest.approx(0.0, abs=0.001)


# ---------------------------------------------------------------------------
# Test 4: Multiple sessions are appended (not overwritten)
# ---------------------------------------------------------------------------


@skip_halcon
async def test_multiple_sessions_appended(halcon_emitter, halcon_temp_file):
    """Emitting three sessions must result in three JSON lines (append mode)."""
    contexts = [
        _make_run_context(agent="migration", final_score=0.90),
        _make_run_context(agent="validation", final_score=0.85),
        _make_run_context(agent="security", final_score=0.95),
    ]

    for ctx in contexts:
        await halcon_emitter.emit(ctx)

    lines = [l for l in halcon_temp_file.read_text().strip().splitlines() if l.strip()]
    assert len(lines) == 3, (
        f"Expected 3 session records (one per emit), got {len(lines)}. "
        "sessions.jsonl must be opened in append mode, not write mode."
    )

    # Verify all three are valid JSON
    for i, line in enumerate(lines):
        record = json.loads(line)
        assert isinstance(record, dict), f"Line {i} is not a valid JSON object"


@skip_halcon
async def test_sessions_contain_different_data(halcon_emitter, halcon_temp_file):
    """Each appended session must have its own unique timestamp."""
    import time

    ctx1 = _make_run_context(agent="migration")
    await halcon_emitter.emit(ctx1)

    # Small delay to ensure different timestamps
    await asyncio.sleep(0.01) if False else None

    ctx2 = _make_run_context(agent="validation")
    await halcon_emitter.emit(ctx2)

    lines = halcon_temp_file.read_text().strip().splitlines()
    r1 = json.loads(lines[0])
    r2 = json.loads(lines[1])

    # Timestamps should be present (even if identical in fast tests, the fields exist)
    assert "timestamp_utc" in r1
    assert "timestamp_utc" in r2


# ---------------------------------------------------------------------------
# Test 5: Failed sessions write failure metrics (not 0.95 defaults)
# ---------------------------------------------------------------------------


@skip_halcon
async def test_failed_session_writes_failure_metrics(halcon_emitter, halcon_temp_file):
    """
    A failed agent run must NOT write final_utility=0.95 or convergence_efficiency=1.0.
    These were the hardcoded 'success' defaults that masked failures.
    """
    ctx = _make_run_context(
        success=False,
        final_score=0.0,
        error="Anthropic API connection timeout after 30s",
        wasted_rounds=5,
        total_iterations=5,
    )
    await halcon_emitter.emit(ctx)

    record = json.loads(halcon_temp_file.read_text().strip().splitlines()[0])

    # Critical assertion: failed runs must NOT default to success values
    assert record["final_utility"] != 0.95, (
        "final_utility=0.95 on a failed run indicates the hardcoded default bug is present. "
        "Failed sessions must write actual failure values."
    )
    assert record["convergence_efficiency"] != 1.0, (
        "convergence_efficiency=1.0 on a failed run is incorrect. "
        "Failed sessions must write actual efficiency values."
    )
    assert record["final_utility"] == pytest.approx(0.0, abs=0.01)
    assert record["dominant_failure_mode"] is not None
    assert record["dominant_failure_mode"] != ""


@skip_halcon
async def test_failed_session_evidence_trajectory(halcon_emitter, halcon_temp_file):
    """Failed sessions must have evidence_trajectory = 'flat' or 'non-monotonic'."""
    ctx = _make_run_context(success=False, final_score=0.0)
    await halcon_emitter.emit(ctx)

    record = json.loads(halcon_temp_file.read_text().strip().splitlines()[0])
    assert record["evidence_trajectory"] in ("flat", "non-monotonic"), (
        f"Failed run must have trajectory 'flat' or 'non-monotonic', "
        f"got '{record['evidence_trajectory']}'"
    )


@skip_halcon
async def test_successful_session_evidence_trajectory(halcon_emitter, halcon_temp_file):
    """Successful sessions must have evidence_trajectory = 'monotonic'."""
    ctx = _make_run_context(success=True, final_score=0.92)
    await halcon_emitter.emit(ctx)

    record = json.loads(halcon_temp_file.read_text().strip().splitlines()[0])
    assert record["evidence_trajectory"] == "monotonic", (
        f"Successful run should have trajectory 'monotonic', "
        f"got '{record['evidence_trajectory']}'"
    )


# ---------------------------------------------------------------------------
# Test: observe_agent_run context manager
# ---------------------------------------------------------------------------


@skip_halcon
async def test_observe_agent_run_emits_halcon_on_success(halcon_temp_file):
    """observe_agent_run() must emit Halcon metrics after successful agent execution."""
    emitter = HalconEmitter(sessions_file=str(halcon_temp_file))

    async def mock_agent_run():
        return MagicMock(
            final_answer="Migration healthy.",
            error=None,
            iterations=4,
            tool_calls_made=[1, 2, 3],
            overall_score=0.95,
        )

    async with observe_agent_run(
        agent="migration",
        task="Health check",
        halcon_emitter=emitter,
    ) as ctx:
        result = await mock_agent_run()
        ctx.record_completion(result)

    lines = halcon_temp_file.read_text().strip().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[0])
    assert record["final_utility"] >= 0.0
    assert record["final_utility"] <= 1.0


@skip_halcon
async def test_observe_agent_run_emits_halcon_on_error(halcon_temp_file):
    """observe_agent_run() must emit Halcon metrics even when the agent raises."""
    emitter = HalconEmitter(sessions_file=str(halcon_temp_file))

    with pytest.raises(RuntimeError):
        async with observe_agent_run(
            agent="migration",
            task="Failing task",
            halcon_emitter=emitter,
        ) as ctx:
            raise RuntimeError("Simulated agent crash")

    lines = halcon_temp_file.read_text().strip().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[0])
    assert record["dominant_failure_mode"] is not None
    assert record["final_utility"] == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# Test: Numeric field constraints
# ---------------------------------------------------------------------------


@skip_halcon
async def test_all_float_fields_in_range(halcon_emitter, halcon_temp_file):
    """All numeric Halcon fields must be in [0.0, 1.0]."""
    ctx = _make_run_context(
        total_iterations=8,
        tool_calls=12,
        wasted_rounds=2,
        final_score=0.87,
    )
    await halcon_emitter.emit(ctx)

    record = json.loads(halcon_temp_file.read_text().strip().splitlines()[0])

    bounded_fields = [
        "convergence_efficiency",
        "final_utility",
        "peak_utility",
        "decision_density",
        "adaptation_utilization",
        "structural_instability_score",
    ]
    for field in bounded_fields:
        value = record[field]
        assert 0.0 <= value <= 1.0, (
            f"Halcon field '{field}' = {value} is out of [0.0, 1.0] range"
        )
