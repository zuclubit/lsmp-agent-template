# Agent Issues Analysis — Legacy-to-Salesforce Migration Platform

**Document version:** 1.0
**Analysis date:** 2026-03-16
**Analyst:** Automated codebase audit
**Scope:** `/agents/`, `/agents/orchestrator/`, `.claude/`
**Classification:** INTERNAL — Engineering Review Required

---

## Executive Summary

A systematic review of the multi-agent migration platform has identified **15 confirmed issues** spanning security, correctness, observability, and architectural soundness. Five issues are classified as **CRITICAL** and require immediate remediation before any production deployment. The most serious finding is that the Data Validation Agent generates entirely fabricated quality scores using `random.randint()`, meaning it is structurally incapable of detecting real data defects even when invoked. Combined with a complete absence of inter-agent handoff enforcement, a production migration run could proceed straight through a security gate that never actually ran.

---

## Issue Catalogue

---

### ISSUE-001 — Data Validation Agent Returns Stub / Fabricated Data

| Field | Value |
|-------|-------|
| **Severity** | CRITICAL |
| **Component** | `agents/data-validation-agent/tools.py` |
| **Category** | Correctness / Data Integrity |

**Description**

Every tool implementation inside `agents/data-validation-agent/tools.py` returns randomly generated numbers rather than querying any real data source. The module imports `random` at line 24 and uses `random.randint()` and `random.uniform()` throughout. The agent therefore cannot detect actual data quality problems; it will report a passing score even if zero records were migrated.

**Evidence**

```
agents/data-validation-agent/tools.py:297
    source_count = random.randint(10000, 50000)
    sf_count = source_count - random.randint(0, int(source_count * 0.02))

agents/data-validation-agent/tools.py:331
    rate = round(random.uniform(0.70, 1.0), 4)

agents/data-validation-agent/tools.py:358
    values = [random.gauss(50000, 15000) for _ in range(1000)]

agents/data-validation-agent/tools.py:456
    dup_groups = random.randint(0, 5)

agents/data-validation-agent/tools.py:516
    object_scores[obj] = round(random.uniform(0.88, 0.99), 3)

agents/data-validation-agent/tools.py:549
    actual_count = random.randint(0, 100)
```

The comment on line 297 explicitly states `# In production: query the migration database and Salesforce SOQL` — confirming this is known stub code that was never replaced.

**Impact**

- Post-migration validation is completely non-functional.
- Governance reports signed off using this agent are based on fabricated data.
- The overall quality score (`0.88`–`0.99`) will almost always trigger a PASS gate, masking real migrations that may have 30%+ record loss.
- Regulatory and audit exposure if quality reports are retained as evidence of due diligence.

**Recommended Fix**

Replace every stub implementation with real API calls against the migration database and Salesforce SOQL endpoint. Wire `MIGRATION_API_BASE` (already defined at line 35) to actual REST calls using the `httpx` client already imported. Add integration tests that assert real HTTP calls are made, preventing re-introduction of stubs.

---

### ISSUE-002 — All Tools Lack Input Validation (No Pydantic)

| Field | Value |
|-------|-------|
| **Severity** | HIGH |
| **Component** | `agents/*/tools.py` (all agents) |
| **Category** | Robustness / Security |

**Description**

Every `dispatch_tool()` function across all agents passes the raw `tool_input` dict directly to the underlying implementation using `**tool_input` (Python dict unpacking). There is no Pydantic model, no type coercion, and no field validation between the JSON that Claude returns and the function that executes.

**Evidence**

```
agents/data-validation-agent/tools.py:602-607
    async def dispatch_tool(tool_name: str, tool_input: Dict[str, Any]) -> Any:
        impl = TOOL_IMPLEMENTATIONS.get(tool_name)
        if not impl:
            raise ValueError(f"Unknown validation tool: {tool_name!r}")
        logger.info("Validation tool dispatched: %s", tool_name)
        return await impl(**tool_input)   # ← raw dict unpacking, no validation

agents/migration-agent/tools.py:557-563
    async def dispatch_tool(tool_name: str, tool_input: Dict[str, Any]) -> Any:
        implementation = TOOL_IMPLEMENTATIONS.get(tool_name)
        ...
        result = await implementation(**tool_input)   # ← same pattern

agents/documentation-agent/agent.py:342
    result = await fn(**(block.input or {}))          # ← same pattern

agents/security-audit-agent/agent.py:539
    result = await fn(**(block.input or {})) if fn else ...  # ← same pattern
```

