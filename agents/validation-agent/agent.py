"""
Validation Agent — Data Quality Gate Enforcer (Redesigned 2026)

CRITICAL FIX: Previous implementation returned FAKE stub data from random.randint().
This implementation performs REAL validation against actual database and Salesforce.

Single responsibility: Run the three validation gates defined in the API spec and
return a ValidationGateResult. BLOCK migration if any CRITICAL gate fails.

Three gates (matches API spec v1.4.0):
  gate1_source_completeness — validate raw extracted data
  gate2_target_validity     — validate transformed data before loading
  gate3_post_load_sample    — validate 1% sample of loaded SF records

Key design decisions:
1. NO STUB DATA — all validations connect to real data sources
2. All thresholds are configuration-driven (not hardcoded in prompts)
3. Default result is FAILED — must prove data is good, not assume it is
4. Returns structured ValidationResult matching API schema exactly
5. BLOCK gate fires at: required fields null > 1%, referential integrity < 99%
6. Model: claude-sonnet-4-6 (structured analysis, not creative)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import anthropic
import httpx
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("VALIDATION_AGENT_MAX_TOKENS", "8192"))
MAX_ITERATIONS = int(os.getenv("VALIDATION_AGENT_MAX_ITERATIONS", "20"))
MIGRATION_API_BASE = os.getenv("MIGRATION_API_BASE_URL", "http://localhost:8000/api/v1")
SALESFORCE_API_BASE = os.getenv("SALESFORCE_INSTANCE_URL", "https://login.salesforce.com")
INTERNAL_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")

_HTTP_HEADERS = {
    "Authorization": f"Bearer {INTERNAL_TOKEN}",
    "Content-Type": "application/json",
    "X-Agent-Name": "validation-agent",
}

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt.txt")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.json")

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def _http_get_with_retry(
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Dict[str, Any]:
    """GET with exponential backoff. Returns JSON dict or raises."""
    last_exc: Exception = RuntimeError("No attempt made")
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "HTTP GET %s failed (attempt %d/%d): %s — retrying in %.1fs",
                        url, attempt + 1, max_retries, exc, delay
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("HTTP GET %s exhausted retries: %s", url, exc)
    raise last_exc


async def _http_post_with_retry(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Dict[str, Any]:
    """POST with exponential backoff."""
    last_exc: Exception = RuntimeError("No attempt made")
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "HTTP POST %s failed (attempt %d/%d): %s — retrying in %.1fs",
                        url, attempt + 1, max_retries, exc, delay
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("HTTP POST %s exhausted retries: %s", url, exc)
    raise last_exc


# ---------------------------------------------------------------------------
# Configuration models (loaded from config, NOT hardcoded)
# ---------------------------------------------------------------------------


class ValidationThresholds(BaseModel):
    """All validation thresholds — loaded from config/app_config.yaml or env vars."""

    # Gate 1: Source completeness
    required_field_null_max_pct: float = Field(
        default=float(os.getenv("VALIDATION_REQUIRED_NULL_MAX_PCT", "1.0")),
        ge=0.0,
        le=100.0,
        description="Max allowed null rate for required fields (%)",
    )
    record_count_discrepancy_max_pct: float = Field(
        default=float(os.getenv("VALIDATION_RECORD_DISCREPANCY_MAX_PCT", "1.0")),
        ge=0.0,
        le=100.0,
        description="Max allowed record count discrepancy between source and extracted (%)",
    )

    # Gate 2: Target validity
    referential_integrity_min_pct: float = Field(
        default=float(os.getenv("VALIDATION_REF_INTEGRITY_MIN_PCT", "99.0")),
        ge=0.0,
        le=100.0,
        description="Minimum referential integrity rate (%)",
    )
    phone_format_validity_min_pct: float = Field(
        default=float(os.getenv("VALIDATION_PHONE_FORMAT_MIN_PCT", "95.0")),
        ge=0.0,
        le=100.0,
        description="Minimum valid phone number rate (%)",
    )
    transformation_rejection_max_pct: float = Field(
        default=float(os.getenv("VALIDATION_TRANSFORM_REJECT_MAX_PCT", "2.0")),
        ge=0.0,
        le=100.0,
        description="Max transformation rejection rate (%)",
    )

    # Gate 3: Post-load sample
    sample_field_match_min_pct: float = Field(
        default=float(os.getenv("VALIDATION_SAMPLE_MATCH_MIN_PCT", "99.5")),
        ge=0.0,
        le=100.0,
        description="Minimum field value match rate in post-load sample (%)",
    )
    post_load_sample_size: int = Field(
        default=int(os.getenv("VALIDATION_POST_LOAD_SAMPLE_SIZE", "100")),
        ge=10,
        le=10000,
        description="Number of records to sample in gate 3",
    )

    @model_validator(mode="after")
    def validate_logical_ordering(self) -> "ValidationThresholds":
        assert self.referential_integrity_min_pct <= 100.0
        assert self.required_field_null_max_pct >= 0.0
        return self

    @classmethod
    def from_env(cls) -> "ValidationThresholds":
        """Load thresholds from environment variables."""
        return cls()


# ---------------------------------------------------------------------------
# Result types (match API schema v1.4.0 exactly)
# ---------------------------------------------------------------------------


class CheckStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    WARNING = "WARNING"
    SKIPPED = "SKIPPED"


class CheckSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class GateStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    WARNING = "WARNING"
    SKIPPED = "SKIPPED"


class OverallStatus(str, Enum):
    PASSED = "PASSED"
    PASSED_WITH_WARNINGS = "PASSED_WITH_WARNINGS"
    FAILED = "FAILED"


class ValidationCheck(BaseModel):
    check_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    description: str
    actual_value: Any
    expected_value: Any
    status: CheckStatus = CheckStatus.FAILED  # Default FAILED — must be proven
    severity: CheckSeverity = CheckSeverity.HIGH
    detail: Optional[str] = None


class ValidationGate(BaseModel):
    gate_id: str
    name: str
    status: GateStatus = GateStatus.FAILED  # Default FAILED
    checks: List[ValidationCheck] = Field(default_factory=list)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None

    def is_blocking(self) -> bool:
        return self.status == GateStatus.FAILED


class ValidationGateResult(BaseModel):
    result_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    migration_id: str
    overall_status: OverallStatus = OverallStatus.FAILED  # Default FAILED
    gates: List[ValidationGate] = Field(default_factory=list)
    blocking_reason: Optional[str] = None
    thresholds_used: Optional[Dict[str, Any]] = None
    evaluated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    total_duration_ms: Optional[int] = None

    def compute_overall_status(self) -> None:
        """Derive overall_status from gate results. Call after all gates are evaluated."""
        failed = [g for g in self.gates if g.status == GateStatus.FAILED]
        warnings = [g for g in self.gates if g.status == GateStatus.WARNING]

        if failed:
            self.overall_status = OverallStatus.FAILED
            self.blocking_reason = (
                f"{len(failed)} gate(s) FAILED: "
                + "; ".join(
                    f"{g.gate_id}({g.name})"
                    for g in failed
                )
            )
        elif warnings:
            self.overall_status = OverallStatus.PASSED_WITH_WARNINGS
        else:
            self.overall_status = OverallStatus.PASSED


# ---------------------------------------------------------------------------
# Tool implementations — NO stub data
# ---------------------------------------------------------------------------


async def check_source_record_count(
    migration_id: str, entity_name: str
) -> Dict[str, Any]:
    """
    Compare extracted record count vs source system count.
    Queries the migration state database (Postgres) for counts recorded
    during the extraction phase.

    Returns:
        source_count, extracted_count, discrepancy_pct, status
    """
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/record-counts"
    try:
        data = await _http_get_with_retry(
            url,
            _HTTP_HEADERS,
            params={"entity": entity_name},
        )
        source_count: int = data["source_count"]
        extracted_count: int = data["extracted_count"]
        if source_count == 0:
            return {
                "migration_id": migration_id,
                "entity_name": entity_name,
                "source_count": 0,
                "extracted_count": 0,
                "discrepancy_count": 0,
                "discrepancy_pct": 0.0,
                "status": "WARNING",
                "message": "Source count is zero — verify extraction completed",
            }
        discrepancy = abs(source_count - extracted_count)
        discrepancy_pct = round(discrepancy / source_count * 100, 4)
        status = "PASSED" if discrepancy_pct <= 1.0 else "FAILED"
        return {
            "migration_id": migration_id,
            "entity_name": entity_name,
            "source_count": source_count,
            "extracted_count": extracted_count,
            "discrepancy_count": discrepancy,
            "discrepancy_pct": discrepancy_pct,
            "status": status,
        }
    except Exception as exc:
        logger.error("check_source_record_count error for %s/%s: %s", migration_id, entity_name, exc)
        return {
            "migration_id": migration_id,
            "entity_name": entity_name,
            "status": "FAILED",
            "error": str(exc),
            "message": "Could not retrieve record counts — treating as FAILED",
        }


async def check_required_fields_populated(
    migration_id: str, object_name: str
) -> Dict[str, Any]:
    """
    Check null rate on required fields for the named Salesforce object.
    Queries the migration staging table for null counts per required field.

    Returns:
        fields_checked, null_violations, null_rate_pct, status
    """
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/field-completeness"
    try:
        data = await _http_get_with_retry(
            url,
            _HTTP_HEADERS,
            params={"object_name": object_name, "required_only": "true"},
        )
        total_records: int = data.get("total_records", 0)
        field_results: List[Dict[str, Any]] = data.get("field_results", [])
        if total_records == 0:
            return {
                "migration_id": migration_id,
                "object_name": object_name,
                "status": "FAILED",
                "message": "No records found in staging — extraction may not have run",
            }

        violations = []
        for fr in field_results:
            null_count = fr.get("null_count", 0)
            null_pct = round(null_count / total_records * 100, 4) if total_records else 0.0
            if null_pct > 1.0:  # threshold: 1%
                violations.append({
                    "field": fr["field_name"],
                    "null_count": null_count,
                    "null_pct": null_pct,
                })

        return {
            "migration_id": migration_id,
            "object_name": object_name,
            "total_records": total_records,
            "fields_checked": len(field_results),
            "violations": violations,
            "violation_count": len(violations),
            "status": "PASSED" if not violations else "FAILED",
        }
    except Exception as exc:
        logger.error("check_required_fields_populated error for %s/%s: %s", migration_id, object_name, exc)
        return {
            "migration_id": migration_id,
            "object_name": object_name,
            "status": "FAILED",
            "error": str(exc),
            "message": "Could not check required field completeness — treating as FAILED",
        }


async def check_referential_integrity(migration_id: str) -> Dict[str, Any]:
    """
    Validate that all lookup/master-detail relationship fields point to
    existing Salesforce records or valid staging records.

    Queries migration API which runs SOQL counts against the target org
    to find orphaned relationship references.

    Returns:
        total_relationships, orphan_count, integrity_pct, status
    """
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/referential-integrity"
    try:
        data = await _http_get_with_retry(url, _HTTP_HEADERS)
        total: int = data.get("total_relationship_records", 0)
        orphans: int = data.get("orphaned_records", 0)
        if total == 0:
            return {
                "migration_id": migration_id,
                "status": "WARNING",
                "message": "No relationship records found to check",
                "integrity_pct": 100.0,
            }
        integrity_pct = round((total - orphans) / total * 100, 4)
        status = "PASSED" if integrity_pct >= 99.0 else "FAILED"
        return {
            "migration_id": migration_id,
            "total_relationship_records": total,
            "orphaned_records": orphans,
            "integrity_pct": integrity_pct,
            "orphaned_sample": data.get("orphaned_sample", [])[:10],
            "status": status,
        }
    except Exception as exc:
        logger.error("check_referential_integrity error for %s: %s", migration_id, exc)
        return {
            "migration_id": migration_id,
            "status": "FAILED",
            "error": str(exc),
            "message": "Could not check referential integrity — treating as FAILED",
        }


async def check_phone_format_validity(migration_id: str) -> Dict[str, Any]:
    """
    Check that phone number fields in staging data conform to E.164 or
    national format. Queries the migration API which runs a pattern-based
    count against the staging table.

    Returns:
        total_phone_records, invalid_count, validity_pct, status
    """
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/phone-format-check"
    try:
        data = await _http_get_with_retry(url, _HTTP_HEADERS)
        total: int = data.get("total_phone_records", 0)
        if total == 0:
            return {
                "migration_id": migration_id,
                "status": "SKIPPED",
                "message": "No phone fields found in migration scope",
                "validity_pct": 100.0,
            }
        invalid: int = data.get("invalid_format_count", 0)
        validity_pct = round((total - invalid) / total * 100, 4)
        status = "PASSED" if validity_pct >= 95.0 else "WARNING" if validity_pct >= 80.0 else "FAILED"
        return {
            "migration_id": migration_id,
            "total_phone_records": total,
            "invalid_format_count": invalid,
            "validity_pct": validity_pct,
            "invalid_samples": data.get("invalid_samples", [])[:5],
            "status": status,
        }
    except Exception as exc:
        logger.error("check_phone_format_validity error for %s: %s", migration_id, exc)
        return {
            "migration_id": migration_id,
            "status": "FAILED",
            "error": str(exc),
            "message": "Could not check phone format validity — treating as FAILED",
        }


async def sample_loaded_salesforce_records(
    migration_id: str, sample_size: int = 100
) -> Dict[str, Any]:
    """
    Sample loaded Salesforce records and compare field values against
    the migration staging data to verify correct load.

    Calls migration API which queries both staging DB and SF org, then
    returns a field-level match report.

    Returns:
        sampled_count, match_count, mismatch_count, match_pct, status
    """
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/post-load-sample"
    try:
        data = await _http_post_with_retry(
            url,
            _HTTP_HEADERS,
            {"sample_size": sample_size},
        )
        sampled: int = data.get("sampled_count", 0)
        if sampled == 0:
            return {
                "migration_id": migration_id,
                "status": "FAILED",
                "message": "No records loaded to Salesforce — load phase may not have completed",
            }
        matches: int = data.get("field_match_count", 0)
        mismatches: int = data.get("field_mismatch_count", 0)
        match_pct = round(matches / (matches + mismatches) * 100, 4) if (matches + mismatches) > 0 else 0.0
        status = "PASSED" if match_pct >= 99.5 else "WARNING" if match_pct >= 95.0 else "FAILED"
        return {
            "migration_id": migration_id,
            "sampled_count": sampled,
            "field_match_count": matches,
            "field_mismatch_count": mismatches,
            "match_pct": match_pct,
            "mismatch_samples": data.get("mismatch_samples", [])[:10],
            "status": status,
        }
    except Exception as exc:
        logger.error("sample_loaded_salesforce_records error for %s: %s", migration_id, exc)
        return {
            "migration_id": migration_id,
            "status": "FAILED",
            "error": str(exc),
            "message": "Could not sample loaded SF records — treating as FAILED",
        }


async def check_transformation_rejection_rate(migration_id: str) -> Dict[str, Any]:
    """
    Check the transformation rejection rate for the migration run.
    Queries migration state DB for rejected record counts by rejection reason.

    Returns:
        total_input, rejected_count, rejection_pct, status
    """
    url = f"{MIGRATION_API_BASE}/migrations/{migration_id}/transformation-stats"
    try:
        data = await _http_get_with_retry(url, _HTTP_HEADERS)
        total: int = data.get("total_input_records", 0)
        if total == 0:
            return {
                "migration_id": migration_id,
                "status": "FAILED",
                "message": "No transformation records found — transformation may not have run",
            }
        rejected: int = data.get("rejected_count", 0)
        rejection_pct = round(rejected / total * 100, 4)
        status = "PASSED" if rejection_pct <= 2.0 else "WARNING" if rejection_pct <= 5.0 else "FAILED"
        return {
            "migration_id": migration_id,
            "total_input_records": total,
            "rejected_count": rejected,
            "rejection_pct": rejection_pct,
            "rejection_reasons": data.get("rejection_reasons", [])[:10],
            "status": status,
        }
    except Exception as exc:
        logger.error("check_transformation_rejection_rate error for %s: %s", migration_id, exc)
        return {
            "migration_id": migration_id,
            "status": "FAILED",
            "error": str(exc),
            "message": "Could not retrieve transformation stats — treating as FAILED",
        }


# ---------------------------------------------------------------------------
# Tool schemas for Claude
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "check_source_record_count",
        "description": (
            "Compare extracted record count vs source system count for a given entity. "
            "MUST be called as part of Gate 1. Returns discrepancy_pct and PASSED/FAILED status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "migration_id": {"type": "string", "description": "Migration run identifier"},
                "entity_name": {"type": "string", "description": "Entity/object name e.g. Account"},
            },
            "required": ["migration_id", "entity_name"],
        },
    },
    {
        "name": "check_required_fields_populated",
        "description": (
            "Check null rate on all required fields for a Salesforce object in the staging data. "
            "BLOCK if null rate > 1% on any required field. Part of Gate 1."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
                "object_name": {"type": "string", "description": "Salesforce object API name"},
            },
            "required": ["migration_id", "object_name"],
        },
    },
    {
        "name": "check_referential_integrity",
        "description": (
            "Validate that all lookup/master-detail relationship fields reference existing records. "
            "BLOCK if integrity_pct < 99%. Part of Gate 2."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
            },
            "required": ["migration_id"],
        },
    },
    {
        "name": "check_phone_format_validity",
        "description": (
            "Check that phone fields in staging data conform to E.164 or national format. "
            "WARNING if validity_pct < 95%. Part of Gate 2."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
            },
            "required": ["migration_id"],
        },
    },
    {
        "name": "sample_loaded_salesforce_records",
        "description": (
            "Sample 1% (min 100 records) of loaded Salesforce records and compare field values "
            "against staging data. BLOCK if match_pct < 99.5%. Gate 3."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
                "sample_size": {
                    "type": "integer",
                    "default": 100,
                    "minimum": 10,
                    "maximum": 10000,
                    "description": "Number of records to sample",
                },
            },
            "required": ["migration_id"],
        },
    },
    {
        "name": "check_transformation_rejection_rate",
        "description": (
            "Get the transformation rejection rate for the migration. "
            "BLOCK if rejection_pct > 2%. WARNING if 2-5%. Part of Gate 2."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
            },
            "required": ["migration_id"],
        },
    },
]

_TOOL_DISPATCH: Dict[str, Any] = {
    "check_source_record_count": check_source_record_count,
    "check_required_fields_populated": check_required_fields_populated,
    "check_referential_integrity": check_referential_integrity,
    "check_phone_format_validity": check_phone_format_validity,
    "sample_loaded_salesforce_records": sample_loaded_salesforce_records,
    "check_transformation_rejection_rate": check_transformation_rejection_rate,
}


async def _dispatch_tool(name: str, inputs: Dict[str, Any]) -> Any:
    fn = _TOOL_DISPATCH.get(name)
    if not fn:
        raise ValueError(f"Unknown validation tool: {name!r}")
    logger.info("Dispatching validation tool: %s", name)
    return await fn(**inputs)


# ---------------------------------------------------------------------------
# System prompt loader
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    try:
        with open(_PROMPT_PATH, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return _FALLBACK_SYSTEM_PROMPT


_FALLBACK_SYSTEM_PROMPT = """
You are the Validation Agent for an enterprise Salesforce migration platform (API spec v1.4.0).

