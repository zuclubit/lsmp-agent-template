"""
Domain-specific exceptions for the Legacy to Salesforce migration system.

These exceptions represent business rule violations, validation errors, and
domain-level error conditions. They contain no framework dependencies and
express the ubiquitous language of the migration domain.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID


class DomainException(Exception):
    """
    Base class for all domain exceptions.
    Carries a human-readable message and optional machine-readable code.
    """

    def __init__(
        self,
        message: str,
        code: str = "DOMAIN_ERROR",
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.context: dict[str, Any] = context or {}

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"code={self.code!r}, "
            f"context={self.context!r})"
        )


# ---------------------------------------------------------------------------
# Validation exceptions
# ---------------------------------------------------------------------------


class ValidationError(DomainException):
    """
    Raised when a value object or entity receives data that does not satisfy
    its invariants (e.g. malformed email, negative amount).
    """

    def __init__(
        self,
        field: str,
        value: Any,
        reason: str,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        message = f"Validation failed for '{field}': {reason} (got {value!r})"
        super().__init__(message=message, code="VALIDATION_ERROR", context=context or {})
        self.field = field
        self.value = value
        self.reason = reason


class MultipleValidationErrors(DomainException):
    """
    Aggregates several ValidationError instances so that callers receive all
    problems at once rather than one at a time.
    """

    def __init__(self, errors: list[ValidationError]) -> None:
        messages = "; ".join(e.message for e in errors)
        super().__init__(
            message=f"Multiple validation errors: {messages}",
            code="MULTIPLE_VALIDATION_ERRORS",
        )
        self.errors = errors


# ---------------------------------------------------------------------------
# Business rule exceptions
# ---------------------------------------------------------------------------


class BusinessRuleViolation(DomainException):
    """
    Raised when an operation would violate a domain business rule even though
    the supplied data is individually valid (e.g. starting a migration that is
    already in progress).
    """

    def __init__(
        self,
        rule: str,
        message: str,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=f"Business rule violated [{rule}]: {message}",
            code="BUSINESS_RULE_VIOLATION",
            context=context or {},
        )
        self.rule = rule


class InvalidStateTransition(BusinessRuleViolation):
    """
    Raised when an aggregate is asked to transition to a state that is not
    reachable from its current state.
    """

    def __init__(self, entity: str, from_state: str, to_state: str) -> None:
        super().__init__(
            rule="INVALID_STATE_TRANSITION",
            message=(
                f"{entity} cannot transition from '{from_state}' to '{to_state}'"
            ),
            context={"entity": entity, "from_state": from_state, "to_state": to_state},
        )
        self.entity = entity
        self.from_state = from_state
        self.to_state = to_state


class ConcurrencyConflict(BusinessRuleViolation):
    """
    Raised when an optimistic-lock version check fails during persistence.
    """

    def __init__(self, entity_type: str, entity_id: str, expected_version: int, actual_version: int) -> None:
        super().__init__(
            rule="CONCURRENCY_CONFLICT",
            message=(
                f"Concurrency conflict on {entity_type} {entity_id}: "
                f"expected version {expected_version}, found {actual_version}"
            ),
            context={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "expected_version": expected_version,
                "actual_version": actual_version,
            },
        )


# ---------------------------------------------------------------------------
# Not-found / aggregate-not-found exceptions
# ---------------------------------------------------------------------------


class EntityNotFound(DomainException):
    """Raised when an entity cannot be located by its identity."""

    def __init__(
        self,
        entity_type: str,
        entity_id: Any,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=f"{entity_type} with id={entity_id!r} was not found",
            code="ENTITY_NOT_FOUND",
            context=context or {},
        )
        self.entity_type = entity_type
        self.entity_id = entity_id


class AccountNotFound(EntityNotFound):
    def __init__(self, account_id: Any) -> None:
        super().__init__(entity_type="Account", entity_id=account_id)


class ContactNotFound(EntityNotFound):
    def __init__(self, contact_id: Any) -> None:
        super().__init__(entity_type="Contact", entity_id=contact_id)


class MigrationJobNotFound(EntityNotFound):
    def __init__(self, job_id: Any) -> None:
        super().__init__(entity_type="MigrationJob", entity_id=job_id)


# ---------------------------------------------------------------------------
# Migration-specific exceptions
# ---------------------------------------------------------------------------


class MigrationError(DomainException):
    """Base class for migration-level errors."""

    def __init__(
        self,
        message: str,
        code: str = "MIGRATION_ERROR",
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, context=context or {})


class MigrationPrerequisiteNotMet(MigrationError):
    """Raised when a required pre-migration condition is not satisfied."""

    def __init__(self, prerequisite: str, detail: str) -> None:
        super().__init__(
            message=f"Migration prerequisite not met [{prerequisite}]: {detail}",
            code="MIGRATION_PREREQUISITE_NOT_MET",
            context={"prerequisite": prerequisite, "detail": detail},
        )
        self.prerequisite = prerequisite
        self.detail = detail


class MigrationAlreadyInProgress(MigrationError):
    """Raised when a second migration is started while one is running."""

    def __init__(self, existing_job_id: UUID) -> None:
        super().__init__(
            message=f"A migration job is already in progress: {existing_job_id}",
            code="MIGRATION_ALREADY_IN_PROGRESS",
            context={"existing_job_id": str(existing_job_id)},
        )
        self.existing_job_id = existing_job_id


class MigrationDataCorruption(MigrationError):
    """Raised when source data fails integrity or checksum verification."""

    def __init__(self, record_id: str, field: str, detail: str) -> None:
        super().__init__(
            message=f"Data corruption detected in record {record_id}, field '{field}': {detail}",
            code="MIGRATION_DATA_CORRUPTION",
            context={"record_id": record_id, "field": field, "detail": detail},
        )


class SalesforceApiError(DomainException):
    """
    Represents an error returned by the Salesforce platform.
    Lives in the domain because it represents a failure in fulfilling a
    domain operation; the adapter layer translates raw HTTP errors into this.
    """

    def __init__(
        self,
        sf_error_code: str,
        sf_message: str,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(
            message=f"Salesforce API error [{sf_error_code}]: {sf_message}",
            code="SALESFORCE_API_ERROR",
            context={
                "sf_error_code": sf_error_code,
                "sf_message": sf_message,
                "status_code": status_code,
            },
        )
        self.sf_error_code = sf_error_code
        self.sf_message = sf_message
        self.status_code = status_code


class DuplicateRecordError(DomainException):
    """Raised when a record with the same natural key already exists."""

    def __init__(self, entity_type: str, key_field: str, key_value: str) -> None:
        super().__init__(
            message=f"Duplicate {entity_type} found: {key_field}={key_value!r}",
            code="DUPLICATE_RECORD",
            context={
                "entity_type": entity_type,
                "key_field": key_field,
                "key_value": key_value,
            },
        )
