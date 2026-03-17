# Contributing to the Legacy-to-Salesforce Migration Platform

Thank you for contributing to LSMP. This document covers development setup, coding standards, testing requirements, and the pull request process.

---

## Development Setup

### Requirements

- Python 3.11 or higher
- Git
- An `ANTHROPIC_API_KEY` for integration tests

### Initial Setup

```bash
# Clone the repository
git clone https://github.com/your-org/s-agent.git
cd s-agent

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install all dependencies including dev extras
pip install -r agents/requirements.txt
pip install pre-commit pytest pytest-asyncio pytest-cov

# Install pre-commit hooks (required before first commit)
pre-commit install

# Verify your setup
pytest tests/ -m unit -v
```

### Pre-Commit Hooks

Pre-commit runs the following checks on every commit:

- `ruff` — linting and import sorting
- `mypy` — static type checking
- `bandit` — security scanning for common Python vulnerabilities
- `detect-secrets` — prevents accidental credential commits
- `pytest tests/ -m unit` — fast unit test gate

If a hook fails, fix the issue and re-stage your files before committing. Do not use `--no-verify`.

---

## Adding a New Agent

Every new agent requires all of the following — PRs missing any item will be rejected:

- [ ] `agents/{agent-name}/agent.py` — agent class inheriting from `BaseAgent`, real tool calls (no stubs)
- [ ] `agents/{agent-name}/agent.md` — plain-English spec: purpose, I/O schema, tools, blocking conditions
- [ ] `agents/{agent-name}/prompts/system_prompt.md` — system prompt loaded at runtime
- [ ] `agents/{agent-name}/schema.json` — JSON Schema 2020-12 for input/output contracts
- [ ] `agents/{agent-name}/tools.py` — tool implementations with `is_error: True` on all error paths
- [ ] `tests/agent-tests/test_{agent_name}.py` — unit tests with mocked Anthropic API (see existing tests for the pattern)
- [ ] Register the agent in `config/agents.yaml` with model, max_iterations, and thresholds
- [ ] Add a `delegate_to_{agent_name}` tool to the orchestrator in `agents/orchestrator-agent/agent.py`
- [ ] Update `CLAUDE.md` with the new agent's section

### What NOT to Change When Adding an Agent

- Do not modify `BlockingGate` logic in `agents/_shared/gates.py`
- Do not change the Halcon emission contract in `monitoring/agent_observability.py`
- Do not alter the audit chain in `security/audit/audit_logger.py`
- Do not weaken the path traversal check in the security agent's `read_file` tool

---

## Code Standards

### Type Hints

All functions and methods must have complete type annotations. Use `from __future__ import annotations` for forward references.

```python
# Correct
async def validate_records(run_id: str, object_type: str) -> ValidationResult:
    ...

# Incorrect — missing return type
async def validate_records(run_id, object_type):
    ...
```

### Pydantic v2

All data contracts use Pydantic v2 models. Do not use v1-style validators (`@validator`). Use `@field_validator` and `model_validator`.

```python
from pydantic import BaseModel, field_validator

class MigrationInput(BaseModel):
    run_id: str
    object_type: str

    @field_validator("run_id")
    @classmethod
    def run_id_must_be_prefixed(cls, v: str) -> str:
        if not v.startswith("run-"):
            raise ValueError("run_id must start with 'run-'")
        return v
```

### No Stubs, No Random Data

Never use `random`, `uuid4()` as a stand-in for real data, or hardcoded fixture responses in production code paths. All validation must call real APIs or use data sourced from the migration control-plane.

### Default to FAILED

Validation results must prove data is good, not assume it. When a quality report cannot be parsed, default to `grade = "F"` and `overall_score = 0.0`.

### Error Handling

All tool functions that can fail must return a dict with an `"error"` key on failure. The caller is responsible for checking for this key and setting `is_error: True` on the tool result block.

```python
# Correct
try:
    result = await api_client.get(url)
    return result.json()
except httpx.HTTPError as exc:
    return {"error": f"API request failed: {exc}"}

# Incorrect — swallowing the error
except Exception:
    return {}
```

### SOQL Safety

Never construct SOQL queries by interpolating user-supplied or record-sourced strings. Use only parameterised queries and the allowlist in `security/policies.yaml`.

---

## Testing Requirements

| Test type | Required for | Location | Network? |
|---|---|---|---|
| Unit | All tools and domain logic | `tests/unit/` | No |
| Agent | All agent loops | `tests/agent-tests/` | Mocked |
| Integration | All external API clients | `tests/integration-tests/` | Yes |
| Failure scenario | Circuit breakers, malformed inputs | `tests/failure-scenarios/` | Mocked |

### Unit Tests

Every tool function must have at least one unit test. Unit tests:

- Must not make real network calls
- Must mock the Anthropic API using the pattern in `tests/agent-tests/conftest.py`
- Must be marked with `@pytest.mark.unit`
- Must complete within 5 seconds

### Integration Tests

Agent loop integration tests:

- Must be marked with `@pytest.mark.integration`
- May make real Anthropic API calls (require `ANTHROPIC_API_KEY`)
- Must use the test Salesforce sandbox, not production
- Must clean up any state they create

### Failure Scenarios

For any new agent action that is destructive or irreversible (pause, cancel, retry), add a test in `tests/failure-scenarios/` that verifies the action is blocked when the appropriate gate has not passed.

### Running the Test Suite

```bash
# All tests
pytest tests/ -v --timeout=60

# Unit tests only (fast, no network)
pytest tests/ -m unit -v

# Agent tests
pytest tests/agent-tests/ -v

# Integration tests
pytest tests/integration-tests/ -v -m integration

# A single test file
pytest tests/agent-tests/test_validation_agent.py -v
```

---

## Security Requirements

- Never commit secrets, API keys, tokens, or credentials. The `detect-secrets` pre-commit hook enforces this.
- Never include real PII, PHI, or CUI in test fixtures. Use synthetic data only.
- All SOQL in tests must use SELECT statements only. No DML.
- Path arguments to `read_file` must be relative paths under `PROJECT_ROOT`. Never use `..` or absolute paths.
- If a security tool reports a CRITICAL or HIGH finding, it must be resolved before the PR can be merged.

---

## Pull Request Checklist

Before opening a PR, confirm:

- [ ] All agent files present and complete (if adding a new agent)
- [ ] Unit tests added or updated for all changed tools
- [ ] `config/agents.yaml` updated if changing model, thresholds, or iterations
- [ ] `CLAUDE.md` updated if changing agent behavior or adding a new agent
- [ ] No hardcoded credentials or tokens (`git diff --staged | grep -iE 'secret|token|key|password'`)
- [ ] No real PII in test fixtures
- [ ] Type hints complete on all new functions
- [ ] Pydantic v2 models used for all new data contracts
- [ ] Error paths return `{"error": "..."}` and set `is_error: True`
- [ ] BlockingGate logic not weakened or bypassed
- [ ] `pytest tests/ -m unit` passes locally

See `.github/PULL_REQUEST_TEMPLATE.md` for the full PR description format.

---

## Questions

Open a GitHub Discussion or ping `@zuclubit/ai-platform` in Slack.
