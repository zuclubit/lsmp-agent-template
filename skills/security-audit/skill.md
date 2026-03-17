---
name: security-audit
description: Static security analysis on migration payloads, configurations, and SOQL queries
type: skill
version: 2.0.0
agent: security-agent
---

# Security Audit Skill

**Version**: 2.0.0
**Agent**: security-agent
**Last Updated**: 2026-03

---

## Purpose

The `security-audit` skill runs four deterministic static security checks on migration payloads,
configuration dicts, file paths, and SOQL query strings. All four checks are implemented as
pure Python code — no LLM is involved in the security decision.

This skill is a **blocking gate** in the migration pipeline. A `BLOCK` gate decision halts all
downstream agents. The security gate must return `ALLOW` before the execution agent is invoked.

---

## Four Deterministic Checks

### 1. Path Whitelist Check

Validates all file paths against a strict allowlist.

**Permitted paths**:
- `/var/data/migration/` (and subdirectories)
- `/tmp/migration-work/` (and subdirectories)

**Detection**:
- Path contains `..` (traversal attempt) → `CRITICAL` / `PATH_TRAVERSAL`
- Path outside whitelisted directories → `CRITICAL` / `UNAUTHORIZED_PATH`

---

### 2. SOQL Injection Check

Validates all SOQL query strings against injection patterns.

| Condition | Severity | Finding Type |
|-----------|----------|-------------|
| Does not start with `SELECT` | CRITICAL | `SOQL_INJECTION` |
| Contains semicolon (`;`) | CRITICAL | `SOQL_INJECTION` |
| Contains DML keywords: `INSERT`, `UPDATE`, `DELETE`, `UPSERT`, `MERGE`, `CREATE`, `DROP`, `ALTER`, `TRUNCATE`, `EXEC`, `EXECUTE` | CRITICAL | `SOQL_INJECTION` |
| Contains `UNION` | HIGH | `SOQL_INJECTION` |

---

### 3. Entropy Check (Hardcoded Secrets)

Scans all string values in the payload recursively for hardcoded credentials.

- Flags strings with Shannon entropy > **4.5** AND length >= **16 characters**
- Key name hints elevate severity: `password`, `secret`, `token`, `key`, `api_key`, `apikey`, `api-key`, `credential`, `private`, `auth`

| Condition | Severity | Finding Type |
|-----------|----------|-------------|
| High entropy + key name hint | CRITICAL | `HARDCODED_SECRET` |
| High entropy + no hint | HIGH | `HARDCODED_SECRET` |

Evidence stored as SHA-256 hash — the raw secret value is never logged.

---

### 4. PII Detection

Detects Personally Identifiable Information patterns in payload string values.

| PII Type | Pattern | Severity |
|----------|---------|---------|
| Email address | RFC 5322 compliant regex | CRITICAL |
| US SSN | `\d{3}-\d{2}-\d{4}` with valid ranges | CRITICAL |
| Credit card | Visa, MasterCard, Amex, Diners, Discover patterns (16-digit Luhn) | CRITICAL |

Evidence masked: raw PII values are never stored. The evidence snippet shows type, count, and a redacted sample.

---

## Risk Score and Gate Decision

Risk score = sum of severity weights (capped at 1.0):

| Severity | Weight |
|----------|--------|
| CRITICAL | 0.40 |
| HIGH | 0.20 |
| MEDIUM | 0.10 |
| LOW | 0.03 |

Any CRITICAL finding sets a minimum `risk_score` of **0.71** (above the block threshold).

**Gate Decision**:
| Decision | Condition |
|----------|-----------|
| `BLOCK` | Any CRITICAL finding OR `risk_score > 0.7` |
| `WARN` | Findings exist but no CRITICAL AND `risk_score <= 0.7` |
| `ALLOW` | No findings |

**`passed` field**:
- `True` when `risk_score <= 0.7` AND no CRITICAL findings
- `False` otherwise

---

## LLM Analysis (Optional)

When `request_llm_analysis=True`, the LLM (`claude-sonnet-4-6`) is invoked to provide:
- Contextual explanation of why each finding is dangerous
- False positive likelihood assessment
- Prioritised remediation recommendations
- Compliance notes (SOX, PCI-DSS, GDPR, NIST)

The LLM NEVER makes the security decision — that is always determined by the deterministic checks.

---

## Input Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | Yes | Migration job identifier |
| `tenant_id` | string | Yes | Multi-tenant identifier |
| `payload` | object | Yes | Migration payload or configuration dict to audit |
| `file_paths` | string[] | No | File paths to check against whitelist |
| `soql_queries` | string[] | No | SOQL query strings to validate |
| `has_sox_scope` | boolean | No | SOX compliance scope flag (default: false) |
| `request_llm_analysis` | boolean | No | Invoke LLM for findings analysis (default: false) |

---

## Output Schema (`SecurityAuditResult`)

| Field | Type | Description |
|-------|------|-------------|
| `audit_id` | UUID | Unique audit run identifier |
| `job_id` | string | Echoed from input |
| `tenant_id` | string | Echoed from input |
| `passed` | boolean | True when risk_score <= 0.7 AND no CRITICAL findings |
| `findings` | SecurityFinding[] | All security findings |
| `risk_score` | float (0.0–1.0) | Composite risk score |
| `gate_decision` | enum | `ALLOW` / `WARN` / `BLOCK` |
| `llm_analysis` | string | Optional LLM narrative; null when not requested |
| `checked_at` | ISO 8601 | Audit timestamp |
| `duration_ms` | integer | Wall-clock execution time |

### SecurityFinding Fields

| Field | Type | Description |
|-------|------|-------------|
| `finding_id` | UUID | Unique finding identifier |
| `finding_type` | enum | `PATH_TRAVERSAL` / `SOQL_INJECTION` / `HARDCODED_SECRET` / `PII_EXPOSURE` / `UNAUTHORIZED_PATH` |
| `severity` | enum | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` |
| `location` | string | Field path, file path, or query index |
| `description` | string | Human-readable description |
| `recommendation` | string | Specific remediation action |
| `evidence_snippet` | string | Redacted/masked evidence — never raw secrets or PII |

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
    print(f"Security gate: BLOCKED")
    for finding in result.findings:
        print(f"  [{finding.severity}] {finding.finding_type}: {finding.description}")
```

---

## When to Use

Use the security-audit skill when:
- Before execution-agent is invoked — always run as a blocking gate
- Auditing migration configuration payloads for secrets or PII
- Validating SOQL queries from dynamic configurations
- Checking file path configurations for path traversal risks

Do NOT use the security-audit skill when:
- Performing runtime behavioral security monitoring (use SIEM/WAF)
- Auditing Salesforce org-level permissions (use Salesforce Security Health Check)
- Scanning source code for vulnerabilities (use bandit/semgrep in CI pipeline)

---

## Performance

The four deterministic checks run entirely in-process. Typical audit latency:
- No LLM analysis: < 50ms
- With LLM analysis: 2–8 seconds (depends on model response time)

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECURITY_AGENT_MODEL` | `claude-sonnet-4-6` | Anthropic model ID (LLM analysis only) |
| `SECURITY_AGENT_MAX_TOKENS` | `4096` | Max tokens for LLM analysis response |
| `ANTHROPIC_API_KEY` | (required for LLM) | Anthropic API key |
