"""
PostgresAccountRepository – outbound adapter implementing AccountRepository.

Reads from the legacy PostgreSQL database using SQLAlchemy async (asyncpg).
Acts as the SOURCE repository during extraction: it reads legacy account
records and maps them to domain Account entities.

During the migration pipeline:
  1. This adapter reads legacy accounts (find_* methods).
  2. SalesforceAccountRepository writes to Salesforce (save methods).
  3. A staging/tracking DB (separate schema) records migration outcomes.

Table assumed: erp.accounts (legacy schema, column names as per legacy_account_schema.json)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

# SQLAlchemy imports – infrastructure, kept in adapter layer only
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.account import (
    Account,
    AccountStatus,
    AccountType,
    ContactInfo,
    Industry,
)
from domain.exceptions.domain_exceptions import AccountNotFound, ConcurrencyConflict
from domain.repositories.account_repository import AccountCriteria, AccountRepository, PagedResult
from domain.value_objects.address import Address
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy DB column → domain field mapping constants
# ---------------------------------------------------------------------------

_TABLE = "erp.accounts"

_SELECT_COLS = """
    acct_id,
    acct_name,
    acct_type,
    acct_status,
    industry_code,
    bill_addr_street,
    bill_addr_unit,
    bill_addr_city,
    bill_addr_state,
    bill_addr_zip,
    bill_addr_country,
    ship_addr_street,
    ship_addr_city,
    ship_addr_state,
    ship_addr_zip,
    ship_addr_country,
    phone_number,
    fax_number,
    website_url,
    email_address,
    annual_revenue,
    employee_count,
    acct_description,
    sf_id,
    created_ts,
    modified_ts,
    row_version
