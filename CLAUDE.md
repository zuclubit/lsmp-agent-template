# CLAUDE.md — Migration Platform Agent System

**Last Updated:** 2026-03-16
**System:** Legacy-to-Salesforce Migration Platform (LSMP)
**Repo Root:** `/Users/oscarvalois/Documents/Github/s-agent/`

---

## Project Overview

This repository implements an **enterprise AI agent system** that autonomously manages
the migration of 4.2M customer records, 18M case records, and 1.1M opportunity records
from Oracle Siebel 8.1, SAP CRM 7.0, and PostgreSQL into Salesforce Government Cloud+.

The system processes **federal government data (CUI/PII/PHI)** under FedRAMP High and
FISMA Moderate constraints. Zero data loss is a hard requirement (RPO = 0).

The agents are **NOT chatbots**. They are autonomous operators that invoke real API
calls against Salesforce Bulk API 2.0, an internal migration control-plane, and
HashiCorp Vault. A bad decision by an agent can pause a production migration or trigger
a P1 incident.

---

## Essential Commands

```bash
# Run all agent tests
cd /Users/oscarvalois/Documents/Github/s-agent
pytest tests/ -v --timeout=60

# Run only fast unit tests (no I/O, no network)
pytest tests/ -m unit -v

# Run agent-specific tests
pytest tests/agent-tests/ -v

# Run integration tests (requires ANTHROPIC_API_KEY)
pytest tests/integration-tests/ -v -m integration

# Run a single agent manually (requires ANTHROPIC_API_KEY)
python -m agents.orchestrator.multi_agent_orchestrator \
  "Check all active migration runs and report anomalies."

# Run the migration agent directly
python -m agents."migration-agent".agent \
  "Run run-abc-123 has a 20% error rate on Account records. Investigate."

# Run the data validation agent
python -m agents."data-validation-agent".agent demo-run-001 Account Contact

# Install dependencies
pip install -r agents/requirements.txt

# Check Halcon session metrics
cat .halcon/retrospectives/sessions.jsonl | python -m json.tool
```

---

## Architecture Overview

```
agents/
├── orchestrator/            # Supervisor agent — routes tasks to specialists
│   └── multi_agent_orchestrator.py   # MultiAgentOrchestrator class
├── migration-agent/         # Controls migration run lifecycle
│   ├── agent.py             # MigrationAgent class
│   └── tools.py             # check_migration_status, pause_migration, etc.
├── data-validation-agent/   # Data quality gating
│   ├── agent.py             # DataValidationAgent class
│   └── tools.py             # validate_record_counts, check_field_completeness, etc.
├── security-audit-agent/    # OWASP scanning, secrets detection, CVE checks
│   └── agent.py             # SecurityAuditAgent + tool implementations
└── documentation-agent/     # Auto-generates runbooks, field maps, reports

security/
├── policies/                # security_policy.md, data_classification.md
├── rbac/                    # OPA roles.yaml, rbac_config.py
├── audit/                   # audit_logger.py (HMAC-chained, Splunk sink)
├── secrets/                 # secrets_manager.py (Vault integration)
└── encryption/              # encryption_service.py (AES-256-GCM)

monitoring/
└── agent_observability.py   # Prometheus metrics, OTel traces, Halcon emitter

tests/
├── agent-tests/             # Unit tests for each agent (mocked Claude API)
├── skill-tests/             # Tests for individual tool/skill implementations
├── integration-tests/       # Halcon integration, full pipeline tests
└── failure-scenarios/       # Adversarial and tool failure tests
```

**Clean Architecture layers:** Domain → Application → Infrastructure (adapters).
Dependencies flow inward only. Infrastructure adapters implement domain ports.

---

## Agent System

### OrchestratorAgent (`agents/orchestrator/multi_agent_orchestrator.py`)

