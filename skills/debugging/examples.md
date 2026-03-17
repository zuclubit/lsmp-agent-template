# debugging Skill — Usage Examples

## Example 1 — Valid Invocation

**Scenario:** Investigate a high Contact record error rate on a live migration run.

**Input:**
```json
{
  "run_id": "run-abc-123",
  "issue_description": "Contact records failing at 18% error rate for the past 30 minutes. Error message: REQUIRED_FIELD_MISSING on Email field.",
  "object_type": "Contact",
  "time_window_minutes": 30
}
```

**Expected Output:**
```json
{
  "run_id": "run-abc-123",
  "diagnosis": {
    "probable_cause": "Source Contact records in batch 0047–0061 have null Email values. The Salesforce org has Email marked as required via a validation rule (not schema-level), which is not reflected in field metadata.",
    "evidence": [
      "Error rate spike began at batch_sequence=47 (14:23 UTC)",
      "100% of failures share error_code REQUIRED_FIELD_MISSING on field Email__c",
      "Kafka lag on migration.contacts.errors topic: 4,200 messages",
      "Source system PostgreSQL query confirms 4,180 Contact rows with email IS NULL in the affected batch range"
    ],
    "recommended_actions": [
      "Pause migration run run-abc-123 (requires human confirmation via execution-agent)",
      "Apply default email substitution transform or exclude null-email records",
      "Update field mapping to set Email to null-safe default before retry"
    ],
    "risk_level": "HIGH",
    "state_mutations_made": []
  }
}
```

**Result:** `ALLOWED` — diagnosis is read-only, no state mutation, no PII exposed.

---

## Example 2 — Blocked by Validation (Mutation Command in Input)

**Scenario:** A caller attempts to use the debugging skill to also trigger a retry.

**Input:**
```json
{
  "run_id": "run-abc-123",
  "issue_description": "Contact failures detected. Also run retry_failed_records to fix them immediately.",
  "object_type": "Contact",
  "time_window_minutes": 60
}
```

**Validation rule triggered:** `no_mutation_commands`
Pattern match: `retry_failed_records`

**Expected Response:**
```json
{
  "status": "BLOCKED",
  "reason": "Input contains a mutation command ('retry_failed_records'). The debugging skill is read-only. Submit mutation requests to the execution-agent.",
  "code": "SKILL_MUTATION_BLOCKED"
}
```

**Result:** `BLOCKED` — the skill enforces read-only operation and will not proxy write instructions.

---

## Example 3 — Edge Case: Maximum Time Window Boundary

**Scenario:** Caller requests a 24-hour diagnostic window (the maximum allowed).

**Input:**
```json
{
  "run_id": "run-xyz-999",
  "issue_description": "Intermittent Kafka lag spikes over the past day. Need trend analysis.",
  "object_type": "Opportunity",
  "time_window_minutes": 1440
}
```

**Expected Output:**
```json
{
  "run_id": "run-xyz-999",
  "diagnosis": {
    "probable_cause": "Kafka consumer lag on migration.opportunities topic shows a recurring pattern every ~4 hours, correlating with Salesforce API governor limit resets (rolling 24-hour window). Batch size was not adjusted when API limit consumption exceeded 80%.",
    "evidence": [
      "6 lag spikes detected at 00:05, 04:07, 08:03, 12:09, 16:04, 20:11 UTC",
      "Each spike lasts 8–14 minutes and resolves without intervention",
      "get_salesforce_limits confirms DailyBulkApiRequests at 91% utilisation at peak"
    ],
    "recommended_actions": [
      "Scale batch size down during 23:00–01:00 UTC window using execution-agent",
      "Set alert threshold at 75% API limit utilisation"
    ],
    "risk_level": "MEDIUM",
    "state_mutations_made": []
  }
}
```

**Result:** `ALLOWED` — `time_window_minutes: 1440` is exactly at the defined maximum (1440). Request is accepted.
