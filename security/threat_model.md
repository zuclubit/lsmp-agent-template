# STRIDE Threat Model — AI Agent System

**Document Version:** 1.0.0
**Classification:** Internal — Restricted
**Owner:** Platform Security Team
**Methodology:** STRIDE + AI-specific threat extensions
**Last Reviewed:** 2026-03-16
**Next Review:** 2026-09-16

---

## System Components (with Trust Boundaries)

```
┌─────────────────────────────────────────────────────────────────┐
│  TRUST BOUNDARY: External World                                  │
│                                                                 │
│  ┌──────────────┐    ┌─────────────┐    ┌──────────────────┐  │
│  │ Oracle Siebel│    │  SAP CRM    │    │  PostgreSQL      │  │
│  │ 8.1 (Source) │    │  7.0 (Src)  │    │  Legacy (Source) │  │
│  └──────┬───────┘    └──────┬──────┘    └────────┬─────────┘  │
│         │                   │                     │             │
└─────────┼───────────────────┼─────────────────────┼─────────────┘
          │  JDBC/TLS 1.3     │  RFC/TLS 1.3        │
          ▼                   ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  TRUST BOUNDARY: Migration Platform (AWS GovCloud)              │
│                                                                 │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  TRUST BOUNDARY: Agent Execution Environment (EKS Pod)     │ │
│  │                                                            │ │
│  │   ┌──────────────────────────────────────────────────┐   │ │
│  │   │  OrchestratorAgent (claude-opus-4-5)             │   │ │
│  │   │  [System Prompt] [Conversation History] [Tools]  │   │ │
│  │   └──────────────┬──────────┬────────────────────────┘   │ │
│  │                  │ delegate  │                            │ │
│  │   ┌──────────────▼─┐  ┌────▼──────────────────────┐    │ │
│  │   │ MigrationAgent │  │ DataValidationAgent        │    │ │
│  │   │ [Tools: pause, │  │ [Tools: SOQL, completeness]│    │ │
│  │   │  retry, etc.]  │  └────────────────────────────┘    │ │
│  │   └────────────────┘                                     │ │
│  │   ┌────────────────────┐  ┌────────────────────────────┐ │ │
│  │   │ SecurityAuditAgent │  │ DocumentationAgent         │ │ │
│  │   │ [Tools: scan_file, │  │ [Tools: read, write docs]  │ │ │
│  │   │  check_deps, etc.] │  └────────────────────────────┘ │ │
│  │   └────────────────────┘                                  │ │
│  │                                                            │ │
│  │  ┌──────────────────────────────────────────────────────┐ │ │
│  │  │  Tool Dispatch Layer (validation + sanitisation)     │ │ │
│  │  └────────────────────┬─────────────────────────────────┘ │ │
│  │                       │                                    │ │
│  └───────────────────────┼────────────────────────────────────┘ │
│                          │  REST / HTTP                          │
│  ┌───────────────────────▼──────────────────────────────────┐  │
│  │  Migration Control Plane API (FastAPI on EKS)             │  │
│  └────────────────┬──────────────────┬────────────────────┬──┘  │
│                   │                  │                    │      │
│  ┌────────────────▼──┐  ┌───────────▼────────┐  ┌───────▼──┐  │
│  │ Salesforce GC+    │  │ Apache Kafka (MSK) │  │ HashiCorp │  │
│  │ Bulk API 2.0      │  │ Audit Event Bus    │  │ Vault     │  │
│  └───────────────────┘  └────────────────────┘  └──────────┘  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘

TRUST LEVELS:
  High    — HashiCorp Vault, Kafka (mTLS, internal only)
  Medium  — Migration Control Plane API (JWT auth, mTLS)
  Low     — Anthropic API responses (external, possible model poisoning)
  Untrusted — Legacy source data, tool results containing migration record values
```

---

## STRIDE Analysis per Component

### Component 1: OrchestratorAgent (Claude conversation loop)