**Model:** `claude-opus-4-5` (env: `ANTHROPIC_MODEL`)
**Max tokens:** 4096 (env: `ORCHESTRATOR_MAX_TOKENS`)
**Pattern:** Supervisor — decomposes tasks, delegates to specialists, synthesises results.

**Tools available to the orchestrator:**
- `delegate_to_migration_agent` — run lifecycle control
- `delegate_to_validation_agent` — data quality checks
- `delegate_to_documentation_agent` — report/doc generation
- `delegate_to_security_agent` — security scanning
- `run_agents_in_parallel` — concurrent execution
- `synthesise_results` — merge multiple agent outputs

**When to invoke:** Any task that requires coordination of 2+ specialists, or when
you don't know which specialist to use.

**Gate logic:** `_do_synthesise()` returns `BLOCKED` if any agent reports an error,
grade D/F, or risk level CRITICAL/HIGH. This is the critical safety gate.

---

### MigrationAgent (`agents/migration-agent/agent.py`)

**Model:** `claude-opus-4-5`
**Max iterations:** 20 (env: `AGENT_MAX_ITERATIONS`)

**Tools:**
| Tool | Endpoint | Notes |
|------|----------|-------|
| `check_migration_status` | `GET /api/v1/migrations/runs/{run_id}` | Safe read |
| `pause_migration` | `POST /api/v1/migrations/runs/{run_id}/pause` | **Destructive** |
| `resume_migration` | `POST /api/v1/migrations/runs/{run_id}/resume` | |
| `cancel_migration` | `POST /api/v1/migrations/runs/{run_id}/cancel` | **Irreversible** |
| `get_error_report` | `GET /api/v1/migrations/errors` | |
| `retry_failed_records` | `POST /api/v1/migrations/retry` | |
| `scale_batch_size` | `POST /api/v1/migrations/runs/{run_id}/batch-size` | |
| `get_salesforce_limits` | `GET /api/v1/integrations/salesforce/limits` | |
| `get_system_health` | `GET /api/v1/health` | |
| `create_incident` | PagerDuty/ServiceNow API | P1–P4 severity |

**When to invoke:** Error rate investigation, run lifecycle control, SF API limit
management, batch size tuning.

---

### DataValidationAgent (`agents/data-validation-agent/agent.py`)

**Model:** `claude-opus-4-5`
**Max iterations:** 15 (env: `VALIDATION_AGENT_MAX_ITERATIONS`)

**Tools:** `validate_record_counts`, `check_field_completeness`, `detect_anomalies`,
`compare_sample_records`, `check_referential_integrity`, `check_duplicate_records`,
`validate_data_types`, `generate_report`, `run_custom_soql_check`, `get_field_metadata`

**Output:** `ValidationResult` with `overall_score` (0.0–1.0), `grade` (A/B/C/D/F).
**Gate:** Grade D or F → orchestrator must NOT proceed to execution.

**KNOWN BUG (FIXED):** The original code defaulted `overall_score = 0.95` and
`grade = "A"` even when no `quality_report` was parsed. This masked validation failures.
The fix: default to `overall_score = 0.0`, `grade = "F"` when `quality_report` is None.

---

### SecurityAuditAgent (`agents/security-audit-agent/agent.py`)

**Model:** `claude-opus-4-5`

**Tools:** `scan_file_for_secrets`, `check_dependency_vulnerabilities`,
`audit_authentication_code`, `check_sql_injection`, `audit_salesforce_permissions`,
`check_pii_handling`, `check_tls_configuration`, `read_file`, `generate_security_report`

**Gate:** `pass_security_gate` is `True` only when `critical_count == 0 AND high_count == 0`.
**Path restriction:** `read_file` resolves paths against `PROJECT_ROOT` env var to
prevent traversal. Never pass user-supplied paths directly.

---

### DocumentationAgent (`agents/documentation-agent/agent.py`)

**Model:** `claude-opus-4-5`
**When to invoke:** Post-migration report generation, runbook updates, field mapping
tables, changelog creation.

