"""
Tool implementations for the Data Validation Agent.

Tools
-----
validate_record_counts    – compare source vs target record counts
check_field_completeness  – analyse null/missing field rates per object
detect_anomalies          – statistical anomaly detection on numeric fields
compare_sample_records    – side-by-side comparison of legacy vs SF records
check_referential_integrity – validate that lookup/master-detail parents exist
validate_data_types       – check field values match expected Salesforce types
check_duplicate_records   – find potential duplicates in the target org
generate_report           – produce a formatted data quality report
run_custom_soql_check     – execute an arbitrary SOQL quality check
get_field_metadata        – retrieve Salesforce field metadata for an object
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import statistics
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

MIGRATION_API_BASE = os.getenv("MIGRATION_API_BASE_URL", "http://localhost:8000/api/v1")
INTERNAL_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "internal-service-token")

_HEADERS = {
    "Authorization": f"Bearer {INTERNAL_TOKEN}",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "validate_record_counts",
        "description": (
            "Compare the number of records in the legacy source system with the "
            "number successfully loaded into Salesforce for a given object type and "
            "migration run. Returns counts, discrepancy count, and percentage match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "object_type": {
                    "type": "string",
                    "description": "Salesforce object type, e.g. 'Account', 'Contact'",
                },
                "include_skipped": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include skipped records in the comparison",
                },
            },
            "required": ["run_id", "object_type"],
        },
    },
    {
        "name": "check_field_completeness",
        "description": (
            "Analyse field completeness (non-null rate) for all fields on a given "
            "Salesforce object. Returns per-field null rate and flags fields below "
            "the expected completeness threshold."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "run_id": {"type": "string"},
                "completeness_threshold": {
                    "type": "number",
                    "description": "Minimum acceptable non-null rate (0.0–1.0). Default 0.95.",
                    "default": 0.95,
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific fields to check. Omit to check all.",
                },
            },
            "required": ["object_type", "run_id"],
        },
    },
    {
        "name": "detect_anomalies",
        "description": (
            "Statistical anomaly detection on numeric and date fields. "
            "Identifies outliers, unexpected distributions, and values outside "
            "historical norms using Z-score and IQR methods."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "run_id": {"type": "string"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Numeric/date fields to analyse.",
                },
                "z_score_threshold": {
                    "type": "number",
                    "description": "Z-score above which a value is an outlier. Default 3.0.",
                    "default": 3.0,
                },
            },
            "required": ["object_type", "run_id", "fields"],
        },
    },
    {
        "name": "compare_sample_records",
        "description": (
            "Select a random or specified sample of records and compare the "
            "legacy source values side-by-side with the Salesforce target values. "
            "Returns field-level discrepancies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "object_type": {"type": "string"},
                "sample_size": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 500,
                },
                "legacy_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific legacy IDs to compare. Overrides sample_size.",
                },
            },
            "required": ["run_id", "object_type"],
        },
    },
    {
        "name": "check_referential_integrity",
        "description": (
            "Verify that all lookup and master-detail relationship fields point to "
            "existing Salesforce records. Returns the count and IDs of orphaned records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "run_id": {"type": "string"},
                "relationship_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relationship field API names to check, e.g. ['AccountId', 'OwnerId']",
                },
            },
            "required": ["object_type", "run_id"],
        },
    },
    {
        "name": "check_duplicate_records",
        "description": (
            "Detect potential duplicate records in Salesforce using configurable "
            "matching fields. Returns duplicate groups with similarity scores."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "run_id": {"type": "string"},
                "match_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fields to use for duplicate matching.",
                },
                "similarity_threshold": {
                    "type": "number",
                    "default": 0.9,
                    "minimum": 0.5,
                    "maximum": 1.0,
                },
            },
            "required": ["object_type", "run_id", "match_fields"],
        },
    },
    {
        "name": "validate_data_types",
        "description": (
            "Verify that field values in Salesforce conform to expected data types "
            "(e.g. emails are valid, phone numbers are formatted, dates are in range)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "run_id": {"type": "string"},
                "sample_size": {"type": "integer", "default": 1000},
            },
            "required": ["object_type", "run_id"],
        },
    },
    {
        "name": "generate_report",
        "description": (
            "Generate a comprehensive data quality report for a migration run. "
            "Aggregates results from all validation checks into a structured report "
            "with an overall quality score and prioritised recommendations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "object_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Objects to include. Omit for all.",
                },
                "report_format": {
                    "type": "string",
                    "enum": ["summary", "detailed", "executive"],
                    "default": "detailed",
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "run_custom_soql_check",
        "description": (
            "Execute a custom SOQL query in Salesforce for ad-hoc data quality checks. "
            "Returns the query result and any validation assessment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "soql": {
                    "type": "string",
                    "description": "SOQL SELECT statement. Must be read-only.",
                },
                "expected_count": {
                    "type": "integer",
                    "description": "Expected result count for pass/fail assertion.",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable description of what this check verifies.",
                },
            },
            "required": ["soql", "description"],
        },
    },
    {
        "name": "get_field_metadata",
        "description": (
            "Retrieve Salesforce field metadata for an object (types, required flags, "
            "picklist values, max lengths). Used to inform validation rules."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific field API names. Omit to get all fields.",
                },
            },
            "required": ["object_type"],
        },
    },
]


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


async def validate_record_counts(
    run_id: str, object_type: str, include_skipped: bool = False
) -> Dict[str, Any]:
    """Compare source vs target record counts."""
    # In production: query the migration database and Salesforce SOQL
    source_count = random.randint(10000, 50000)
    sf_count = source_count - random.randint(0, int(source_count * 0.02))
    skipped = random.randint(0, 20) if include_skipped else 0
    failed = source_count - sf_count - skipped
    match_pct = sf_count / source_count * 100 if source_count else 0

    return {
        "run_id": run_id,
        "object_type": object_type,
        "source_count": source_count,
        "salesforce_count": sf_count,
        "failed_count": failed,
        "skipped_count": skipped,
        "discrepancy_count": source_count - sf_count - skipped,
        "match_percentage": round(match_pct, 2),
        "status": "PASS" if match_pct >= 99.0 else "WARNING" if match_pct >= 95.0 else "FAIL",
    }


async def check_field_completeness(
    run_id: str,
    object_type: str,
    completeness_threshold: float = 0.95,
    fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Analyse field null rates."""
    default_fields = fields or [
        "Name", "Phone", "BillingCity", "BillingCountry",
        "Industry", "AnnualRevenue", "NumberOfEmployees",
        "Legacy_Customer_ID__c",
    ]
    results = []
    failing_fields = []
    for f in default_fields:
        rate = round(random.uniform(0.70, 1.0), 4)
        status = "PASS" if rate >= completeness_threshold else "FAIL"
        results.append({"field": f, "completeness_rate": rate, "status": status})
        if status == "FAIL":
            failing_fields.append({"field": f, "rate": rate})

    return {
        "run_id": run_id,
        "object_type": object_type,
        "threshold": completeness_threshold,
        "total_fields_checked": len(results),
        "failing_fields_count": len(failing_fields),
        "failing_fields": failing_fields,
        "field_results": results,
        "overall_status": "PASS" if not failing_fields else "FAIL",
    }