**Impact**

- A prompt-injected or malformed tool call can supply unexpected keyword arguments, causing `TypeError` exceptions that silently enter the `{"error": str(exc)}` path (see ISSUE-011), masking the underlying problem.
- Fields with required constraints defined in the JSON schema (e.g., `run_id`, `soql`) can be omitted without detection at the Python layer.
- No audit evidence of what was actually validated.

**Recommended Fix**

Introduce one Pydantic v2 model per tool in `agents/_shared/schemas.py`. Each `dispatch_tool()` should instantiate the appropriate model from `tool_input`, allowing Pydantic to raise a `ValidationError` with a structured message before any side-effecting code runs.

---

### ISSUE-003 — No Handoff Protocol Between Agents

| Field | Value |
|-------|-------|
| **Severity** | CRITICAL |
| **Component** | `agents/orchestrator/multi_agent_orchestrator.py` |
| **Category** | Architecture / Safety |

**Description**

The orchestrator delegates to agents and collects their results, but there is no blocking gate that prevents downstream agents from proceeding when an upstream agent reports a critical finding. The Security Agent can return `pass_security_gate: False` with `risk_level: CRITICAL`, and the Migration Agent will still be invoked for the same pipeline run.

**Evidence**

```
agents/orchestrator/multi_agent_orchestrator.py:406-418
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
            "is_error": isinstance(result, dict) and "error" in result,  # ← only checks for error key
        })
    messages.append({"role": "user", "content": tool_results})
```

The `_do_synthesise()` method at line 597 does detect `risk_level: CRITICAL` and appends to `issues`, but synthesis is only called if Claude explicitly invokes the `synthesise_results` tool. Claude may choose to skip it, and even when called, the synthesised `BLOCKED` status is advisory text — it does not raise an exception or prevent further delegation.

**Impact**

- A Critical security finding (e.g., hardcoded credentials detected) does not stop the migration from being started.
- Validation grade D/F does not prevent the documentation agent from writing a report asserting migration success.
- The security gate is purely advisory — there is no enforcement mechanism at the orchestrator layer.

**Recommended Fix**

Implement `AgentHandoff` as a structured Pydantic model with a `blocking: bool` field (see proposed `context_protocol.py`). After each agent invocation, the orchestrator must check `pass_security_gate` and `grade` before proceeding. If a blocking condition is met, raise a `HandoffBlockedException` rather than passing the result to the next agent.

---

### ISSUE-004 — SOQL Injection Risk in `run_custom_soql_check`

| Field | Value |
|-------|-------|
| **Severity** | CRITICAL |
| **Component** | `agents/data-validation-agent/tools.py:542` |
| **Category** | Security / OWASP A03:Injection |

**Description**

The `run_custom_soql_check()` tool accepts an arbitrary SOQL string from Claude and (per its stated production intent) would execute it against the live Salesforce org. The tool schema description says "Must be read-only" but there is no code enforcement of this constraint. DML operations (`DELETE`, `UPDATE`, `INSERT`, `MERGE`) are valid within Salesforce's Tooling API and certain SOQL extensions; a prompt-injected payload could cause mass data destruction.

**Evidence**

```
agents/data-validation-agent/tools.py:242-263  (tool schema)
    "name": "run_custom_soql_check",
    "description": "Execute a custom SOQL query ... Must be read-only.",
    "input_schema": {
        "properties": {
            "soql": {
                "type": "string",
                "description": "SOQL SELECT statement. Must be read-only.",
            },
            ...
        },
        "required": ["soql", "description"],
    }

agents/data-validation-agent/tools.py:542-561
    async def run_custom_soql_check(
        soql: str,
        description: str,
        expected_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute a custom SOQL quality check."""
        # In production: execute via SalesforceClient.query()
        actual_count = random.randint(0, 100)   # ← currently stubbed
        ...
```

There is no regex guard, no allowlist of permitted statement types, and no attempt to parse or validate the SOQL before execution. The comment confirms this will be connected to a live `SalesforceClient.query()` call in production.