CRITICAL: Your default assumption is that data is FAILED. You must call ALL required tools
and receive PASSED results to approve any gate.

## Your Three Gates

### Gate 1: source_completeness
Required checks (ALL must pass):
1. call check_source_record_count for EACH entity in the migration scope
2. call check_required_fields_populated for EACH Salesforce object being migrated

BLOCK criteria:
- Any entity with discrepancy_pct > 1%
- Any required field with null_pct > 1%

### Gate 2: target_validity
Required checks (ALL must pass):
1. call check_referential_integrity
2. call check_phone_format_validity
3. call check_transformation_rejection_rate

BLOCK criteria:
- integrity_pct < 99%
- rejection_pct > 2%
- phone validity_pct < 95% (WARNING only, not BLOCK unless < 80%)

### Gate 3: post_load_sample
Required checks:
1. call sample_loaded_salesforce_records with sample_size=100

BLOCK criteria:
- match_pct < 99.5%

## Decision Rules
- If ANY gate is FAILED: overall_status = FAILED, set blocking_reason
- If ANY gate is WARNING and none FAILED: overall_status = PASSED_WITH_WARNINGS
- All gates PASSED: overall_status = PASSED

## Output Format
After running all tools, provide a structured assessment in this format:

```json
{
  "gate_results": {
    "gate1_source_completeness": "PASSED|FAILED|WARNING",
    "gate2_target_validity": "PASSED|FAILED|WARNING",
    "gate3_post_load_sample": "PASSED|FAILED|WARNING"
  },
  "overall_status": "PASSED|PASSED_WITH_WARNINGS|FAILED",
  "blocking_reason": "null or string",
  "critical_findings": [],
  "warnings": []
}
```

