"""
Unit tests for Account domain entity.

Tests domain logic, value objects, and business rules enforced by the
Account aggregate root defined in domain/entities/account.py.

Design:
  - Pure unit tests: no I/O, no database, no HTTP.
  - Each test follows the Arrange / Act / Assert pattern.
  - parametrize is used for invalid-input boundary checks.
  - Fixtures provide canonical valid objects reused across tests.
"""

from __future__ import annotations

import uuid
from datetime import timezone

import pytest

from domain.entities.account import (
    Account,
    AccountStatus,
    AccountType,
    ContactInfo,
    Industry,
)
from domain.events.migration_events import RecordMigrated
from domain.exceptions.domain_exceptions import BusinessRuleViolation, ValidationError
from domain.value_objects.address import Address
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

LEGACY_ID = "LEGACY-ACC-001"
# Valid 18-char Account SF ID (prefix 001, valid checksum)
SF_ID_18 = "001B000000KmPzAIAV"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_billing_address() -> Address:
    return Address(
        street="123 Enterprise Blvd",
        city="San Francisco",
        country_code="US",
        state="CA",
        postal_code="94105",
    )


def _make_email() -> Email:
    return Email("billing@acme-corp.example.com")


def _make_sf_id() -> SalesforceId:
    return SalesforceId(SF_ID_18)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def valid_account() -> Account:
    """Minimal valid Account created via the factory method with all fields."""
    return Account.create(
        legacy_id=LEGACY_ID,
        name="Acme Corporation",
        account_type=AccountType.CUSTOMER,
        status=AccountStatus.ACTIVE,
        industry=Industry.TECHNOLOGY,
        billing_address=_make_billing_address(),
        primary_email=_make_email(),
        annual_revenue=5_000_000.0,
        number_of_employees=250,
        description="Enterprise software company",
    )


@pytest.fixture()
def sf_id() -> SalesforceId:
    return _make_sf_id()


# ===========================================================================
# 1. Creation – happy path
# ===========================================================================


class TestAccountCreation:
    """Account.create() factory method — valid inputs."""

    def test_returns_account_instance(self, valid_account: Account) -> None:
        """Account.create() with valid data returns an Account aggregate."""
        assert isinstance(valid_account, Account)

    def test_assigns_unique_uuid(self) -> None:
        """Each account gets a unique UUID as its identity."""
        a1 = Account.create(legacy_id="L-001", name="Alpha Corp")
        a2 = Account.create(legacy_id="L-002", name="Beta Corp")
        assert isinstance(a1.account_id, uuid.UUID)
        assert a1.account_id != a2.account_id

    def test_stores_name_stripped(self) -> None:
        """Name is stored stripped of surrounding whitespace."""
        account = Account.create(legacy_id="L-001", name="  Trimmed Corp  ")
        assert account.name == "Trimmed Corp"

    def test_stores_legacy_id_stripped(self) -> None:
        """Legacy ID is stored stripped of surrounding whitespace."""
        account = Account.create(legacy_id="  L-001  ", name="Corp")
        assert account.legacy_id == "L-001"

    def test_defaults_type_to_prospect(self) -> None:
        """When no account_type is given the default is PROSPECT."""
        account = Account.create(legacy_id="L-001", name="New Lead Corp")
        assert account.account_type == AccountType.PROSPECT

    def test_defaults_status_to_active(self) -> None:
        """When no status is given the default is ACTIVE."""
        account = Account.create(legacy_id="L-001", name="Corp")
        assert account.status == AccountStatus.ACTIVE

    def test_timestamps_are_utc(self, valid_account: Account) -> None:
        """created_at and updated_at are UTC-aware timestamps."""
        assert valid_account.created_at.tzinfo == timezone.utc
        assert valid_account.updated_at.tzinfo == timezone.utc

    def test_version_starts_at_zero(self, valid_account: Account) -> None:
        """Version counter starts at 0 on a freshly created account."""
        assert valid_account.version == 0

    def test_not_migrated_initially(self, valid_account: Account) -> None:
        """A freshly created account has no Salesforce ID and is_migrated is False."""
        assert valid_account.is_migrated is False
        assert valid_account.salesforce_id is None

    def test_optional_fields_default_to_none(self) -> None:
        """Account can be created with only the required fields."""
        account = Account.create(legacy_id="MIN-001", name="Minimal Corp")
        assert account.billing_address is None
        assert account.primary_email is None
        assert account.annual_revenue is None
        assert account.number_of_employees is None
        assert account.industry is None


# ===========================================================================
# 2. Creation – invalid inputs
# ===========================================================================


