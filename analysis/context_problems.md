# Context Management Problems — Legacy-to-Salesforce Migration Platform

**Document version:** 1.0
**Analysis date:** 2026-03-16
**Analyst:** Automated codebase audit
**Scope:** All agent `run()` methods, context construction, conversation history management
**Classification:** INTERNAL — Engineering Review Required

---

## Executive Summary

Context management in the current multi-agent platform is ad hoc, inconsistent, and fragile. Each agent constructs its own context shape, conversation history is passed as unvalidated raw Python dicts, and there is no mechanism to track how much of the available context window has been consumed. In a long-running migration pipeline these problems compound: context accumulates unchecked until the API returns a `context_length_exceeded` error, agents make decisions based on context built from a different agent's conversation, and there is no way to trace a single user request across the four agents it touches.

---

## Problem Catalogue

---

### CTXP-001 — Conversation History Passed as Raw `List[Dict]` With No Validation

**Severity:** HIGH
**Component:** `agents/migration-agent/agent.py:152-176`

**Description**

The `MigrationAgent.run()` method accepts an optional `conversation_history: Optional[List[Dict[str, Any]]]` parameter and prepends it directly to the messages list without any structural validation. The Anthropic Messages API requires strict alternating `user`/`assistant` roles; a corrupted history (two consecutive `assistant` messages, a missing `role` key, a `content` field that is neither a string nor a list of content blocks) will cause a `400 Bad Request` from the API.

**Evidence**

```python
# agents/migration-agent/agent.py:152-176
async def run(
    self,
    task: str,
    context: Optional[Dict[str, Any]] = None,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
) -> AgentResult:
    ...
    messages: List[Dict[str, Any]] = list(conversation_history or [])   # ← no validation
    messages.append({"role": "user", "content": user_message})
```

There is no check that `conversation_history` items have `role` and `content` keys, that roles alternate correctly, or that content blocks conform to the Anthropic content block schema. If a caller passes a history from a failed previous run that ended on an `assistant` turn, the next call will have two consecutive `assistant` messages and return a 400.

**Impact**

- Corrupted histories cause cryptic `anthropic.BadRequestError` exceptions that surface as `result.error = "..."` with no indication that context structure is the root cause.
- Multi-turn agent workflows that accumulate history over multiple `run()` calls will silently degrade as history grows.
- No validation means any caller can inject arbitrary messages into the agent's reasoning context — a form of indirect prompt injection.

**Recommended Fix**

Validate history in `BaseAgent.__init__` or at the start of `run()`:
```python
def _validate_history(self, history: List[Dict]) -> List[Dict]:
    valid_roles = {"user", "assistant"}
    for i, msg in enumerate(history):
        if "role" not in msg or msg["role"] not in valid_roles:
            raise ValueError(f"History[{i}] has invalid role: {msg.get('role')!r}")
        if "content" not in msg:
            raise ValueError(f"History[{i}] missing content field")
    # Check alternating roles
    for i in range(1, len(history)):
        if history[i]["role"] == history[i-1]["role"]:
            raise ValueError(f"History[{i}] and [{i-1}] have the same role")
    return history
```

---

### CTXP-002 — `object_types` List Serialised to Comma-Separated String — Cannot Iterate Later

**Severity:** HIGH
**Component:** `agents/data-validation-agent/agent.py:159-160`

**Description**

When an `object_types` list is passed to `DataValidationAgent.run()`, it is immediately joined into a single string and stored in the context dict. Any downstream code that needs to iterate over individual object types must re-parse this string, which introduces fragility if object type names contain commas.

**Evidence**

```python
# agents/data-validation-agent/agent.py:157-165
ctx = dict(context or {})
if run_id:
    ctx["run_id"] = run_id
if object_types:
    ctx["object_types"] = ", ".join(object_types)    # ← List → String, information loss

user_message = task
if ctx:
    ctx_lines = "\n".join(f"  {k}: {v}" for k, v in ctx.items())
    user_message = f"{task}\n\nContext:\n{ctx_lines}"
```

The `ValidationResult` dataclass at line 78 stores `object_types: List[str]` as a proper list, but `ctx["object_types"]` — which is injected into the agent's prompt — is a string. If Claude reads the context and decides to iterate objects based on the context value (e.g., to call `check_field_completeness` once per object), it would need to re-split on `", "`, which fails for Salesforce API names that contain no commas but could fail silently with leading/trailing whitespace.

**Impact**

