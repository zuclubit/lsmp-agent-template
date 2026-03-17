"""
FastAPI inbound adapter.

Maps HTTP requests to application commands and queries, executes use cases,
and serialises results back to HTTP responses.

Endpoints:
  POST   /migrations                       → StartMigrationUseCase
  POST   /migrations/{job_id}/pause        → PauseMigrationUseCase
  POST   /migrations/{job_id}/resume       → (inline)
  GET    /migrations/{job_id}              → MigrationService.get_job_status
  GET    /migrations/{job_id}/report       → GenerateMigrationReportUseCase
  GET    /migrations                       → List jobs
  POST   /migrations/validate              → ValidateMigrationDataUseCase
  GET    /migrations/dashboard             → Dashboard query
  GET    /health                           → Health check

Authentication: Bearer token (header) validated by a dependency.
All input is validated by Pydantic models (FastAPI built-in).
All domain exceptions are translated to appropriate HTTP status codes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from application.commands.migration_commands import (
    GenerateMigrationReportCommand,
    PauseMigrationCommand,
    ResumeMigrationCommand,
    StartMigrationCommand,
    ValidateMigrationDataCommand,
)
from application.use_cases.generate_migration_report import GenerateMigrationReportUseCase
from application.use_cases.pause_migration import PauseMigrationUseCase
from application.use_cases.start_migration import StartMigrationUseCase
from application.use_cases.validate_migration_data import ValidateMigrationDataUseCase
from application.services.migration_service import MigrationService
from domain.exceptions.domain_exceptions import (
    BusinessRuleViolation,
    EntityNotFound,
    MigrationAlreadyInProgress,
    MigrationPrerequisiteNotMet,
    ValidationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["migrations"])


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class StartMigrationRequest(BaseModel):
    source_system: str = Field(..., min_length=1, max_length=100)
    target_org_id: str = Field(..., min_length=15, max_length=18)
    record_types: list[str] = Field(..., min_length=1)
    batch_size: int = Field(default=200, ge=1, le=2000)
    dry_run: bool = Field(default=False)
    phases_to_run: list[str] = Field(default_factory=list)
    error_threshold_percent: float = Field(default=5.0, ge=0.0, le=100.0)
    notification_emails: list[str] = Field(default_factory=list)
    max_retries: int = Field(default=3, ge=0, le=10)

    @field_validator("record_types")
    @classmethod
    def validate_record_types(cls, v: list[str]) -> list[str]:
        allowed = {"Account", "Contact", "Opportunity", "Lead", "Case"}
        invalid = [rt for rt in v if rt not in allowed]
        if invalid:
            raise ValueError(f"Unknown record types: {invalid}. Allowed: {allowed}")
        return v


class PauseMigrationRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class ValidateMigrationRequest(BaseModel):
    record_types: list[str] = Field(default_factory=list)
    sample_size: int = Field(default=0, ge=0)
    fail_on_warnings: bool = Field(default=False)
    job_id: Optional[str] = Field(default=None)


class GenerateReportRequest(BaseModel):
    format: str = Field(default="html", pattern="^(html|json|csv|pdf)$")
    include_errors: bool = Field(default=True)
    include_charts: bool = Field(default=True)
    output_path: Optional[str] = Field(default=None)


class MigrationJobResponse(BaseModel):
    job_id: str
    status: str
    source_system: str
    target_org_id: str
    initiated_by: str
    dry_run: bool
    total_records: int
    records_succeeded: int
    records_failed: int
    records_skipped: int
    completion_percent: float
    error_rate_percent: float
    current_phase: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: Optional[str] = None
    duration_seconds: Optional[float] = None


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str = "1.0.0"
    checks: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Dependency injection helpers
# (In production, wire via a DI container like dependency-injector)
# ---------------------------------------------------------------------------


async def get_current_user(request: Request) -> str:
    """
    Extract and validate the bearer token from the Authorization header.
    Returns the user identity (email or service account name).

    In production, validate a JWT against your IdP (Okta, Auth0, Azure AD).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth_header[7:]
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token cannot be empty",
        )
    # TODO: validate JWT signature, expiry, and extract sub/email claim
    return f"user:{token[:8]}"


async def get_start_migration_use_case(request: Request) -> StartMigrationUseCase:
    """Retrieve the use case from the app's dependency container."""
    return request.app.state.start_migration_use_case


async def get_pause_migration_use_case(request: Request) -> PauseMigrationUseCase:
    return request.app.state.pause_migration_use_case


async def get_validate_use_case(request: Request) -> ValidateMigrationDataUseCase:
    return request.app.state.validate_migration_use_case


async def get_report_use_case(request: Request) -> GenerateMigrationReportUseCase:
    return request.app.state.generate_report_use_case


async def get_migration_service(request: Request) -> MigrationService:
    return request.app.state.migration_service


# ---------------------------------------------------------------------------
# Exception handlers (translate domain exceptions → HTTP responses)
# ---------------------------------------------------------------------------