**Impact**

- An adversarial prompt could inject `DELETE [SELECT Id FROM Account]` or `UPDATE Account SET IsDeleted=true WHERE ...` via Tooling API.
- Even without adversarial intent, Claude may hallucinate non-SELECT SOQL in an agentic context.
- Data destruction in the production Salesforce org with no undo path.

**Recommended Fix**

Before passing any SOQL to the client: (1) parse the statement with a lightweight SOQL parser or regex to confirm the root keyword is `SELECT`; (2) reject any statement containing `DELETE`, `UPDATE`, `INSERT`, `MERGE`, `UPSERT`, `CREATE`, `DROP` as root keywords; (3) impose a maximum result-set limit (`LIMIT 10000`) on all custom checks; (4) execute in a read-only connected app OAuth context scoped to `api` but not `full`.

---

### ISSUE-005 — Documentation Agent Writes Files Without Path Sanitization

| Field | Value |
|-------|-------|
| **Severity** | CRITICAL |
| **Component** | `agents/documentation-agent/agent.py:161-185` |
| **Category** | Security / Path Traversal |

**Description**

The `_tool_write_documentation()` function constructs a filesystem path by joining `PROJECT_ROOT` with the `file_path` argument passed directly from Claude's tool call output. There is no normalisation, no path containment check, and no allowlist of permitted write directories. An agent reasoning error — or a prompt injection — could write arbitrary files anywhere on the filesystem.

**Evidence**

```
agents/documentation-agent/agent.py:161-185
    async def _tool_write_documentation(
        file_path: str,                      # ← raw agent-supplied string
        content: str,
        mode: str = "create",
        section_header: Optional[str] = None,
    ) -> Dict[str, Any]:
        project_root = os.getenv("PROJECT_ROOT", "/Users/oscarvalois/Documents/Github/s-agent")
        full_path = os.path.join(project_root, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)  # ← creates directories anywhere
        ...
        with open(full_path, "w", encoding="utf-8") as fh:      # ← writes anywhere
            fh.write(content)
```

`os.path.join()` does **not** prevent path traversal. If `file_path` is `"../../.env"`, `full_path` will resolve to the parent of `PROJECT_ROOT` and overwrite the `.env` file containing `ANTHROPIC_API_KEY` and Salesforce credentials.

**Impact**

- Agent can overwrite `.env`, SSH keys, cron jobs, or Python source files.
- Path traversal to `/etc/cron.d/` enables persistent code execution on the host.
- Silent — the function returns `{"success": True}` regardless of what was written or where.

**Recommended Fix**

After constructing `full_path`, call `os.path.realpath(full_path)` and assert it starts with `os.path.realpath(project_root)`. If the assertion fails, raise `ValueError("Path traversal detected")` and log the attempted path. Additionally, maintain an explicit allowlist of subdirectories that documentation may be written to (e.g., `docs/`, `reports/`).

---

### ISSUE-006 — Default Grade A (Score 0.95) Hardcoded When `quality_report` Is None

| Field | Value |
|-------|-------|
| **Severity** | HIGH |
| **Component** | `agents/data-validation-agent/agent.py:212-216` |
| **Category** | Correctness / Silent False Positive |

**Description**

When the Data Validation Agent completes its loop but `quality_report` is `None` — which happens whenever Claude does not emit a JSON block containing `overall_quality_score` — the agent silently defaults to `overall_score = 0.95` and `grade = "A"`. This means a failed or partial run is indistinguishable from a perfect validation.

**Evidence**

```
agents/data-validation-agent/agent.py:212-216
    overall_score = 0.95
    grade = "A"
    if quality_report:
        overall_score = quality_report.get("overall_quality_score", 0.95)
        grade = quality_report.get("grade", "A")
```

The fallback inside the `if quality_report:` block also defaults to `0.95` and `"A"`, meaning a malformed report (missing the `overall_quality_score` key) is also silently upgraded to an A.

**Impact**

- Any invocation where Claude does not produce the expected JSON block — network error, max_tokens truncation, unexpected agent reasoning path — returns a synthetic A grade.
- Downstream orchestrator logic that gates migrations on `grade not in ("D", "F")` will always pass.
- No observable difference between a successful validation and a completely failed one.

