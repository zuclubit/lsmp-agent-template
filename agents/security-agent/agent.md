# Security Agent Specification

**Version**: 2.1.0
**Model**: `claude-sonnet-4-6` (override: `SECURITY_AGENT_MODEL` env var)
**API Spec**: v2.1.0
**Last Updated**: 2026-03

---

## Purpose

The Security Agent performs static security checks on migration payloads and configurations
before execution is permitted. It is a blocking gate in the pipeline — a BLOCK decision
halts all downstream agents.

The four security checks are implemented as deterministic Python code (no LLM involved):
1. **Path whitelist check** — only `/var/data/migration/` and `/tmp/migration-work/` allowed
2. **SOQL injection check** — SELECT-only; no semicolons, UNION, or DML keywords
3. **Entropy check** — Shannon entropy > 4.5 flags potential hardcoded secrets
4. **PII detection** — email, SSN, and credit card patterns reject if raw PII found

The LLM is invoked **only** for analytical commentary when `request_llm_analysis=True`.

---

## Input Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | Yes | Migration job identifier |
| `tenant_id` | string | Yes | Multi-tenant identifier |
| `payload` | object | Yes | Migration payload or configuration dict to audit |
| `file_paths` | string[] | No | File paths to check against whitelist |
| `soql_queries` | string[] | No | SOQL query strings to validate |
| `has_sox_scope` | boolean | No | SOX compliance scope flag (default: `false`) |
| `request_llm_analysis` | boolean | No | Invoke LLM for findings analysis (default: `false`) |

---

## Output Schema (`SecurityAuditResult`)

| Field | Type | Description |
|-------|------|-------------|
| `audit_id` | UUID | Unique audit run identifier |
| `job_id` | string | Echoed from input |
| `tenant_id` | string | Echoed from input |
| `passed` | boolean | True when `risk_score <= 0.7` AND no CRITICAL findings |
| `findings` | SecurityFinding[] | All security findings from deterministic checks |
| `risk_score` | float (0.0–1.0) | Composite risk score — > 0.7 blocks pipeline |
| `gate_decision` | enum | `ALLOW` / `WARN` / `BLOCK` |
| `llm_analysis` | string | Optional LLM narrative; `null` when not requested |
| `checked_at` | ISO 8601 | Audit timestamp |
| `duration_ms` | integer | Wall-clock execution time |

### SecurityFinding Fields

| Field | Type | Description |
|-------|------|-------------|
| `finding_id` | UUID | Unique finding identifier |
| `finding_type` | enum | `PATH_TRAVERSAL` / `SOQL_INJECTION` / `HARDCODED_SECRET` / `PII_EXPOSURE` / `UNAUTHORIZED_PATH` |
| `severity` | enum | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` |
| `location` | string | Field path, file path, or query index |
| `description` | string | Human-readable finding description |
| `recommendation` | string | Specific remediation action |
| `evidence_snippet` | string | Redacted/masked evidence — never raw secrets or PII |

---

## Security Check Details

### 1. Path Whitelist Check

Permitted read-only paths:
- `/var/data/migration/`
- `/tmp/migration-work/`

| Condition | Severity |
|-----------|----------|
| Path contains `..` (traversal) | CRITICAL |
| Path outside whitelisted directories | CRITICAL |

### 2. SOQL Injection Check

| Condition | Severity |
|-----------|----------|
| Does not start with `SELECT` | CRITICAL |
| Contains semicolon (`;`) | CRITICAL |
| Contains DML keywords (INSERT/UPDATE/DELETE/etc.) | CRITICAL |
| Contains `UNION` | HIGH |

### 3. Entropy Check (Hardcoded Secrets)

- Scans all string values in the payload recursively
- Flags strings with Shannon entropy > **4.5** and length >= 16 characters
- Key name hints (password, secret, token, key, api_key, etc.) elevate to CRITICAL

| Condition | Severity |
|-----------|----------|
| High entropy + secret key name hint | CRITICAL |
| High entropy + no key name hint | HIGH |

### 4. PII Detection

| PII Type | Pattern | Severity |
|----------|---------|----------|
| Email address | RFC 5322 pattern | CRITICAL |
| US SSN | `\d{3}-\d{2}-\d{4}` (valid ranges) | CRITICAL |
| Credit card | Visa / MasterCard / Amex / Diners / Discover | CRITICAL |

---

## Risk Score and Blocking Rules

Risk score = sum of severity weights (capped at 1.0):

| Severity | Weight |
|----------|--------|
| CRITICAL | 0.40 |
| HIGH | 0.20 |
| MEDIUM | 0.10 |
| LOW | 0.03 |

Any CRITICAL finding sets a minimum risk_score of **0.71** (above the block threshold).

**Pipeline BLOCKED when:**
- `risk_score > 0.7`, OR
- Any finding with `severity = CRITICAL`

**Gate Decision:**
- `BLOCK` — any CRITICAL finding or risk_score > 0.7
- `WARN` — findings exist but risk_score <= 0.7 and no CRITICAL findings
- `ALLOW` — no findings

---

## When to Use

Use the Security Agent when:
- Before execution-agent is invoked — always run as a blocking gate
- Auditing migration configuration payloads for secrets or PII
- Validating SOQL queries from dynamic configurations
- Checking file path configurations for path traversal risks

Do NOT use the Security Agent when:
- Performing runtime behavioral security monitoring (use SIEM/WAF)
- Auditing Salesforce org-level permissions (use Salesforce Security Health Check)
- Scanning source code for vulnerabilities (use bandit/semgrep in CI pipeline)

---

## Model Rationale

`claude-sonnet-4-6` is used **only** for `llm_analysis` tasks when findings require
contextual interpretation. The four core security checks are pure Python code — no LLM.

This design ensures:
- Security decisions are deterministic and auditable
- No LLM hallucination can approve a pipeline with CRITICAL findings
- Low latency — most audits complete in < 50ms (no LLM call)
- The LLM adds value for explaining findings and assessing false positives

---

## Example Invocation

```python
from agents.security_agent.agent import run_security_agent

result = run_security_agent(
    job_id="job-acme-2026-001",
    tenant_id="tenant-acme",
    payload={
        "source_connection": {"host": "db.internal", "port": 5432},
        "batch_size": 1000,
    },
    file_paths=["/var/data/migration/acme/accounts.csv"],
    soql_queries=["SELECT Id, Name, Phone FROM Account WHERE IsActive = true LIMIT 1000"],
    has_sox_scope=True,
    request_llm_analysis=False,
)

if result.passed:
    print(f"Security gate: {result.gate_decision} (risk_score={result.risk_score:.2f})")
else:
    print(f"Security gate: BLOCKED (risk_score={result.risk_score:.2f})")
    for finding in result.findings:
        print(f"  [{finding.severity.value}] {finding.finding_type.value}: {finding.description}")
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECURITY_AGENT_MODEL` | `claude-sonnet-4-6` | Anthropic model ID (for LLM analysis only) |
| `SECURITY_AGENT_MAX_TOKENS` | `4096` | Maximum tokens for LLM analysis response |
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key (only needed for LLM analysis) |
