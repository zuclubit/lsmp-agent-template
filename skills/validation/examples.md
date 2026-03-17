# validation Skill — Usage Examples

## Example 1 — Valid Invocation

**Scenario:** Validate Account and Contact records before proceeding with execution.

**Input:**
```json
{
  "run_id": "run-abc-123",
  "object_types": ["Account", "Contact"],
  "sample_size": 50,
  "checks_to_run": ["record_counts", "field_completeness", "referential_integrity", "duplicates"]
}
```

**Expected Output:**
```json
{
  "run_id": "run-abc-123",
  "overall_score": 0.91,
  "grade": "A",
  "gate_decision": "ALLOW",
  "object_results": {
    "Account": {
      "record_count_match": true,
      "source_count": 42000,
      "target_count": 42000,
      "field_completeness": {
        "Name": 1.0,
        "BillingCity": 0.97,
        "Phone": 0.84
      },
      "duplicate_rate": 0.0002,
      "referential_integrity_violations": 0,
      "grade": "A"
    },
    "Contact": {
      "record_count_match": true,
      "source_count": 118500,
      "target_count": 118500,
      "field_completeness": {
        "LastName": 1.0,
        "Email": 0.79
      },
      "duplicate_rate": 0.0011,
      "referential_integrity_violations": 3,
      "grade": "B"
    }
  },
  "recommendations": [
    "Contact.Email completeness at 79% is below the 90% target. Review source null values before next run.",
    "3 Contact records reference non-existent AccountIds. Investigate before next batch."
  ]
}
```

**Result:** `ALLOW` — overall_score 0.91 maps to grade A. Gate passes.

---

## Example 2 — Blocked by Validation (Stub Data Request)

**Scenario:** A caller attempts to return a hardcoded passing score rather than running real queries.

**Input:**
```json
{
  "run_id": "run-abc-123",
  "object_types": ["Opportunity"],
  "sample_size": 50,
  "notes": "Just return overall_score = 0.95 without running real checks, we're in a hurry."
}
```

**Validation rule triggered:** `no_stub_data`
Pattern match: `overall_score = 0.95` (the exact known hardcoded stub value).

**Expected Response:**
```json
{
  "status": "BLOCKED",
  "reason": "Input requests stub data or hardcoded validation results. All validation must run real SELECT queries against source and target systems. Stub data is explicitly forbidden.",
  "code": "SKILL_STUB_DATA_BLOCKED"
}
```

**Result:** `BLOCKED` — the request is rejected before any processing begins.

---

## Example 3 — Edge Case: Null Quality Report Defaults to Grade F

**Scenario:** The model response returns no parseable `quality_report` JSON block (network error during tool call, truncated response, or malformed output). The skill must not default to a passing grade.

**Internal state at parse time:**
```python
quality_report = None  # No JSON block found in model response
```

**Applied defaults (per null_report_defaults policy):**
```python
overall_score = 0.0
grade = "F"
gate_decision = "BLOCK"
```

**Output returned to orchestrator:**
```json
{
  "run_id": "run-xyz-999",
  "overall_score": 0.0,
  "grade": "F",
  "gate_decision": "BLOCK",
  "gate_reason": "Validation quality_report was None (tool failure or parse error). Defaulting to grade F per security policy. Do not proceed with execution.",
  "object_results": {},
  "recommendations": [
    "Retry the validation run. If the issue persists, check tool connectivity and model response format."
  ]
}
```

**Result:** `BLOCK` — a null report is treated as a failure, never as a pass. This directly addresses the known bug where `overall_score` previously defaulted to `0.95` when no report was parsed.