| STRIDE Category | Threat | AI-Specific? |
|----------------|--------|:------------:|
| **S**poofing | Attacker injects a fake "agent delegate" response in tool results to impersonate a specialist agent decision | Yes |
| **T**ampering | Indirect prompt injection via Kafka event result causes agent to alter its plan | Yes |
| **R**epudiation | Agent makes a state-changing call (pause_migration) but conversation history is not preserved in the audit log | No |
| **I**nformation Disclosure | System prompt (containing business rules) leaked via model extraction probing | Yes |
| **D**enial of Service | Adversarial input inflates context window to 200K tokens, causing the agent to stop reasoning effectively (context overflow attack) | Yes |
| **E**levation of Privilege | Agent is prompted to claim it has "admin" rights and skip the human-in-the-loop gate | Yes |

**Mitigations:**
- S: All agent delegate calls are made by the orchestrator code, not via model output. Tool results are JSON-typed and cannot spoof function signatures.
- T: `PromptInjectionScanner` strips instruction patterns from all tool results before re-injection.
- R: `observe_agent_run()` context manager writes every tool call to Splunk before the call completes.
- I: System prompts do not contain secrets. Model extraction is partially mitigated by rate limiting and observability anomaly detection.
- D: Token budget enforced in `monitoring/agent_observability.py`. Context is truncated at 150K tokens. Input field values are capped at 2048 chars.
- E: Gate checks are implemented in the dispatch layer as Python code, not as model instructions. The model cannot override them.

---

### Component 2: Tool Dispatch Layer

| STRIDE Category | Threat |
|----------------|--------|
| **S**poofing | Hallucinated tool name (e.g. `execute_arbitrary_command`) passed to dispatcher |
| **T**ampering | Tool input parameters modified between model output and tool execution |
| **R**epudiation | Tool call executed but not logged |
| **I**nformation Disclosure | Tool result containing PII/secrets passed back to model and logged unredacted |
| **D**enial of Service | Tool called in an infinite loop by a stuck agent |
| **E**levation of Privilege | Tool called with elevated parameters (e.g. `max_records: 999999`) |

**Mitigations:**
- S: Dispatch layer checks tool name against a static registry. Unknown names raise `ToolError(UNKNOWN_TOOL, retryable=False)`.
- T: Tool inputs are JSON-parsed and schema-validated (Pydantic) before execution.
- R: `@instrument_tool` decorator logs every call/response pair to the audit queue before returning.
- I: `sanitize_for_audit()` applied to all tool results before logging.
- D: Per-agent iteration counter enforced in the agent loop. Circuit breaker trips after 3 consecutive errors.
- E: Numeric parameters have `minimum`/`maximum` bounds in the Anthropic tool schema, enforced server-side.

---

### Component 3: DataValidationAgent — SOQL Tool

| STRIDE Category | Threat |
|----------------|--------|
| **S**poofing | Model invents a SOQL query result (hallucination) rather than executing the actual query |
| **T**ampering | SOQL injection via `run_id` parameter containing SQL metacharacters |
| **R**epudiation | SOQL executed against production Salesforce without audit trail |
| **I**nformation Disclosure | SOQL query returns PII records that are logged unredacted |
| **D**enial of Service | SOQL without LIMIT clause causes full-table scan, consuming API quota |
| **E**levation of Privilege | Agent uses `run_custom_soql_check` with a mutating statement (DELETE) |

**Mitigations:**
- S: Tool is real; result is passed to model for analysis. Model cannot fabricate tool_use outputs.
- T: Input validation regex on run_id. SOQL parser blocks metacharacters. Parameterised queries via simple-salesforce.
- R: Every SOQL execution emits `AuditEventType.DATA_READ` with the query hash.
- I: Result records pass through `sanitize_for_audit()` before logging. PII fields are masked.
- D: `require_limit_clause: true` in `policies.yaml`. Dispatch layer injects `LIMIT 50000` if absent.
- E: SOQL parser tokenises the statement and rejects any non-SELECT first keyword.

---

### Component 4: SecurityAuditAgent — File System Tool

