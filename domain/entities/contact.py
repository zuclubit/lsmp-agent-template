"""
Contact domain entity.

A Contact belongs to (is owned by) an Account in the legacy system and must
be migrated to a Salesforce Contact linked to the corresponding Account.

Design:
  - Entity with UUID identity.
  - References Account by identity (account_id), not by object reference,
    to keep aggregate boundaries clean.
  - Value objects used for Email and Address.
  - Domain events raised on significant state changes.
  - No framework imports.
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
)
from domain.value_objects.address import Address
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Supporting enumerations
# ---------------------------------------------------------------------------


class ContactStatus(str, Enum):
    ACTIVE = "Active"
    INACTIVE = "Inactive"
    DO_NOT_CONTACT = "DoNotContact"
    DECEASED = "Deceased"


class Salutation(str, Enum):
    MR = "Mr."
    MS = "Ms."
    MRS = "Mrs."
    DR = "Dr."
    PROF = "Prof."
    NONE = ""


class LeadSource(str, Enum):
    WEB = "Web"
    PHONE = "Phone"
    EMAIL = "Email"
    REFERRAL = "Referral"
    ADVERTISING = "Advertising"
    PARTNER = "Partner"
    TRADE_SHOW = "Trade Show"
    WORD_OF_MOUTH = "Word of Mouth"
    OTHER = "Other"


# ---------------------------------------------------------------------------
# Name value object
# ---------------------------------------------------------------------------


class PersonName:
    """
    Immutable value object representing a person's name.
    Keeps salutation, first, middle, last, and suffix together.
    """

    __slots__ = ("_salutation", "_first_name", "_middle_name", "_last_name", "_suffix")

    def __init__(
        self,
        first_name: str,
        last_name: str,
        salutation: Salutation = Salutation.NONE,
        middle_name: Optional[str] = None,
        suffix: Optional[str] = None,
    ) -> None:
        if not first_name or not first_name.strip():
            raise ValidationError("first_name", first_name, "first_name cannot be blank")
        if len(first_name.strip()) > 80:
            raise ValidationError("first_name", first_name, "first_name exceeds 80 characters")
        if not last_name or not last_name.strip():
            raise ValidationError("last_name", last_name, "last_name cannot be blank")
        if len(last_name.strip()) > 80:
            raise ValidationError("last_name", last_name, "last_name exceeds 80 characters")

        object.__setattr__(self, "_salutation", salutation)
        object.__setattr__(self, "_first_name", first_name.strip())
        object.__setattr__(self, "_middle_name", middle_name.strip() if middle_name else None)
        object.__setattr__(self, "_last_name", last_name.strip())
        object.__setattr__(self, "_suffix", suffix.strip() if suffix else None)

    def __setattr__(self, name: str, value: object) -> None:  # type: ignore[override]
        raise AttributeError("PersonName is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PersonName):
            return NotImplemented
        return (
            self._first_name == other._first_name
            and self._last_name == other._last_name
            and self._middle_name == other._middle_name
            and self._salutation == other._salutation
            and self._suffix == other._suffix
        )

    def __hash__(self) -> int:
        return hash((self._first_name, self._middle_name, self._last_name))

    def __repr__(self) -> str:
        return f"PersonName({self.full_name!r})"

    def __str__(self) -> str:
        return self.full_name

    @property
    def salutation(self) -> Salutation:
        return self._salutation

    @property
    def first_name(self) -> str:
        return self._first_name

    @property
    def middle_name(self) -> Optional[str]:
        return self._middle_name

    @property
    def last_name(self) -> str:
        return self._last_name

    @property
    def suffix(self) -> Optional[str]:
        return self._suffix

    @property
    def full_name(self) -> str:
        parts = []
        if self._salutation and self._salutation != Salutation.NONE:
            parts.append(self._salutation.value)
        parts.append(self._first_name)
        if self._middle_name:
            parts.append(self._middle_name)
        parts.append(self._last_name)
        if self._suffix:
            parts.append(self._suffix)
        return " ".join(parts)

    @property
    def display_name(self) -> str:
        """Informal form used in UI: FirstName LastName."""
        return f"{self._first_name} {self._last_name}"


# ---------------------------------------------------------------------------
# Contact aggregate
# ---------------------------------------------------------------------------


class Contact:
    """
    Contact aggregate root.

    Belongs to exactly one Account (referenced by account_id).
    Tracks migration state independently of its owning Account.

    Invariants:
      - name (first + last) is required.
      - email is recommended; absence is permitted for legacy data quality reasons.
      - A Contact linked to a DoNotContact status cannot receive marketing comms.
      - A Contact cannot be migrated twice.
      - Account must be migrated before its Contacts (enforced at use-case level).
    """

    def __init__(
        self,
        contact_id: UUID,
        legacy_id: str,
        account_id: UUID,
        legacy_account_id: str,
        name: PersonName,
        status: ContactStatus,
        email: Optional[Email],
        mobile_phone: Optional[str],
        work_phone: Optional[str],
        mailing_address: Optional[Address],
        title: Optional[str],
        department: Optional[str],
        lead_source: Optional[LeadSource],
        do_not_call: bool,
        do_not_email: bool,
        salesforce_id: Optional[SalesforceId],
        salesforce_account_id: Optional[SalesforceId],
        created_at: datetime,
        updated_at: datetime,
        version: int = 0,
    ) -> None:
        self._contact_id = contact_id
        self._legacy_id = legacy_id
        self._account_id = account_id
        self._legacy_account_id = legacy_account_id
        self._name = name
        self._status = status
        self._email = email
        self._mobile_phone = mobile_phone
        self._work_phone = work_phone
        self._mailing_address = mailing_address
        self._title = title
        self._department = department
        self._lead_source = lead_source
        self._do_not_call = do_not_call
        self._do_not_email = do_not_email
        self._salesforce_id = salesforce_id
        self._salesforce_account_id = salesforce_account_id
        self._created_at = created_at
        self._updated_at = updated_at
        self._version = version
        self._domain_events: list[DomainEvent] = []

    # ------------------------------------------------------------------
    # Factory method
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        legacy_id: str,
        account_id: UUID,
        legacy_account_id: str,
        name: PersonName,
        status: ContactStatus = ContactStatus.ACTIVE,
        email: Optional[Email] = None,
        mobile_phone: Optional[str] = None,
        work_phone: Optional[str] = None,
        mailing_address: Optional[Address] = None,
        title: Optional[str] = None,
        department: Optional[str] = None,
        lead_source: Optional[LeadSource] = None,
        do_not_call: bool = False,
        do_not_email: bool = False,
    ) -> "Contact":
        if not legacy_id or not legacy_id.strip():
            raise ValidationError("legacy_id", legacy_id, "legacy_id cannot be blank")

        now = _utcnow()
        return cls(
            contact_id=uuid.uuid4(),
            legacy_id=legacy_id.strip(),
            account_id=account_id,
            legacy_account_id=legacy_account_id.strip(),
            name=name,
            status=status,
            email=email,
            mobile_phone=mobile_phone,
            work_phone=work_phone,
            mailing_address=mailing_address,
            title=title.strip() if title else None,
            department=department.strip() if department else None,
            lead_source=lead_source,
            do_not_call=do_not_call,
            do_not_email=do_not_email,
            salesforce_id=None,
            salesforce_account_id=None,
            created_at=now,
            updated_at=now,
            version=0,
        )

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def contact_id(self) -> UUID:
        return self._contact_id

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Contact):
            return NotImplemented
        return self._contact_id == other._contact_id

    def __hash__(self) -> int:
        return hash(self._contact_id)

    def __repr__(self) -> str:
        return (
            f"Contact(id={self._contact_id}, legacy_id={self._legacy_id!r}, "
            f"name={self._name.display_name!r}, status={self._status.value})"
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def legacy_id(self) -> str:
        return self._legacy_id

    @property
    def account_id(self) -> UUID:
        return self._account_id

    @property
    def legacy_account_id(self) -> str:
        return self._legacy_account_id

    @property
    def name(self) -> PersonName:
        return self._name

    @property
    def status(self) -> ContactStatus:
        return self._status

    @property
    def email(self) -> Optional[Email]:
        return self._email

    @property
    def mobile_phone(self) -> Optional[str]:
        return self._mobile_phone

    @property
    def work_phone(self) -> Optional[str]:
        return self._work_phone

    @property
    def mailing_address(self) -> Optional[Address]:
        return self._mailing_address

    @property
    def title(self) -> Optional[str]:
        return self._title

    @property
    def department(self) -> Optional[str]:
        return self._department

    @property
    def lead_source(self) -> Optional[LeadSource]:
        return self._lead_source

    @property
    def do_not_call(self) -> bool:
        return self._do_not_call

    @property
    def do_not_email(self) -> bool:
        return self._do_not_email

    @property
    def salesforce_id(self) -> Optional[SalesforceId]:
        return self._salesforce_id

    @property
    def salesforce_account_id(self) -> Optional[SalesforceId]:
        return self._salesforce_account_id

    @property
    def is_migrated(self) -> bool:
        return self._salesforce_id is not None

    @property
    def is_contactable(self) -> bool:
        """True when the contact can receive outbound communications."""
        return (
            self._status not in (ContactStatus.DO_NOT_CONTACT, ContactStatus.DECEASED)
            and not self._do_not_call
            and not self._do_not_email
        )

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
    # Domain events
    # ------------------------------------------------------------------

    def collect_events(self) -> list[DomainEvent]:
        events = list(self._domain_events)
        self._domain_events.clear()
        return events

    # ------------------------------------------------------------------
    # Mutating domain methods
    # ------------------------------------------------------------------

    def mark_migrated(
        self,
        salesforce_id: SalesforceId,
        salesforce_account_id: SalesforceId,
        migration_job_id: str,
        phase: str = "data_load",
    ) -> None:
        if self.is_migrated:
            raise BusinessRuleViolation(
                rule="CONTACT_ALREADY_MIGRATED",
                message=(
                    f"Contact {self._legacy_id} is already migrated "
                    f"to Salesforce ID {self._salesforce_id}"
                ),
            )

        self._salesforce_id = salesforce_id
        self._salesforce_account_id = salesforce_account_id
        self._updated_at = _utcnow()
        self._version += 1

        self._domain_events.append(
            RecordMigrated(
                migration_job_id=migration_job_id,
                legacy_record_id=self._legacy_id,
                salesforce_record_id=str(salesforce_id),
                record_type="Contact",
                phase=phase,
            )
        )

    def update_email(self, email: Email) -> None:
        self._email = email
        self._updated_at = _utcnow()
        self._version += 1

    def update_mailing_address(self, address: Address) -> None:
        self._mailing_address = address
        self._updated_at = _utcnow()
        self._version += 1

    def set_do_not_contact(self, do_not_call: bool, do_not_email: bool) -> None:
        self._do_not_call = do_not_call
        self._do_not_email = do_not_email
        if do_not_call and do_not_email:
            self._status = ContactStatus.DO_NOT_CONTACT
        self._updated_at = _utcnow()
        self._version += 1

    def deactivate(self) -> None:
        self._status = ContactStatus.INACTIVE
        self._updated_at = _utcnow()
        self._version += 1

    # ------------------------------------------------------------------
    # Salesforce mapping helper
    # ------------------------------------------------------------------

    def to_salesforce_payload(self, salesforce_account_id: Optional[SalesforceId] = None) -> dict[str, object]:
        sf_acct_id = salesforce_account_id or self._salesforce_account_id
        payload: dict[str, object] = {
            "FirstName": self._name.first_name,
            "LastName": self._name.last_name,
            "Salutation": self._name.salutation.value if self._name.salutation != Salutation.NONE else None,
            "Title": self._title,
            "Department": self._department,
            "MobilePhone": self._mobile_phone,
            "Phone": self._work_phone,
            "DoNotCall": self._do_not_call,
            "HasOptedOutOfEmail": self._do_not_email,
            "LeadSource": self._lead_source.value if self._lead_source else None,
            "Legacy_ID__c": self._legacy_id,
        }

        if sf_acct_id:
            payload["AccountId"] = str(sf_acct_id)

        if self._email:
            payload["Email"] = str(self._email)

        if self._mailing_address:
            for k, v in self._mailing_address.to_salesforce_dict().items():
                payload[f"Mailing{k}"] = v

        return {k: v for k, v in payload.items() if v is not None}