**Recommended Fix**

Default `overall_score` and `grade` to sentinel values indicating failure, not success:
```python
overall_score = 0.0
grade = "F"
if quality_report:
    overall_score = quality_report.get("overall_quality_score", 0.0)
    raw_grade = quality_report.get("grade")
    if raw_grade not in ("A", "B", "C", "D", "F"):
        grade = "F"  # reject unknown grades
    else:
        grade = raw_grade
```
Additionally, when `quality_report is None`, set `error = "No quality report produced"` and log a warning.

---

### ISSUE-007 — Deprecated Model `claude-opus-4-5` Used Across All Agents

| Field | Value |
|-------|-------|
| **Severity** | HIGH |
| **Component** | All agent `agent.py` files, `multi_agent_orchestrator.py` |
| **Category** | Operational / Maintainability |

**Description**

The environment variable default `ANTHROPIC_MODEL` is set to `"claude-opus-4-5"` in every agent file and in the orchestrator. This model is deprecated. Current best practice is `claude-sonnet-4-6`.

**Evidence**

```
agents/data-validation-agent/agent.py:42
    MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")

agents/migration-agent/agent.py:51
    MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")

agents/documentation-agent/agent.py:43
    MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")

agents/security-audit-agent/agent.py:47
    MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")

agents/orchestrator/multi_agent_orchestrator.py:51
    MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")
```

**Impact**

- Calls may fail or be re-routed by Anthropic's API once the model is decommissioned.
- No single config point — the model string is duplicated in 5 files, making consistent updates error-prone.
- The orchestrator and its sub-agents could end up on different models if `ANTHROPIC_MODEL` is set in the environment, since all agents share the same variable name.

**Recommended Fix**

Centralise the model constant in `agents/_shared/base_agent.py`:
```python
CURRENT_MODEL = "claude-sonnet-4-6"
```
All agents should import from this module rather than each declaring their own fallback. Per-agent model overrides should use agent-specific environment variables (e.g., `MIGRATION_AGENT_MODEL`).

---

### ISSUE-008 — `.claude/settings.local.json` Allows `chmod:*` — Overly Permissive

| Field | Value |
|-------|-------|
| **Severity** | HIGH |
| **Component** | `.claude/settings.local.json` |
| **Category** | Security / Least Privilege |

**Description**

The Claude Code local settings file grants blanket permission for `chmod` on any path. This means any Claude Code session can modify file permissions on secrets files, credentials, or the `.env` file without triggering a permission prompt.

**Evidence**

```
.claude/settings.local.json:3-4
    "allow": [
      "Bash(chmod:*)",
      "Bash(find:*)"
    ]
```

**Impact**

- Claude Code can `chmod 777 .env` or `chmod +x` any file, including newly created scripts that could then be executed.
- An agent session that has been compromised via prompt injection has unrestricted ability to alter filesystem permissions.
- Combined with ISSUE-005 (path traversal in docs agent), an attacker could write a malicious script and then `chmod +x` it.

**Recommended Fix**

Remove `Bash(chmod:*)` entirely or restrict it to specific non-sensitive paths:
```json
{
  "permissions": {
    "allow": [
      "Bash(find:./agents/*)",
      "Bash(find:./tests/*)"
    ],
    "deny": [
      "Bash(chmod:*)",
      "Bash(rm:*)",
      "Bash(curl:*)"
    ]
  }
}
```
Never allow unrestricted `chmod` in a settings file committed to version control.

---

### ISSUE-009 — No Rate Limiting on Tool Invocations

| Field | Value |
|-------|-------|
| **Severity** | HIGH |
| **Component** | All agent `dispatch_tool()` functions |
| **Category** | Operational / Cost / Abuse Prevention |

**Description**

There is no rate limiting, concurrency ceiling, or per-session invocation budget on any tool call across any agent. The `max_iterations` parameter bounds how many times Claude calls the API, but within each iteration Claude can request arbitrarily many parallel tool calls (via `tool_use` blocks), all of which are dispatched concurrently via `asyncio.gather()`.

**Evidence**

