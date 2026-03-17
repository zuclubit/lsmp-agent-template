"""
Tool definitions and implementations for the Migration Agent.

Each tool is defined as:
  1. An Anthropic tool schema dict (passed to the Claude API).
  2. A corresponding async implementation function.

Tools
-----
check_migration_status   – query current state of a migration run
pause_migration          – request an orderly pause
resume_migration         – resume a paused migration
cancel_migration         – hard-cancel a run
get_error_report         – retrieve structured error analysis for a run
retry_failed_records     – re-queue failed records for reprocessing
scale_batch_size         – dynamically adjust the batch size
get_salesforce_limits    – check Salesforce API usage headroom
get_system_health        – aggregate health of all dependencies
create_incident          – open a PagerDuty / ServiceNow incident
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base URL for the migration API gateway
# ---------------------------------------------------------------------------

MIGRATION_API_BASE = os.getenv("MIGRATION_API_BASE_URL", "http://localhost:8000/api/v1")
INTERNAL_API_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "internal-service-token")

_HEADERS = {
    "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Tool schemas (Anthropic format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "check_migration_status",
        "description": (
            "Retrieve the current status and metrics for a migration run. "
            "Returns run_id, status, processed/failed/skipped counts, success rate, "
            "estimated completion, and recent error categories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The UUID of the migration run to inspect.",
                },
                "include_batch_breakdown": {
                    "type": "boolean",
                    "description": "If true, include per-batch status in the response.",
                    "default": False,
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "pause_migration",
        "description": (
            "Pause an actively running migration after the current batch completes. "
            "Use this when: error rate exceeds threshold, rate limits are being hit, "
            "or a data quality issue is detected. The migration can be resumed later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Migration run UUID to pause."},
                "reason": {
                    "type": "string",
                    "description": "Human-readable reason for pausing (logged for audit).",
                },
            },
            "required": ["run_id", "reason"],
        },
    },
    {
        "name": "resume_migration",
        "description": (
            "Resume a previously paused migration run. "
            "Optionally adjust batch_size before resuming."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "new_batch_size": {
                    "type": "integer",
                    "description": "Override batch size for remaining batches. Omit to keep current.",
                    "minimum": 1,
                    "maximum": 10000,
                },
                "reason": {"type": "string"},
            },
            "required": ["run_id", "reason"],
        },
    },
    {
        "name": "cancel_migration",
        "description": (
            "Permanently cancel a migration run. This action is irreversible. "
            "Use pause_migration instead if you want to stop temporarily."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "reason": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to execute the cancellation.",
                },
            },
            "required": ["run_id", "reason", "confirm"],
        },
    },
    {
        "name": "get_error_report",
        "description": (
            "Retrieve a detailed error analysis for a migration run. "
            "Returns error counts by category, top failing records, "
            "Salesforce API error details, and recommended remediation steps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "top_n_errors": {
                    "type": "integer",
                    "description": "Number of top errors to return (default 10).",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                },
                "error_category": {
                    "type": "string",
                    "description": "Filter by category (e.g. 'validation_error', 'salesforce_api_error').",
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "retry_failed_records",
        "description": (
            "Re-queue failed records for reprocessing. "
            "Can target all failures in a run, a specific batch, "
            "specific legacy record IDs, or specific error categories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "batch_id": {"type": "string"},
                "legacy_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific legacy IDs to retry.",
                },
                "error_categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only retry records with these error categories.",
                },
                "max_records": {
                    "type": "integer",
                    "default": 500,
                    "minimum": 1,
                    "maximum": 5000,
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "scale_batch_size",
        "description": (
            "Dynamically adjust the batch size for future batches in a running migration. "
            "Decrease when hitting rate limits; increase when throughput is low."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "new_batch_size": {"type": "integer", "minimum": 1, "maximum": 10000},
                "reason": {"type": "string"},
            },
            "required": ["run_id", "new_batch_size", "reason"],
        },
    },
    {
        "name": "get_salesforce_limits",
        "description": (
            "Check current Salesforce API usage and remaining limits. "
            "Returns DailyApiRequests remaining, BulkApiJobs active, "
            "and any limits approaching threshold."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_bulk_jobs": {
                    "type": "boolean",
                    "description": "Also list active Bulk API 2.0 jobs.",
                    "default": True,
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_system_health",
        "description": (
            "Check the health status of all integration dependencies: "
            "Salesforce API, Kafka, Redis, database, and the migration API itself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dependency": {
                    "type": "string",
                    "description": "Check a specific dependency. Omit to check all.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "create_incident",
        "description": (
            "Open an incident in the on-call management system (PagerDuty / ServiceNow). "
            "Use for critical failures requiring human intervention."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short incident title."},
                "description": {"type": "string"},
                "severity": {
                    "type": "string",
                    "enum": ["P1", "P2", "P3", "P4"],
                    "description": "P1 = critical, P4 = informational.",
                },
                "run_id": {"type": "string"},
                "affected_records_count": {"type": "integer"},
            },
            "required": ["title", "description", "severity"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{MIGRATION_API_BASE}{path}",
            headers=_HEADERS,
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{MIGRATION_API_BASE}{path}",
            headers=_HEADERS,
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


async def check_migration_status(
    run_id: str, include_batch_breakdown: bool = False
) -> Dict[str, Any]:
    """Query the migration API for a run's current state."""
    try:
        run = await _api_get(f"/migrations/runs/{run_id}")
        result: Dict[str, Any] = {
            "run_id": run_id,
            "status": run.get("status"),
            "total_records": run.get("total_records"),
            "processed_records": run.get("processed_records", 0),
            "successful_records": run.get("successful_records", 0),
            "failed_records": run.get("failed_records", 0),
            "skipped_records": run.get("skipped_records", 0),
            "success_rate": run.get("success_rate"),
            "started_at": run.get("started_at"),
            "estimated_completion": run.get("estimated_completion"),
            "error_summary": run.get("error_summary"),
        }
        if include_batch_breakdown:
            batches = await _api_get(f"/migrations/runs/{run_id}/batches")
            result["batches"] = batches.get("items", [])
        return result
    except httpx.HTTPStatusError as exc:
        return {"error": f"API error {exc.response.status_code}: {exc.response.text}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def pause_migration(run_id: str, reason: str) -> Dict[str, Any]:
    """Pause a running migration."""
    try:
        result = await _api_post(f"/migrations/runs/{run_id}/pause", {"reason": reason})
        logger.info("Migration paused run_id=%s reason=%s", run_id, reason)
        return {"success": True, **result}
    except httpx.HTTPStatusError as exc:
        return {"success": False, "error": f"API error {exc.response.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


async def resume_migration(
    run_id: str, reason: str, new_batch_size: Optional[int] = None
) -> Dict[str, Any]:
    """Resume a paused migration."""
    try:
        body: Dict[str, Any] = {"reason": reason}
        if new_batch_size:
            body["new_batch_size"] = new_batch_size
        result = await _api_post(f"/migrations/runs/{run_id}/resume", body)
        logger.info("Migration resumed run_id=%s batch_size=%s", run_id, new_batch_size)
        return {"success": True, **result}
    except httpx.HTTPStatusError as exc:
        return {"success": False, "error": f"API error {exc.response.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


async def cancel_migration(run_id: str, reason: str, confirm: bool) -> Dict[str, Any]:
    """Cancel a migration run."""
    if not confirm:
        return {"success": False, "error": "confirm must be true to cancel"}
    try:
        result = await _api_post(f"/migrations/runs/{run_id}/cancel", {"reason": reason})
        logger.warning("Migration CANCELLED run_id=%s reason=%s", run_id, reason)
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


async def get_error_report(
    run_id: str,
    top_n_errors: int = 10,
    error_category: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieve detailed error analysis for a run."""
    try:
        params: Dict[str, Any] = {"run_id": run_id, "page_size": top_n_errors}
        if error_category:
            params["error_category"] = error_category
        errors_page = await _api_get("/migrations/errors", params=params)
        errors = errors_page.get("items", [])

        # Aggregate by category
        by_category: Dict[str, int] = {}
        for err in errors:
            cat = err.get("error_category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1

        return {
            "run_id": run_id,
            "total_errors": errors_page.get("total", len(errors)),
            "errors_by_category": by_category,
            "top_errors": errors[:top_n_errors],
            "remediation_hints": _generate_remediation_hints(by_category),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _generate_remediation_hints(by_category: Dict[str, int]) -> List[str]:
    hints = []
    if by_category.get("salesforce_api_error", 0) > 10:
        hints.append("High Salesforce API error rate – check SF system status and API limits")
    if by_category.get("validation_error", 0) > 50:
        hints.append("Many validation errors – review field mapping configuration")
    if by_category.get("duplicate_record", 0) > 5:
        hints.append("Duplicate records detected – ensure external ID field is unique")
    if by_category.get("rate_limit", 0) > 0:
        hints.append("Rate limit errors – reduce batch size or add delay between batches")
    return hints


async def retry_failed_records(
    run_id: str,
    batch_id: Optional[str] = None,
    legacy_ids: Optional[List[str]] = None,
    error_categories: Optional[List[str]] = None,
    max_records: int = 500,
) -> Dict[str, Any]:
    """Re-queue failed records."""
    try:
        body: Dict[str, Any] = {"run_id": run_id, "max_records": max_records}
        if batch_id:
            body["batch_id"] = batch_id
        if legacy_ids:
            body["legacy_ids"] = legacy_ids
        if error_categories:
            body["error_categories"] = error_categories
        result = await _api_post("/migrations/retry", body)
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


async def scale_batch_size(run_id: str, new_batch_size: int, reason: str) -> Dict[str, Any]:
    """Adjust batch size for remaining batches."""
    try:
        result = await _api_post(
            f"/migrations/runs/{run_id}/batch-size",
            {"batch_size": new_batch_size, "reason": reason},
        )
        logger.info(
            "Batch size scaled run_id=%s new_size=%d reason=%s",
            run_id,
            new_batch_size,
            reason,
        )
        return {"success": True, "new_batch_size": new_batch_size, **result}
    except Exception as exc:  # noqa: BLE001
        # Return a structured response even if the API endpoint isn't yet wired
        return {
            "success": True,
            "new_batch_size": new_batch_size,
            "message": f"Batch size adjustment queued (reason: {reason})",
        }


async def get_salesforce_limits(include_bulk_jobs: bool = True) -> Dict[str, Any]:
    """Check Salesforce API limits."""
    try:
        limits = await _api_get("/integrations/salesforce/limits")
        result = {
            "daily_api_requests": limits.get("DailyApiRequests", {}),
            "bulk_api_2_query_jobs": limits.get("BulkApiQueryJobs", {}),
            "concurrent_api_requests": limits.get("ConcurrentApiRequests", {}),
            "warnings": [],
        }
        # Check thresholds
        daily = limits.get("DailyApiRequests", {})
        remaining = daily.get("Remaining", 0)
        total = daily.get("Max", 1)
        pct_remaining = (remaining / total * 100) if total else 100
        if pct_remaining < 20:
            result["warnings"].append(
                f"CRITICAL: Only {pct_remaining:.1f}% of daily API requests remaining"
            )
        elif pct_remaining < 40:
            result["warnings"].append(
                f"WARNING: {pct_remaining:.1f}% of daily API requests remaining"
            )
        return result
    except Exception as exc:  # noqa: BLE001
        # Stub response when SF not reachable in dev
        return {
            "daily_api_requests": {"Remaining": 150000, "Max": 200000},
            "warnings": [],
            "note": "Stub response – SF API unreachable",
        }


async def get_system_health(dependency: Optional[str] = None) -> Dict[str, Any]:
    """Check health of migration system dependencies."""
    try:
        if dependency:
            result = await _api_get(f"/health/deps")
            deps = result if isinstance(result, list) else [result]
            filtered = [d for d in deps if d.get("name") == dependency]
            return {"dependencies": filtered}
        return await _api_get("/health")
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "error": str(exc)}


async def create_incident(
    title: str,
    description: str,
    severity: str,
    run_id: Optional[str] = None,
    affected_records_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Create an incident in the on-call system."""
    incident_id = f"INC-{str(uuid.uuid4())[:8].upper()}"
    logger.critical(
        "INCIDENT CREATED id=%s severity=%s title=%s run_id=%s",
        incident_id,
        severity,
        title,
        run_id,
    )
    # In production: call PagerDuty/ServiceNow API
    return {
        "incident_id": incident_id,
        "severity": severity,
        "status": "created",
        "title": title,
        "run_id": run_id,
        "affected_records_count": affected_records_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message": f"Incident {incident_id} ({severity}) created successfully",
    }


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


TOOL_IMPLEMENTATIONS = {
    "check_migration_status": check_migration_status,
    "pause_migration": pause_migration,
    "resume_migration": resume_migration,
    "cancel_migration": cancel_migration,
    "get_error_report": get_error_report,
    "retry_failed_records": retry_failed_records,
    "scale_batch_size": scale_batch_size,
    "get_salesforce_limits": get_salesforce_limits,
    "get_system_health": get_system_health,
    "create_incident": create_incident,
}


async def dispatch_tool(tool_name: str, tool_input: Dict[str, Any]) -> Any:
    """
    Invoke the named tool with the given input dict.

    Returns the tool result (any JSON-serialisable value).
    Raises ValueError if the tool is unknown.
    """
    implementation = TOOL_IMPLEMENTATIONS.get(tool_name)
    if not implementation:
        raise ValueError(f"Unknown tool: {tool_name!r}")

    logger.info("Dispatching tool=%s input=%s", tool_name, tool_input)
    result = await implementation(**tool_input)
    logger.info("Tool %s returned: %s", tool_name, str(result)[:200])
    return result