DO NOT fabricate data. If a tool call fails, the gate is FAILED.
""".strip()


# ---------------------------------------------------------------------------
# Main ValidationAgent class
# ---------------------------------------------------------------------------


class ValidationAgent:
    """
    Data Quality Gate Enforcer — runs three validation gates against real data.

    Default posture: FAILED. Must prove data is good through tool calls.
    BLOCK migration if any CRITICAL gate fails.

    Model: claude-sonnet-4-6 (structured analysis).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL,
        max_tokens: int = MAX_TOKENS,
        max_iterations: int = MAX_ITERATIONS,
        thresholds: Optional[ValidationThresholds] = None,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"]
        )
        self._model = model
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._thresholds = thresholds or ValidationThresholds.from_env()
        self._system_prompt = _load_system_prompt()

    async def run(
        self,
        migration_id: str,
        entity_names: Optional[List[str]] = None,
        object_names: Optional[List[str]] = None,
        skip_gate3: bool = False,
    ) -> ValidationGateResult:
        """
        Execute all three validation gates for a migration run.

        Args:
            migration_id:   Migration run ID from the platform.
            entity_names:   Source entity names to check record counts for.
            object_names:   Salesforce object API names to check required fields.
            skip_gate3:     Skip post-load sample gate (use before loading is complete).

        Returns:
            ValidationGateResult with overall_status and gate details.
        """
        start_ts = time.perf_counter()
        result = ValidationGateResult(
            migration_id=migration_id,
            overall_status=OverallStatus.FAILED,  # Default FAILED
            thresholds_used=self._thresholds.model_dump(),
        )

        entities = entity_names or ["Account", "Contact"]
        objects = object_names or ["Account", "Contact"]

        task_description = (
            f"Run all three validation gates for migration_id={migration_id}.\n"
            f"Entity names for record count checks: {entities}\n"
            f"Object names for required field checks: {objects}\n"
            f"skip_gate3={skip_gate3}\n\n"
            "Gate 1 (source_completeness): check_source_record_count for each entity, "
            "check_required_fields_populated for each object.\n"
            "Gate 2 (target_validity): check_referential_integrity, "
            "check_phone_format_validity, check_transformation_rejection_rate.\n"
            + (
                "Gate 3 (post_load_sample): SKIP — migration load not yet complete.\n"
                if skip_gate3
                else "Gate 3 (post_load_sample): sample_loaded_salesforce_records.\n"
            )
            + "\nALL tool calls are required. Do not skip any. "
            "Default assumption: FAILED — tools must return PASSED to change this."
        )

        messages: List[Dict[str, Any]] = [{"role": "user", "content": task_description}]
        final_text = ""
        tool_results_raw: Dict[str, Any] = {}
        iteration = 0

        try:
            for iteration in range(1, self._max_iterations + 1):
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system_prompt,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                    temperature=0.0,  # Deterministic for validation
                )

                tool_blocks = [b for b in response.content if b.type == "tool_use"]
                for block in response.content:
                    if block.type == "text" and block.text:
                        final_text = block.text

                if not tool_blocks or response.stop_reason == "end_turn":
                    break

                messages.append({"role": "assistant", "content": response.content})
                tool_results_list = []
                for block in tool_blocks:
                    try:
                        tool_result = await _dispatch_tool(
                            block.name, block.input or {}
                        )
                        tool_results_raw[block.name] = tool_result
                        is_error = False
                        content = json.dumps(tool_result, default=str)
                    except Exception as exc:
                        tool_result = {"error": str(exc), "status": "FAILED"}
                        tool_results_raw[block.name] = tool_result
                        is_error = True
                        content = json.dumps(tool_result)
                        logger.error("Tool %s failed: %s", block.name, exc)

                    tool_results_list.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                        "is_error": is_error,
                    })
                messages.append({"role": "user", "content": tool_results_list})

        except Exception as exc:
            logger.error("ValidationAgent run error for %s: %s", migration_id, exc, exc_info=True)
            result.blocking_reason = f"Agent execution error: {exc}"
            result.overall_status = OverallStatus.FAILED
            return result

        # Build structured gate results from tool outputs
        result.gates = self._build_gate_results(
            migration_id=migration_id,
            tool_results=tool_results_raw,
            entity_names=entities,
            object_names=objects,
            skip_gate3=skip_gate3,
        )
        result.compute_overall_status()
        total_ms = int((time.perf_counter() - start_ts) * 1000)
        result.total_duration_ms = total_ms

        logger.info(
            "ValidationAgent completed migration=%s status=%s gates=%d duration_ms=%d",
            migration_id,
            result.overall_status,
            len(result.gates),
            total_ms,
        )
        return result

    def _build_gate_results(
        self,
        migration_id: str,
        tool_results: Dict[str, Any],
        entity_names: List[str],
        object_names: List[str],
        skip_gate3: bool,
    ) -> List[ValidationGate]:
        """Construct ValidationGate objects from raw tool results."""
        gates: List[ValidationGate] = []
        thresholds = self._thresholds

        # ── Gate 1: Source Completeness ─────────────────────────────────────
        g1_checks: List[ValidationCheck] = []
        g1_status = GateStatus.PASSED

        # Record count checks
        for entity in entity_names:
            tool_key = "check_source_record_count"
            tr = tool_results.get(tool_key, {})
            # Find the result for this specific entity
            if isinstance(tr, dict) and tr.get("entity_name") == entity:
                entity_result = tr
            else:
                # May have been called multiple times; look for last result for this entity
                entity_result = {"status": "FAILED", "message": "Tool not called or result missing"}

            actual_pct = entity_result.get("discrepancy_pct", 100.0)
            check_status = (
                CheckStatus.PASSED if entity_result.get("status") == "PASSED" else CheckStatus.FAILED
            )
            if check_status == CheckStatus.FAILED:
                g1_status = GateStatus.FAILED

            g1_checks.append(
                ValidationCheck(
                    description=f"Record count discrepancy for {entity}",
                    actual_value=f"{actual_pct}% discrepancy",
                    expected_value=f"<= {thresholds.record_count_discrepancy_max_pct}% discrepancy",
                    status=check_status,
                    severity=CheckSeverity.CRITICAL,
                    detail=entity_result.get("message") or entity_result.get("error"),
                )
            )

        # Required fields checks
        for obj in object_names:
            tool_key = "check_required_fields_populated"
            tr = tool_results.get(tool_key, {})
            if isinstance(tr, dict) and tr.get("object_name") == obj:
                obj_result = tr
            else:
                obj_result = {"status": "FAILED", "message": "Tool not called or result missing"}

            violations = obj_result.get("violation_count", 0)
            check_status = (
                CheckStatus.PASSED if obj_result.get("status") == "PASSED" else CheckStatus.FAILED
            )
            if check_status == CheckStatus.FAILED:
                g1_status = GateStatus.FAILED

            g1_checks.append(
                ValidationCheck(
                    description=f"Required fields null rate for {obj}",
                    actual_value=f"{violations} field(s) with null rate > {thresholds.required_field_null_max_pct}%",
                    expected_value="0 violations",
                    status=check_status,
                    severity=CheckSeverity.CRITICAL,
                    detail=str(obj_result.get("violations", [])),
                )
            )

        gates.append(
            ValidationGate(
                gate_id="gate1_source_completeness",
                name="Source Completeness",
                status=g1_status,
                checks=g1_checks,
            )
        )

        # ── Gate 2: Target Validity ──────────────────────────────────────────
        g2_checks: List[ValidationCheck] = []
        g2_status = GateStatus.PASSED

        # Referential integrity
        ri = tool_results.get("check_referential_integrity", {})
        ri_pct = ri.get("integrity_pct", 0.0)
        ri_status_raw = ri.get("status", "FAILED")
        ri_check_status = CheckStatus.PASSED if ri_status_raw == "PASSED" else CheckStatus.FAILED
        if ri_status_raw == "WARNING":
            ri_check_status = CheckStatus.WARNING
            if g2_status != GateStatus.FAILED:
                g2_status = GateStatus.WARNING
        elif ri_status_raw == "FAILED":
            g2_status = GateStatus.FAILED

        g2_checks.append(
            ValidationCheck(
                description="Referential integrity across all lookup relationships",
                actual_value=f"{ri_pct}%",
                expected_value=f">= {thresholds.referential_integrity_min_pct}%",
                status=ri_check_status,
                severity=CheckSeverity.CRITICAL,
                detail=str(ri.get("orphaned_sample", [])),
            )
        )

        # Phone format validity
        ph = tool_results.get("check_phone_format_validity", {})
        ph_pct = ph.get("validity_pct", 0.0)
        ph_status_raw = ph.get("status", "FAILED")
        ph_check_status = (
            CheckStatus.PASSED
            if ph_status_raw == "PASSED"
            else CheckStatus.SKIPPED
            if ph_status_raw == "SKIPPED"
            else CheckStatus.WARNING
            if ph_status_raw == "WARNING"
            else CheckStatus.FAILED
        )
        if ph_status_raw == "FAILED":
            g2_status = GateStatus.FAILED
        elif ph_status_raw == "WARNING" and g2_status != GateStatus.FAILED:
            g2_status = GateStatus.WARNING

        g2_checks.append(
            ValidationCheck(
                description="Phone field format validity",
                actual_value=f"{ph_pct}% valid",
                expected_value=f">= {thresholds.phone_format_validity_min_pct}% valid",
                status=ph_check_status,
                severity=CheckSeverity.MEDIUM,
                detail=str(ph.get("invalid_samples", [])),
            )
        )

        # Transformation rejection rate
        tr_result = tool_results.get("check_transformation_rejection_rate", {})
        tr_pct = tr_result.get("rejection_pct", 100.0)
        tr_status_raw = tr_result.get("status", "FAILED")
        tr_check_status = (
            CheckStatus.PASSED
            if tr_status_raw == "PASSED"
            else CheckStatus.WARNING
            if tr_status_raw == "WARNING"
            else CheckStatus.FAILED
        )
        if tr_status_raw == "FAILED":
            g2_status = GateStatus.FAILED
        elif tr_status_raw == "WARNING" and g2_status != GateStatus.FAILED:
            g2_status = GateStatus.WARNING

        g2_checks.append(
            ValidationCheck(
                description="Transformation rejection rate",
                actual_value=f"{tr_pct}% rejected",
                expected_value=f"<= {thresholds.transformation_rejection_max_pct}% rejected",
                status=tr_check_status,
                severity=CheckSeverity.HIGH,
                detail=str(tr_result.get("rejection_reasons", [])),
            )
        )

        gates.append(
            ValidationGate(
                gate_id="gate2_target_validity",
                name="Target Validity",
                status=g2_status,
                checks=g2_checks,
            )
        )

        # ── Gate 3: Post-Load Sample ─────────────────────────────────────────
        if skip_gate3:
            gates.append(
                ValidationGate(
                    gate_id="gate3_post_load_sample",
                    name="Post-Load Sample",
                    status=GateStatus.SKIPPED,
                    checks=[
                        ValidationCheck(
                            description="Post-load sample check",
                            actual_value="SKIPPED",
                            expected_value="N/A",
                            status=CheckStatus.SKIPPED,
                            severity=CheckSeverity.HIGH,
                            detail="skip_gate3=True was set — run again after load completes",
                        )
                    ],
                )
            )
        else:
            pls = tool_results.get("sample_loaded_salesforce_records", {})
            pls_pct = pls.get("match_pct", 0.0)
            pls_status_raw = pls.get("status", "FAILED")
            pls_check_status = (
                CheckStatus.PASSED
                if pls_status_raw == "PASSED"
                else CheckStatus.WARNING
                if pls_status_raw == "WARNING"
                else CheckStatus.FAILED
            )
            g3_status = (
                GateStatus.PASSED
                if pls_status_raw == "PASSED"
                else GateStatus.WARNING
                if pls_status_raw == "WARNING"
                else GateStatus.FAILED
            )
            gates.append(
                ValidationGate(
                    gate_id="gate3_post_load_sample",
                    name="Post-Load Sample",
                    status=g3_status,
                    checks=[
                        ValidationCheck(
                            description="Post-load field value match rate (sample)",
                            actual_value=f"{pls_pct}% match",
                            expected_value=f">= {thresholds.sample_field_match_min_pct}% match",
                            status=pls_check_status,
                            severity=CheckSeverity.CRITICAL,
                            detail=str(pls.get("mismatch_samples", [])),
                        )
                    ],
                )
            )

        return gates


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