```
agents/data-validation-agent/agent.py:241-245
    tasks = [
        asyncio.create_task(self._run_tool(block))
        for block in tool_use_blocks          # ← no ceiling on concurrent tasks
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

agents/migration-agent/agent.py:317-321
    tasks = [
        asyncio.create_task(self._execute_single_tool(block))
        for block in tool_use_blocks          # ← same pattern, no limit
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
```

**Impact**

- A single agent invocation can generate hundreds of concurrent Salesforce API calls in a loop, exhausting the daily API limit for all tenants.
- Infinite retry loops can occur if a tool consistently fails and Claude interprets the error as something to retry.
- Runaway cost: each Anthropic API call in a tight loop accumulates token charges.

**Recommended Fix**

Implement a `asyncio.Semaphore` with a configurable cap (e.g., `MAX_CONCURRENT_TOOLS = 5`) in each dispatch function. Add a per-session invocation counter and raise `ToolBudgetExceededException` when the budget is spent. Instrument each tool call with a Salesforce API call counter linked to a daily budget alarm.

---

### ISSUE-010 — Agents Run Independently With No Shared State

| Field | Value |
|-------|-------|
| **Severity** | HIGH |
| **Component** | All agents |
| **Category** | Architecture / Consistency |

**Description**

Each agent instantiates its own `anthropic.AsyncAnthropic()` client and maintains entirely separate conversation history. There is no shared state store, no distributed context, and no mechanism to prevent two agents from making contradictory decisions about the same migration run simultaneously.

**Evidence**

```
agents/data-validation-agent/agent.py:122-128
    self._client = anthropic.AsyncAnthropic(...)
    self._system_prompt = _load_system_prompt()

agents/migration-agent/agent.py:138-144
    self._client = anthropic.AsyncAnthropic(...)
    self._system_prompt = _load_system_prompt()

agents/orchestrator/multi_agent_orchestrator.py:328-333
    self._agents: Dict[AgentName, Any] = {
        AgentName.MIGRATION: MigrationAgent(api_key=_agent_key),
        AgentName.VALIDATION: DataValidationAgent(api_key=_agent_key),
        ...
    }
```

When `run_agents_in_parallel` is invoked (orchestrator line 474), both the Migration Agent and Validation Agent run concurrently against the same `run_id` with no mutual exclusion. The Migration Agent may pause the run while the Validation Agent is mid-validation, producing an inconsistent snapshot.

**Impact**

- Race conditions on migration run state changes.
- Contradictory agent outputs: one agent reports the run as RUNNING while another reports it PAUSED.
- No way to correlate log lines from parallel agent invocations to a single user-visible operation.

**Recommended Fix**

Introduce a `RequestContext` (see proposed `context_protocol.py`) with a `trace_id` propagated to every agent invocation. Use a distributed lock (Redis `SET NX`) on `run_id` before any state-mutating tool calls. Pass the context via `derive_child_context()` so that all agents in a pipeline share a common `job_id`.

---

### ISSUE-011 — Error Handling Returns `{"error": "..."}` — Agents Don't Check for Error Keys

| Field | Value |
|-------|-------|
| **Severity** | MEDIUM |
| **Component** | All agents |
| **Category** | Robustness / Error Propagation |

**Description**

When a tool raises an exception, all agents catch it and return `{"error": str(exc)}` as the tool result. This dict is then serialised to JSON and injected back into the conversation as a `tool_result` block. Claude is not explicitly instructed to treat this as a fatal error; it often treats the error message as informational and continues reasoning, masking the failure.

**Evidence**

```
agents/data-validation-agent/agent.py:249-254
    if isinstance(result, Exception):
        content = json.dumps({"error": str(result)})
        is_error = True

agents/migration-agent/agent.py:324-328
    if isinstance(result, Exception):
        content = json.dumps({"error": str(result)})
        is_error = True

agents/orchestrator/multi_agent_orchestrator.py:484
    return {"error": f"Unknown supervisor tool: {tool_name}"}, []
```

The orchestrator checks `"error" in result` (line 417) but only uses this to set `is_error` on the tool_result block, which Claude may or may not surface. There is no programmatic gate that halts the loop when a critical tool fails.

**Impact**

- A failed `pause_migration` call is silently absorbed; the migration continues running.
- A failed `create_incident` is never retried.
- Error information is present in the conversation but invisible to monitoring systems.