- Ambiguous prompt context: `"Account, Contact, Custom_Object__c"` is passed to Claude as a single opaque string rather than a structured list.
- Claude may misparse the list if extra spaces or special characters appear.
- The conversion is one-way: once joined, the original list cannot be recovered without string parsing.

**Recommended Fix**

Pass structured context to the agent system via a dedicated `RequestContext` object rather than flattening to string. For the user message, use a JSON-formatted context block:
```python
import json
ctx_json = json.dumps(ctx, indent=2)
user_message = f"{task}\n\n```json\n{ctx_json}\n```"
```
This preserves type information (arrays remain arrays) and is unambiguous for Claude to parse.

---

### CTXP-003 — System Prompts Rebuilt on Every Call — Expensive and Inconsistent

**Severity:** MEDIUM
**Component:** All agent `agent.py` files

**Description**

Each agent reads its system prompt from disk in `_load_system_prompt()`, called once at `__init__`. However, for the orchestrator and documentation agent, the system prompt is rebuilt on every call using f-strings that embed dynamic content (agent capabilities, environment state). If any environment variable changes between calls within a long-running process, different invocations get different system prompts, making reasoning inconsistent.

**Evidence**

```python
# agents/orchestrator/multi_agent_orchestrator.py:631-667
def _build_supervisor_prompt(self) -> str:              # ← called inside run() loop
    capabilities = "\n".join(
        f"- **{cap.name.value}**: {cap.description}"
        for cap in AGENT_REGISTRY.values()
    )
    return f"""You are the **Migration Platform Orchestrator** ...\n{capabilities}..."""

# agents/orchestrator/multi_agent_orchestrator.py:375
system_prompt = self._build_supervisor_prompt()         # ← rebuilt every orchestration.run()
```

For the data-validation and migration agents, the prompt is loaded from disk in `__init__` but any environment changes after startup (e.g., a config reload) are not reflected without restarting the process. There is no cache invalidation, no prompt versioning, and no way to tell which prompt version was in use during a given run.

**Impact**

- Re-reading the system prompt from disk on a cold path (e.g., hot-reload scenario) introduces I/O latency.
- Prompt consistency cannot be guaranteed across the lifetime of a multi-hour migration.
- No audit trail of which prompt version governed a specific agent decision.

**Recommended Fix**

Pin the system prompt at construction time and store it with a SHA-256 hash:
```python
self._system_prompt = _load_system_prompt()
self._system_prompt_hash = hashlib.sha256(self._system_prompt.encode()).hexdigest()[:12]
```
Log `system_prompt_hash` in every API call. Introduce `CONTEXT_SCHEMA_VERSION` to version prompt changes, and reload only when the version changes (checked against a persisted value).

---

### CTXP-004 — No Context Isolation Between Agents — Conversation Bleed

**Severity:** HIGH
**Component:** `agents/orchestrator/multi_agent_orchestrator.py:377`

**Description**

When the orchestrator delegates to a sub-agent, it passes only the raw task string and a flat dict as context. There is no isolation between the orchestrator's conversation history and the sub-agent's. More critically, the sub-agent instances are stored in `self._agents` as long-lived objects. If an agent retains any state between calls (e.g., `self._tool_call_history`, `self._files_read`, `self._files_written`), that state from a previous invocation bleeds into the next.

**Evidence**

```python
# agents/documentation-agent/agent.py:269-271
self._files_read: List[str] = []     # ← instance variable, survives between run() calls
self._files_written: List[str] = []  # ← instance variable, survives between run() calls

# agents/documentation-agent/agent.py:281-282
async def run(self, task, files=None, context=None):
    start_ts = time.perf_counter()
    self._files_read = []            # ← reset in run(), but NOT thread-safe
    self._files_written = []
```

If two concurrent orchestration tasks call `DocumentationAgent.run()` simultaneously (which `run_agents_in_parallel` enables), they will overwrite each other's `_files_read` and `_files_written` lists, corrupting both results.

**Evidence of concurrent access:**
```python
# agents/orchestrator/multi_agent_orchestrator.py:571-594
async def _run_parallel(self, tasks):
    coroutines = []
    for task_def in tasks:
        coroutines.append(self._delegate(agent_name, tool_input))   # ← delegates to shared agent instance
    results = await asyncio.gather(*coroutines, return_exceptions=True)
```

**Impact**

- Race condition: `_files_written` from job A is overwritten by job B; job A's result reports zero files written.
- Context from one migration run's documentation task bleeds into the next run's task via shared instance state.
- Non-deterministic behaviour under concurrent load that is impossible to reproduce in unit tests using sequential execution.