---

## CRITICAL RULES FOR AI AGENTS

1. **NEVER call `cancel_migration` without `confirm: true`** and explicit human approval
   recorded in the orchestration event log.

2. **NEVER proceed to execution if the validation gate is BLOCKED.** Check the
   `OrchestrationResult.final_answer` for "BLOCKED" before continuing.

3. **NEVER pass raw user input to SOQL queries.** All `run_custom_soql_check` calls
   must use only SELECT statements. The allowed statement list in `policies.yaml`
   is enforced at the dispatch layer.

4. **NEVER use `read_file` with absolute paths or `..` traversal.** Use relative
   paths from project root only. The tool validates against `PROJECT_ROOT`.

5. **NEVER hardcode API keys or tokens.** The `INTERNAL_SERVICE_TOKEN` in `tools.py`
   is injected from Vault Agent via env var at runtime.

6. **NEVER ignore tool errors.** If a tool returns `{"error": "..."}`, treat it as
   a failure and escalate. The previous system silently passed on tool errors.

7. **NEVER emit Halcon metrics with fabricated values.** `convergence_efficiency`
   and `final_utility` must be computed from actual agent run data, not hardcoded.

8. **Token budget:** Max 200K tokens per orchestration session. The observability
   module tracks this. If you approach 150K, summarise context and drop early messages.

9. **Rate limiting:** Max 10 agent invocations per orchestration run. Parallel
   execution counts as multiple invocations.

10. **Human-in-the-loop gate:** Any security finding rated CRITICAL, or any migration
    action on runs with > 1M records, requires explicit human approval before execution.

---

## Working with Halcon

Halcon is the retrospective metrics system. After every agent run, metrics are
appended to `.halcon/retrospectives/sessions.jsonl`.

**Required schema:**
```json
{
  "timestamp_utc": "ISO-8601",
  "convergence_efficiency": 0.0–1.0,
  "final_utility": 0.0–1.0,
  "peak_utility": 0.0–1.0,
  "decision_density": 0.0–1.0,
  "adaptation_utilization": 0.0–1.0,
  "wasted_rounds": 0,
  "structural_instability_score": 0.0,
  "dominant_failure_mode": null | "string",
  "evidence_trajectory": "monotonic" | "non-monotonic" | "flat",
  "inferred_problem_class": "string"
}
```

**Calculations:**
- `convergence_efficiency = 1 - (wasted_rounds / total_iterations)`
- `final_utility = quality_score * (1 - error_rate)`
- `peak_utility = max(utility_per_iteration)`
- `decision_density = tool_calls / total_iterations`

Use `HalconEmitter` from `monitoring/agent_observability.py` — do not write to
sessions.jsonl directly.

---

## Context Servers

No MCP context servers are currently configured. Context is injected via:

1. **Environment variables** — `ANTHROPIC_API_KEY`, `MIGRATION_API_BASE_URL`,
   `INTERNAL_SERVICE_TOKEN`, `PROJECT_ROOT`, `ENVIRONMENT`

2. **System prompts** — loaded from `agents/{agent-name}/prompts/system_prompt.md`

3. **Tool results** — agents query the migration API at `http://localhost:8000/api/v1`
   in development, or the EKS service at the `MIGRATION_API_BASE_URL` env var in prod.

4. **Vault Agent sidecar** — injects secrets as env vars at pod startup in Kubernetes.

---

## Security Boundaries

### What agents CAN do:
- Read migration run status, error reports, SF limits (read-only tools)
- Pause/resume migrations (with audit trail)
- Retry failed records (bounded to max 5000 records per call)
- Run SOQL SELECT queries with a LIMIT clause
- Scan files under `PROJECT_ROOT` for security issues
- Create incidents (PagerDuty/ServiceNow)
- Write documentation/reports to `docs/` directory

