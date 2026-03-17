# documentation Skill — Usage Examples

## Example 1 — Valid Invocation

**Scenario:** Generate a post-migration runbook for the Account object migration.

**Input:**
```json
{
  "doc_type": "runbook",
  "subject": "Post-migration runbook for Account records — run-abc-123",
  "source_artifacts": [
    "docs/field_mappings/account_map.yaml",
    "agents/migration-agent/tools.py"
  ],
  "output_path": "monitoring/runbooks/account_migration_runbook.md"
}
```

**Expected Output (excerpt):**
```markdown
# Account Migration Runbook — run-abc-123

## Overview
Covers the migration of Account records from Oracle Siebel 8.1 to Salesforce Government Cloud+.

## Pre-checks
1. Confirm validation gate status: `ALLOW` required before execution.
2. Verify Salesforce API limits: DailyBulkApiRequests must be below 80% utilisation.
3. Check source Oracle DB connectivity via `GET /api/v1/health`.

## Execution Steps
| Step | Action | Agent |
|------|--------|-------|
| 1 | Run data validation on Account | validation-agent |
| 2 | Review ValidationResult grade (must be A or B) | orchestrator-agent |
| 3 | Submit migration run via execution-agent | execution-agent |
| 4 | Monitor Kafka lag on migration.accounts topic | debugging-agent |

## Rollback Procedure
Rollback requires explicit human confirmation. Submit request to execution-agent with action: ROLLBACK.

## Contacts
Escalate P1 incidents to the on-call engineer via PagerDuty.
```

**Result:** `ALLOWED` — output is sanitized, no credentials, no PII, path is within `monitoring/runbooks/`.

---

## Example 2 — Blocked by Validation (Credential in Input)

**Scenario:** An agent passes a live session token as part of the source artifact content.

**Input:**
```json
{
  "doc_type": "api_reference",
  "subject": "Salesforce Bulk API 2.0 integration reference",
  "source_artifacts": ["docs/sf_client.py"],
  "output_path": "docs/api_reference/bulk_api.md",
  "notes": "Use token='00D5g000004XYZ!AQEAQExample' for the example curl commands."
}
```

**Validation rule triggered:** `no_credential_exposure`
Pattern match: `token='00D5g...'`

**Expected Response:**
```json
{
  "status": "BLOCKED",
  "reason": "Input contains a credential pattern matching rule 'no_credential_exposure'. Remove live tokens from input. Use placeholder values such as '$SALESFORCE_SESSION_TOKEN' in examples.",
  "code": "SKILL_INPUT_BLOCKED"
}
```

**Result:** `BLOCKED` — input rejected before doc generation begins.

---

## Example 3 — Edge Case: Output Path at Boundary of Allowed Directories

**Scenario:** Caller requests documentation written directly to the `docs/` root (valid) versus attempting `docs/../agents/` (blocked).

**Valid sub-case input:**
```json
{
  "doc_type": "changelog",
  "subject": "v1.2.0 release changelog",
  "output_path": "docs/changelog/v1.2.0.md"
}
```

**Result:** `ALLOWED` — `docs/changelog/v1.2.0.md` resolves within `docs/`.

**Invalid sub-case input:**
```json
{
  "doc_type": "changelog",
  "subject": "v1.2.0 release changelog",
  "output_path": "docs/../agents/orchestrator/system_prompt.md"
}
```

**Validation rule triggered:** `write_path_enforcement`
Canonical path `agents/orchestrator/system_prompt.md` is not under `docs/`, `monitoring/runbooks/`, or `architecture/decisions/`.

**Expected Response:**
```json
{
  "status": "BLOCKED",
  "reason": "Output path resolves outside approved write directories after canonicalization. Path traversal detected ('..') — request rejected.",
  "code": "SKILL_PATH_TRAVERSAL_BLOCKED"
}
```

**Result:** `BLOCKED` — path traversal attempt is caught by canonical path check.