async def detect_anomalies(
    run_id: str,
    object_type: str,
    fields: List[str],
    z_score_threshold: float = 3.0,
) -> Dict[str, Any]:
    """Statistical anomaly detection on numeric fields."""
    anomalies = []
    for field in fields:
        values = [random.gauss(50000, 15000) for _ in range(1000)]
        mean = statistics.mean(values)
        std = statistics.stdev(values)
        outlier_count = sum(1 for v in values if abs(v - mean) / std > z_score_threshold)
        if outlier_count > 0:
            anomalies.append({
                "field": field,
                "outlier_count": outlier_count,
                "outlier_pct": round(outlier_count / len(values) * 100, 2),
                "mean": round(mean, 2),
                "std_dev": round(std, 2),
                "z_score_threshold": z_score_threshold,
                "example_outlier_values": [
                    round(mean + z_score_threshold * std * 1.5, 2),
                    round(mean - z_score_threshold * std * 1.2, 2),
                ],
            })

    return {
        "run_id": run_id,
        "object_type": object_type,
        "fields_analysed": fields,
        "anomalies_found": len(anomalies),
        "anomalies": anomalies,
        "status": "PASS" if not anomalies else "WARNING",
    }


async def compare_sample_records(
    run_id: str,
    object_type: str,
    sample_size: int = 20,
    legacy_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Side-by-side field comparison of legacy vs Salesforce records."""
    ids = legacy_ids or [f"CUST-{random.randint(10000, 99999)}" for _ in range(sample_size)]
    discrepancies = []
    exact_matches = 0

    for legacy_id in ids[:sample_size]:
        # Simulate occasional field discrepancy
        if random.random() < 0.05:
            discrepancies.append({
                "legacy_id": legacy_id,
                "field": random.choice(["Phone", "BillingCity", "AnnualRevenue"]),
                "legacy_value": "555-0100",
                "salesforce_value": "+1 555-0100",
                "discrepancy_type": "FORMAT_DIFFERENCE",
            })
        else:
            exact_matches += 1

    return {
        "run_id": run_id,
        "object_type": object_type,
        "sample_size": len(ids[:sample_size]),
        "exact_matches": exact_matches,
        "records_with_discrepancies": len(discrepancies),
        "discrepancy_rate": round(len(discrepancies) / len(ids[:sample_size]), 4),
        "discrepancies": discrepancies,
        "status": "PASS" if len(discrepancies) == 0 else "WARNING",
    }


async def check_referential_integrity(
    run_id: str,
    object_type: str,
    relationship_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Check for orphaned records in relationship fields."""
    fields = relationship_fields or ["AccountId", "OwnerId", "CreatedById"]
    results = []
    for field in fields:
        orphan_count = random.randint(0, 3)
        results.append({
            "field": field,
            "orphan_count": orphan_count,
            "sample_orphan_ids": [str(uuid.uuid4())[:8] for _ in range(min(orphan_count, 3))],
            "status": "PASS" if orphan_count == 0 else "FAIL",
        })
    total_orphans = sum(r["orphan_count"] for r in results)
    return {
        "run_id": run_id,
        "object_type": object_type,
        "fields_checked": fields,
        "total_orphaned_records": total_orphans,
        "field_results": results,
        "status": "PASS" if total_orphans == 0 else "FAIL",
    }


async def check_duplicate_records(
    run_id: str,
    object_type: str,
    match_fields: List[str],
    similarity_threshold: float = 0.9,
) -> Dict[str, Any]:
    """Detect duplicate records in Salesforce."""
    dup_groups = random.randint(0, 5)
    return {
        "run_id": run_id,
        "object_type": object_type,
        "match_fields": match_fields,
        "similarity_threshold": similarity_threshold,
        "duplicate_groups_found": dup_groups,
        "total_duplicate_records": dup_groups * 2,
        "status": "PASS" if dup_groups == 0 else "WARNING",
        "duplicate_groups": [
            {
                "group_id": str(uuid.uuid4())[:8],
                "record_ids": [str(uuid.uuid4())[:15], str(uuid.uuid4())[:15]],
                "similarity_score": round(random.uniform(similarity_threshold, 1.0), 3),
                "matching_fields": match_fields[:2],
            }
            for _ in range(dup_groups)
        ],
    }


async def validate_data_types(
    run_id: str, object_type: str, sample_size: int = 1000
) -> Dict[str, Any]:
    """Validate field value data types."""
    issues = []
    checks = [
        {"field": "Phone", "type": "phone", "invalid_count": random.randint(0, 5)},
        {"field": "Website", "type": "url", "invalid_count": random.randint(0, 2)},
        {"field": "AnnualRevenue", "type": "decimal", "invalid_count": 0},
    ]
    for check in checks:
        if check["invalid_count"] > 0:
            issues.append({
                "field": check["field"],
                "expected_type": check["type"],
                "invalid_count": check["invalid_count"],
                "invalid_rate": round(check["invalid_count"] / sample_size, 4),
            })

    return {
        "run_id": run_id,
        "object_type": object_type,
        "sample_size": sample_size,
        "fields_checked": len(checks),
        "fields_with_type_issues": len(issues),
        "issues": issues,
        "status": "PASS" if not issues else "WARNING",
    }


async def generate_report(
    run_id: str,
    object_types: Optional[List[str]] = None,
    report_format: str = "detailed",
) -> Dict[str, Any]:
    """Generate an aggregated data quality report."""
    objects = object_types or ["Account", "Contact"]
    object_scores: Dict[str, float] = {}
    for obj in objects:
        object_scores[obj] = round(random.uniform(0.88, 0.99), 3)

    overall_score = round(statistics.mean(object_scores.values()), 3)
    return {
        "report_id": str(uuid.uuid4()),
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format": report_format,
        "overall_quality_score": overall_score,
        "grade": "A" if overall_score >= 0.97 else "B" if overall_score >= 0.93 else "C",
        "object_scores": object_scores,
        "summary": {
            "critical_issues": 0,
            "high_issues": random.randint(0, 2),
            "medium_issues": random.randint(1, 5),
            "low_issues": random.randint(2, 8),
        },
        "top_recommendations": [
            "Normalise phone number formats to E.164 before next run",
            "Review NULL values in BillingCity for Accounts without billing address",
            "Validate external ID uniqueness in source before bulk upsert",
        ],
        "status": "PASS" if overall_score >= 0.95 else "REVIEW_REQUIRED",
    }


async def run_custom_soql_check(
    soql: str,
    description: str,
    expected_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute a custom SOQL quality check."""
    # In production: execute via SalesforceClient.query()
    actual_count = random.randint(0, 100)
    passed = True
    if expected_count is not None:
        passed = actual_count == expected_count

    return {
        "description": description,
        "soql": soql,
        "actual_count": actual_count,
        "expected_count": expected_count,
        "status": "PASS" if passed else "FAIL",
        "note": "Stub result – connect to real Salesforce org for live check",
    }


async def get_field_metadata(
    object_type: str,
    fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Retrieve Salesforce field metadata."""
    all_fields = fields or ["Name", "Phone", "BillingCity", "AnnualRevenue", "Industry"]
    metadata = []
    for field in all_fields:
        metadata.append({
            "api_name": field,
            "label": field.replace("__c", "").replace("_", " "),
            "type": "string",
            "required": field in ("Name",),
            "max_length": 255 if field not in ("AnnualRevenue",) else None,
            "updateable": True,
            "createable": True,
        })
    return {"object_type": object_type, "fields": metadata, "total": len(metadata)}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

TOOL_IMPLEMENTATIONS = {
    "validate_record_counts": validate_record_counts,
    "check_field_completeness": check_field_completeness,
    "detect_anomalies": detect_anomalies,
    "compare_sample_records": compare_sample_records,
    "check_referential_integrity": check_referential_integrity,
    "check_duplicate_records": check_duplicate_records,
    "validate_data_types": validate_data_types,
    "generate_report": generate_report,
    "run_custom_soql_check": run_custom_soql_check,
    "get_field_metadata": get_field_metadata,
}


async def dispatch_tool(tool_name: str, tool_input: Dict[str, Any]) -> Any:
    impl = TOOL_IMPLEMENTATIONS.get(tool_name)
    if not impl:
        raise ValueError(f"Unknown validation tool: {tool_name!r}")
    logger.info("Validation tool dispatched: %s", tool_name)
    return await impl(**tool_input)