def register_exception_handlers(app: Any) -> None:
    """Register domain exception → HTTP status code mappings on the FastAPI app."""

    @app.exception_handler(ValidationError)
    async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": exc.code, "message": exc.message, "field": exc.field},
        )

    @app.exception_handler(EntityNotFound)
    async def not_found_handler(request: Request, exc: EntityNotFound) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(MigrationAlreadyInProgress)
    async def conflict_handler(request: Request, exc: MigrationAlreadyInProgress) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(MigrationPrerequisiteNotMet)
    async def prerequisite_handler(request: Request, exc: MigrationPrerequisiteNotMet) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            content={"error": exc.code, "message": exc.message, "prerequisite": exc.prerequisite},
        )

    @app.exception_handler(BusinessRuleViolation)
    async def business_rule_handler(request: Request, exc: BusinessRuleViolation) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": exc.code, "message": exc.message},
        )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health_check(request: Request) -> HealthResponse:
    """Liveness + basic readiness probe."""
    checks: dict[str, str] = {}
    # Add database and SF connectivity checks in production
    return HealthResponse(
        status="healthy",
        timestamp=datetime.utcnow().isoformat() + "Z",
        checks=checks,
    )


@router.post(
    "/migrations",
    response_model=MigrationJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new migration job",
)
async def start_migration(
    body: StartMigrationRequest,
    current_user: str = Depends(get_current_user),
    use_case: StartMigrationUseCase = Depends(get_start_migration_use_case),
) -> MigrationJobResponse:
    """
    Initiate a new data migration run.

    Returns HTTP 202 Accepted immediately; the migration runs asynchronously.
    Poll GET /migrations/{job_id} for status updates.
    """
    command = StartMigrationCommand(
        command_id=str(uuid.uuid4()),
        issued_by=current_user,
        source_system=body.source_system,
        target_org_id=body.target_org_id,
        record_types=tuple(body.record_types),
        batch_size=body.batch_size,
        dry_run=body.dry_run,
        phases_to_run=tuple(body.phases_to_run),
        error_threshold_percent=body.error_threshold_percent,
        notification_emails=tuple(body.notification_emails),
        max_retries=body.max_retries,
    )
    logger.info("API: StartMigration request from %s", current_user)
    job_dto = await use_case.execute(command)
    return MigrationJobResponse(**{
        k: v for k, v in vars(job_dto).items()
        if k in MigrationJobResponse.model_fields
    })


@router.get(
    "/migrations/{job_id}",
    response_model=MigrationJobResponse,
    summary="Get migration job status",
)
async def get_migration_status(
    job_id: str,
    current_user: str = Depends(get_current_user),
    migration_service: MigrationService = Depends(get_migration_service),
) -> MigrationJobResponse:
    """Return the current status of a migration job."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="job_id must be a valid UUID")

    job_dto = await migration_service.get_job_status(job_uuid)
    return MigrationJobResponse(**{
        k: v for k, v in vars(job_dto).items()
        if k in MigrationJobResponse.model_fields
    })


@router.post(
    "/migrations/{job_id}/pause",
    response_model=MigrationJobResponse,
    summary="Pause a running migration",
)
async def pause_migration(
    job_id: str,
    body: PauseMigrationRequest,
    current_user: str = Depends(get_current_user),
    use_case: PauseMigrationUseCase = Depends(get_pause_migration_use_case),
) -> MigrationJobResponse:
    """Pause a running migration at the end of the current batch."""
    command = PauseMigrationCommand(
        command_id=str(uuid.uuid4()),
        issued_by=current_user,
        job_id=job_id,
        reason=body.reason,
    )
    job_dto = await use_case.execute(command)
    return MigrationJobResponse(**{
        k: v for k, v in vars(job_dto).items()
        if k in MigrationJobResponse.model_fields
    })


@router.post(
    "/migrations/validate",
    summary="Validate migration data without starting a migration",
)
async def validate_migration_data(
    body: ValidateMigrationRequest,
    current_user: str = Depends(get_current_user),
    use_case: ValidateMigrationDataUseCase = Depends(get_validate_use_case),
) -> dict[str, Any]:
    """
    Run pre-migration validation checks and return a summary.
    Does not write to Salesforce or change any migration state.
    """
    command = ValidateMigrationDataCommand(
        command_id=str(uuid.uuid4()),
        issued_by=current_user,
        record_types=tuple(body.record_types),
        sample_size=body.sample_size,
        fail_on_warnings=body.fail_on_warnings,
        job_id=body.job_id,
    )
    summary = await use_case.execute(command)
    return {
        "total_records": summary.total_records,
        "records_passed": summary.records_passed,
        "records_with_warnings": summary.records_with_warnings,
        "records_with_errors": summary.records_with_errors,
        "blocking_errors_found": summary.blocking_errors_found,
        "can_proceed": summary.can_proceed,
        "validated_at": summary.validated_at,
        "rule_results": [
            {
                "rule_name": r.rule_name,
                "severity": r.severity,
                "records_failed": r.records_failed,
                "is_blocking": r.is_blocking,
                "sample_failures": r.sample_failures[:5],
            }
            for r in summary.rule_results
        ],
    }


@router.post(
    "/migrations/{job_id}/report",
    summary="Generate a migration report",
)
async def generate_report(
    job_id: str,
    body: GenerateReportRequest,
    current_user: str = Depends(get_current_user),
    use_case: GenerateMigrationReportUseCase = Depends(get_report_use_case),
) -> Any:
    """
    Generate a comprehensive migration report.
    Returns HTML directly if format=html, otherwise JSON.
    """
    command = GenerateMigrationReportCommand(
        command_id=str(uuid.uuid4()),
        issued_by=current_user,
        job_id=job_id,
        format=body.format,
        include_errors=body.include_errors,
        include_charts=body.include_charts,
        output_path=body.output_path,
    )
    report_dto, report_url = await use_case.execute(command)

    if body.format == "html":
        content = use_case._render(report_dto, "html")  # type: ignore[attr-defined]
        return HTMLResponse(content=content.decode("utf-8"))

    return JSONResponse(content=report_dto.to_dict())
