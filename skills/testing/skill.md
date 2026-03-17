---
name: testing
description: Generates complete pytest test suites for migration pipeline components
type: skill
version: 2.0.0
agent: testing-agent
---

# Testing Skill

**Version**: 2.0.0
**Agent**: testing-agent
**Last Updated**: 2026-03

---

## Purpose

The `testing` skill generates complete, runnable `pytest` test suites for migration pipeline
components. Given source code and a test type, it produces test files with appropriate fixtures,
mocking strategies, and assertions. Generated tests follow the project's existing conventions
and are designed to be committed directly to the `tests/` directory.

Tests default to targeting **80% line coverage**. The generator adds edge cases and error paths
to meet higher coverage targets when `coverage_target_percent` is raised.

---

## Test Types

### `unit`

Tests a single function, method, or class in isolation. All external dependencies (databases,
HTTP clients, Salesforce API, Kafka) are mocked.

**Characteristics**:
- Uses `unittest.mock.patch` or `pytest-mock`'s `mocker` fixture
- Follows Arrange-Act-Assert (AAA) pattern — each test covers exactly one behaviour
- `@pytest.mark.parametrize` for data-driven scenarios
- `@pytest.mark.asyncio` for coroutine-based subjects (or auto-mode if configured)
- Does NOT start real services or make network calls

**Naming convention**: `tests/unit/{module_path}/test_{source_file}.py`

---

### `integration`

Tests interaction between two or more components with real or containerised dependencies.
External SaaS (Salesforce, Vault) remains mocked.

**Characteristics**:
- `pytest` fixtures with `scope="session"` or `scope="module"` for shared state
- Real database reads/writes against a test schema (in-memory SQLite or testcontainers PostgreSQL)
- Validates end-to-end data flow through a pipeline segment
- Tests rollback and error recovery paths

**Naming convention**: `tests/integration/{domain}/test_{scenario}.py`

---

### `contract`

Validates that an API consumer or producer honours the agreed contract (request/response shape,
required fields, status codes).

**Characteristics**:
- Generated for adapter classes that call external APIs (migration REST API, Salesforce)
- Asserts on schema structure using `jsonschema.validate` against the relevant `schema.json`
- Captures consumer-driven contract expectations
- Does not assert on runtime values — only structure and required fields

**Naming convention**: `tests/contracts/{service_name}/test_{contract_name}.py`

---

### `migration_validation`

End-to-end tests that verify data integrity after a migration job by querying source and target.

**Characteristics**:
- Uses the `ValidationAgent` or `run_validation()` function internally
- Parametrized by entity name and tenant
- Checks: record counts, required fields, referential integrity, field-level sample match
- Marked with `@pytest.mark.migration_validation` for selective execution
- Requires a running migration API and Salesforce sandbox connection

**Naming convention**: `tests/migration_validation/{entity}/test_post_migration_{entity}.py`

---

## Standard Fixtures

All generated test suites reference fixtures from `conftest.py`:

| Fixture | Scope | Description |
|---------|-------|-------------|
| `mock_migration_api` | `function` | `AsyncMock` wrapping the migration API HTTP client |
| `mock_sf_client` | `function` | `MagicMock`/`AsyncMock` wrapping the Salesforce API client |
| `db_session` | `function` | SQLAlchemy Session bound to test database; rolls back after each test |
| `test_tenant_id` | `session` | Fixed tenant ID string: `"test-tenant-001"` |
| `sample_migration_id` | `function` | Freshly generated migration run UUID |
| `sample_job_id` | `function` | Freshly generated job UUID |
| `mock_anthropic_client` | `function` | `AsyncMock` wrapping `anthropic.AsyncAnthropic` — prevents real API calls |

---

## Async Test Pattern

For coroutine-based components, generated tests use `pytest-asyncio`:

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_execute_step_returns_completed_on_success(
    mock_migration_api: AsyncMock,
    sample_migration_id: str,
) -> None:
    # Arrange
    mock_migration_api.get.return_value.json.return_value = {"already_completed": False}
    mock_migration_api.post.return_value.json.return_value = {
        "status": "COMPLETED",
        "records_processed": 1000,
        "duration_ms": 5000,
    }

    # Act
    from agents.execution_agent.agent import execute_single_step, PlanStep
    result = await execute_single_step(
        plan_step=PlanStep(
            step_id="test-step-001",
            step_type="extract",
            entity_name="Account",
            phase="extraction",
            sequence_number=1,
        ),
        migration_id=sample_migration_id,
        tenant_id="test-tenant-001",
    )

    # Assert
    assert result.status.value == "COMPLETED"
    assert result.records_processed == 1000
```

---

## Input Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `component_type` | string | Yes | `adapter`, `validator`, `transformer`, `pipeline`, `tool`, `api_endpoint`, `agent` |
| `source_code` | string | Yes | Source code of the component under test |
| `test_type` | string | Yes | `unit`, `integration`, `contract`, `migration_validation` |
| `coverage_target_percent` | integer | No | Desired line coverage (default: 80, range: 1–100) |

---

## Output Schema

| Field | Type | Description |
|-------|------|-------------|
| `test_code` | string | Complete, runnable pytest test file content |
| `test_file_path` | string | Recommended file path starting with `tests/` |
| `estimated_coverage` | integer | Estimated line coverage the generated tests will achieve |
| `test_count` | integer | Total number of `def test_*` functions in the file |
| `fixtures_required` | string[] | Fixture names the test file depends on (must be in conftest.py) |

---

## Coverage Requirements

| Test Type | Minimum Coverage | Recommended |
|-----------|-----------------|-------------|
| `unit` | 80% | 90%+ |
| `integration` | 70% | 80%+ |
| `contract` | N/A (structural tests) | N/A |
| `migration_validation` | N/A (E2E tests) | N/A |

For `unit` tests, the generator includes:
- Happy path test
- Invalid input test (Pydantic validation)
- HTTP error path test (for tools that call HTTP APIs)
- Retry exhaustion test (for components with retry logic)
- Empty result set test
- Boundary value tests for threshold-based logic

---

## Example Invocation

```python
from agents.testing_agent.agent import generate_tests

result = await generate_tests(
    component_type="agent",
    source_code=open("agents/execution_agent/agent.py").read(),
    test_type="unit",
    coverage_target_percent=90,
)

print(f"Generated {result.test_count} tests (~{result.estimated_coverage}% coverage)")
print(f"Write to: {result.test_file_path}")
```

---

## When to Use

Use the testing skill when:
- A new component is created and unit tests are needed immediately
- Integration tests are needed before promoting a migration to production
- Contract tests are needed to lock in the migration API schema
- Post-migration validation tests are needed for acceptance sign-off

Do NOT use the testing skill when:
- Load/performance testing is needed (use k6 or Locust directly)
- Security scanning is needed (use bandit/semgrep in CI)
- The component under test requires manual test data setup that cannot be mocked

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Model for test generation |
| `TESTING_AGENT_MAX_TOKENS` | `8192` | Max tokens for test generation response |