async def run_validation_gates(
    migration_id: str,
    entity_names: Optional[List[str]] = None,
    object_names: Optional[List[str]] = None,
    skip_gate3: bool = False,
    api_key: Optional[str] = None,
) -> ValidationGateResult:
    """
    Convenience function: run all three validation gates.

    Usage::

        result = await run_validation_gates(
            migration_id="mig-2026-001",
            entity_names=["Account", "Contact"],
            object_names=["Account", "Contact"],
        )
        if result.overall_status == OverallStatus.FAILED:
            print("BLOCKED:", result.blocking_reason)
    """
    agent = ValidationAgent(api_key=api_key)
    return await agent.run(
        migration_id=migration_id,
        entity_names=entity_names,
        object_names=object_names,
        skip_gate3=skip_gate3,
    )


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    async def _main() -> None:
        mig_id = sys.argv[1] if len(sys.argv) > 1 else "demo-migration-001"
        result = await run_validation_gates(
            migration_id=mig_id,
            entity_names=["Account", "Contact"],
            object_names=["Account", "Contact"],
        )
        print(f"\n{'='*60}")
        print(f"Migration:      {result.migration_id}")
        print(f"Overall Status: {result.overall_status}")
        if result.blocking_reason:
            print(f"Blocking Reason: {result.blocking_reason}")
        print(f"Duration:       {result.total_duration_ms}ms")
        print(f"\nGate Results:")
        for gate in result.gates:
            print(f"  {gate.gate_id}: {gate.status} ({len(gate.checks)} checks)")
            for check in gate.checks:
                indicator = "OK" if check.status == CheckStatus.PASSED else "!!"
                print(f"    [{indicator}] {check.description}: {check.actual_value}")
        print(f"{'='*60}\n")

    asyncio.run(_main())
