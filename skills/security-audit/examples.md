# security-audit Skill — Usage Examples

## Example 1 — Valid Invocation

**Scenario:** Pre-deployment secrets scan on the migration agent integration directory.

**Input:**
```json
{
  "scan_targets": ["integrations/", "agents/migration-agent/"],
  "scan_type": "secrets",
  "severity_threshold": "HIGH"
}
```

**Expected Output:**
```json
{
  "scan_summary": {
    "files_scanned": 23,
    "critical_count": 0,
    "high_count": 1,
    "medium_count": 2,
    "low_count": 5,
    "risk_score": 0.41,
    "pass_security_gate": false
  },
  "findings": [
    {
      "severity": "HIGH",
      "type": "high_entropy_string",
      "file": "integrations/sf_client.py",
      "line": 47,
      "description": "High-entropy string (entropy=5.1) detected in variable 'default_timeout_token'. Verify this is not a hardcoded credential.",
      "recommendation": "Move to environment variable injected via Vault Agent sidecar."
    }
  ],
  "gate_decision": "BLOCK",
  "gate_reason": "1 HIGH finding present. Pass requires critical_count == 0 AND high_count == 0."
}
```

**Result:** `BLOCK` — one HIGH finding prevents gate passage. The risk_score (0.41) is below the 0.7 absolute threshold, but the high_count == 1 rule takes precedence.

---

## Example 2 — Blocked by Validation (Path Traversal in Scan Target)

**Scenario:** A caller attempts to scan secrets files outside the project root.

**Input:**
```json
{
  "scan_targets": ["agents/", "../../etc/passwd"],
  "scan_type": "secrets"
}
```

**Validation rule triggered:** `no_path_traversal`
Pattern match: `..` in scan target `../../etc/passwd`

**Expected Response:**
```json
{
  "status": "BLOCKED",
  "reason": "Scan target '../../etc/passwd' contains path traversal sequence '..'. All scan targets must be relative paths within PROJECT_ROOT.",
  "code": "SKILL_PATH_TRAVERSAL_BLOCKED"
}
```

**Result:** `BLOCKED` — the entire request is rejected. No files are scanned.

---

## Example 3 — Edge Case: Risk Score at the Blocking Threshold

**Scenario:** A full scan returns a risk_score of exactly 0.7 (the block threshold).

**Input:**
```json
{
  "scan_targets": ["agents/security-audit-agent/"],
  "scan_type": "full",
  "severity_threshold": "MEDIUM"
}
```

**Computed risk score scenario:**
- critical_count: 0
- high_count: 0
- medium_count: 7
- low_count: 4
- risk_score: 0.70 (exactly at threshold)

**Expected Output:**
```json
{
  "scan_summary": {
    "files_scanned": 8,
    "critical_count": 0,
    "high_count": 0,
    "medium_count": 7,
    "low_count": 4,
    "risk_score": 0.70,
    "pass_security_gate": false
  },
  "gate_decision": "BLOCK",
  "gate_reason": "risk_score 0.70 meets or exceeds the block threshold of 0.70. Remediate MEDIUM findings before proceeding."
}
```

**Result:** `BLOCK` — a risk_score of exactly 0.7 triggers a block (the condition is `>=`, not `>`). The caller must remediate medium findings to bring the score below the threshold.
