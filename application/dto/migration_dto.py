"""
Data Transfer Objects (DTOs) for the migration application layer.

DTOs cross the boundary between the application layer and the outside world
(API adapters, CLI adapters, event listeners).  They use only primitive Python
types so they can be trivially serialised to/from JSON, CSV, etc.

DTOs do NOT contain business logic.
They do NOT reference domain entities directly.
They may contain simple data-shaping helpers (e.g., to/from_dict).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Address DTO
# ---------------------------------------------------------------------------


@dataclass
class AddressDTO:
    street: Optional[str] = None
    unit: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country_code: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AddressDTO":
        return cls(
            street=data.get("street"),
            unit=data.get("unit"),
            city=data.get("city"),
            state=data.get("state"),
            postal_code=data.get("postal_code", data.get("zip")),
            country_code=data.get("country_code", data.get("country")),
        )


# ---------------------------------------------------------------------------
# Account DTOs
# ---------------------------------------------------------------------------


@dataclass
class AccountDTO:
    """Full account representation returned to API consumers."""

    account_id: str
    legacy_id: str
    name: str
    account_type: str
    status: str
    industry: Optional[str] = None
    billing_address: Optional[AddressDTO] = None
    shipping_address: Optional[AddressDTO] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    annual_revenue: Optional[float] = None
    number_of_employees: Optional[int] = None
    description: Optional[str] = None
    salesforce_id: Optional[str] = None
    is_migrated: bool = False
    created_at: Optional[str] = None   # ISO-8601 string
    updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["billing_address"] = self.billing_address.to_dict() if self.billing_address else None
        d["shipping_address"] = self.shipping_address.to_dict() if self.shipping_address else None
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class CreateAccountDTO:
    """Payload accepted when staging a new account for migration."""

    legacy_id: str
    name: str
    account_type: str = "Prospect"
    status: str = "Active"
    industry: Optional[str] = None
    billing_address: Optional[AddressDTO] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    annual_revenue: Optional[float] = None
    number_of_employees: Optional[int] = None
    description: Optional[str] = None


@dataclass
class AccountMigrationResultDTO:
    """Outcome of migrating a single account to Salesforce."""

    legacy_id: str
    name: str
    status: str             # "succeeded" | "failed" | "skipped" | "dry_run"
    salesforce_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    migrated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Contact DTOs
# ---------------------------------------------------------------------------


@dataclass
class ContactDTO:
    """Full contact representation returned to API consumers."""

    contact_id: str
    legacy_id: str
    legacy_account_id: str
    first_name: str
    last_name: str
    salutation: Optional[str] = None
    title: Optional[str] = None
    department: Optional[str] = None
    email: Optional[str] = None
    mobile_phone: Optional[str] = None
    work_phone: Optional[str] = None
    mailing_address: Optional[AddressDTO] = None
    do_not_call: bool = False
    do_not_email: bool = False
    lead_source: Optional[str] = None
    status: str = "Active"
    salesforce_id: Optional[str] = None
    salesforce_account_id: Optional[str] = None
    is_migrated: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mailing_address"] = self.mailing_address.to_dict() if self.mailing_address else None
        return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------
# Migration Job DTOs
# ---------------------------------------------------------------------------


@dataclass
class StartMigrationDTO:
    """
    Payload accepted by the API/CLI to initiate a migration run.
    Maps 1-to-1 with StartMigrationCommand after validation.
    """

    source_system: str
    target_org_id: str
    record_types: list[str]
    batch_size: int = 200
    dry_run: bool = False
    phases_to_run: list[str] = field(default_factory=list)
    error_threshold_percent: float = 5.0
    notification_emails: list[str] = field(default_factory=list)
    max_retries: int = 3

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StartMigrationDTO":
        return cls(
            source_system=data["source_system"],
            target_org_id=data["target_org_id"],
            record_types=data.get("record_types", []),
            batch_size=int(data.get("batch_size", 200)),
            dry_run=bool(data.get("dry_run", False)),
            phases_to_run=data.get("phases_to_run", []),
            error_threshold_percent=float(data.get("error_threshold_percent", 5.0)),
            notification_emails=data.get("notification_emails", []),
            max_retries=int(data.get("max_retries", 3)),
        )


@dataclass
class PhaseProgressDTO:
    """Progress of a single migration phase."""

    phase: str
    status: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    records_processed: int = 0
    records_succeeded: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    success_rate: float = 1.0


@dataclass
class MigrationJobDTO:
    """Full migration job representation for API responses."""

    job_id: str
    status: str
    source_system: str
    target_org_id: str
    initiated_by: str
    dry_run: bool
    batch_size: int
    error_threshold_percent: float
    record_types: list[str]
    total_records: int
    records_succeeded: int
    records_failed: int
    records_skipped: int
    completion_percent: float
    error_rate_percent: float
    current_phase: Optional[str] = None
    phases: list[PhaseProgressDTO] = field(default_factory=list)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: Optional[str] = None
    duration_seconds: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "phases": [asdict(p) for p in self.phases],
        }


@dataclass
class MigrationReportDTO:
    """Comprehensive migration report output."""

    job_id: str
    generated_at: str
    status: str
    source_system: str
    target_org_id: str
    dry_run: bool
    initiated_by: str
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_seconds: Optional[float]
    total_records: int
    records_succeeded: int
    records_failed: int
    records_skipped: int
    success_rate: float
    error_rate: float
    phases: list[PhaseProgressDTO] = field(default_factory=list)
    error_summary: dict[str, int] = field(default_factory=dict)
    account_results: list[AccountMigrationResultDTO] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "phases": [asdict(p) for p in self.phases],
            "account_results": [asdict(r) for r in self.account_results],
        }


# ---------------------------------------------------------------------------
# Validation DTOs
# ---------------------------------------------------------------------------


@dataclass
class ValidationRuleResultDTO:
    """Result of applying a single validation rule to the dataset."""

    rule_name: str
    description: str
    severity: str       # "error" | "warning" | "info"
    records_checked: int
    records_passed: int
    records_failed: int
    sample_failures: list[dict[str, Any]] = field(default_factory=list)
    is_blocking: bool = False


@dataclass
class ValidationSummaryDTO:
    """Aggregated validation result returned to the caller."""

    total_records: int
    records_passed: int
    records_with_warnings: int
    records_with_errors: int
    blocking_errors_found: bool
    rule_results: list[ValidationRuleResultDTO] = field(default_factory=list)
    validated_at: Optional[str] = None
    can_proceed: bool = True


# ---------------------------------------------------------------------------
# Notification DTOs
# ---------------------------------------------------------------------------


@dataclass
class NotificationDTO:
    """Payload sent to the notification service."""

    recipients: list[str]
    subject: str
    body: str
    notification_type: str   # "migration_started" | "migration_completed" | "migration_failed" | etc.
    job_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    send_at: Optional[datetime] = None   # None = send immediately