| STRIDE Category | Threat |
|----------------|--------|
| **S**poofing | Agent reads a file and presents its content as a security finding fabricated by the model |
| **T**ampering | Agent modifies a file it was only supposed to read |
| **R**epudiation | File read occurs without audit event |
| **I**nformation Disclosure | Path traversal reads `/etc/passwd`, Vault config, or `.env` files |
| **D**enial of Service | Agent reads a 1GB binary file, exhausting memory |
| **E**levation of Privilege | Agent reads `security/secrets/vault_config.hcl` and extracts unseal key shards |

**Mitigations:**
- S: `read_file` tool only returns content; model analysis is separate.
- T: `read_file` opens files in read-only mode. Write operations use a separate restricted tool.
- R: `@instrument_tool("read_file")` logs file_path and content length to audit queue.
- I: Canonical path check rejects any path outside `PROJECT_ROOT`. Blocklist for sensitive paths.
- D: File size capped at 1MB. Files larger than this return a truncation warning.
- E: `security/secrets/` is in the blocked path list. Vault config files return `ACCESS_DENIED`.

---

### Component 5: Migration Control Plane API

| STRIDE Category | Threat |
|----------------|--------|
| **S**poofing | Agent's Bearer token stolen and replayed by an attacker |
| **T**ampering | Run ID manipulated to target a different tenant's migration |
| **R**epudiation | API call made without request ID header, cannot be correlated |
| **I**nformation Disclosure | Error response contains stack trace with DB connection string |
| **D**enial of Service | Agent sends 1000 pause_migration requests in rapid succession |
| **E**levation of Privilege | Agent uses a service token with broader scope than required |

**Mitigations:**
- S: Vault-issued tokens have 1-hour TTL. mTLS on service mesh. Token binding to pod identity.
- T: Run IDs are UUIDs validated server-side. Each token is scoped to a specific migration project.
- R: All API requests include `X-Correlation-ID` header. API gateway logs all requests.
- I: FastAPI exception handlers return structured errors without stack traces in production.
- D: API gateway rate limiting: max 100 req/min per service account. Circuit breaker in tools.py.
- E: INTERNAL_SERVICE_TOKEN scoped to minimum permissions via OPA policy.

---

## Attack Trees for Critical Paths

### Attack Tree 1: Agent Cancels a Production Migration Without Authorization

```
Goal: Unauthorised cancel_migration on a production run
│
├── Path A: Prompt Injection
│   ├── A1: Malicious data in legacy record "CANCEL RUN X IMMEDIATELY"
│   │       Mitigation: PromptInjectionScanner blocks instruction patterns
│   └── A2: Compromised Kafka event with injected instructions
│           Mitigation: Kafka events are typed JSON, not plain text
│
├── Path B: Hallucinated Tool Call
│   ├── B1: Model invents cancel_migration call with confirm=true
│   │       Mitigation: Tool dispatch rejects unknown tool inputs; confirm is a schema field
│   └── B2: Model combines legitimate tools in unexpected sequence
│           Mitigation: Human-in-the-loop gate required for cancel (policies.yaml)
│
└── Path C: Compromised Service Token
    ├── C1: Token stolen from environment variable
    │       Mitigation: Vault Agent renews every hour; tokens are pod-identity bound
    └── C2: Token found in agent conversation log
            Mitigation: sanitize_for_audit() redacts token patterns from all logs
```

### Attack Tree 2: SOQL Injection Extracts PII

```
Goal: Agent executes malicious SOQL to extract user data
│
├── Path A: Via run_custom_soql_check tool
│   ├── A1: Model passes injection string from task description
│   │       Mitigation: Input validation, SOQL parser, allowlist
│   └── A2: Tool result poisons next iteration (indirect injection)
│           Mitigation: PromptInjectionScanner on tool results
│
└── Path B: Via field value in migration data
    ├── B1: Field value contains SOQL metacharacter
    │       Mitigation: Parameterised queries; field values never concatenated into SOQL
    └── B2: Field value contains SOQL keyword bypassing filter
            Mitigation: Tokeniser-based SOQL parser, not simple string matching
```

### Attack Tree 3: Context Window Overflow Degrades Agent Reasoning