class TestAccountCreationValidation:
    """Account.create() raises ValidationError on bad data."""

    @pytest.mark.parametrize(
        "name",
        ["", "   ", "a" * 256],
        ids=["empty", "whitespace_only", "exceeds_255_chars"],
    )
    def test_rejects_invalid_name(self, name: str) -> None:
        """Blank or too-long names must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Account.create(legacy_id="L-001", name=name)
        assert exc_info.value.field == "name"

    @pytest.mark.parametrize(
        "legacy_id",
        ["", "   "],
        ids=["empty", "whitespace_only"],
    )
    def test_rejects_blank_legacy_id(self, legacy_id: str) -> None:
        """Blank legacy_id must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Account.create(legacy_id=legacy_id, name="Corp")
        assert exc_info.value.field == "legacy_id"

    @pytest.mark.parametrize(
        "revenue",
        [-0.01, -1_000.0, -1],
        ids=["negative_fraction", "large_negative", "minus_one"],
    )
    def test_rejects_negative_revenue(self, revenue: float) -> None:
        """Negative annual_revenue must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Account.create(legacy_id="L-001", name="Corp", annual_revenue=revenue)
        assert exc_info.value.field == "annual_revenue"

    @pytest.mark.parametrize(
        "employees",
        [-1, -100],
        ids=["minus_one", "large_negative"],
    )
    def test_rejects_negative_employees(self, employees: int) -> None:
        """Negative number_of_employees must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Account.create(legacy_id="L-001", name="Corp", number_of_employees=employees)
        assert exc_info.value.field == "number_of_employees"


# ===========================================================================
# 3. Domain events
# ===========================================================================


class TestAccountDomainEvents:
    """Domain events are raised and collectable."""

    def test_mark_migrated_raises_record_migrated_event(
        self, valid_account: Account, sf_id: SalesforceId
    ) -> None:
        """mark_migrated() must append a RecordMigrated event."""
        valid_account.mark_migrated(salesforce_id=sf_id, migration_job_id="job-123")
        events = valid_account.collect_events()
        assert len(events) == 1
        assert isinstance(events[0], RecordMigrated)

    def test_collect_events_drains_queue(
        self, valid_account: Account, sf_id: SalesforceId
    ) -> None:
        """Calling collect_events() twice returns events only on the first call."""
        valid_account.mark_migrated(sf_id, "job-001")
        first = valid_account.collect_events()
        second = valid_account.collect_events()
        assert len(first) == 1
        assert len(second) == 0

    def test_record_migrated_event_carries_correct_ids(
        self, valid_account: Account, sf_id: SalesforceId
    ) -> None:
        """RecordMigrated carries the legacy_id, sf_id, and record_type."""
        valid_account.mark_migrated(sf_id, "job-999")
        event = valid_account.collect_events()[0]
        assert isinstance(event, RecordMigrated)
        assert event.legacy_record_id == LEGACY_ID
        assert event.salesforce_record_id == str(sf_id)
        assert event.record_type == "Account"

    def test_no_events_on_fresh_account(self, valid_account: Account) -> None:
        """A freshly created account has no pending domain events."""
        assert valid_account.collect_events() == []


# ===========================================================================
# 4. mark_migrated business rules
# ===========================================================================


class TestMarkMigrated:
    """mark_migrated() mutating method and its business rules."""

    def test_sets_salesforce_id_and_is_migrated(
        self, valid_account: Account, sf_id: SalesforceId
    ) -> None:
        """After mark_migrated(), salesforce_id is set and is_migrated is True."""
        valid_account.mark_migrated(sf_id, "job-001")
        assert valid_account.salesforce_id == sf_id
        assert valid_account.is_migrated is True

    def test_increments_version(self, valid_account: Account, sf_id: SalesforceId) -> None:
        """mark_migrated() must bump the optimistic-lock version counter."""
        before = valid_account.version
        valid_account.mark_migrated(sf_id, "job-001")
        assert valid_account.version == before + 1

    def test_updates_updated_at(self, valid_account: Account, sf_id: SalesforceId) -> None:
        """mark_migrated() must advance updated_at."""
        original_ts = valid_account.updated_at
        valid_account.mark_migrated(sf_id, "job-001")
        assert valid_account.updated_at >= original_ts

    def test_cannot_migrate_already_migrated_account(
        self, valid_account: Account, sf_id: SalesforceId
    ) -> None:
        """Calling mark_migrated() twice raises ACCOUNT_ALREADY_MIGRATED."""
        valid_account.mark_migrated(sf_id, "job-001")
        valid_account.collect_events()  # drain
        with pytest.raises(BusinessRuleViolation) as exc_info:
            valid_account.mark_migrated(sf_id, "job-002")
        assert exc_info.value.rule == "ACCOUNT_ALREADY_MIGRATED"

    def test_cannot_migrate_suspended_account(self, sf_id: SalesforceId) -> None:
        """A suspended account must not be migrated."""
        account = Account.create(
            legacy_id="SUSP-001",
            name="Suspended Corp",
            status=AccountStatus.SUSPENDED,
        )
        with pytest.raises(BusinessRuleViolation) as exc_info:
            account.mark_migrated(sf_id, "job-001")
        assert exc_info.value.rule == "SUSPENDED_ACCOUNT_CANNOT_BE_MIGRATED"


