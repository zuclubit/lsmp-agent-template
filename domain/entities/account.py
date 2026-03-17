"""
Account domain entity.

The Account is a core aggregate within the migration bounded context.
It represents a business customer in the legacy system that must be
migrated to Salesforce as an Account object.

Design principles applied:
  - Entity: equality based on identity (account_id), not attribute values.
  - Aggregate root: owns and enforces invariants over its data.
  - No framework imports: pure Python, no ORM, no HTTP, no Salesforce SDK.
  - Value objects: Address, Email, SalesforceId are used for typed fields.
  - Domain events: mutations raise events recorded on the aggregate.
  - Business rules: enforced in mutating methods, raising DomainExceptions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import UUID

from domain.events.migration_events import DomainEvent, RecordMigrated
from domain.exceptions.domain_exceptions import (
    BusinessRuleViolation,
    ValidationError,
    DuplicateRecordError,
)
from domain.value_objects.address import Address
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Supporting enumerations (domain concepts, not Salesforce-specific labels)
# ---------------------------------------------------------------------------


class AccountType(str, Enum):
    PROSPECT = "Prospect"
    CUSTOMER = "Customer"
    PARTNER = "Partner"
    COMPETITOR = "Competitor"
    OTHER = "Other"


class AccountStatus(str, Enum):
    ACTIVE = "Active"
    INACTIVE = "Inactive"
    SUSPENDED = "Suspended"
    PENDING_REVIEW = "PendingReview"


class Industry(str, Enum):
    AGRICULTURE = "Agriculture"
    BANKING = "Banking"
    CONSTRUCTION = "Construction"
    EDUCATION = "Education"
    ELECTRONICS = "Electronics"
    ENERGY = "Energy"
    FINANCE = "Finance"
    FOOD_BEVERAGE = "FoodAndBeverage"
    GOVERNMENT = "Government"
    HEALTHCARE = "Healthcare"
    HOSPITALITY = "Hospitality"
    INSURANCE = "Insurance"
    MANUFACTURING = "Manufacturing"
    MEDIA = "Media"
    NOT_FOR_PROFIT = "NotForProfit"
    REAL_ESTATE = "RealEstate"
    RETAIL = "Retail"
    TECHNOLOGY = "Technology"
    TELECOMMUNICATIONS = "Telecommunications"
    TRANSPORTATION = "Transportation"
    UTILITIES = "Utilities"
    OTHER = "Other"


# ---------------------------------------------------------------------------
# ContactInfo value object (nested, lightweight)
# ---------------------------------------------------------------------------


class ContactInfo:
    """
    Immutable value object aggregating phone and website for an Account.
    """

    __slots__ = ("_phone", "_fax", "_website")

    def __init__(
        self,
        phone: Optional[str] = None,
        fax: Optional[str] = None,
        website: Optional[str] = None,
    ) -> None:
        # Minimal normalisation; deep validation deferred to a dedicated VO if needed
        phone_n = self._normalise_phone(phone) if phone else None
        fax_n = self._normalise_phone(fax) if fax else None
        website_n = website.strip() if website else None

        if website_n and not (website_n.startswith("http://") or website_n.startswith("https://")):
            website_n = "https://" + website_n

        object.__setattr__(self, "_phone", phone_n)
        object.__setattr__(self, "_fax", fax_n)
        object.__setattr__(self, "_website", website_n)

    @staticmethod
    def _normalise_phone(raw: str) -> str:
        import re
        digits = re.sub(r"[^\d+]", "", raw.strip())
        return digits

    def __setattr__(self, name: str, value: object) -> None:  # type: ignore[override]
        raise AttributeError("ContactInfo is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContactInfo):
            return NotImplemented
        return (
            self._phone == other._phone
            and self._fax == other._fax
            and self._website == other._website
        )

    def __hash__(self) -> int:
        return hash((self._phone, self._fax, self._website))

    def __repr__(self) -> str:
        return f"ContactInfo(phone={self._phone!r}, website={self._website!r})"

    @property
    def phone(self) -> Optional[str]:
        return self._phone

    @property
    def fax(self) -> Optional[str]:
        return self._fax

    @property
    def website(self) -> Optional[str]:
        return self._website


# ---------------------------------------------------------------------------
# Account aggregate root
# ---------------------------------------------------------------------------


class Account:
    """
    Account aggregate root.

    Lifecycle:
        Account.create(...)  →  new unsaved account (raises AccountCreated event)
        account.mark_migrated(sf_id)  →  links legacy record to Salesforce record
        account.deactivate(reason)    →  sets status to INACTIVE
        account.update_billing_address(addr)  →  replaces address VO

    Invariants:
        - name is required and ≤ 255 characters.
        - annual_revenue, if provided, must be ≥ 0.
        - number_of_employees, if provided, must be ≥ 0.
        - A migrated account cannot be migrated a second time.
        - A suspended account cannot be migrated.
    """

    def __init__(
        self,
        account_id: UUID,
        legacy_id: str,
        name: str,
        account_type: AccountType,
        status: AccountStatus,
        industry: Optional[Industry],
        billing_address: Optional[Address],
        shipping_address: Optional[Address],
        contact_info: Optional[ContactInfo],
        primary_email: Optional[Email],
        annual_revenue: Optional[float],
        number_of_employees: Optional[int],
        description: Optional[str],
        salesforce_id: Optional[SalesforceId],
        created_at: datetime,
        updated_at: datetime,
        version: int = 0,
    ) -> None:
        self._account_id = account_id
        self._legacy_id = legacy_id
        self._name = name
        self._account_type = account_type
        self._status = status
        self._industry = industry
        self._billing_address = billing_address
        self._shipping_address = shipping_address
        self._contact_info = contact_info
        self._primary_email = primary_email
        self._annual_revenue = annual_revenue
        self._number_of_employees = number_of_employees
        self._description = description
        self._salesforce_id = salesforce_id
        self._created_at = created_at
        self._updated_at = updated_at
        self._version = version
        self._domain_events: list[DomainEvent] = []

    # ------------------------------------------------------------------
    # Factory method (preferred over direct construction)
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        legacy_id: str,
        name: str,
        account_type: AccountType = AccountType.PROSPECT,
        status: AccountStatus = AccountStatus.ACTIVE,
        industry: Optional[Industry] = None,
        billing_address: Optional[Address] = None,
        shipping_address: Optional[Address] = None,
        contact_info: Optional[ContactInfo] = None,
        primary_email: Optional[Email] = None,
        annual_revenue: Optional[float] = None,
        number_of_employees: Optional[int] = None,
        description: Optional[str] = None,
    ) -> "Account":
        """
        Create a new Account, enforcing all creation invariants.
        Raises ValidationError or BusinessRuleViolation on failure.
        """
        cls._validate_name(name)
        cls._validate_revenue(annual_revenue)
        cls._validate_employees(number_of_employees)
        if not legacy_id or not legacy_id.strip():
            raise ValidationError("legacy_id", legacy_id, "legacy_id cannot be blank")

        now = _utcnow()
        account = cls(
            account_id=uuid.uuid4(),
            legacy_id=legacy_id.strip(),
            name=name.strip(),
            account_type=account_type,
            status=status,
            industry=industry,
            billing_address=billing_address,
            shipping_address=shipping_address,
            contact_info=contact_info,
            primary_email=primary_email,
            annual_revenue=annual_revenue,
            number_of_employees=number_of_employees,
            description=description.strip() if description else None,
            salesforce_id=None,
            created_at=now,
            updated_at=now,
            version=0,
        )
        return account

    # ------------------------------------------------------------------
    # Private validators (static methods = no state dependency)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("name", name, "Account name cannot be blank")
        if len(name.strip()) > 255:
            raise ValidationError("name", name, "Account name cannot exceed 255 characters")

    @staticmethod
    def _validate_revenue(revenue: Optional[float]) -> None:
        if revenue is not None and revenue < 0:
            raise ValidationError(
                "annual_revenue", revenue, "Annual revenue cannot be negative"
            )

    @staticmethod
    def _validate_employees(count: Optional[int]) -> None:
        if count is not None and count < 0:
            raise ValidationError(
                "number_of_employees", count, "Number of employees cannot be negative"
            )

    # ------------------------------------------------------------------
    # Identity & equality (entity: identity-based)
    # ------------------------------------------------------------------

    @property
    def account_id(self) -> UUID:
        return self._account_id

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Account):
            return NotImplemented
        return self._account_id == other._account_id

    def __hash__(self) -> int:
        return hash(self._account_id)

    def __repr__(self) -> str:
        return (
            f"Account(id={self._account_id}, legacy_id={self._legacy_id!r}, "
            f"name={self._name!r}, status={self._status.value})"
        )

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def legacy_id(self) -> str:
        return self._legacy_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def account_type(self) -> AccountType:
        return self._account_type

    @property
    def status(self) -> AccountStatus:
        return self._status

    @property
    def industry(self) -> Optional[Industry]:
        return self._industry

    @property
    def billing_address(self) -> Optional[Address]:
        return self._billing_address

    @property
    def shipping_address(self) -> Optional[Address]:
        return self._shipping_address

    @property
    def contact_info(self) -> Optional[ContactInfo]:
        return self._contact_info

    @property
    def primary_email(self) -> Optional[Email]:
        return self._primary_email

    @property
    def annual_revenue(self) -> Optional[float]:
        return self._annual_revenue

    @property
    def number_of_employees(self) -> Optional[int]:
        return self._number_of_employees

    @property
    def description(self) -> Optional[str]:
        return self._description

    @property
    def salesforce_id(self) -> Optional[SalesforceId]:
        return self._salesforce_id

    @property
    def is_migrated(self) -> bool:
        return self._salesforce_id is not None

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at

    @property
    def version(self) -> int:
        return self._version

    # ------------------------------------------------------------------
    # Domain event access
    # ------------------------------------------------------------------

    def collect_events(self) -> list[DomainEvent]:
        """Drain and return all pending domain events."""
        events = list(self._domain_events)
        self._domain_events.clear()
        return events

    # ------------------------------------------------------------------
    # Mutating domain methods (enforce business rules, record events)
    # ------------------------------------------------------------------

    def mark_migrated(
        self,
        salesforce_id: SalesforceId,
        migration_job_id: str,
        phase: str = "data_load",
    ) -> None:
        """
        Link this account to its newly created Salesforce record.

        Business rules:
          - Cannot migrate a record that is already migrated.
          - Cannot migrate a suspended account.
        """
        if self.is_migrated:
            raise BusinessRuleViolation(
                rule="ACCOUNT_ALREADY_MIGRATED",
                message=(
                    f"Account {self._legacy_id} is already migrated "
                    f"to Salesforce ID {self._salesforce_id}"
                ),
                context={"legacy_id": self._legacy_id, "sf_id": str(self._salesforce_id)},
            )
        if self._status == AccountStatus.SUSPENDED:
            raise BusinessRuleViolation(
                rule="SUSPENDED_ACCOUNT_CANNOT_BE_MIGRATED",
                message=f"Account {self._legacy_id} is suspended and cannot be migrated",
                context={"legacy_id": self._legacy_id, "status": self._status.value},
            )

        self._salesforce_id = salesforce_id
        self._updated_at = _utcnow()
        self._version += 1

        self._domain_events.append(
            RecordMigrated(
                migration_job_id=migration_job_id,
                legacy_record_id=self._legacy_id,
                salesforce_record_id=str(salesforce_id),
                record_type="Account",
                phase=phase,
            )
        )

    def update_name(self, new_name: str) -> None:
        self._validate_name(new_name)
        self._name = new_name.strip()
        self._updated_at = _utcnow()
        self._version += 1

    def update_billing_address(self, address: Address) -> None:
        self._billing_address = address
        self._updated_at = _utcnow()
        self._version += 1

    def update_shipping_address(self, address: Address) -> None:
        self._shipping_address = address
        self._updated_at = _utcnow()
        self._version += 1

    def update_contact_info(self, contact_info: ContactInfo) -> None:
        self._contact_info = contact_info
        self._updated_at = _utcnow()
        self._version += 1

    def update_primary_email(self, email: Email) -> None:
        self._primary_email = email
        self._updated_at = _utcnow()
        self._version += 1

    def update_revenue(self, revenue: float) -> None:
        self._validate_revenue(revenue)
        self._annual_revenue = revenue
        self._updated_at = _utcnow()
        self._version += 1

    def deactivate(self, reason: str = "") -> None:
        if self._status == AccountStatus.INACTIVE:
            raise BusinessRuleViolation(
                rule="ACCOUNT_ALREADY_INACTIVE",
                message=f"Account {self._legacy_id} is already inactive",
            )
        self._status = AccountStatus.INACTIVE
        self._updated_at = _utcnow()
        self._version += 1

    def suspend(self, reason: str = "") -> None:
        if self._status == AccountStatus.SUSPENDED:
            raise BusinessRuleViolation(
                rule="ACCOUNT_ALREADY_SUSPENDED",
                message=f"Account {self._legacy_id} is already suspended",
            )
        self._status = AccountStatus.SUSPENDED
        self._updated_at = _utcnow()
        self._version += 1

    def reactivate(self) -> None:
        if self._status == AccountStatus.ACTIVE:
            raise BusinessRuleViolation(
                rule="ACCOUNT_ALREADY_ACTIVE",
                message=f"Account {self._legacy_id} is already active",
            )
        self._status = AccountStatus.ACTIVE
        self._updated_at = _utcnow()
        self._version += 1

    # ------------------------------------------------------------------
    # Salesforce mapping helper
    # ------------------------------------------------------------------

    def to_salesforce_payload(self) -> dict[str, object]:
        """
        Produce a dict suitable for the Salesforce REST API create/update body.
        Keys follow Salesforce standard field API names.
        """
        payload: dict[str, object] = {
            "Name": self._name,
            "Type": self._account_type.value,
            "Industry": self._industry.value if self._industry else None,
            "AnnualRevenue": self._annual_revenue,
            "NumberOfEmployees": self._number_of_employees,
            "Description": self._description,
            # Legacy reference stored in an external ID field
            "Legacy_ID__c": self._legacy_id,
        }

        if self._primary_email:
            payload["Email__c"] = str(self._primary_email)

        if self._contact_info:
            payload["Phone"] = self._contact_info.phone
            payload["Fax"] = self._contact_info.fax
            payload["Website"] = self._contact_info.website

        if self._billing_address:
            for k, v in self._billing_address.to_salesforce_dict().items():
                payload[f"Billing{k}"] = v

        if self._shipping_address:
            for k, v in self._shipping_address.to_salesforce_dict().items():
                payload[f"Shipping{k}"] = v

        # Remove None values – Salesforce treats absence differently from null
        return {k: v for k, v in payload.items() if v is not None}
