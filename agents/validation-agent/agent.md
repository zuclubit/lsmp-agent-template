# Validation Agent

**Version:** 2.0.0 (Redesigned 2026)
**Model:** claude-sonnet-4-6
**Owner:** Platform Engineering — Data Quality Guild
**API Spec:** v1.4.0

---

## Purpose

The Validation Agent is the data quality gate enforcer for the enterprise migration platform. Its single responsibility is to run three deterministic validation gates against real data sources and return a `ValidationGateResult`. It BLOCKS migration progress if any CRITICAL gate fails.

This agent replaces the previous `data-validation-agent` which returned stub data from `random.randint()`.

---

## Architecture

```
ValidationAgent.run(migration_id)
    │
    ├── Gate 1: source_completeness
    │       ├── check_source_record_count (per entity)
    │       └── check_required_fields_populated (per SF object)
    │
    ├── Gate 2: target_validity
    │       ├── check_referential_integrity
    │       ├── check_phone_format_validity
    │       └── check_transformation_rejection_rate
    │
    └── Gate 3: post_load_sample
            └── sample_loaded_salesforce_records
```

All tool calls hit real HTTP endpoints on the Migration API (`MIGRATION_API_BASE_URL`) with exponential backoff retry (3 attempts max). No tool result is fabricated.

---

## Security Properties

- **Default posture: FAILED** — a result must be proven, not assumed
- Temperature = 0.0 — deterministic, non-creative analysis only
- All HTTP calls carry `Authorization: Bearer <INTERNAL_SERVICE_TOKEN>`
- No PII is stored in agent state; only counts, percentages, and IDs are handled

---

## Three Gates

### Gate 1: `gate1_source_completeness`

Validates raw extracted data before transformation.

| Check | Tool | Block Threshold |
|-------|------|----------------|
| Record count discrepancy per entity | `check_source_record_count` | discrepancy_pct > 1% |
| Required field null rate per object | `check_required_fields_populated` | null_pct > 1% on any required field |

### Gate 2: `gate2_target_validity`

Validates transformed data before loading to Salesforce.

| Check | Tool | Block Threshold | Warning Threshold |
|-------|------|----------------|-------------------|
| Referential integrity | `check_referential_integrity` | integrity_pct < 99% | integrity_pct < 99.5% |
| Phone format validity | `check_phone_format_validity` | validity_pct < 80% | validity_pct < 95% |
| Transformation rejection rate | `check_transformation_rejection_rate` | rejection_pct > 5% | rejection_pct > 2% |

### Gate 3: `gate3_post_load_sample`

Validates a sample of records after they have been loaded to Salesforce.

| Check | Tool | Block Threshold |
|-------|------|----------------|
| Post-load field match rate | `sample_loaded_salesforce_records` | match_pct < 99.5% |

Gate 3 can be skipped (set `skip_gate3=True`) when called before the load phase completes.

---

## Decision Matrix

| Gate 1 | Gate 2 | Gate 3 | Overall Status |
|--------|--------|--------|----------------|
| PASSED | PASSED | PASSED | PASSED |
| PASSED | PASSED | WARNING | PASSED_WITH_WARNINGS |
| PASSED | WARNING | PASSED | PASSED_WITH_WARNINGS |
| FAILED | any | any | FAILED (BLOCKED) |
| any | FAILED | any | FAILED (BLOCKED) |
| any | any | FAILED | FAILED (BLOCKED) |

---

## Configuration

All thresholds are loaded from environment variables — nothing is hardcoded:

| Env Variable | Default | Description |
|---|---|---|
| `VALIDATION_REQUIRED_NULL_MAX_PCT` | `1.0` | Max null rate for required fields |
| `VALIDATION_RECORD_DISCREPANCY_MAX_PCT` | `1.0` | Max record count discrepancy |
| `VALIDATION_REF_INTEGRITY_MIN_PCT` | `99.0` | Min referential integrity rate |
| `VALIDATION_PHONE_FORMAT_MIN_PCT` | `95.0` | Min valid phone format rate |
| `VALIDATION_TRANSFORM_REJECT_MAX_PCT` | `2.0` | Max transformation rejection rate |
| `VALIDATION_SAMPLE_MATCH_MIN_PCT` | `99.5` | Min post-load field match rate |
| `VALIDATION_POST_LOAD_SAMPLE_SIZE` | `100` | Records to sample in gate 3 |
| `MIGRATION_API_BASE_URL` | `http://localhost:8000/api/v1` | Migration API base |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Claude model to use |

---

## Usage

```python
from agents.validation_agent.agent import run_validation_gates, OverallStatus

result = await run_validation_gates(
    migration_id="mig-2026-001",
    entity_names=["Account", "Contact"],
    object_names=["Account", "Contact"],
)

if result.overall_status == OverallStatus.FAILED:
    raise MigrationBlockedError(result.blocking_reason)
```

---

## Error Handling

- If a tool HTTP call fails after 3 retries: the check status is set to FAILED
- If the agent loop throws an unhandled exception: the overall status is FAILED
- The result always contains a `blocking_reason` when `overall_status == FAILED`

---

## Limitations

- Requires the Migration API to be running and accessible
- Requires Salesforce connectivity for Gate 3 (post-load sample)
- Gate 3 results depend on the load phase having completed; set `skip_gate3=True` if running before load
- The agent does not retry individual tool calls across the orchestration boundary — retries happen within each HTTP call (3 attempts)
- Not designed for real-time streaming validation; processes a completed migration batch