### What agents CANNOT do:
- Execute SOQL DELETE, UPDATE, INSERT, MERGE, DROP, GRANT, CREATE
- Access files outside `PROJECT_ROOT`
- Cancel migrations without `confirm: true`
- Access production secrets directly (Vault Agent handles injection)
- Make outbound HTTP calls to arbitrary URLs (egress is restricted)
- Modify RBAC roles or Vault policies
- Access other agents' conversation histories

### Prompt injection protection:
All tool results are scanned for injection patterns before being fed back to the model.
The `PromptInjectionScanner` in `monitoring/agent_observability.py` strips patterns
matching `IGNORE PREVIOUS INSTRUCTIONS`, `[SYSTEM]`, `<|im_start|>`, etc.

---

## Common Workflows

### Post-migration validation pipeline:
```python
from agents.orchestrator.multi_agent_orchestrator import run_post_migration_pipeline

result = await run_post_migration_pipeline(
    run_id="run-abc-123",
    object_types=["Account", "Contact", "Opportunity"],
)
# result.final_answer contains BLOCKED or APPROVED with findings
```

### Pre-deployment security check:
```python
from agents.orchestrator.multi_agent_orchestrator import run_security_preflight

result = await run_security_preflight(
    directories=["integrations/", "agents/migration-agent/"],
)
# result.agent_results["security"]["pass_gate"] must be True before deploy
```

### Investigating a high error rate:
```python
from agents.migration_agent.agent import MigrationAgent

agent = MigrationAgent()
result = await agent.run(
    "Run run-abc-123 has 18% error rate on Contact records. "
    "Identify root cause and take corrective action.",
    context={"run_id": "run-abc-123", "error_rate": 0.18},
)
```

---

## Known Issues and Workarounds

### Issue 1: ValidationAgent returns grade "A" on tool failure
**Root cause:** `overall_score` defaulted to `0.95`, `grade = "A"` when no JSON
report was extracted from the model's response.
**Fix:** Default to `grade = "F"`, `overall_score = 0.0` when `quality_report is None`.
**Status:** Fixed in `data-validation-agent/agent.py` — tests in `test_validation_agent.py`.

### Issue 2: SOQL injection via migration data
**Root cause:** Legacy records containing SOQL keywords were passed unvalidated to
`run_custom_soql_check`.
**Fix:** `policies.yaml` enforces allowlist at dispatch layer. Blocked keywords:
DELETE, UPDATE, INSERT, DROP, CREATE, MERGE, GRANT.
**Status:** Enforced. See `security/policies.yaml`.

### Issue 3: Halcon metrics hardcoded to 0.95
**Root cause:** Previous `HalconEmitter` always wrote `final_utility: 0.95` regardless
of actual run outcome.
**Fix:** `HalconEmitter.emit()` now computes metrics from `AgentRunContext`.
**Status:** Fixed in `monitoring/agent_observability.py`.

### Issue 4: Tool errors silently ignored
**Root cause:** Agent loops caught `Exception` and returned `{"error": str(exc)}`
which was passed to the model without marking `is_error: True`.
**Fix:** All `{"error": ...}` results now set `is_error: True` on the tool_result block,
and the orchestrator's `_do_synthesise()` treats them as blocking issues.
**Status:** Fixed throughout all agents.

### Issue 5: path traversal in read_file
**Root cause:** `_read_file_tool` joined `PROJECT_ROOT` with an arbitrary `file_path`
parameter without validating that the resolved path was still under `PROJECT_ROOT`.
**Fix:** Canonical path check added. Returns `ToolError(ACCESS_DENIED)` if outside root.
**Status:** Fixed in security agent.

### Issue 6: Token exhaustion on large validation runs
**Workaround:** Limit `sample_size` to 50 records for `compare_sample_records` in
agent calls. The full 500-record comparison is reserved for scheduled batch jobs.

---

*This file is read by Claude Code at the start of every session. Keep it accurate.*
*When making changes to agents, update the relevant sections here first.*