# ===========================================================================
# 5. State machine – deactivate / suspend / reactivate
# ===========================================================================


class TestAccountStateTransitions:
    """deactivate(), suspend(), and reactivate() state machine."""

    def test_deactivate_sets_status_inactive(self, valid_account: Account) -> None:
        """deactivate() must set status to INACTIVE."""
        valid_account.deactivate("No longer a customer")
        assert valid_account.status == AccountStatus.INACTIVE

    def test_deactivate_already_inactive_raises(self, valid_account: Account) -> None:
        """Deactivating an already-inactive account raises ACCOUNT_ALREADY_INACTIVE."""
        valid_account.deactivate()
        with pytest.raises(BusinessRuleViolation) as exc_info:
            valid_account.deactivate()
        assert exc_info.value.rule == "ACCOUNT_ALREADY_INACTIVE"

    def test_suspend_sets_status_suspended(self, valid_account: Account) -> None:
        """suspend() must set status to SUSPENDED."""
        valid_account.suspend("Fraud detected")
        assert valid_account.status == AccountStatus.SUSPENDED

    def test_suspend_already_suspended_raises(self, valid_account: Account) -> None:
        """Suspending an already-suspended account raises ACCOUNT_ALREADY_SUSPENDED."""
        valid_account.suspend()
        with pytest.raises(BusinessRuleViolation) as exc_info:
            valid_account.suspend()
        assert exc_info.value.rule == "ACCOUNT_ALREADY_SUSPENDED"

    def test_reactivate_from_inactive_sets_active(self, valid_account: Account) -> None:
        """reactivate() from INACTIVE must set status back to ACTIVE."""
        valid_account.deactivate()
        valid_account.reactivate()
        assert valid_account.status == AccountStatus.ACTIVE

    def test_reactivate_already_active_raises(self, valid_account: Account) -> None:
        """Reactivating an already-active account raises ACCOUNT_ALREADY_ACTIVE."""
        with pytest.raises(BusinessRuleViolation) as exc_info:
            valid_account.reactivate()
        assert exc_info.value.rule == "ACCOUNT_ALREADY_ACTIVE"


# ===========================================================================
# 6. Field-update mutating methods
# ===========================================================================


class TestAccountMutations:
    """Field-update methods increment version and update timestamps."""

    def test_update_name_changes_name(self, valid_account: Account) -> None:
        """update_name() replaces the name and increments version."""
        before = valid_account.version
        valid_account.update_name("Acme Corp Renamed")
        assert valid_account.name == "Acme Corp Renamed"
        assert valid_account.version == before + 1

    def test_update_name_rejects_blank(self, valid_account: Account) -> None:
        """update_name() with blank value raises ValidationError."""
        with pytest.raises(ValidationError):
            valid_account.update_name("")

    def test_update_billing_address_replaces_vo(self, valid_account: Account) -> None:
        """update_billing_address() stores a new Address value object."""
        new_addr = Address(
            street="456 New HQ Ave",
            city="Austin",
            country_code="US",
            state="TX",
            postal_code="73301",
        )
        valid_account.update_billing_address(new_addr)
        assert valid_account.billing_address == new_addr

    def test_update_revenue_rejects_negative(self, valid_account: Account) -> None:
        """update_revenue() with a negative value raises ValidationError."""
        with pytest.raises(ValidationError):
            valid_account.update_revenue(-500.0)

    def test_update_primary_email_replaces_vo(self, valid_account: Account) -> None:
        """update_primary_email() stores a new Email value object."""
        new_email = Email("cfo@newdomain.example.com")
        valid_account.update_primary_email(new_email)
        assert valid_account.primary_email == new_email

    def test_update_contact_info_replaces_vo(self, valid_account: Account) -> None:
        """update_contact_info() stores a new ContactInfo value object."""
        ci = ContactInfo(phone="4155551234", website="https://new.example.com")
        valid_account.update_contact_info(ci)
        assert valid_account.contact_info == ci


