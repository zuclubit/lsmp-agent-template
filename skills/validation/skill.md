---
name: validation
description: Three-gate data quality validation for Salesforce migration jobs
type: skill
version: 2.0.0
agent: validation-agent
---

# Validation Skill

**Version**: 2.0.0
**Agent**: validation-agent
**API Spec**: v1.4.0
**Last Updated**: 2026-03

---

## Purpose

The `validation` skill performs three mandatory data quality gates on Salesforce migration jobs.
Each gate is deterministic — it queries the migration API via real HTTP calls and evaluates
the results against configurable thresholds. No random or synthetic data is ever used.

This skill is a **blocking gate** in the pipeline:
- Gate 1 (`source_completeness`) must pass before extraction begins
- Gate 2 (`target_validity`) must pass before cut-over is attempted
- Gate 3 (`post_load_sample`) must pass before the job is marked complete

Default posture: all gates start FAILED. Gates must be proven via real data.

---

## The Three Gates

### Gate 1 — `source_completeness`

**When**: Before extraction begins (pre-flight check).

**Checks performed**:
- Source record count is non-zero and within the expected range
- Required fields (e.g. Name, Id) have a null rate below `required_field_null_max_pct` (default 1.0%)
- Referential integrity holds: all foreign key references resolve
- Phone format validity: US E.164 format compliance rate above threshold

**Blocks when**:
- Record count is zero
- Any required field null rate exceeds `required_field_null_max_pct`
- Referential integrity rate falls below `referential_integrity_min_pct` (default 99.0%)

**HTTP tools used**:
- `check_source_record_count` — GET /migrations/{id}/sources/{entity}/count
- `check_required_fields_populated` — GET /migrations/{id}/sources/{entity}/fields/null-rates
- `check_referential_integrity` — GET /migrations/{id}/sources/{entity}/referential-integrity
- `check_phone_format_validity` — GET /migrations/{id}/sources/{entity}/phone-formats

---

### Gate 2 — `target_validity`

**When**: After load completes, before cut-over.

**Checks performed**:
- Target record count is within `count_tolerance_pct` (default 0.1%) of source count
- Loaded records pass Salesforce object validation rules
- Transformation rejection rate is below `transform_rejection_max_pct` (default 0.5%)

**Blocks when**:
- Target count deviates from source by more than `count_tolerance_pct`
- Transformation rejection rate exceeds `transform_rejection_max_pct`

**HTTP tools used**:
- `check_required_fields_populated` — on target objects
- `check_transformation_rejection_rate` — GET /migrations/{id}/transformations/{entity}/rejections

---

### Gate 3 — `post_load_sample`

**When**: After cut-over, as final quality verification.

**Checks performed**:
- Sample N records from Salesforce (default 50, configurable up to 10,000)
- Compare sampled records field-by-field against source
- Field match rate must meet `sample_match_rate_min` (default 99.9%)

**Blocks when**:
- Zero records can be sampled from the loaded Salesforce object
- Field match rate falls below `sample_match_rate_min`

**HTTP tools used**:
- `sample_loaded_salesforce_records` — POST /migrations/{id}/salesforce/{entity}/sample

**May be skipped**: When `skip_gate3=True` is passed (e.g. for test/dry-run invocations).

---

## Threshold Configuration

All thresholds are configurable via environment variables with sensible defaults:

| Threshold | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `required_field_null_max_pct` | `VALIDATION_REQUIRED_NULL_MAX_PCT` | 1.0 | Max % null in required fields |
| `referential_integrity_min_pct` | `VALIDATION_REF_INTEGRITY_MIN_PCT` | 99.0 | Min % of FK references that resolve |
| `count_tolerance_pct` | `VALIDATION_COUNT_TOLERANCE_PCT` | 0.1 | Max % deviation: source vs target count |
| `phone_format_min_pct` | `VALIDATION_PHONE_FORMAT_MIN_PCT` | 95.0 | Min % of phone values in E.164 format |
| `transform_rejection_max_pct` | `VALIDATION_TRANSFORM_REJECT_MAX_PCT` | 0.5 | Max % of records rejected during transform |
| `sample_size` | `VALIDATION_SAMPLE_SIZE` | 50 | Records to sample in Gate 3 |
| `sample_match_rate_min` | `VALIDATION_SAMPLE_MATCH_MIN` | 99.9 | Min field match rate in Gate 3 sample |

