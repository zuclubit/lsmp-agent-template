"""
FastAPI router – Migration status and control endpoints.

Endpoints
---------
GET  /migrations/runs                   – list migration runs
POST /migrations/runs                   – start a new migration run
GET  /migrations/runs/{run_id}          – get run details
POST /migrations/runs/{run_id}/pause    – pause a running migration
POST /migrations/runs/{run_id}/resume   – resume a paused migration
POST /migrations/runs/{run_id}/cancel   – cancel a migration run
GET  /migrations/runs/{run_id}/batches  – list batches for a run
GET  /migrations/batches/{batch_id}     – get batch details
GET  /migrations/records/{legacy_id}    – get per-record migration status
GET  /migrations/stats                  – aggregate statistics
GET  /migrations/errors                 – list errors with filtering
POST /migrations/retry                  – retry failed records
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/migrations", tags=["migrations"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ObjectTypeFilter(str, Enum):
    ACCOUNT = "Account"
    CONTACT = "Contact"
    OPPORTUNITY = "Opportunity"
    LEAD = "Lead"
    ALL = "all"


class StartMigrationRequest(BaseModel):
    object_types: List[str] = Field(
        ..., description="Salesforce object types to migrate", min_length=1
    )
    batch_size: int = Field(default=200, ge=1, le=10000)
    dry_run: bool = Field(
        default=False, description="Validate and transform without writing to Salesforce"
    )
    filter_query: Optional[str] = Field(
        None,
        description="Optional SQL/SOQL-like filter to restrict source records",
        max_length=1000,
    )
    priority: int = Field(default=5, ge=1, le=10)
    notify_on_completion: Optional[str] = Field(
        None, description="Email or webhook URL to notify on completion"
    )
    schedule_at: Optional[datetime] = Field(
        None, description="ISO-8601 datetime to start; immediate if null"
    )


class MigrationRunResponse(BaseModel):
    run_id: str
    status: RunStatus
    object_types: List[str]
    dry_run: bool
    batch_size: int
    total_records: Optional[int] = None
    processed_records: int = 0
    successful_records: int = 0
    failed_records: int = 0
    skipped_records: int = 0
    success_rate: Optional[float] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    estimated_completion: Optional[datetime] = None
    created_by: Optional[str] = None
    error_summary: Optional[str] = None


class BatchResponse(BaseModel):
    batch_id: str
    run_id: str
    object_type: str
    status: str
    total_records: int
    processed_records: int
    failed_records: int
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None


class RecordStatusResponse(BaseModel):
    legacy_id: str
    salesforce_id: Optional[str] = None
    object_type: str
    status: str
    last_attempt_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    batch_id: Optional[str] = None
    run_id: Optional[str] = None


class MigrationStats(BaseModel):
    total_runs: int
    active_runs: int
    total_records_migrated: int
    total_records_failed: int
    overall_success_rate: float
    avg_throughput_per_hour: float
    top_error_categories: List[Dict[str, Any]]
    by_object_type: Dict[str, Dict[str, Any]]
    last_updated: datetime


class ErrorRecord(BaseModel):
    error_id: str
    run_id: str
    batch_id: Optional[str] = None
    legacy_id: str
    object_type: str
    phase: str
    error_message: str
    error_category: str
    occurred_at: datetime
    retry_count: int
    is_retryable: bool


class RetryRequest(BaseModel):
    run_id: Optional[str] = None
    batch_id: Optional[str] = None
    legacy_ids: Optional[List[str]] = Field(None, max_length=1000)
    error_categories: Optional[List[str]] = None
    max_records: int = Field(default=500, ge=1, le=5000)


class RetryResponse(BaseModel):
    retry_run_id: str
    records_queued: int
    estimated_start: datetime


class PaginatedResponse(BaseModel):
    items: List[Any]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool


# ---------------------------------------------------------------------------
# In-memory stub store (replace with real DB service in production)
# ---------------------------------------------------------------------------


_RUNS_STORE: Dict[str, Dict[str, Any]] = {}
_BATCHES_STORE: Dict[str, Dict[str, Any]] = {}
_RECORDS_STORE: Dict[str, Dict[str, Any]] = {}


def _get_run_or_404(run_id: str) -> Dict[str, Any]:
    run = _RUNS_STORE.get(run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Migration run '{run_id}' not found"},
        )
    return run


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/runs",
    response_model=PaginatedResponse,
    summary="List migration runs",
)
async def list_migration_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[RunStatus] = Query(None, alias="status"),
    object_type: ObjectTypeFilter = Query(ObjectTypeFilter.ALL),
) -> PaginatedResponse:
    """Return a paginated list of migration runs, optionally filtered."""
    runs = list(_RUNS_STORE.values())

    if status_filter:
        runs = [r for r in runs if r["status"] == status_filter.value]
    if object_type != ObjectTypeFilter.ALL:
        runs = [r for r in runs if object_type.value in r.get("object_types", [])]

    runs.sort(key=lambda r: r["created_at"], reverse=True)
    total = len(runs)
    start = (page - 1) * page_size
    page_items = runs[start : start + page_size]

    return PaginatedResponse(
        items=page_items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-total // page_size)),
        has_next=start + page_size < total,
        has_prev=page > 1,
    )


@router.post(
    "/runs",
    response_model=MigrationRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new migration run",
)
async def start_migration_run(
    body: StartMigrationRequest,
    background_tasks: BackgroundTasks,
) -> MigrationRunResponse:
    """
    Enqueue a new migration run.

    The run is created in PENDING state and transitions to RUNNING once
    the orchestrator picks it up (async via background task).
    """
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    run = {
        "run_id": run_id,
        "status": RunStatus.PENDING.value,
        "object_types": body.object_types,
        "dry_run": body.dry_run,
        "batch_size": body.batch_size,
        "processed_records": 0,
        "successful_records": 0,
        "failed_records": 0,
        "skipped_records": 0,
        "created_at": now.isoformat(),
        "filter_query": body.filter_query,
        "priority": body.priority,
    }
    _RUNS_STORE[run_id] = run

    logger.info(
        "Migration run created run_id=%s objects=%s dry_run=%s",
        run_id,
        body.object_types,
        body.dry_run,
    )

    # In production, this would enqueue a message to the orchestrator service
    background_tasks.add_task(_simulate_run_start, run_id)

    return MigrationRunResponse(
        run_id=run_id,
        status=RunStatus.PENDING,
        object_types=body.object_types,
        dry_run=body.dry_run,
        batch_size=body.batch_size,
        created_at=now,
    )


@router.get(
    "/runs/{run_id}",
    response_model=MigrationRunResponse,
    summary="Get migration run details",
)
async def get_migration_run(run_id: str) -> MigrationRunResponse:
    """Return full details for a single migration run."""
    run = _get_run_or_404(run_id)
    return MigrationRunResponse(**{
        **run,
        "status": RunStatus(run["status"]),
        "created_at": datetime.fromisoformat(run["created_at"]),
    })


@router.post(
    "/runs/{run_id}/pause",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Pause a running migration",
)
async def pause_migration_run(run_id: str) -> Dict[str, str]:
    """
    Signal the orchestrator to pause the migration after the current batch
    completes.  In-flight API calls to Salesforce are not interrupted.
    """
    run = _get_run_or_404(run_id)
    if run["status"] != RunStatus.RUNNING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "invalid_state",
                "message": f"Run is '{run['status']}', must be 'running' to pause",
            },
        )
    _RUNS_STORE[run_id]["status"] = RunStatus.PAUSED.value
    logger.info("Migration run paused run_id=%s", run_id)
    return {"run_id": run_id, "status": RunStatus.PAUSED.value, "message": "Run paused"}


@router.post(
    "/runs/{run_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resume a paused migration",
)
async def resume_migration_run(run_id: str) -> Dict[str, str]:
    """Resume a previously paused migration run."""
    run = _get_run_or_404(run_id)
    if run["status"] != RunStatus.PAUSED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "invalid_state",
                "message": f"Run is '{run['status']}', must be 'paused' to resume",
            },
        )
    _RUNS_STORE[run_id]["status"] = RunStatus.RUNNING.value
    logger.info("Migration run resumed run_id=%s", run_id)
    return {"run_id": run_id, "status": RunStatus.RUNNING.value, "message": "Run resumed"}


@router.post(
    "/runs/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a migration run",
)
async def cancel_migration_run(run_id: str) -> Dict[str, str]:
    """Cancel a pending, running, or paused migration run."""
    run = _get_run_or_404(run_id)
    if run["status"] in (RunStatus.COMPLETED.value, RunStatus.CANCELLED.value):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_state", "message": "Run is already terminal"},
        )
    _RUNS_STORE[run_id]["status"] = RunStatus.CANCELLED.value
    logger.info("Migration run cancelled run_id=%s", run_id)
    return {"run_id": run_id, "status": RunStatus.CANCELLED.value}


@router.get(
    "/runs/{run_id}/batches",
    response_model=PaginatedResponse,
    summary="List batches for a run",
)
async def list_batches(
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PaginatedResponse:
    _get_run_or_404(run_id)
    batches = [b for b in _BATCHES_STORE.values() if b.get("run_id") == run_id]
    total = len(batches)
    start = (page - 1) * page_size
    return PaginatedResponse(
        items=batches[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-total // page_size)),
        has_next=start + page_size < total,
        has_prev=page > 1,
    )


@router.get(
    "/records/{legacy_id}",
    response_model=RecordStatusResponse,
    summary="Get per-record migration status",
)
async def get_record_status(legacy_id: str) -> RecordStatusResponse:
    record = _RECORDS_STORE.get(legacy_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"No migration record for '{legacy_id}'"},
        )
    return RecordStatusResponse(**record)


@router.get(
    "/stats",
    response_model=MigrationStats,
    summary="Aggregate migration statistics",
)
async def get_migration_stats() -> MigrationStats:
    """Return organisation-wide migration statistics."""
    total = len(_RUNS_STORE)
    active = sum(1 for r in _RUNS_STORE.values() if r["status"] == RunStatus.RUNNING.value)
    return MigrationStats(
        total_runs=total,
        active_runs=active,
        total_records_migrated=sum(r.get("successful_records", 0) for r in _RUNS_STORE.values()),
        total_records_failed=sum(r.get("failed_records", 0) for r in _RUNS_STORE.values()),
        overall_success_rate=0.0,
        avg_throughput_per_hour=0.0,
        top_error_categories=[],
        by_object_type={},
        last_updated=datetime.now(timezone.utc),
    )


@router.get(
    "/errors",
    response_model=PaginatedResponse,
    summary="List migration errors with filtering",
)
async def list_errors(
    run_id: Optional[str] = Query(None),
    object_type: Optional[str] = Query(None),
    error_category: Optional[str] = Query(None),
    is_retryable: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> PaginatedResponse:
    """Return a paginated, filterable list of migration errors."""
    # In production this would query the errors table
    errors: List[Dict[str, Any]] = []
    total = len(errors)
    return PaginatedResponse(
        items=errors,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-total // page_size)),
        has_next=False,
        has_prev=False,
    )


@router.post(
    "/retry",
    response_model=RetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry failed migration records",
)
async def retry_failed_records(body: RetryRequest) -> RetryResponse:
    """
    Queue a retry run for previously failed records.

    Records are selected by run_id, batch_id, specific legacy IDs,
    or error category (any combination).
    """
    retry_run_id = str(uuid.uuid4())
    logger.info(
        "Retry run created id=%s source_run=%s max_records=%d",
        retry_run_id,
        body.run_id,
        body.max_records,
    )
    return RetryResponse(
        retry_run_id=retry_run_id,
        records_queued=0,  # populated by orchestrator
        estimated_start=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Background task stub
# ---------------------------------------------------------------------------


async def _simulate_run_start(run_id: str) -> None:
    """Stub – in production this publishes an event to the orchestrator queue."""
    import asyncio
    await asyncio.sleep(0.1)
    if run_id in _RUNS_STORE:
        _RUNS_STORE[run_id]["status"] = RunStatus.RUNNING.value
        _RUNS_STORE[run_id]["started_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("Migration run transitioned to RUNNING run_id=%s", run_id)