# ===========================================================================
# 7. Entity equality (identity-based, not value-based)
# ===========================================================================


class TestAccountEquality:
    """Account equality is based on account_id, not attribute values."""

    def test_same_object_equals_itself(self, valid_account: Account) -> None:
        """An account must equal itself."""
        assert valid_account == valid_account

    def test_different_ids_not_equal(self) -> None:
        """Two accounts with different UUIDs must not be equal."""
        a1 = Account.create(legacy_id="L-001", name="Corp A")
        a2 = Account.create(legacy_id="L-002", name="Corp A")
        assert a1 != a2

    def test_hash_consistent_with_equality(self) -> None:
        """hash(account) must be stable across calls."""
        account = Account.create(legacy_id="L-001", name="Corp")
        assert hash(account) == hash(account)

    def test_accounts_usable_as_set_members(self) -> None:
        """Two accounts with distinct IDs occupy two slots in a set."""
        a1 = Account.create(legacy_id="L-001", name="Corp A")
        a2 = Account.create(legacy_id="L-002", name="Corp B")
        assert len({a1, a2}) == 2

    def test_not_equal_to_non_account(self, valid_account: Account) -> None:
        """Comparing to a non-Account returns NotImplemented."""
        result = valid_account.__eq__("not an account")
        assert result is NotImplemented


# ===========================================================================
# 8. Salesforce payload serialisation
# ===========================================================================


class TestToSalesforcePayload:
    """Account.to_salesforce_payload() produces a valid SF REST body."""

    def test_contains_required_fields(self, valid_account: Account) -> None:
        """Payload must include Name, Type, and Legacy_ID__c."""
        payload = valid_account.to_salesforce_payload()
        assert "Name" in payload
        assert "Type" in payload
        assert "Legacy_ID__c" in payload

    def test_name_matches_account_name(self, valid_account: Account) -> None:
        """Name in payload matches the Account name."""
        payload = valid_account.to_salesforce_payload()
        assert payload["Name"] == valid_account.name

    def test_excludes_none_values(self) -> None:
        """Payload must not contain None values."""
        account = Account.create(legacy_id="L-001", name="Minimal Corp")
        payload = account.to_salesforce_payload()
        for v in payload.values():
            assert v is not None

    def test_includes_billing_address_fields(self, valid_account: Account) -> None:
        """BillingStreet, BillingCity, BillingCountry present when address is set."""
        payload = valid_account.to_salesforce_payload()
        assert "BillingStreet" in payload
        assert "BillingCity" in payload
        assert "BillingCountry" in payload

    def test_includes_email_custom_field(self, valid_account: Account) -> None:
        """Email__c is present when primary_email is set."""
        payload = valid_account.to_salesforce_payload()
        assert "Email__c" in payload

    def test_legacy_id_matches(self, valid_account: Account) -> None:
        """Legacy_ID__c in payload equals the account's legacy_id."""
        payload = valid_account.to_salesforce_payload()
        assert payload["Legacy_ID__c"] == LEGACY_ID


# ===========================================================================
# 9. ContactInfo nested value object
# ===========================================================================


class TestContactInfo:
    """ContactInfo nested value object — immutability, normalisation, equality."""

    def test_is_immutable(self) -> None:
        """ContactInfo must reject attribute assignment after construction."""
        ci = ContactInfo(phone="5551234567")
        with pytest.raises(AttributeError):
            ci.phone = "9999999999"  # type: ignore[misc]

    def test_normalises_phone_strips_punctuation(self) -> None:
        """Phone punctuation is stripped, leaving only digits (and leading +)."""
        ci = ContactInfo(phone="(555) 123-4567")
        assert ci.phone == "5551234567"

    def test_prepends_https_to_bare_domain(self) -> None:
        """Website without a scheme gets https:// prepended."""
        ci = ContactInfo(website="acme.example.com")
        assert ci.website == "https://acme.example.com"

    def test_equality_by_value(self) -> None:
        """Two ContactInfo objects with identical values are equal."""
        ci1 = ContactInfo(phone="5551234567", website="https://example.com")
        ci2 = ContactInfo(phone="5551234567", website="https://example.com")
        assert ci1 == ci2

    def test_all_optional_fields_can_be_none(self) -> None:
        """ContactInfo can be created with all optional fields omitted."""
        ci = ContactInfo()
        assert ci.phone is None
        assert ci.fax is None
        assert ci.website is None
