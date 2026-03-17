"""
ValidateMigrationData use case.

Orchestrates a pre-migration validation sweep across all staged records
without performing any writes to Salesforce.

Validation pipeline (pluggable rules):
  1. Structural validation   – required fields, data types, lengths
  2. Referential validation  – account exists for every contact, no orphans
  3. Business-rule validation – duplicate detection, email uniqueness, etc.
  4. Salesforce constraint validation – field-length limits, picklist values
  5. Mapping coverage validation – every legacy field has a mapping rule

Returns a ValidationSummaryDTO indicating whether migration can proceed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, Any

from application.commands.migration_commands import ValidateMigrationDataCommand
from application.dto.migration_dto import ValidationRuleResultDTO, ValidationSummaryDTO
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation rule protocol (Strategy pattern)
# ---------------------------------------------------------------------------


@dataclass
class RuleContext:
    """Contextual data passed to each validation rule."""

    record_type: str
    records: list[dict[str, Any]]
    field_mappings: dict[str, Any]
    salesforce_schema: dict[str, Any]
    reference_data: dict[str, Any]


class ValidationRule(Protocol):
    """
    Port: a single validation rule applied to a batch of records.

    Every adapter that provides a validation check implements this protocol.
    """

    @property
    def rule_name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def severity(self) -> str: ...   # "error" | "warning" | "info"

    @property
    def is_blocking(self) -> bool: ...

    async def apply(self, context: RuleContext) -> ValidationRuleResultDTO: ...


class DataSourcePort(Protocol):
    """Port: reads staged records from the extraction store."""

    async def fetch_records(
        self, record_type: str, limit: int, offset: int
    ) -> list[dict[str, Any]]: ...

    async def count_records(self, record_type: str) -> int: ...


class MappingRepository(Protocol):
    """Port: retrieves field mapping configurations."""

    async def get_field_mappings(self, record_type: str) -> dict[str, Any]: ...

    async def get_salesforce_schema(self, record_type: str) -> dict[str, Any]: ...

    async def get_reference_data(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


class ValidateMigrationDataUseCase:
    """
    Orchestrates the data validation phase of the migration pipeline.

    Validation rules are injected as a list of ValidationRule implementations,
    enabling easy addition or removal of rules without changing this class.
    """

    BATCH_SIZE = 500

    def __init__(
        self,
        data_source: DataSourcePort,
        mapping_repository: MappingRepository,
        validation_rules: list[ValidationRule],
    ) -> None:
        self._data_source = data_source
        self._mapping_repository = mapping_repository
        self._rules = validation_rules

    async def execute(
        self, command: ValidateMigrationDataCommand
    ) -> ValidationSummaryDTO:
        """
        Run all registered validation rules against the staged data.

        Returns a ValidationSummaryDTO.  Does not raise on validation failures;
        callers inspect can_proceed to decide whether to abort.
        """
        logger.info(
            "ValidateMigrationData: record_types=%s sample_size=%d fail_on_warnings=%s",
            command.record_types,
            command.sample_size,
            command.fail_on_warnings,
        )

        record_types = list(command.record_types) if command.record_types else ["Account", "Contact"]
        all_rule_results: list[ValidationRuleResultDTO] = []
        total_records = 0
        total_passed = 0
        total_warnings = 0
        total_errors = 0
        blocking_errors_found = False

        # Pre-fetch shared reference data once
        reference_data = await self._mapping_repository.get_reference_data()

        for record_type in record_types:
            logger.info("Validating %s records…", record_type)

            # Fetch field mappings and SF schema for this type
            field_mappings = await self._mapping_repository.get_field_mappings(record_type)
            sf_schema = await self._mapping_repository.get_salesforce_schema(record_type)

            # Fetch records (paginated)
            records = await self._fetch_all_records(record_type, command.sample_size)
            total_records += len(records)

            context = RuleContext(
                record_type=record_type,
                records=records,
                field_mappings=field_mappings,
                salesforce_schema=sf_schema,
                reference_data=reference_data,
            )

            # Apply each rule
            for rule in self._rules:
                logger.debug("Applying rule '%s' to %d %s records", rule.rule_name, len(records), record_type)
                try:
                    result = await rule.apply(context)
                    all_rule_results.append(result)

                    total_passed += result.records_passed
                    if result.severity == "warning":
                        total_warnings += result.records_failed
                    elif result.severity == "error":
                        total_errors += result.records_failed

                    if result.is_blocking and result.records_failed > 0:
                        blocking_errors_found = True
                        logger.error(
                            "Blocking validation failure: rule=%s failures=%d",
                            rule.rule_name,
                            result.records_failed,
                        )

                except Exception as exc:
                    logger.exception("Validation rule '%s' raised an exception: %s", rule.rule_name, exc)
                    all_rule_results.append(
                        ValidationRuleResultDTO(
                            rule_name=rule.rule_name,
                            description=rule.description,
                            severity="error",
                            records_checked=len(records),
                            records_passed=0,
                            records_failed=len(records),
                            is_blocking=True,
                        )
                    )
                    blocking_errors_found = True

        if command.fail_on_warnings:
            blocking_errors_found = blocking_errors_found or total_warnings > 0

        can_proceed = not blocking_errors_found

        logger.info(
            "Validation complete: total=%d passed=%d warnings=%d errors=%d can_proceed=%s",
            total_records,
            total_passed,
            total_warnings,
            total_errors,
            can_proceed,
        )

        return ValidationSummaryDTO(
            total_records=total_records,
            records_passed=total_passed,
            records_with_warnings=total_warnings,
            records_with_errors=total_errors,
            blocking_errors_found=blocking_errors_found,
            rule_results=all_rule_results,
            validated_at=datetime.now(tz=timezone.utc).isoformat(),
            can_proceed=can_proceed,
        )

    async def _fetch_all_records(
        self, record_type: str, sample_size: int
    ) -> list[dict[str, Any]]:
        """Paginate through the data source and collect records."""
        if sample_size > 0:
            return await self._data_source.fetch_records(record_type, limit=sample_size, offset=0)

        all_records: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = await self._data_source.fetch_records(
                record_type, limit=self.BATCH_SIZE, offset=offset
            )
            all_records.extend(batch)
            if len(batch) < self.BATCH_SIZE:
                break
            offset += self.BATCH_SIZE
        return all_records


# ---------------------------------------------------------------------------
# Built-in validation rules (concrete implementations)
# ---------------------------------------------------------------------------


class RequiredFieldsRule:
    """
    Checks that required fields are present and non-empty.
    Configuration-driven via the field_mappings in the rule context.
    """

    rule_name = "REQUIRED_FIELDS"
    description = "Ensure all mapped required fields are present and non-empty"
    severity = "error"
    is_blocking = True

    async def apply(self, context: RuleContext) -> ValidationRuleResultDTO:
        required_fields = [
            mapping["legacy_field"]
            for mapping in context.field_mappings.get("fields", [])
            if mapping.get("required", False)
        ]

        failures = []
        for record in context.records:
            missing = [f for f in required_fields if not record.get(f)]
            if missing:
                failures.append({
                    "record_id": record.get("id", "unknown"),
                    "missing_fields": missing,
                })

        return ValidationRuleResultDTO(
            rule_name=self.rule_name,
            description=self.description,
            severity=self.severity,
            records_checked=len(context.records),
            records_passed=len(context.records) - len(failures),
            records_failed=len(failures),
            sample_failures=failures[:10],
            is_blocking=self.is_blocking,
        )


class DuplicateLegacyIdRule:
    """Detects duplicate legacy IDs within the staged dataset."""

    rule_name = "DUPLICATE_LEGACY_IDS"
    description = "Detect duplicate legacy record IDs that would cause upsert conflicts"
    severity = "error"
    is_blocking = True

    async def apply(self, context: RuleContext) -> ValidationRuleResultDTO:
        seen: dict[str, int] = {}
        for record in context.records:
            lid = str(record.get("id", ""))
            seen[lid] = seen.get(lid, 0) + 1

        duplicates = [{"legacy_id": k, "count": v} for k, v in seen.items() if v > 1]

        return ValidationRuleResultDTO(
            rule_name=self.rule_name,
            description=self.description,
            severity=self.severity,
            records_checked=len(context.records),
            records_passed=len(context.records) - len(duplicates),
            records_failed=len(duplicates),
            sample_failures=duplicates[:10],
            is_blocking=self.is_blocking,
        )


class EmailFormatRule:
    """Warns when email fields contain invalid format."""

    rule_name = "EMAIL_FORMAT"
    description = "Validate email address format in records"
    severity = "warning"
    is_blocking = False

    _EMAIL_FIELDS = ("email", "contact_email", "primary_email", "email_address")

    async def apply(self, context: RuleContext) -> ValidationRuleResultDTO:
        import re
        pattern = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

        bad = []
        for record in context.records:
            for ef in self._EMAIL_FIELDS:
                val = record.get(ef)
                if val and not pattern.match(str(val).strip()):
                    bad.append({"record_id": record.get("id"), "field": ef, "value": val})
                    break

        return ValidationRuleResultDTO(
            rule_name=self.rule_name,
            description=self.description,
            severity=self.severity,
            records_checked=len(context.records),
            records_passed=len(context.records) - len(bad),
            records_failed=len(bad),
            sample_failures=bad[:10],
            is_blocking=self.is_blocking,
        )


class FieldLengthRule:
    """Checks that field values do not exceed Salesforce maximum lengths."""

    rule_name = "FIELD_LENGTH"
    description = "Ensure field values do not exceed Salesforce field length limits"
    severity = "error"
    is_blocking = True

    async def apply(self, context: RuleContext) -> ValidationRuleResultDTO:
        sf_fields = {
            f["name"]: f.get("length", 255)
            for f in context.salesforce_schema.get("fields", [])
            if f.get("type") in ("string", "textarea", "picklist")
        }

        length_violations = []
        for record in context.records:
            for mapping in context.field_mappings.get("fields", []):
                sf_field = mapping.get("salesforce_field")
                legacy_field = mapping.get("legacy_field")
                max_len = sf_fields.get(sf_field)
                if max_len and legacy_field:
                    val = record.get(legacy_field)
                    if val and len(str(val)) > max_len:
                        length_violations.append({
                            "record_id": record.get("id"),
                            "field": legacy_field,
                            "value_length": len(str(val)),
                            "max_length": max_len,
                        })

        return ValidationRuleResultDTO(
            rule_name=self.rule_name,
            description=self.description,
            severity=self.severity,
            records_checked=len(context.records),
            records_passed=len(context.records) - len(length_violations),
            records_failed=len(length_violations),
            sample_failures=length_violations[:10],
            is_blocking=self.is_blocking,
        )