**Recommended Fix**

Define a `ToolError` dataclass with `code`, `message`, `retryable`, and `fatal` fields. Any tool returning a fatal error must propagate it to the agent loop as a raised exception, not as a dict. The agent loop must check for fatal errors before proceeding to the next iteration.

---

### ISSUE-012 — Max Tokens Truncation Causes Silent Loop Termination

| Field | Value |
|-------|-------|
| **Severity** | MEDIUM |
| **Component** | `agents/migration-agent/agent.py:265-268` |
| **Category** | Robustness / Observability |

**Description**

When Claude's response hits the `max_tokens` limit (`stop_reason == "max_tokens"`), the agent loop logs a warning and breaks. The final `final_answer` at that point may be an incomplete mid-sentence response. No error is set on `AgentResult`, no retry is attempted, and no continuation mechanism exists.

**Evidence**

```
agents/migration-agent/agent.py:265-268
    if response.stop_reason == "max_tokens":
        logger.warning("Claude hit max_tokens limit – stopping loop")
        break
```

`AgentResult.error` remains `None` (line 188 initialises it as `None`). A caller checking `result.error` will conclude the run succeeded.

**Impact**

- A report truncated mid-analysis returns to the orchestrator as a successful completion.
- The orchestrator synthesises a PASS based on a partial response.
- No alerting, no retry, no human escalation path.

**Recommended Fix**

When `stop_reason == "max_tokens"`, set `result.error = "Response truncated at max_tokens limit"` and `result.final_answer` to the partial text clearly marked as incomplete. Consider implementing a continuation strategy: append the truncated response to history and prompt "Please continue from where you left off." Cap continuations at 2 attempts.

---

### ISSUE-013 — No Halcon Integration (Zero Adaptation Utilization)

| Field | Value |
|-------|-------|
| **Severity** | MEDIUM |
| **Component** | `.halcon/retrospectives/sessions.jsonl` |
| **Category** | Observability / Learning |

**Description**

The Halcon session log contains a single entry dated 2026-03-16 with `adaptation_utilization: 0.0` and `decision_density: 0.0`. This indicates the Halcon adaptive learning system has been installed but never integrated with the agent runtime. No agent emits Halcon metrics, no feedback loop exists, and no retrospective learning is occurring.

**Evidence**

```
.halcon/retrospectives/sessions.jsonl:1
    {
      "adaptation_utilization": 0.0,
      "convergence_efficiency": 1.0,
      "decision_density": 0.0,
      "dominant_failure_mode": null,
      "evidence_trajectory": "monotonic",
      "final_utility": 0.5,
      "inferred_problem_class": "deterministic-linear",
      "peak_utility": 0.0,
      "structural_instability_score": 0.0,
      "timestamp_utc": "2026-03-16T23:23:01.375036+00:00",
      "wasted_rounds": 0
    }
```

`peak_utility: 0.0` alongside `final_utility: 0.5` suggests the session metrics were never populated with real agent run data. None of the agent `run()` methods write to the Halcon session file or call any Halcon SDK methods.

**Impact**

- Platform is not learning from past failures; recurring error patterns are not automatically surfaced.
- No adaptive batch sizing based on historical success rates.
- Halcon infrastructure cost (if any) is being incurred with zero return.

**Recommended Fix**

Implement `_track_halcon_metrics()` in `BaseAgent` (see proposed `base_agent.py`). After each agent run, write a Halcon session entry with `decision_density` = (tool_calls_made / iterations), `adaptation_utilization` = (actions_taken / possible_actions), and `final_utility` derived from the task outcome score. Wire the session writer to append to `sessions.jsonl` and, eventually, to the Halcon API.

---

### ISSUE-014 — Conflicting Batch Size Rules in System Prompt Cause Oscillation

| Field | Value |
|-------|-------|
| **Severity** | MEDIUM |
| **Component** | `agents/migration-agent/prompts/system_prompt.md` |
| **Category** | Correctness / Prompt Engineering |

**Description**

The Migration Agent system prompt contains two batch size rules that contradict each other under specific conditions, creating a potential oscillation loop where the agent alternately halves and increases the batch size on successive calls.

**Evidence**

