# testing Skill — Usage Examples

## Example 1 — Valid Invocation

**Scenario:** Generate unit tests for the `check_migration_status` tool in the migration agent.

**Input:**
```json
{
  "component_under_test": "check_migration_status tool in agents/migration-agent/tools.py",
  "test_type": "unit",
  "source_file": "agents/migration-agent/tools.py",
  "coverage_targets": [
    "happy_path_returns_run_status",
    "http_404_raises_tool_error",
    "http_500_raises_tool_error",
    "response_strips_pii_fields"
  ]
}
```

**Expected Output (excerpt):**
```python
# tests/agent-tests/test_migration_tools.py
import pytest
from unittest.mock import AsyncMock, patch
from agents.migration_agent.tools import check_migration_status

SYNTHETIC_RUN = {
    "run_id": "run-test-001",
    "status": "IN_PROGRESS",
    "object_type": "Account",
    "records_processed": 10000,
    "records_failed": 5,
    "error_rate": 0.0005,
}

@pytest.mark.unit
async def test_check_migration_status_happy_path():
    with patch("agents.migration_agent.tools._http_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = SYNTHETIC_RUN
        result = await check_migration_status("run-test-001")
    assert result["status"] == "IN_PROGRESS"
    assert result["run_id"] == "run-test-001"

@pytest.mark.unit
async def test_check_migration_status_404_raises_tool_error():
    with patch("agents.migration_agent.tools._http_get", side_effect=Exception("404 Not Found")):
        result = await check_migration_status("run-nonexistent")
    assert "error" in result
    assert result.get("is_error") is True

@pytest.mark.unit
async def test_check_migration_status_strips_pii():
    pii_run = {**SYNTHETIC_RUN, "customer_email": "john.doe@example.com"}
    with patch("agents.migration_agent.tools._http_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = pii_run
        result = await check_migration_status("run-test-001")
    assert "customer_email" not in result
```

**Result:** `ALLOWED` — synthetic fixtures used, no real PII, no mocked validation gates.

---

## Example 2 — Blocked by Validation (Mocking the Validation Gate)

**Scenario:** A developer tries to generate a test that mocks the validation gate to always return ALLOW.

**Input:**
```json
{
  "component_under_test": "orchestrator post-migration pipeline",
  "test_type": "integration",
  "source_file": "agents/orchestrator/multi_agent_orchestrator.py",
  "coverage_targets": ["mock the validation_gate to return ALLOW so we can test execution path directly"]
}
```

**Validation rule triggered:** `no_mock_validation_gate`
Pattern match: `mock the validation_gate`

**Expected Response:**
```json
{
  "status": "BLOCKED",
  "reason": "Input requests mocking of the validation gate, which is explicitly forbidden. Validation gates must be exercised through real implementations. Use a synthetic migration run fixture that naturally produces a passing ValidationResult instead.",
  "code": "SKILL_MOCK_GATE_BLOCKED"
}
```

**Result:** `BLOCKED` — the test would bypass a critical safety control.

---

## Example 3 — Edge Case: Synthetic Fixture with Boundary-Value Error Rate

**Scenario:** Generate a failure scenario test that exercises the 20% error rate threshold at exactly the boundary.

**Input:**
```json
{
  "component_under_test": "MigrationAgent error rate investigation logic",
  "test_type": "failure_scenario",
  "source_file": "agents/migration-agent/agent.py",
  "coverage_targets": [
    "error_rate_exactly_at_20pct_threshold",
    "error_rate_one_basis_point_above_threshold",
    "error_rate_one_basis_point_below_threshold"
  ]
}
```

**Expected Output (excerpt):**
```python
# tests/failure-scenarios/test_error_rate_boundary.py
import pytest

BOUNDARY_RUNS = [
    # (error_rate, should_trigger_investigation)
    (0.1999, False),   # 1 basis point below threshold — no auto-investigate
    (0.2000, True),    # exactly at threshold — triggers investigation
    (0.2001, True),    # 1 basis point above threshold — triggers investigation
]

@pytest.mark.parametrize("error_rate,expect_investigation", BOUNDARY_RUNS)
async def test_error_rate_threshold_boundary(error_rate, expect_investigation, mock_api_client):
    mock_api_client.get_run_status.return_value = {
        "run_id": "run-boundary-001",
        "error_rate": error_rate,
        "status": "IN_PROGRESS",
    }
    result = await migration_agent.run(
        f"Monitor run-boundary-001. Error rate is {error_rate}.",
        context={"run_id": "run-boundary-001"},
    )
    if expect_investigation:
        assert "investigate" in result.lower() or "error_report" in str(result)
    else:
        assert "no action required" in result.lower()
```

**Result:** `ALLOWED` — all fixtures are synthetic, boundary values are precisely specified, no credentials or PII present.