---

## Gate Decision Logic

```
For each gate:
1. Run all checks via real HTTP calls with 3-retry exponential backoff
2. Evaluate each check against its threshold
3. Gate status:
   - PASSED             — all checks pass
   - PASSED_WITH_WARNINGS — non-blocking checks fail
   - FAILED             — any blocking check fails

Overall validation status:
   - PASSED             — all gates are PASSED
   - PASSED_WITH_WARNINGS — all gates pass but at least one has warnings
   - FAILED             — any gate is FAILED
```

A migration proceeds to the next phase only when all gates are `PASSED` or `PASSED_WITH_WARNINGS`.
A `FAILED` gate halts the pipeline and blocks execution.

---

## HTTP Retry Policy

All validation tool HTTP calls use exponential backoff:
- Max retries: 3
- Delays: 1s -> 2s -> 4s
- Timeout: 30 seconds per request
- On exhausted retries: the check is marked FAILED with `status=HTTP_ERROR`

---

## Output (`ValidationGateResult`)

| Field | Type | Description |
|-------|------|-------------|
| `validation_id` | UUID | Unique validation run identifier |
| `migration_id` | string | Echoed from input |
| `overall_status` | enum | `PASSED` / `PASSED_WITH_WARNINGS` / `FAILED` |
| `gates` | ValidationGate[] | Results for each gate that was evaluated |
| `entity_names` | string[] | Entity names that were validated |
| `validated_at` | ISO 8601 | Timestamp of validation run |
| `duration_ms` | integer | Wall-clock execution time |

### ValidationGate Fields

| Field | Type | Description |
|-------|------|-------------|
| `gate_id` | UUID | Unique gate run identifier |
| `gate_name` | string | `source_completeness` / `target_validity` / `post_load_sample` |
| `status` | enum | `PASSED` / `PASSED_WITH_WARNINGS` / `FAILED` |
| `checks` | ValidationCheck[] | Individual check results |
| `evaluated_at` | ISO 8601 | When this gate was evaluated |

### ValidationCheck Fields

| Field | Type | Description |
|-------|------|-------------|
| `check_name` | string | Name of the check (e.g. `record_count`) |
| `status` | enum | `PASSED` / `FAILED` / `WARNING` / `SKIPPED` / `ERROR` |
| `entity_name` | string | Entity this check applied to |
| `measured_value` | any | Actual measured value |
| `threshold_value` | any | Threshold the value was compared against |
| `message` | string | Human-readable explanation |
| `is_blocking` | bool | Whether a failure of this check blocks the gate |

---

## Example Invocation

```python
from agents.validation_agent.agent import run_validation

result = await run_validation(
    migration_id="mig-001",
    entity_names=["Account", "Contact", "Opportunity"],
    object_names=["Account", "Contact", "Opportunity"],
    skip_gate3=False,
)

if result.overall_status == "PASSED":
    print(f"Validation passed — {len(result.gates)} gates checked")
else:
    for gate in result.gates:
        if gate.status == "FAILED":
            for check in gate.checks:
                if check.status == "FAILED":
                    print(f"[FAIL] {gate.gate_name}.{check.check_name}: {check.message}")
```

---

## When to Use

Use the validation skill when:
- Before extraction — gate1 verifies source completeness
- After load — gate2 verifies target record counts and transformation quality
- After cut-over — gate3 spot-checks loaded records against source

Do NOT use the validation skill when:
- Performing security checks on payloads (use security-audit skill)
- Checking configuration files for secrets (use security-agent)
- Running schema migrations (that is the execution agent's responsibility)

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIGRATION_API_BASE_URL` | `http://localhost:8000/api/v1` | Migration API base URL |
| `INTERNAL_SERVICE_TOKEN` | `""` | Bearer token for API calls |
| `ANTHROPIC_API_KEY` | (required for LLM features) | Anthropic API key |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Model for LLM-assisted analysis |
| All threshold vars | (see Thresholds table above) | Threshold overrides |
