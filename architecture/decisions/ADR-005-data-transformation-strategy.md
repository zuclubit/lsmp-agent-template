# ADR-005: Data Transformation Strategy

**Status:** Accepted
**Date:** 2025-11-22
**Deciders:** Platform Architecture Team, Data Engineering Lead, Migration Domain Experts
**Supersedes:** N/A
**Tags:** `data-engineering`, `transformation`, `schema-registry`, `data-quality`, `migration`

---

## Table of Contents

1. [Context and Problem Statement](#1-context-and-problem-statement)
2. [Decision Drivers](#2-decision-drivers)
3. [Considered Options](#3-considered-options)
4. [Decision Outcome](#4-decision-outcome)
5. [Transformation Rule Engine Design](#5-transformation-rule-engine-design)
6. [Schema Registry Architecture](#6-schema-registry-architecture)
7. [Data Quality Validation Gates](#7-data-quality-validation-gates)
8. [Pros and Cons of Options](#8-pros-and-cons-of-options)
9. [Implementation Examples](#9-implementation-examples)
10. [Related Decisions](#10-related-decisions)

---

## 1. Context and Problem Statement

Legacy enterprise systems accumulate decades of organic data model evolution. The migration platform must translate from these legacy schemas to Salesforce's object model while preserving data fidelity. Key challenges observed across client engagements:

**Structural Complexity:**
- **Denormalized tables**: Oracle EBS stores party information across 40+ tables with complex join paths. A single Salesforce Account may require data from `HZ_PARTIES`, `HZ_PARTY_SITES`, `HZ_LOCATIONS`, `HZ_ORG_CONTACTS`, `HZ_CUST_ACCOUNTS`, and `HZ_CUST_ACCT_SITES`
- **Encoded values**: Legacy systems use integer or single-character codes (`STATUS = 'A'`, `TYPE = 3`) that must be mapped to Salesforce picklist values
- **Hierarchical flattening**: SAP organizational hierarchies (client → company code → plant → storage location) must be flattened into Salesforce Account hierarchy fields
- **Composite keys**: Legacy systems use multi-column natural keys that must be collapsed into Salesforce External ID fields for upsert operations
- **Soft deletes**: Records marked `DELETED = 1` or `ACTIVE_FLAG = 'N'` require conditional exclusion or transformation to Salesforce inactive status

**Data Quality Issues (Observed Across 15+ Client Migrations):**
- 23% of legacy customer records have null or malformed email addresses
- 41% of phone numbers are in non-standard formats
- 8–15% of address fields contain truncated data from column length constraints in legacy systems
- 3–7% of records have referential integrity violations (orphaned child records)
- Date fields stored as VARCHAR in formats ranging from 'MM/DD/YYYY' to Julian dates to UNIX timestamps

**Compliance Requirements:**
- PII transformations must be auditable — every field-level modification must be traceable to a specific rule version
- Transformation rules are governed artifacts — changes require approval workflow before deployment
- Some government clients require transformation logic to be independently auditable by their security officers

**Scale Requirements:**
- 50M–500M records per migration
- Transformation throughput must exceed 50,000 records/second to complete within acceptable migration windows
- Rule changes mid-migration must not require re-extraction from legacy systems

---

## 2. Decision Drivers

| Priority | Driver |
|----------|--------|
| P0 | Auditable transformations — every modification traced to a rule version |
| P0 | Rule changes deployable without re-extraction (Kafka replay capability) |
| P0 | Support for complex multi-table source joins in transformation logic |
| P1 | Throughput: 50,000+ records/second at peak |
| P1 | Data quality gates that halt migration before loading corrupt data to Salesforce |
| P1 | Non-programmer access — business analysts must be able to review transformation rules |
| P2 | Schema registry for managing Salesforce and legacy schema versions |
| P2 | Incremental rule deployment — update transformation for one object type without affecting others |
| P3 | Visual rule editor for business analysts (future capability) |

---

## 3. Considered Options

1. **Rule-Based Transformation Engine with Schema Registry** (selected)
2. **Direct SQL Transformation in Stored Procedures**
3. **Salesforce Data Loader with Mapping CSVs Only**
4. **Apache Spark with Python Transform Scripts**
5. **dbt (data build tool) with Staging Database**

---

## 4. Decision Outcome

**Chosen option: Rule-Based Transformation Engine with Confluent Schema Registry.**

A Python-based rule engine processes transformation rules defined in YAML configuration files. Rules are version-controlled, reviewed through standard PR workflow, and loaded at runtime from a versioned configuration store. The Confluent Schema Registry manages both legacy source schemas and Salesforce target schemas, enabling evolution tracking and compatibility enforcement.

### Positive Consequences

- **Auditable rules**: Every transformed field is tagged with the rule ID and rule version that produced it. Audit queries can reconstruct exactly what transformation logic was applied to any record.
- **Rule versioning and replay**: When a rule is corrected, the extraction events are replayed through the new rule version. The original extracted data is never re-queried from the legacy system.
- **Business-readable rules**: YAML rule definitions are readable by non-engineers. Client project managers and data stewards can review what transformations are applied to their data.
- **Incremental deployment**: Rules for `Account` objects can be updated independently of rules for `Contact` or `Opportunity` objects.
- **Data quality gates**: Quality validation runs on transformed records before they enter the loading queue. Configurable thresholds (e.g., fail if >1% of records have null required fields) can halt migration automatically.
- **Schema evolution tracking**: Confluent Schema Registry enforces backward compatibility on source schemas (CDC captures) and tracks Salesforce API version schema changes.

### Negative Consequences

- **Complex rules require engineering**: While simple field mappings are YAML-based, complex transformations (multi-table joins, conditional logic, date parsing) require Python transformation functions. Business analysts cannot write these independently.
- **Rule engine maintenance**: The Python rule engine is custom code that must be maintained, tested, and optimized. Performance optimization (vectorized operations via pandas/polars) requires data engineering expertise.
- **Schema registry operational dependency**: If Schema Registry is unavailable, new schemas cannot be registered and deserialization may fail for new schema versions. HA deployment is mandatory (ADR-003 addresses this).
- **YAML rule complexity at scale**: Large migrations (100+ object types, 500+ field mappings) produce large YAML rule sets. Navigation and governance tooling (rule search, impact analysis) must be built.

---

## 5. Transformation Rule Engine Design

### 5.1 Rule Definition Format (YAML)

```yaml
# transformation_rules/salesforce/account_from_oracle_ebs.yaml
rule_set_id: "oracle-ebs-to-sf-account-v3"
rule_set_version: "3.2.1"
approved_by: "migration-team@client.gov"
approved_date: "2025-11-20"
effective_date: "2025-11-22"

source:
  system: "oracle-ebs-prod"
  primary_entity: "HZ_PARTIES"
  joins:
    - entity: "HZ_CUST_ACCOUNTS"
      join_key: "HZ_PARTIES.PARTY_ID = HZ_CUST_ACCOUNTS.PARTY_ID"
      join_type: "LEFT OUTER"
    - entity: "HZ_PARTY_SITES"
      join_key: "HZ_PARTIES.PARTY_ID = HZ_PARTY_SITES.PARTY_ID"
      join_type: "LEFT OUTER"
      filter: "HZ_PARTY_SITES.PRIMARY_FLAG = 'Y'"
    - entity: "HZ_LOCATIONS"
      join_key: "HZ_PARTY_SITES.LOCATION_ID = HZ_LOCATIONS.LOCATION_ID"
      join_type: "LEFT OUTER"
  filter: "HZ_PARTIES.PARTY_TYPE = 'ORGANIZATION' AND HZ_PARTIES.STATUS = 'A'"

target:
  salesforce_object: "Account"
  operation: "upsert"
  external_id_field: "Legacy_ID__c"

field_mappings:
  - target_field: "Legacy_ID__c"
    source_expression: "HZ_PARTIES.PARTY_ID"
    transformation: "to_string"
    required: true
    null_action: "reject_record"  # Cannot migrate without a legacy ID

  - target_field: "Name"
    source_expression: "HZ_PARTIES.PARTY_NAME"
    transformation: "trim_whitespace | truncate(255)"
    required: true
    null_action: "reject_record"

  - target_field: "AccountNumber"
    source_expression: "HZ_CUST_ACCOUNTS.ACCOUNT_NUMBER"
    transformation: "trim_whitespace"
    required: false
    null_action: "leave_null"

  - target_field: "Type"
    source_expression: "HZ_PARTIES.CATEGORY_CODE"
    transformation:
      type: "lookup"
      lookup_table: "account_type_mapping"
      default: "Other"
    # lookup_table defined in shared/lookup_tables.yaml

  - target_field: "BillingStreet"
    source_expression: >
      concat_not_null(
        HZ_LOCATIONS.ADDRESS1,
        HZ_LOCATIONS.ADDRESS2,
        HZ_LOCATIONS.ADDRESS3,
        separator="\n"
      )
    transformation: "truncate(255)"
    required: false
    null_action: "leave_null"

  - target_field: "BillingCity"
    source_expression: "HZ_LOCATIONS.CITY"
    transformation: "trim_whitespace | title_case"
    required: false
    null_action: "leave_null"

  - target_field: "BillingState"
    source_expression: "HZ_LOCATIONS.STATE"
    transformation:
      type: "lookup"
      lookup_table: "us_state_code_mapping"
      default: null
    required: false
    null_action: "leave_null"

  - target_field: "BillingPostalCode"
    source_expression: "HZ_LOCATIONS.POSTAL_CODE"
    transformation:
      type: "regex_extract"
      pattern: "^(\\d{5})(?:-\\d{4})?$"
      group: 1
      on_no_match: "leave_null"
    required: false
    null_action: "leave_null"

  - target_field: "BillingCountryCode"
    source_expression: "HZ_LOCATIONS.COUNTRY"
    transformation:
      type: "lookup"
      lookup_table: "iso_country_code_mapping"
      default: "US"
    required: false
    null_action: "set_default"

  - target_field: "Phone"
    source_expression: "HZ_PARTIES.PHONE_NUMBER"
    transformation:
      type: "phone_normalize"
      default_country: "US"
      format: "E164"
      on_invalid: "leave_null"
    required: false
    null_action: "leave_null"

  - target_field: "Website"
    source_expression: "HZ_PARTIES.URL"
    transformation:
      type: "url_normalize"
      on_invalid: "leave_null"
    required: false
    null_action: "leave_null"

  - target_field: "CreatedDate_Legacy__c"
    source_expression: "HZ_PARTIES.CREATION_DATE"
    transformation:
      type: "date_parse"
      input_formats:
        - "%Y-%m-%d %H:%M:%S"
        - "%d-%b-%Y"
        - "%m/%d/%Y"
      output_format: "ISO8601"
      timezone: "America/New_York"
    required: false
    null_action: "leave_null"

data_quality_checks:
  - check_id: "account_name_not_empty"
    description: "Account Name must be non-empty after transformation"
    expression: "target.Name is not None and len(target.Name.strip()) > 0"
    severity: "ERROR"
    action_on_failure: "reject_record"

  - check_id: "valid_state_code"
    description: "BillingState must be valid US state code if country is US"
    expression: >
      target.BillingCountryCode != 'US' or
      target.BillingState is None or
      target.BillingState in VALID_US_STATES
    severity: "WARNING"
    action_on_failure: "flag_and_continue"

  - check_id: "phone_format_valid"
    description: "Phone number must be E.164 format if present"
    expression: >
      target.Phone is None or
      re.match(r'^\+[1-9]\d{1,14}$', target.Phone) is not None
    severity: "WARNING"
    action_on_failure: "nullify_field"
    nullify_fields: ["Phone"]

post_transform_hooks:
  - hook: "compute_account_health_score"
    module: "hooks.salesforce.account_enrichment"
    config:
      score_field: "Account_Health_Score__c"
      scoring_fields: ["Name", "Phone", "Website", "BillingStreet"]
```

### 5.2 Rule Engine Core Implementation

```python
# migration_platform/transformation/rule_engine.py

import yaml
import hashlib
import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime
import polars as pl

@dataclass
class TransformationResult:
    """Result of a single record transformation."""
    source_record_id: str
    target_record: Optional[Dict[str, Any]]
    rule_set_id: str
    rule_set_version: str
    applied_rules: List[str]
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    rejected: bool = False
    rejection_reason: Optional[str] = None
    checksum: Optional[str] = None

    def __post_init__(self):
        if self.target_record:
            self.checksum = hashlib.sha256(
                str(sorted(self.target_record.items())).encode()
            ).hexdigest()


class RuleEngine:
    """
    Rule-based transformation engine for legacy-to-Salesforce migrations.

    Loads rule definitions from YAML, applies field mappings and transformations,
    executes data quality checks, and produces auditable transformation results.
    """

    def __init__(self, rule_set_path: str, lookup_tables_path: str):
        self.rule_set = self._load_and_validate_rules(rule_set_path)
        self.lookup_tables = self._load_lookup_tables(lookup_tables_path)
        self._compile_transformations()

    def _load_and_validate_rules(self, path: str) -> dict:
        with open(path) as f:
            rule_set = yaml.safe_load(f)
        # Validate required fields
        required = ['rule_set_id', 'rule_set_version', 'approved_by',
                    'effective_date', 'source', 'target', 'field_mappings']
        missing = [k for k in required if k not in rule_set]
        if missing:
            raise ValueError(f"Rule set missing required fields: {missing}")
        return rule_set

    def transform_record(self, source_record: Dict[str, Any]) -> TransformationResult:
        """Transform a single source record according to loaded rules."""
        source_id = str(source_record.get('PARTY_ID', 'UNKNOWN'))
        target = {}
        applied_rules = []
        warnings = []
        errors = []

        for mapping in self.rule_set['field_mappings']:
            target_field = mapping['target_field']
            try:
                value = self._evaluate_expression(
                    mapping['source_expression'], source_record
                )
                transformed = self._apply_transformation(
                    value, mapping.get('transformation'), source_record
                )

                if transformed is None and mapping.get('required'):
                    action = mapping.get('null_action', 'reject_record')
                    if action == 'reject_record':
                        return TransformationResult(
                            source_record_id=source_id,
                            target_record=None,
                            rule_set_id=self.rule_set['rule_set_id'],
                            rule_set_version=self.rule_set['rule_set_version'],
                            applied_rules=applied_rules,
                            rejected=True,
                            rejection_reason=f"Required field {target_field} is null after transformation"
                        )
                    elif action == 'set_default':
                        transformed = mapping.get('default')

                target[target_field] = transformed
                applied_rules.append(f"{target_field}:rule_set_v{self.rule_set['rule_set_version']}")

            except Exception as e:
                errors.append(f"Field {target_field}: {str(e)}")

        # Run data quality checks
        for check in self.rule_set.get('data_quality_checks', []):
            passed, detail = self._run_quality_check(check, target, source_record)
            if not passed:
                if check['severity'] == 'ERROR' and check['action_on_failure'] == 'reject_record':
                    return TransformationResult(
                        source_record_id=source_id,
                        target_record=None,
                        rule_set_id=self.rule_set['rule_set_id'],
                        rule_set_version=self.rule_set['rule_set_version'],
                        applied_rules=applied_rules,
                        rejected=True,
                        rejection_reason=f"Quality check failed: {check['check_id']}: {detail}"
                    )
                elif check['action_on_failure'] == 'nullify_field':
                    for f in check.get('nullify_fields', []):
                        target[f] = None
                    warnings.append(f"Quality check {check['check_id']} failed; nullified fields")
                else:
                    warnings.append(f"Quality check {check['check_id']} warning: {detail}")

        return TransformationResult(
            source_record_id=source_id,
            target_record=target,
            rule_set_id=self.rule_set['rule_set_id'],
            rule_set_version=self.rule_set['rule_set_version'],
            applied_rules=applied_rules,
            warnings=warnings,
            errors=errors
        )
```

---

## 6. Schema Registry Architecture

### 6.1 Schema Management Strategy

The Confluent Schema Registry manages two schema namespaces:

**Legacy Source Schemas** (`legacy.{source_system}.{entity}`)
- Registered when a new source system is onboarded
- Versioned via CDC connector — schema changes in legacy systems trigger new schema registration
- Compatibility: BACKWARD (new consumers can read old data, enabling replay)

**Salesforce Target Schemas** (`salesforce.{org_alias}.{object}`)
- Refreshed from Salesforce Metadata API on each deployment
- Tracks API version changes (e.g., Spring '26 adding new required fields)
- Compatibility: FORWARD (alerts if new Salesforce schema breaks existing transformation rules)

### 6.2 Schema Compatibility Policy

```
Source schemas: BACKWARD_TRANSITIVE
  - Reason: Consumer (transformation engine) must be able to read all historical
    extraction events for replay. A schema change in the legacy system must not
    break replay of 6-month-old extraction events.

Target schemas: FORWARD_TRANSITIVE
  - Reason: When Salesforce adds new required fields, the transformation rules
    must be updated before migration. FORWARD compatibility check catches this
    at schema registration time (Salesforce release update) rather than at
    migration runtime.
```

---

## 7. Data Quality Validation Gates

### 7.1 Gate Architecture

Validation gates operate at three points in the pipeline:

```
GATE 1: Pre-Transformation (Source Completeness)
  - Run on raw extracted records
  - Check: Source schema completeness (required fields for transformation)
  - Check: Record count vs. expected (>1% deviation triggers warning)
  - Check: Referential integrity (orphan detection)
  - Failure action: Pause migration, alert, require human approval to continue

GATE 2: Post-Transformation (Target Validity)
  - Run on transformed records before Kafka validation topic
  - Check: Salesforce schema compliance (required fields, picklist values, lengths)
  - Check: Per-record data quality checks from rule YAML
  - Check: Duplicate detection (External ID uniqueness within batch)
  - Failure action: Per-record routing (valid → validated topic, invalid → failed topic)

GATE 3: Post-Load Sampling (Salesforce Confirmation)
  - Run after each batch loads to Salesforce
  - Sample 1% of loaded records (min 100, max 1000)
  - Query Salesforce API to verify field values match expected
  - Check: Cross-field consistency (e.g., State set correctly for Country)
  - Failure action: Pause migration, trigger AI validation agent, alert
```

### 7.2 Quality Metric Thresholds

| Metric | Warning Threshold | Error Threshold | Action |
|--------|------------------|-----------------|--------|
| Null rate for required fields | > 0.1% | > 1.0% | Pause migration |
| Transformation rejection rate | > 0.5% | > 2.0% | Alert + pause |
| Phone format failure rate | > 5% | > 20% | Warning (expected in legacy data) |
| Email format failure rate | > 10% | > 30% | Warning |
| Referential integrity violation rate | > 0.1% | > 1.0% | Pause migration |
| Post-load sample mismatch | > 0% | > 0.01% | Immediate pause + escalation |
| Duplicate External ID rate | > 0% | > 0% | Always reject — duplicates not tolerated |

### 7.3 Quality Report Schema

```json
{
  "quality_report": {
    "job_id": "mig-20251122-abc",
    "tenant_id": "gov-dod-001",
    "report_generated_at": "2025-11-22T14:30:00Z",
    "rule_set_id": "oracle-ebs-to-sf-account-v3",
    "rule_set_version": "3.2.1",
    "total_records_processed": 2847391,
    "records_successfully_transformed": 2789044,
    "records_rejected": 58347,
    "rejection_rate_pct": 2.05,
    "rejection_categories": {
      "required_field_null": 41203,
      "quality_check_failed": 12891,
      "schema_validation_error": 4253
    },
    "field_quality_summary": {
      "Name": {"null_count": 0, "truncated_count": 124, "quality_score": 99.99},
      "Phone": {"null_count": 412093, "invalid_format_count": 23841, "quality_score": 71.2},
      "BillingStreet": {"null_count": 89231, "truncated_count": 2847, "quality_score": 96.9}
    },
    "gate_results": {
      "gate1_source_completeness": "PASSED",
      "gate2_target_validity": "WARNING",
      "gate3_post_load_sample": "PASSED"
    }
  }
}
```

---

## 8. Pros and Cons of Options

### Option 2: Direct SQL Transformation in Stored Procedures

**Pros:** Database-native performance; DBA familiarity; ACID transactions.

**Cons:** Non-portable across databases; transformation logic buried in stored procedures (not reviewable by business stakeholders); no rule versioning; requires re-extraction for rule changes; performance degrades under concurrent migration loads; no data quality framework; cannot replay from Kafka.

**Verdict:** Rejected. Acceptable only for one-time migrations of < 1M records with dedicated DBA resources.

---

### Option 3: Salesforce Data Loader with Mapping CSVs Only

**Pros:** Zero custom code; Salesforce-provided tooling; simple field mapping UI.

**Cons:** No complex transformation logic (only simple field mappings); no conditional logic; no data cleansing; no quality gates; no audit trail below the field mapping level; single-threaded performance (max ~5,000 records/hour for data explorer); no Kafka integration; requires pre-processed data from external tool.

**Verdict:** Rejected as standalone solution. Used as a supplementary tool for small lookups and reference data loads, not primary migration path.

---

### Option 4: Apache Spark with Python Transform Scripts

**Pros:** Excellent throughput (distributed); Python ecosystem (pandas, numpy); rich data manipulation; Delta Lake for versioned datasets.

**Cons:** High infrastructure cost for Spark cluster; transformation logic in Python scripts (not YAML — not business-readable); no native rule versioning; complex to integrate with Kafka for streaming mode; operational overhead of Spark cluster; overkill for incremental migration; Delta Lake adds another storage system.

**Verdict:** Rejected. Performance benefits unnecessary given polars-based vectorized processing in the rule engine achieves required throughput. Spark complexity not justified.

---

### Option 5: dbt with Staging Database

**Pros:** Version-controlled SQL transformations; excellent lineage tracking; test framework built-in; wide community adoption; dbt Cloud has CI/CD.

**Cons:** Requires staging database (expensive for 500M records); transformation logic in SQL (not business-readable YAML); full database load before any records reach Salesforce (no streaming); no native Kafka integration; does not support Salesforce as a target without custom packages; staging database becomes bottleneck and single point of failure.

**Verdict:** Rejected as primary pipeline. Could be valuable for pre-extraction legacy data profiling and analytics. Not suitable for streaming migration pipeline.

---

## 9. Implementation Examples

### 9.1 Lookup Table Definition

```yaml
# transformation_rules/shared/lookup_tables.yaml
lookup_tables:
  account_type_mapping:
    source_values:
      "CUST": "Customer - Direct"
      "RESELL": "Channel Partner / Reseller"
      "PROSPECT": "Prospect"
      "PARTNER": "Technology Partner"
      "GOVT": "Government"
      "INTERNAL": null   # Internal accounts not migrated
    default: "Other"
    description: "Maps Oracle EBS party category codes to Salesforce Account Type picklist"

  us_state_code_mapping:
    description: "Maps full state names to 2-letter ISO codes"
    source_values:
      "ALABAMA": "AL"
      "ALASKA": "AK"
      "ARIZONA": "AZ"
      # ... (all 50 states + territories)
      "DISTRICT OF COLUMBIA": "DC"
    default: null
```

### 9.2 Custom Transformation Hook

```python
# hooks/salesforce/account_enrichment.py

def compute_account_health_score(target_record: dict, config: dict) -> dict:
    """
    Computes a completeness health score for the Account record.
    Higher score = more complete record = higher confidence in migration quality.
    """
    scoring_fields = config.get('scoring_fields', [])
    score_field = config.get('score_field', 'Account_Health_Score__c')

    filled = sum(
        1 for f in scoring_fields
        if target_record.get(f) is not None and str(target_record[f]).strip()
    )

    score = round((filled / len(scoring_fields)) * 100, 1) if scoring_fields else 0
    target_record[score_field] = score
    return target_record
```

---

## 10. Related Decisions

- [ADR-003: Event-Driven Architecture](./ADR-003-event-driven-architecture.md) — Transformation engine consumes from `extracted` Kafka topic and publishes to `transformed` topic
- [ADR-004: Zero Trust Security Model](./ADR-004-zero-trust-security-model.md) — Transformation service identity and access to rule configuration in Vault
- [ADR-007: AI Agent Orchestration](./ADR-007-ai-agent-orchestration.md) — AI agents assist with rule generation for complex transformations and DLQ remediation

---

*Last reviewed: 2025-11-22*
*Next review due: 2026-05-22*
*Document owner: Data Engineering Lead*