**Recommended Fix**

Never share agent instances across concurrent invocations. Either instantiate a new agent per delegation call, or use a factory pattern with a connection pool. All per-invocation state must be held in local variables inside `run()`, not in instance variables.

---

### CTXP-005 — Context Window Management Missing — No Token Tracking

**Severity:** HIGH
**Component:** All agents

**Description**

No agent tracks the cumulative token count of its conversation history. As tool results accumulate across iterations (each can be several kilobytes of JSON), the context window silently fills. The agents have no mechanism to prune old messages, summarise intermediate results, or warn when approaching the context limit. The only response to an exceeded context is an API error that surfaces as `result.error`.

**Evidence**

Each iteration appends two new messages to the conversation history:
```python
# agents/migration-agent/agent.py:270-278
messages.append({"role": "assistant", "content": response.content})
tool_results = await self._execute_tools_concurrently(tool_use_blocks)
messages.append({
    "role": "user",
    "content": tool_results,            # ← can be large JSON arrays
})
```

With `MAX_ITERATIONS = 20` and each tool result averaging 500 tokens, a full run accumulates `~20,000` input tokens before Claude even starts responding. For the Data Validation Agent with 9 tool types, a single validation pass can easily generate `50,000–100,000` tokens of history, approaching the `claude-sonnet-4-6` context window.

**Impact**

- Silent context overflow causes `400` errors that appear as generic agent failures.
- No graceful degradation: the agent cannot summarise and continue.
- No visibility into token burn rate, making cost estimation impossible.

**Recommended Fix**

Add token budget tracking to `BaseAgent`:
```python
CONTEXT_WINDOW_TOKENS = 200_000  # for claude-sonnet-4-6
WARN_THRESHOLD = 0.80
STOP_THRESHOLD = 0.95

tokens_used = response.usage.input_tokens + response.usage.output_tokens
self._total_tokens_used += tokens_used
if self._total_tokens_used / CONTEXT_WINDOW_TOKENS > STOP_THRESHOLD:
    raise ContextWindowExhaustedException(...)
elif self._total_tokens_used / CONTEXT_WINDOW_TOKENS > WARN_THRESHOLD:
    logger.warning("Context window at %.0f%%", ...)
```
Additionally, implement a sliding window strategy: when usage exceeds 70%, replace the oldest tool result messages with a one-sentence summary.

---

### CTXP-006 — No Structured Context Protocol — Each Agent Invents Its Own Context Shape

**Severity:** MEDIUM
**Component:** All agent `run()` methods

**Description**

Each agent accepts a different combination of positional and keyword arguments for contextual information. There is no shared `RequestContext` type. The Migration Agent takes `context: Optional[Dict[str, Any]]` (flat dict). The Validation Agent takes `run_id`, `object_types`, and `context` separately. The Documentation Agent takes `files` and `context`. The Security Agent takes `scope`. The orchestrator synthesises calls to all four, maintaining four separate translation layers.

**Evidence**

```python
# agents/migration-agent/agent.py:152-157
async def run(self, task: str,
              context: Optional[Dict[str, Any]] = None,
              conversation_history: Optional[List[Dict[str, Any]]] = None)

# agents/data-validation-agent/agent.py:134-140
async def run(self, task: str,
              run_id: Optional[str] = None,
              object_types: Optional[List[str]] = None,
              context: Optional[Dict[str, Any]] = None)

# agents/documentation-agent/agent.py:273-278
async def run(self, task: str,
              files: Optional[List[str]] = None,
              context: Optional[Dict[str, Any]] = None)

# agents/security-audit-agent/agent.py:500-505
async def run(self, task: str,
              scope: Optional[str] = None,
              context: Optional[Dict[str, Any]] = None)
```

**Impact**

- The orchestrator must maintain a separate adapter for each agent's calling convention (lines 497–545 in `multi_agent_orchestrator.py`).
- Adding a new context field (e.g., `tenant_id`) requires updating 5 files.
- No compile-time guarantee that required fields are passed: `run_id` can silently be `None` throughout a validation run.

**Recommended Fix**

Adopt a single `RequestContext` Pydantic model (see `agents/_shared/context_protocol.py`) passed as the second positional argument to every agent's `run()` method:
```python
async def run(self, task: str, context: RequestContext) -> AgentResult
```
All agent-specific context fields (`run_id`, `object_types`, `scope`) live as optional fields in `RequestContext.metadata`.

---