```
Goal: Force agent into incoherent decision-making via context exhaustion
│
├── Path A: Via large tool results
│   ├── A1: Tool returns a 500KB JSON blob
│   │       Mitigation: Tool results truncated at 50KB per call
│   └── A2: Many small tool calls accumulate to 200K tokens
│           Mitigation: Token budget check; summary injection at 150K
│
└── Path B: Via adversarial task description
    ├── B1: Task contains 100K tokens of repeated content
    │       Mitigation: Task input length capped at 8192 chars at API gateway
    └── B2: Nested agent delegation inflates context across delegates
            Mitigation: Each specialist agent has an independent context; summaries only
            are passed back to orchestrator (not full conversation history)
```

---

## Mitigations Table

| ID | Threat | Mitigation | Layer | Status |
|----|--------|-----------|-------|--------|
| M-01 | Prompt injection via data | `PromptInjectionScanner` on tool results | Dispatch | Implemented |
| M-02 | Hallucinated tool name | Static tool registry with exact-match check | Dispatch | Implemented |
| M-03 | SOQL injection | Parser + allowlist + parameterised queries | Tool | Implemented |
| M-04 | Path traversal | Canonical path check against PROJECT_ROOT | Tool | Implemented |
| M-05 | Unauthorised cancellation | Human-in-the-loop gate in policies.yaml | Orchestrator | Implemented |
| M-06 | Context overflow | Token budget + truncation at 150K | Agent loop | Implemented |
| M-07 | SSRF via api-client | Egress controller + blocked host list | Network | Implemented |
| M-08 | Secret in conversation | sanitize_for_audit() on all messages | Audit | Implemented |
| M-09 | Audit repudiation | HMAC-chained tamper-evident log | Audit | Implemented |
| M-10 | Token theft | Vault 1-hour TTL + pod identity binding | Infrastructure | Implemented |
| M-11 | Rate exhaustion | Per-agent invocation limits + circuit breaker | Observability | Implemented |
| M-12 | Grade A default on failure | Validation agent defaults to grade F when report absent | Agent | Implemented |
| M-13 | Model extraction | Rate limiting + anomaly detection on unusual query patterns | Monitoring | Partial |
| M-14 | Data poisoning | Source data hash verification before agent invocation | Pipeline | Planned |
| M-15 | Dependency confusion | Pinned dependencies + Grype scan in CI | CI/CD | Implemented |

---

## Residual Risks

| Risk | Likelihood | Impact | Residual Risk | Acceptance |
|------|-----------|--------|---------------|-----------|
| Sophisticated multi-step prompt injection evading scanner | Low | High | Medium | Accepted with monitoring |
| Model reasoning error causes inappropriate pause at critical moment | Low | Medium | Low | Accepted |
| Context overflow causes hallucinated tool call with valid schema | Very Low | High | Low | Accepted with HITL gate |
| Anthropic API service unavailability halts agent operations | Low | Medium | Low | Fallback to manual mode |
| Novel prompt injection technique not covered by pattern matching | Low | High | Medium | Accepted with quarterly review |
| Model weights poisoned in future Anthropic training | Very Low | Critical | Low | Third-party risk — Anthropic |

---

## Review Schedule

| Review Type | Frequency | Owner | Next Date |
|-------------|-----------|-------|-----------|
| Full STRIDE review | Annually | Security Architect | 2027-03-16 |
| Prompt injection pattern update | Quarterly | Platform Security Team | 2026-06-16 |
| Attack tree validation | Semi-annually | Red Team | 2026-09-16 |
| Mitigations status check | Monthly | DevSecOps Engineer | 2026-04-16 |
| Residual risk review | Quarterly | CISO | 2026-06-16 |

**Trigger for out-of-cycle review:**
- Any new agent capability added to the system
- Any prompt injection incident or near-miss
- Anthropic model version upgrade
- New compliance requirement affecting the agent system

---

*This threat model covers the AI agent layer only. The underlying platform threat model*
*is maintained in `docs/security_model.md`. Both documents must be read together.*