```
agents/migration-agent/prompts/system_prompt.md:60-63
    - Standard batch size: 200 records
    - When SF API errors exceed 5% in a batch: halve the batch size (minimum 50)
    - When throughput is healthy and error rate < 1%: increase batch size by 25% (maximum 2000)
    - For Bulk API 2.0 jobs: minimum 2000 records per job (SF requirement)
```

The conflict: the standard REST API minimum after halving is 50, but the Bulk API 2.0 minimum is 2000. An agent managing a Bulk API 2.0 job with a 6% error rate will attempt to halve below 2000, violating the Bulk API constraint. On the next iteration, it reads the Bulk API minimum rule and restores to 2000, which exceeds the "5% threshold → halve" trigger again. This creates a two-state oscillation.

Additionally, the maximum `25% increase` rule does not specify a stabilization threshold: an agent in a healthy run will continuously grow the batch size until hitting 2000, potentially destabilising throughput.

**Impact**

- Infinite oscillation of `scale_batch_size` tool calls consuming orchestration loop iterations.
- If the agent hits `max_iterations` mid-oscillation, the run is left at an arbitrary batch size.
- Increased token consumption and API cost per oscillation cycle.

**Recommended Fix**

Add a hysteresis band: "Do not adjust batch size if the current batch size was changed in the last 3 batches." Separate REST API and Bulk API 2.0 decision trees explicitly. Cap batch size increases at one step per 10 successfully completed batches.

---

### ISSUE-015 — No Audit Trail — All Orchestration Events Are In-Memory Only

| Field | Value |
|-------|-------|
| **Severity** | MEDIUM |
| **Component** | `agents/orchestrator/multi_agent_orchestrator.py:335` |
| **Category** | Observability / Compliance |

**Description**

The orchestrator maintains its event log as an in-memory Python list (`self._event_log: List[OrchestrationEvent]`). Once the `MultiAgentOrchestrator` instance is garbage-collected, all orchestration events — including agent delegations, handoffs, escalations, and decisions — are permanently lost.

**Evidence**

```
agents/orchestrator/multi_agent_orchestrator.py:335
    self._event_log: List[OrchestrationEvent] = []

agents/orchestrator/multi_agent_orchestrator.py:362-363
    self._orchestration_id = str(uuid.uuid4())
    self._event_log = []              # ← reset on every run, no persistence

agents/orchestrator/multi_agent_orchestrator.py:441-449
    return OrchestrationResult(
        ...
        events=list(self._event_log),  # ← returned in result but never persisted
        ...
    )
```

The `OrchestrationResult` dataclass carries the events, but only for the duration of the calling code's scope. There is no write to a database, message queue, or log file.

**Impact**

- Post-incident forensics are impossible: no record of which agent was invoked, when, in what order, or what it decided.
- SOC 2 Type II audit requirements for change management and access logging cannot be met.
- Multi-run analytics (e.g., "which agent fails most often?") have no data source.

**Recommended Fix**

After each `_emit_event()` call, publish the event to a structured logging sink (e.g., structlog with a JSON handler to stdout, consumed by a log aggregator). Additionally persist the event to a `migration_platform.orchestration_events` PostgreSQL table. The `OrchestrationEvent` dataclass already has all required fields; add a `to_dict()` method and a repository class.

---

## Risk Matrix

| Issue | Severity | Likelihood of Triggering | Business Impact | Risk Score |
|-------|----------|--------------------------|-----------------|------------|
| ISSUE-001 Stub validation data | CRITICAL | Certain (every run) | Very High — false compliance reports | 25 |
| ISSUE-004 SOQL injection | CRITICAL | Possible (adversarial prompt) | Very High — data destruction | 20 |
| ISSUE-005 Path traversal in docs agent | CRITICAL | Possible (prompt injection) | Very High — filesystem compromise | 20 |
| ISSUE-003 No handoff protocol | CRITICAL | Certain (by design) | High — security gate bypassed | 20 |
| ISSUE-006 Hardcoded grade A default | CRITICAL | Certain (error paths) | High — silent false positives | 16 |
| ISSUE-008 chmod:* permissions | HIGH | Possible | High — privilege escalation | 12 |
| ISSUE-002 No input validation | HIGH | Likely | Medium — tool call failures | 12 |
| ISSUE-007 Deprecated model | HIGH | Certain (at deprecation) | Medium — service outage | 10 |
| ISSUE-009 No rate limiting | HIGH | Likely (busy periods) | Medium — API cost blowout | 10 |
| ISSUE-010 No shared state | HIGH | Certain (parallel runs) | Medium — race conditions | 10 |
| ISSUE-011 Error key not checked | MEDIUM | Likely | Medium — silent failures | 9 |
| ISSUE-012 Max tokens truncation | MEDIUM | Possible | Medium — incomplete decisions | 6 |
| ISSUE-014 Batch size oscillation | MEDIUM | Possible | Low — inefficiency | 6 |
| ISSUE-013 Halcon not integrated | MEDIUM | Certain | Low — missed learning | 4 |
| ISSUE-015 No persistent audit trail | MEDIUM | Certain | Medium — compliance gap | 9 |

