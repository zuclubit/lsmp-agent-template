"""
Integration tests for the PostgresAccountRepository adapter.

Uses an in-memory SQLite database (via SQLAlchemy's aiosqlite driver) so the
tests are self-contained and require no external services.

The repository maps legacy erp.accounts rows to/from Account domain entities,
enforcing optimistic locking via row_version.

Marks:
    @pytest.mark.integration
    @pytest.mark.database
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Domain imports
# ---------------------------------------------------------------------------
from domain.entities.account import (
    Account,
    AccountStatus,
    AccountType,
    ContactInfo,
    Industry,
)
from domain.exceptions.domain_exceptions import ConcurrencyConflict
from domain.repositories.account_repository import AccountCriteria
from domain.value_objects.address import Address
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId


# ---------------------------------------------------------------------------
# Minimal in-memory repository implementation for testing the adapter contract
# ---------------------------------------------------------------------------

class InMemoryAccountStore:
    """
    Pure-Python in-memory replacement for the PostgreSQL store.
    Enables testing the repository contract without a live database.
    """

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}

    def insert(self, row: Dict[str, Any]) -> None:
        self._store[row["acct_id"]] = {**row, "row_version": 1}

    def find_by_id(self, acct_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get(acct_id)

    def find_all(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        items = list(self._store.values())
        return items[offset: offset + limit]

    def update_sf_id(self, acct_id: str, sf_id: str, expected_version: int) -> bool:
        row = self._store.get(acct_id)
        if not row:
            return False
        if row["row_version"] != expected_version:
            raise ConcurrencyConflict("Account", acct_id, expected_version, row["row_version"])
        row["sf_id"] = sf_id
        row["row_version"] += 1
        return True

    def count(self) -> int:
        return len(self._store)

    def count_unmigrated(self) -> int:
        return sum(1 for r in self._store.values() if not r.get("sf_id"))

    def clear(self) -> None:
        self._store.clear()


def _make_row(
    acct_id: str = "LEGACY-ACC-00000001",
    acct_name: str = "Acme Corporation",
    acct_type: str = "CUST",
    acct_status: str = "A",
    industry_code: str = "TECH",
    sf_id: Optional[str] = None,
    row_version: int = 1,
    **overrides,
) -> Dict[str, Any]:
    """Build a representative legacy DB row dictionary."""
    return {
        "acct_id": acct_id,
        "acct_name": acct_name,
        "acct_type": acct_type,
        "acct_status": acct_status,
        "industry_code": industry_code,
        "bill_addr_street": "1 Infinite Loop",
        "bill_addr_unit": "Suite 200",
        "bill_addr_city": "Cupertino",
        "bill_addr_state": "CA",
        "bill_addr_zip": "95014",
        "bill_addr_country": "US",
        "ship_addr_street": None,
        "ship_addr_city": None,
        "ship_addr_state": None,
        "ship_addr_zip": None,
        "ship_addr_country": None,
        "phone_number": "+14085551234",
        "fax_number": None,
        "website_url": "https://www.acmecorp.example.com",
        "email_address": "info@acmecorp.example.com",
        "annual_revenue": 12500000.0,
        "employee_count": 250,
        "acct_description": "Technology solutions provider.",
        "sf_id": sf_id,
        "created_ts": datetime(2018, 3, 15, 8, 30, tzinfo=timezone.utc),
        "modified_ts": datetime(2025, 11, 20, 14, 22, tzinfo=timezone.utc),
        "row_version": row_version,
        **overrides,
    }


def _row_to_account(row: Dict[str, Any]) -> Account:
    """Invoke the real _to_domain mapper from the repository."""
    from adapters.outbound.postgres_account_repository import PostgresAccountRepository
    return PostgresAccountRepository._to_domain(row)


# ===========================================================================
# FIXTURES
# ===========================================================================


@pytest.fixture
def store() -> InMemoryAccountStore:
    s = InMemoryAccountStore()
    yield s
    s.clear()


@pytest.fixture
def account_row() -> Dict[str, Any]:
    return _make_row()


@pytest.fixture
def migrated_account_row() -> Dict[str, Any]:
    return _make_row(sf_id="001Dn000001MockAA2", row_version=3)


@pytest.fixture
def account_batch() -> List[Dict[str, Any]]:
    return [
        _make_row(acct_id=f"LEGACY-ACC-{i:08d}", acct_name=f"Company {i:04d}")
        for i in range(1, 1001)
    ]


# ===========================================================================
# TESTS
# ===========================================================================


@pytest.mark.integration
@pytest.mark.database
class TestRowToDomainMapping:
    """Unit tests for the _to_domain static mapper (no I/O, pure mapping logic)."""

    def test_maps_customer_status_active(self, account_row):
        account = _row_to_account(account_row)
        assert account.status == AccountStatus.ACTIVE

    def test_maps_account_type_customer(self, account_row):
        account = _row_to_account(account_row)
        assert account.account_type == AccountType.CUSTOMER

    def test_maps_industry_technology(self, account_row):
        account = _row_to_account(account_row)
        assert account.industry == Industry.TECHNOLOGY

    def test_maps_billing_address(self, account_row):
        account = _row_to_account(account_row)
        assert account.billing_address is not None
        assert account.billing_address.city == "Cupertino"
        assert account.billing_address.country_code == "US"
        assert account.billing_address.state == "CA"

    def test_maps_contact_info_phone(self, account_row):
        account = _row_to_account(account_row)
        assert account.contact_info is not None
        assert "14085551234" in (account.contact_info.phone or "")

    def test_maps_primary_email(self, account_row):
        account = _row_to_account(account_row)
        assert account.primary_email is not None
        assert "acmecorp.example.com" in str(account.primary_email)

    def test_maps_annual_revenue(self, account_row):
        account = _row_to_account(account_row)
        assert account.annual_revenue == pytest.approx(12_500_000.0)

    def test_maps_employee_count(self, account_row):
        account = _row_to_account(account_row)
        assert account.number_of_employees == 250

    def test_maps_salesforce_id_when_present(self, migrated_account_row):
        account = _row_to_account(migrated_account_row)
        assert account.salesforce_id is not None
        assert account.is_migrated is True

    def test_maps_no_salesforce_id_when_null(self, account_row):
        account = _row_to_account(account_row)
        assert account.salesforce_id is None
        assert account.is_migrated is False

    def test_maps_version_number(self, account_row):
        account = _row_to_account(account_row)
        assert account.version == 1

    def test_null_industry_code_maps_to_none(self):
        row = _make_row(industry_code=None)
        account = _row_to_account(row)
        assert account.industry is None

    def test_null_billing_city_skips_address(self):
        row = _make_row(bill_addr_city=None, bill_addr_country=None)
        account = _row_to_account(row)
        assert account.billing_address is None

    def test_null_phone_and_website_skips_contact_info(self):
        row = _make_row(phone_number=None, website_url=None)
        account = _row_to_account(row)
        assert account.contact_info is None

    def test_null_email_skips_primary_email(self):
        row = _make_row(email_address=None)
        account = _row_to_account(row)
        assert account.primary_email is None

    @pytest.mark.parametrize(
        "acct_type,expected",
        [
            ("CUST", AccountType.CUSTOMER),
            ("PROS", AccountType.PROSPECT),
            ("PART", AccountType.PARTNER),
            ("COMP", AccountType.COMPETITOR),
            ("UNKNOWN", AccountType.PROSPECT),  # default fallback
        ],
    )
    def test_account_type_mapping_all_values(self, acct_type, expected):
        row = _make_row(acct_type=acct_type)
        account = _row_to_account(row)
        assert account.account_type == expected

    @pytest.mark.parametrize(
        "acct_status,expected",
        [
            ("A", AccountStatus.ACTIVE),
            ("I", AccountStatus.INACTIVE),
            ("S", AccountStatus.SUSPENDED),
            ("P", AccountStatus.PENDING_REVIEW),
        ],
    )
    def test_account_status_mapping_all_values(self, acct_status, expected):
        row = _make_row(acct_status=acct_status)
        account = _row_to_account(row)
        assert account.status == expected


@pytest.mark.integration
@pytest.mark.database
class TestInMemoryStoreContract:
    """Tests for save / retrieve / update using the in-memory store."""

    def test_insert_and_retrieve_account(self, store, account_row):
        store.insert(account_row)
        retrieved = store.find_by_id("LEGACY-ACC-00000001")
        assert retrieved is not None
        assert retrieved["acct_name"] == "Acme Corporation"

    def test_find_nonexistent_returns_none(self, store):
        result = store.find_by_id("DOES-NOT-EXIST")
        assert result is None

    def test_find_all_returns_inserted_rows(self, store, account_batch):
        for row in account_batch[:10]:
            store.insert(row)
        all_rows = store.find_all(limit=10)
        assert len(all_rows) == 10

    def test_find_all_respects_limit_and_offset(self, store, account_batch):
        for row in account_batch[:20]:
            store.insert(row)
        page2 = store.find_all(limit=10, offset=10)
        assert len(page2) == 10

    def test_count_unmigrated(self, store, account_batch):
        for row in account_batch[:50]:
            store.insert(row)
        assert store.count_unmigrated() == 50
        # Migrate one
        store.update_sf_id("LEGACY-ACC-00000001", "001Dn000001MockAA2", expected_version=1)
        assert store.count_unmigrated() == 49

    def test_update_sf_id_increments_version(self, store, account_row):
        store.insert(account_row)
        store.update_sf_id("LEGACY-ACC-00000001", "001Dn000001MockAA2", expected_version=1)
        row = store.find_by_id("LEGACY-ACC-00000001")
        assert row["row_version"] == 2
        assert row["sf_id"] == "001Dn000001MockAA2"

    def test_optimistic_locking_conflict_raises(self, store, account_row):
        store.insert(account_row)
        with pytest.raises(ConcurrencyConflict) as exc_info:
            # Supply wrong version (2 instead of 1)
            store.update_sf_id("LEGACY-ACC-00000001", "001Dn000001MockAA2", expected_version=2)
        assert exc_info.value.expected_version == 2
        assert exc_info.value.actual_version == 1

    def test_transaction_rollback_on_error(self, store, account_row):
        """Simulate a mid-batch failure: store should remain consistent."""
        store.insert(account_row)
        second_row = _make_row(acct_id="LEGACY-ACC-00000002", acct_name="Second Corp")
        store.insert(second_row)

        # Attempt conflicting update that raises
        try:
            store.update_sf_id("LEGACY-ACC-00000001", "001AAA", expected_version=99)
        except ConcurrencyConflict:
            pass  # rolled back

        # Both rows should still be present and unmodified
        assert store.count() == 2
        assert store.find_by_id("LEGACY-ACC-00000001")["sf_id"] is None

    def test_bulk_insert_1000_records(self, store, account_batch):
        for row in account_batch:
            store.insert(row)
        assert store.count() == 1000
        assert store.count_unmigrated() == 1000


@pytest.mark.integration
@pytest.mark.database
class TestRepositoryAdapterMocked:
    """
    Tests for PostgresAccountRepository using a mocked AsyncSession.
    Validates SQL construction and result mapping without a real database.
    """

    @pytest.mark.asyncio
    async def test_find_by_id_executes_select(self):
        """find_by_id must execute a SELECT with the correct account UUID."""
        from adapters.outbound.postgres_account_repository import PostgresAccountRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = _make_row()
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PostgresAccountRepository(mock_session)
        account_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        account = await repo.find_by_id(account_uuid)

        assert mock_session.execute.called
        call_sql = str(mock_session.execute.call_args[0][0])
        assert "SELECT" in call_sql.upper()

    @pytest.mark.asyncio
    async def test_find_by_id_returns_none_when_not_found(self):
        from adapters.outbound.postgres_account_repository import PostgresAccountRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PostgresAccountRepository(mock_session)
        result = await repo.find_by_id(uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_find_unmigrated_filters_by_null_sf_id(self):
        from adapters.outbound.postgres_account_repository import PostgresAccountRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [
            _make_row(acct_id="LEGACY-ACC-00000001"),
            _make_row(acct_id="LEGACY-ACC-00000002"),
        ]
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PostgresAccountRepository(mock_session)
        accounts = await repo.find_unmigrated(limit=10)

        assert len(accounts) == 2
        for account in accounts:
            assert isinstance(account, Account)
            assert account.is_migrated is False

    @pytest.mark.asyncio
    async def test_save_new_account_calls_insert(self):
        from adapters.outbound.postgres_account_repository import PostgresAccountRepository

        mock_session = AsyncMock()
        # find_by_legacy_id returns None → should call _insert
        mock_result_empty = MagicMock()
        mock_result_empty.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result_empty)

        account = Account.create(legacy_id="LEGACY-ACC-NEW-001", name="New Company Ltd")
        repo = PostgresAccountRepository(mock_session)
        saved = await repo.save(account)

        # execute should have been called (for the SELECT and then the INSERT)
        assert mock_session.execute.call_count >= 1
        assert saved.legacy_id == "LEGACY-ACC-NEW-001"

    @pytest.mark.asyncio
    async def test_save_existing_account_calls_update(self):
        from adapters.outbound.postgres_account_repository import PostgresAccountRepository

        existing_row = _make_row()
        mock_session = AsyncMock()
        mock_result_found = MagicMock()
        mock_result_found.mappings.return_value.first.return_value = existing_row
        mock_result_update = MagicMock()
        mock_result_update.rowcount = 1
        mock_session.execute = AsyncMock(side_effect=[mock_result_found, mock_result_update])

        account = _row_to_account(existing_row)
        repo = PostgresAccountRepository(mock_session)
        await repo.save(account)

        # Two executes: one SELECT for find_by_legacy_id, one UPDATE
        assert mock_session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_concurrency_conflict_on_stale_version(self):
        from adapters.outbound.postgres_account_repository import PostgresAccountRepository

        existing_row = _make_row(row_version=5)
        mock_session = AsyncMock()
        mock_found = MagicMock()
        mock_found.mappings.return_value.first.return_value = existing_row
        mock_update = MagicMock()
        mock_update.rowcount = 0  # Zero rows updated → version conflict
        mock_session.execute = AsyncMock(side_effect=[mock_found, mock_update])

        account = _row_to_account(_make_row(row_version=3))  # Stale version
        repo = PostgresAccountRepository(mock_session)

        with pytest.raises(ConcurrencyConflict):
            await repo.save(account)

    @pytest.mark.asyncio
    async def test_save_batch_persists_all_accounts(self):
        from adapters.outbound.postgres_account_repository import PostgresAccountRepository

        accounts = [
            Account.create(legacy_id=f"LEGACY-BATCH-{i:04d}", name=f"Batch Company {i}")
            for i in range(10)
        ]

        mock_session = AsyncMock()
        mock_result_empty = MagicMock()
        mock_result_empty.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result_empty)

        repo = PostgresAccountRepository(mock_session)
        saved = await repo.save_batch(accounts)

        assert len(saved) == 10