### CTXP-007 — Missing `request_id` / `trace_id` Propagation

**Severity:** HIGH
**Component:** All agents, orchestrator

**Description**

No agent generates or propagates a `trace_id` or `request_id`. Each agent generates its own `uuid.uuid4()` for internal use (orchestration_id, incident IDs), but these are not correlated. A single user request that triggers the orchestrator → migration agent → validation agent → documentation agent chain produces log lines with four unrelated identifiers, making cross-agent tracing impossible.

**Evidence**

```python
# agents/orchestrator/multi_agent_orchestrator.py:362
self._orchestration_id = str(uuid.uuid4())    # ← not propagated to sub-agents

# agents/orchestrator/multi_agent_orchestrator.py:369
logger.info("Orchestration started id=%s task=%s", self._orchestration_id, task[:100])
```

Sub-agents log with their own identifiers:
```python
# agents/migration-agent/agent.py:178-182
logger.info("MigrationAgent starting task: %s (model=%s max_iter=%d)", task[:120], ...)
# ← no trace_id in this log line
```

**Impact**

- When a migration fails across multiple agent boundaries, there is no log query that returns all events for that specific pipeline execution.
- Support engineers must manually correlate logs by timestamp, which is error-prone and time-consuming.
- Distributed tracing tools (Jaeger, Datadog APM) cannot be used without trace ID propagation.

**Recommended Fix**

The `orchestration_id` must be passed to every sub-agent invocation as `parent_trace_id`. Sub-agents must generate their own `invocation_id` and log both. Use the `derive_child_context()` function (see `context_protocol.py`) to ensure every child context traces back to the root request. Include `trace_id` in every log call via a logging filter:
```python
class TraceIdFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = getattr(_ctx_var, 'trace_id', 'unset')
        return True
```

---

### CTXP-008 — Memory Is Ephemeral — Agent Knowledge Resets Between Invocations

**Severity:** MEDIUM
**Component:** All agents

**Description**

Agents have no persistent memory. Each call to `agent.run()` starts with a blank conversation history (unless the caller explicitly passes `conversation_history`, which only `MigrationAgent` supports and only as an unsanitised raw list — see CTXP-001). Institutional knowledge accumulated in one invocation (e.g., "this migration run has a persistent duplicate issue in the AccountId field") is lost when the invocation ends.

**Evidence**

```python
# agents/migration-agent/agent.py:170-175
messages: List[Dict[str, Any]] = list(conversation_history or [])
messages.append({"role": "user", "content": user_message})
# ← if conversation_history is None (the default), every run starts fresh
```

The `DataValidationAgent`, `DocumentationAgent`, and `SecurityAuditAgent` do not even accept a `conversation_history` parameter — memory is structurally impossible for them.

**Impact**

- If the orchestrator calls the Validation Agent twice for the same run (e.g., mid-run and post-run), the second call has no knowledge of the first call's findings.
- The agent cannot build up a mental model of recurring problems across a multi-day migration project.
- Every invocation re-discovers the same issues, wasting tokens and time.

**Recommended Fix**

Implement a lightweight agent memory store backed by Redis or PostgreSQL. Key: `(agent_type, job_id)`. Value: a compressed summary of the last N invocations' findings. Before each run, load the memory summary and prepend it to the system prompt as a `## Prior Context` section. After each run, update the memory with a 2–3 sentence summary of key findings.

---

### CTXP-009 — Tool Results Not Validated Before Injecting Into Conversation History

**Severity:** MEDIUM
**Component:** All agent `_execute_tools*` methods

**Description**

Tool results are serialised to JSON using `json.dumps(result, default=str)` and appended to the conversation history without any schema validation. If a tool returns an unexpected structure (e.g., a nested list where Claude expects a dict, or a very large string field), it is injected verbatim into the history and may confuse Claude's subsequent reasoning.

**Evidence**

```python
# agents/data-validation-agent/agent.py:252-260
content = json.dumps(result, default=str)
is_error = False
tool_results.append({
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": content,          # ← result injected with no schema check
    "is_error": is_error,
})
```

The `default=str` in `json.dumps` is a silent coercion that converts non-JSON-serialisable objects (datetimes, Pydantic models, exceptions) to their string representations without warning. This means a `datetime` object becomes `"2026-03-16 12:00:00+00:00"` rather than an ISO 8601 string, which Claude may interpret inconsistently.

**Impact**