"""

# ---------------------------------------------------------------------------
# Account status / type / industry maps (legacy code → domain enum)
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, AccountStatus] = {
    "A": AccountStatus.ACTIVE,
    "I": AccountStatus.INACTIVE,
    "S": AccountStatus.SUSPENDED,
    "P": AccountStatus.PENDING_REVIEW,
}

_TYPE_MAP: dict[str, AccountType] = {
    "CUST": AccountType.CUSTOMER,
    "PROS": AccountType.PROSPECT,
    "PART": AccountType.PARTNER,
    "COMP": AccountType.COMPETITOR,
}

_INDUSTRY_MAP: dict[str, Industry] = {
    "TECH": Industry.TECHNOLOGY,
    "FIN": Industry.FINANCE,
    "HLTH": Industry.HEALTHCARE,
    "MFG": Industry.MANUFACTURING,
    "RET": Industry.RETAIL,
    "GOVT": Industry.GOVERNMENT,
    "EDU": Industry.EDUCATION,
    "MEDIA": Industry.MEDIA,
    "BANK": Industry.BANKING,
    "INS": Industry.INSURANCE,
    "TRANS": Industry.TRANSPORTATION,
    "UTIL": Industry.UTILITIES,
}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class PostgresAccountRepository(AccountRepository):
    """
    Reads legacy account data from the source PostgreSQL database.

    The session is injected per request (Unit of Work pattern):
    each application service method receives a fresh session within
    its transaction boundary.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # AccountRepository interface
    # ------------------------------------------------------------------

    async def find_by_id(self, account_id: UUID) -> Optional[Account]:
        """Look up by domain UUID (stored in a UUID column in the staging schema)."""
        result = await self._session.execute(
            text(f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE acct_id = :id LIMIT 1"),
            {"id": str(account_id)},
        )
        row = result.mappings().first()
        return self._to_domain(dict(row)) if row else None

    async def find_by_legacy_id(self, legacy_id: str) -> Optional[Account]:
        result = await self._session.execute(
            text(f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE acct_id = :id LIMIT 1"),
            {"id": legacy_id},
        )
        row = result.mappings().first()
        return self._to_domain(dict(row)) if row else None

    async def find_by_salesforce_id(self, salesforce_id: SalesforceId) -> Optional[Account]:
        result = await self._session.execute(
            text(f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE sf_id = :sf_id LIMIT 1"),
            {"sf_id": salesforce_id.id18},
        )
        row = result.mappings().first()
        return self._to_domain(dict(row)) if row else None

    async def find_all(self, limit: int = 100, offset: int = 0) -> list[Account]:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLS} FROM {_TABLE} "
                f"ORDER BY created_ts ASC "
                f"LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
        return [self._to_domain(dict(row)) for row in result.mappings().all()]

    async def find_by_criteria(self, criteria: AccountCriteria) -> PagedResult:
        where_parts: list[str] = []
        params: dict[str, Any] = {}

        if criteria.is_migrated is True:
            where_parts.append("sf_id IS NOT NULL")
        elif criteria.is_migrated is False:
            where_parts.append("sf_id IS NULL")

        if criteria.statuses:
            status_codes = [
                k for k, v in _STATUS_MAP.items() if v in criteria.statuses
            ]
            where_parts.append("acct_status = ANY(:statuses)")
            params["statuses"] = status_codes

        if criteria.account_types:
            type_codes = [k for k, v in _TYPE_MAP.items() if v in criteria.account_types]
            where_parts.append("acct_type = ANY(:types)")
            params["types"] = type_codes

        if criteria.industries:
            industry_codes = [k for k, v in _INDUSTRY_MAP.items() if v in criteria.industries]
            where_parts.append("industry_code = ANY(:industries)")
            params["industries"] = industry_codes

        if criteria.name_contains:
            where_parts.append("acct_name ILIKE :name_pattern")
            params["name_pattern"] = f"%{criteria.name_contains}%"

        if criteria.created_after:
            where_parts.append("created_ts >= :created_after")
            params["created_after"] = criteria.created_after

        if criteria.created_before:
            where_parts.append("created_ts <= :created_before")
            params["created_before"] = criteria.created_before

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        order_dir = "ASC" if criteria.order_asc else "DESC"
        order_col = self._map_order_by(criteria.order_by)

        count_sql = text(f"SELECT COUNT(*) FROM {_TABLE} {where_sql}")
        count_result = await self._session.execute(count_sql, params)
        total_count = count_result.scalar() or 0

        params["limit"] = criteria.limit
        params["offset"] = criteria.offset
        data_sql = text(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} {where_sql} "
            f"ORDER BY {order_col} {order_dir} "
            f"LIMIT :limit OFFSET :offset"
        )
        data_result = await self._session.execute(data_sql, params)
        items = [self._to_domain(dict(row)) for row in data_result.mappings().all()]

        return PagedResult(
            items=items,
            total_count=total_count,
            limit=criteria.limit,
            offset=criteria.offset,
        )

    async def find_unmigrated(self, limit: int = 200) -> list[Account]:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLS} FROM {_TABLE} "
                f"WHERE sf_id IS NULL "
                f"ORDER BY acct_id ASC "
                f"LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [self._to_domain(dict(row)) for row in result.mappings().all()]

    async def count_unmigrated(self) -> int:
        result = await self._session.execute(
            text(f"SELECT COUNT(*) FROM {_TABLE} WHERE sf_id IS NULL")
        )
        return result.scalar() or 0

    async def save(self, account: Account) -> Account:
        """
        Upsert an Account back to the legacy/staging database.

        Uses optimistic locking via row_version.
        """
        existing = await self.find_by_legacy_id(account.legacy_id)
        if existing is None:
            await self._insert(account)
        else:
            await self._update(account)
        return account

    async def save_batch(self, accounts: list[Account]) -> list[Account]:
        for account in accounts:
            await self.save(account)
        return accounts

    async def delete(self, account_id: UUID) -> bool:
        result = await self._session.execute(
            text(f"DELETE FROM {_TABLE} WHERE acct_id = :id"),
            {"id": str(account_id)},
        )
        return result.rowcount > 0

    async def exists(self, account_id: UUID) -> bool:
        result = await self._session.execute(
            text(f"SELECT 1 FROM {_TABLE} WHERE acct_id = :id LIMIT 1"),
            {"id": str(account_id)},
        )
        return result.scalar() is not None

    async def find_by_email(self, email: Email) -> list[Account]:
        result = await self._session.execute(
            text(f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE LOWER(email_address) = :email"),
            {"email": str(email)},
        )
        return [self._to_domain(dict(row)) for row in result.mappings().all()]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _insert(self, account: Account) -> None:
        await self._session.execute(
            text(f"""
                INSERT INTO {_TABLE} (
                    acct_id, acct_name, acct_type, acct_status, industry_code,
                    bill_addr_street, bill_addr_city, bill_addr_state, bill_addr_zip, bill_addr_country,
                    phone_number, fax_number, website_url, email_address,
                    annual_revenue, employee_count, acct_description, sf_id,
                    created_ts, modified_ts, row_version
                ) VALUES (
                    :acct_id, :acct_name, :acct_type, :acct_status, :industry_code,
                    :bill_street, :bill_city, :bill_state, :bill_zip, :bill_country,
                    :phone, :fax, :website, :email,
                    :annual_revenue, :employee_count, :description, :sf_id,
                    :created_ts, :modified_ts, 1
                )
            """),
            self._to_db_params(account),
        )

    async def _update(self, account: Account) -> None:
        result = await self._session.execute(
            text(f"""
                UPDATE {_TABLE} SET
                    acct_name = :acct_name,
                    acct_status = :acct_status,
                    sf_id = :sf_id,
                    modified_ts = :modified_ts,
                    row_version = row_version + 1
                WHERE acct_id = :acct_id AND row_version = :expected_version
            """),
            {
                **self._to_db_params(account),
                "expected_version": account.version,
            },
        )
        if result.rowcount == 0:
            raise ConcurrencyConflict(
                entity_type="Account",
                entity_id=account.legacy_id,
                expected_version=account.version,
                actual_version=-1,
            )

    @staticmethod
    def _to_db_params(account: Account) -> dict[str, Any]:
        params: dict[str, Any] = {
            "acct_id": account.legacy_id,
            "acct_name": account.name,
            "acct_type": next((k for k, v in _TYPE_MAP.items() if v == account.account_type), "PROS"),
            "acct_status": next((k for k, v in _STATUS_MAP.items() if v == account.status), "A"),
            "industry_code": next((k for k, v in _INDUSTRY_MAP.items() if v == account.industry), None),
            "phone": account.contact_info.phone if account.contact_info else None,
            "fax": account.contact_info.fax if account.contact_info else None,
            "website": account.contact_info.website if account.contact_info else None,
            "email": str(account.primary_email) if account.primary_email else None,
            "annual_revenue": account.annual_revenue,
            "employee_count": account.number_of_employees,
            "description": account.description,
            "sf_id": account.salesforce_id.id18 if account.salesforce_id else None,
            "created_ts": account.created_at,
            "modified_ts": account.updated_at,
            "bill_street": None,
            "bill_city": None,
            "bill_state": None,
            "bill_zip": None,
            "bill_country": None,
        }
        if account.billing_address:
            params.update({
                "bill_street": account.billing_address.street,
                "bill_city": account.billing_address.city,
                "bill_state": account.billing_address.state,
                "bill_zip": account.billing_address.postal_code,
                "bill_country": account.billing_address.country_code,
            })
        return params

    @classmethod
    def _to_domain(cls, row: dict[str, Any]) -> Account:
        """Map a PostgreSQL row dict to an Account domain entity."""
        billing_address: Optional[Address] = None
        if row.get("bill_addr_city") and row.get("bill_addr_country"):
            try:
                billing_address = Address(
                    street=row.get("bill_addr_street", ""),
                    city=row["bill_addr_city"],
                    country_code=row["bill_addr_country"],
                    state=row.get("bill_addr_state"),
                    postal_code=row.get("bill_addr_zip"),
                    unit=row.get("bill_addr_unit"),
                )
            except Exception as e:
                logger.debug("Could not parse billing address: %s", e)

        contact_info: Optional[ContactInfo] = None
        if row.get("phone_number") or row.get("website_url"):
            contact_info = ContactInfo(
                phone=row.get("phone_number"),
                fax=row.get("fax_number"),
                website=row.get("website_url"),
            )

        primary_email: Optional[Email] = Email.try_parse(row["email_address"]) if row.get("email_address") else None

        sf_id: Optional[SalesforceId] = None
        if row.get("sf_id"):
            sf_id = SalesforceId.try_parse(row["sf_id"])

        raw_status = row.get("acct_status", "A")
        status = _STATUS_MAP.get(raw_status, AccountStatus.ACTIVE)

        raw_type = row.get("acct_type", "PROS")
        account_type = _TYPE_MAP.get(raw_type, AccountType.PROSPECT)

        raw_industry = row.get("industry_code")
        industry: Optional[Industry] = _INDUSTRY_MAP.get(raw_industry) if raw_industry else None

        created_at = row.get("created_ts") or datetime.now(tz=timezone.utc)
        if isinstance(created_at, datetime) and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        updated_at = row.get("modified_ts") or created_at
        if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        domain_id_str = str(row.get("acct_id", ""))
        try:
            domain_uuid = UUID(domain_id_str)
        except ValueError:
            domain_uuid = uuid.uuid4()

        return Account(
            account_id=domain_uuid,
            legacy_id=str(row.get("acct_id", "")),
            name=row.get("acct_name", ""),
            account_type=account_type,
            status=status,
            industry=industry,
            billing_address=billing_address,
            shipping_address=None,
            contact_info=contact_info,
            primary_email=primary_email,
            annual_revenue=row.get("annual_revenue"),
            number_of_employees=row.get("employee_count"),
            description=row.get("acct_description"),
            salesforce_id=sf_id,
            created_at=created_at,
            updated_at=updated_at,
            version=row.get("row_version", 0),
        )

    @staticmethod
    def _map_order_by(field: str) -> str:
        mapping = {
            "created_at": "created_ts",
            "updated_at": "modified_ts",
            "name": "acct_name",
            "legacy_id": "acct_id",
        }
        return mapping.get(field, "created_ts")