*Risk Score = Severity × Likelihood (1=rare, 2=possible, 3=likely, 4=certain) × Impact (1=low, 2=medium, 3=high, 4=very high)*

---

## Priority Order for Remediation

### Sprint 1 — Block All Production Deployments (Do First)

1. **ISSUE-001** — Replace all stub/random data in `data-validation-agent/tools.py` with real API calls.
2. **ISSUE-004** — Add SOQL statement type validation to `run_custom_soql_check` before connecting to any real Salesforce org.
3. **ISSUE-005** — Add `os.path.realpath()` containment check to `_tool_write_documentation`.
4. **ISSUE-006** — Change defaults to fail-safe values (`grade = "F"`, `score = 0.0`).

### Sprint 2 — Architectural Safety (Complete Before Scale-Out)

5. **ISSUE-003** — Implement `AgentHandoff` with `blocking: bool` enforcement in the orchestrator.
6. **ISSUE-008** — Remove `Bash(chmod:*)` from `.claude/settings.local.json`.
7. **ISSUE-002** — Add Pydantic validation to all `dispatch_tool()` functions.
8. **ISSUE-011** — Introduce structured `ToolError` and halt loop on fatal errors.

### Sprint 3 — Operational Quality (Complete Within 30 Days)

9. **ISSUE-007** — Migrate all agents to `claude-sonnet-4-6` via a centralised `CURRENT_MODEL` constant.
10. **ISSUE-009** — Add `asyncio.Semaphore` rate limiting to all tool dispatchers.
11. **ISSUE-012** — Implement max_tokens recovery continuation logic.
12. **ISSUE-015** — Persist `OrchestrationEvent` to PostgreSQL and structured log stream.

### Sprint 4 — Observability and Learning (Complete Within 60 Days)

13. **ISSUE-010** — Introduce `RequestContext` with `trace_id` propagation and distributed locking.
14. **ISSUE-014** — Rewrite batch size guidance with hysteresis and separate REST/Bulk API decision trees.
15. **ISSUE-013** — Implement `_track_halcon_metrics()` in `BaseAgent` and wire to session log.

---

## Appendix: Files Reviewed

| File | Lines | Issues Found |
|------|-------|--------------|
| `agents/data-validation-agent/tools.py` | 607 | ISSUE-001, ISSUE-002, ISSUE-004 |
| `agents/data-validation-agent/agent.py` | 331 | ISSUE-002, ISSUE-006, ISSUE-007, ISSUE-012 |
| `agents/migration-agent/agent.py` | 434 | ISSUE-007, ISSUE-010, ISSUE-011, ISSUE-012 |
| `agents/migration-agent/tools.py` | 565 | ISSUE-002, ISSUE-009 |
| `agents/migration-agent/prompts/system_prompt.md` | 141 | ISSUE-014 |
| `agents/documentation-agent/agent.py` | 379 | ISSUE-002, ISSUE-005, ISSUE-007 |
| `agents/security-audit-agent/agent.py` | 595 | ISSUE-002, ISSUE-007 |
| `agents/orchestrator/multi_agent_orchestrator.py` | 786 | ISSUE-003, ISSUE-007, ISSUE-009, ISSUE-010, ISSUE-011, ISSUE-015 |
| `.claude/settings.local.json` | 8 | ISSUE-008 |
| `.halcon/retrospectives/sessions.jsonl` | 1 | ISSUE-013 |