- Unvalidated tool results can cause Claude to make decisions based on malformed data.
- Very large tool results (e.g., a field metadata dump for a 500-field object) consume disproportionate context window tokens.
- `default=str` silently hides serialisation errors that should be surfaced as tool failures.

**Recommended Fix**

Define a `ToolOutput` Pydantic model for every tool (see `agents/_shared/schemas.py`). Validate the raw result through the model before serialisation:
```python
validated = ToolOutput.model_validate(raw_result)
content = validated.model_dump_json()
```
Additionally, impose a maximum tool result size (e.g., `10,000` characters). Results exceeding this limit should be summarised or paginated.

---

### CTXP-010 — No Context Versioning — Prompt Changes Invisible to Running Agents

**Severity:** LOW
**Component:** All agents

**Description**

System prompts are loaded from `.md` files without any version tracking. When a prompt is updated (e.g., adding a new batch size rule), any currently running agent continues using the old prompt for the duration of its invocation. There is no mechanism to detect that the prompt has changed, no version embedded in log lines, and no way to determine retrospectively which prompt version governed a specific migration decision.

**Evidence**

```python
# agents/migration-agent/agent.py:56-71
_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "prompts", "system_prompt.md"
)

def _load_system_prompt() -> str:
    try:
        with open(_SYSTEM_PROMPT_PATH, encoding="utf-8") as fh:
            return fh.read()             # ← no version, no hash, no timestamp
    except FileNotFoundError:
        ...
```

The prompt content is read once, stored in `self._system_prompt`, and used for the lifetime of the agent instance. If the orchestrator long-polls (e.g., by reusing the same `MultiAgentOrchestrator` instance across multiple requests), all requests after a prompt update still use the old cached value.

**Impact**

- A prompt fix for ISSUE-014 (batch size oscillation) deployed mid-run does not take effect for running agents.
- Audit reconstruction of "what rules did the agent follow?" requires checking git blame on `.md` files.
- Discrepancy between the prompt a developer thinks is running and the prompt that is actually running.

**Recommended Fix**

Embed a `PROMPT_VERSION` comment at the top of each `.md` file (e.g., `<!-- version: 1.3 -->`). Parse and store this version at load time. Log the prompt version on every API call:
```
logger.info("Calling Claude model=%s prompt_version=%s trace_id=%s", ...)
```
Include the prompt version in `AgentResult` and `OrchestrationResult` metadata. For long-running processes, check for prompt file changes on a 5-minute interval and log a warning if the on-disk version differs from the loaded version.

---

## Summary Table

| ID | Problem | Severity | Affected Files |
|----|---------|----------|----------------|
| CTXP-001 | Raw `List[Dict]` history, no validation | HIGH | `migration-agent/agent.py:152` |
| CTXP-002 | `object_types` list → string, no iteration | HIGH | `data-validation-agent/agent.py:160` |
| CTXP-003 | System prompt rebuilt every call | MEDIUM | `multi_agent_orchestrator.py:631` |
| CTXP-004 | No context isolation, shared agent instances | HIGH | `multi_agent_orchestrator.py:328` |
| CTXP-005 | No token budget tracking | HIGH | All agents |
| CTXP-006 | No structured context protocol | MEDIUM | All agent `run()` methods |
| CTXP-007 | No `trace_id` propagation | HIGH | All agents |
| CTXP-008 | Ephemeral memory, resets per invocation | MEDIUM | All agents |
| CTXP-009 | Tool results not validated before injection | MEDIUM | All agent tool dispatch |
| CTXP-010 | No prompt versioning | LOW | All agent prompt loaders |

---

## Proposed Remediation Architecture

The ten problems above are all symptoms of a single root cause: context is treated as an untyped string bag rather than a first-class structured protocol. The remedy is to introduce:

1. **`RequestContext`** (Pydantic model) — typed, validated, versioned, immutable context passed to every agent invocation. Carries `trace_id`, `job_id`, `tenant_id`, `invocation_id`.

2. **`derive_child_context()`** — creates a new immutable child context from a parent, threading the `trace_id` through the entire agent graph.

3. **`ContextValidator`** — validates `RequestContext` at agent entry points before any tool call is made.

4. **`TokenBudgetTracker`** — tracks cumulative input/output tokens across iterations and raises structured warnings or stops the loop before the API rejects the request.

5. **`AgentMemoryStore`** — Redis-backed per-`(agent_type, job_id)` short-term memory that survives between invocations for the duration of a migration project.

See `agents/_shared/context_protocol.py` and `agents/_shared/base_agent.py` for the reference implementation of this architecture.
