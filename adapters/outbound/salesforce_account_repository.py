"""
SalesforceAccountRepository – outbound adapter implementing AccountRepository.

Translates between the domain Account aggregate and Salesforce REST API records.
Uses the simple_salesforce library for API calls.

Responsibilities:
  - Maps domain entities ↔ Salesforce field dictionaries.
  - Handles Salesforce-specific errors and translates them to domain exceptions.
  - Implements bulk operations using Salesforce Bulk API 2.0.
  - Respects governor limits: batch sizes ≤ 200 for REST, ≤ 10,000 for Bulk.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from domain.entities.account import (
    Account,
    AccountStatus,
    AccountType,
    ContactInfo,
    Industry,
)
from domain.exceptions.domain_exceptions import (
    AccountNotFound,
    DuplicateRecordError,
    SalesforceApiError,
)
from domain.repositories.account_repository import AccountCriteria, AccountRepository, PagedResult
from domain.value_objects.address import Address
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Salesforce client protocol (dependency inversion for testability)
# ---------------------------------------------------------------------------


class SalesforceClientProtocol:
    """
    Thin wrapper around simple_salesforce.Salesforce that we depend on via
    duck typing.  In tests, inject a mock or a stub.
    """

    def query(self, soql: str) -> dict[str, Any]: ...
    def query_more(self, next_records_url: str, identifier_is_url: bool = False) -> dict[str, Any]: ...

    @property
    def Account(self) -> Any: ...

    def bulk(self) -> Any: ...


# ---------------------------------------------------------------------------
# Field-name constants
# ---------------------------------------------------------------------------

_EXTERNAL_ID_FIELD = "Legacy_ID__c"

_SOQL_FIELDS = (
    "Id, Name, Type, Industry, AnnualRevenue, NumberOfEmployees, Description, "
    "Phone, Fax, Website, Email__c, "
    "BillingStreet, BillingCity, BillingState, BillingPostalCode, BillingCountry, "
    "ShippingStreet, ShippingCity, ShippingState, ShippingPostalCode, ShippingCountry, "
    f"{_EXTERNAL_ID_FIELD}, CreatedDate, LastModifiedDate, SystemModstamp"
)


# ---------------------------------------------------------------------------
# Adapter implementation
# ---------------------------------------------------------------------------


class SalesforceAccountRepository(AccountRepository):
    """
    Concrete AccountRepository that reads/writes Salesforce Account records.

    All write operations use the External ID field (Legacy_ID__c) for upsert
    so that re-runs are idempotent.
    """

    def __init__(self, sf_client: SalesforceClientProtocol) -> None:
        self._sf = sf_client

    # ------------------------------------------------------------------
    # AccountRepository interface
    # ------------------------------------------------------------------

    async def find_by_id(self, account_id: UUID) -> Optional[Account]:
        """
        Salesforce does not store our internal UUID; this queries by the
        external ID field which carries the legacy system ID (used as proxy).
        For a full implementation, a custom UUID__c field would be needed.
        """
        logger.debug("find_by_id: %s (querying by external ID)", account_id)
        # In production, store the UUID in a custom field UUID__c
        # For now, treat UUID as external ID
        soql = (
            f"SELECT {_SOQL_FIELDS} FROM Account "
            f"WHERE {_EXTERNAL_ID_FIELD} = '{account_id}' LIMIT 1"
        )
        return await self._query_one(soql)

    async def find_by_legacy_id(self, legacy_id: str) -> Optional[Account]:
        logger.debug("find_by_legacy_id: %s", legacy_id)
        safe_id = legacy_id.replace("'", "\\'")
        soql = (
            f"SELECT {_SOQL_FIELDS} FROM Account "
            f"WHERE {_EXTERNAL_ID_FIELD} = '{safe_id}' LIMIT 1"
        )
        return await self._query_one(soql)

    async def find_by_salesforce_id(self, salesforce_id: SalesforceId) -> Optional[Account]:
        logger.debug("find_by_salesforce_id: %s", salesforce_id)
        soql = (
            f"SELECT {_SOQL_FIELDS} FROM Account "
            f"WHERE Id = '{salesforce_id.id18}' LIMIT 1"
        )
        return await self._query_one(soql)

    async def find_all(self, limit: int = 100, offset: int = 0) -> list[Account]:
        soql = (
            f"SELECT {_SOQL_FIELDS} FROM Account "
            f"ORDER BY CreatedDate ASC "
            f"LIMIT {limit} OFFSET {offset}"
        )
        return await self._query_many(soql)

    async def find_by_criteria(self, criteria: AccountCriteria) -> PagedResult:
        where_clauses: list[str] = []

        if criteria.is_migrated is True:
            where_clauses.append(f"{_EXTERNAL_ID_FIELD} != null")
        elif criteria.is_migrated is False:
            where_clauses.append(f"{_EXTERNAL_ID_FIELD} = null")

        if criteria.name_contains:
            safe = criteria.name_contains.replace("'", "\\'")
            where_clauses.append(f"Name LIKE '%{safe}%'")

        if criteria.account_types:
            types_str = ", ".join(f"'{t.value}'" for t in criteria.account_types)
            where_clauses.append(f"Type IN ({types_str})")

        if criteria.industries:
            ind_str = ", ".join(f"'{i.value}'" for i in criteria.industries)
            where_clauses.append(f"Industry IN ({ind_str})")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        order_dir = "ASC" if criteria.order_asc else "DESC"

        count_soql = f"SELECT COUNT() FROM Account {where_sql}"
        data_soql = (
            f"SELECT {_SOQL_FIELDS} FROM Account {where_sql} "
            f"ORDER BY {criteria.order_by} {order_dir} "
            f"LIMIT {criteria.limit} OFFSET {criteria.offset}"
        )

        try:
            count_result = self._sf.query(count_soql)
            total_count = count_result.get("totalSize", 0)
        except Exception as exc:
            logger.warning("Count query failed: %s", exc)
            total_count = 0

        items = await self._query_many(data_soql)
        return PagedResult(
            items=items,
            total_count=total_count,
            limit=criteria.limit,
            offset=criteria.offset,
        )

    async def find_unmigrated(self, limit: int = 200) -> list[Account]:
        # In Salesforce context "unmigrated" means not yet in our system
        # This would typically query the staging store, not SF directly
        return await self.find_all(limit=limit, offset=0)

    async def count_unmigrated(self) -> int:
        result = self._sf.query(f"SELECT COUNT() FROM Account WHERE {_EXTERNAL_ID_FIELD} = null")
        return result.get("totalSize", 0)

    async def save(self, account: Account) -> Account:
        payload = account.to_salesforce_payload()
        logger.debug("save account: legacy_id=%s", account.legacy_id)

        try:
            if account.salesforce_id:
                # Update existing record
                self._sf.Account.update(account.salesforce_id.id18, payload)
            else:
                # Upsert by external ID for idempotency
                result = self._sf.Account.upsert(
                    f"{_EXTERNAL_ID_FIELD}/{account.legacy_id}",
                    payload,
                )
                if result and isinstance(result, dict) and result.get("id"):
                    # Update the domain entity with the assigned Salesforce ID
                    # This would normally happen via mark_migrated(); here we peek at the response
                    pass
        except Exception as exc:
            self._translate_sf_exception(exc, account.legacy_id)

        return account

    async def save_batch(self, accounts: list[Account]) -> list[Account]:
        """Upsert accounts using Salesforce Bulk API 2.0."""
        logger.info("save_batch: %d accounts", len(accounts))
        records = [
            {**acct.to_salesforce_payload(), _EXTERNAL_ID_FIELD: acct.legacy_id}
            for acct in accounts
        ]
        try:
            bulk = self._sf.bulk()
            results = bulk.Account.upsert(records, external_id_field=_EXTERNAL_ID_FIELD, batch_size=200)
            for acct, result in zip(accounts, results):
                if result.get("success") and result.get("id"):
                    sf_id = SalesforceId.try_parse(result["id"])
                    if sf_id and not acct.is_migrated:
                        logger.debug(
                            "Account %s → Salesforce %s", acct.legacy_id, sf_id
                        )
        except Exception as exc:
            logger.error("Bulk save failed: %s", exc)
            self._translate_sf_exception(exc, "batch")
        return accounts

    async def delete(self, account_id: UUID) -> bool:
        account = await self.find_by_id(account_id)
        if account is None or account.salesforce_id is None:
            return False
        try:
            self._sf.Account.delete(account.salesforce_id.id18)
            return True
        except Exception as exc:
            logger.error("Delete failed for account %s: %s", account_id, exc)
            return False

    async def exists(self, account_id: UUID) -> bool:
        return (await self.find_by_id(account_id)) is not None

    async def find_by_email(self, email: Email) -> list[Account]:
        safe_email = str(email).replace("'", "\\'")
        soql = f"SELECT {_SOQL_FIELDS} FROM Account WHERE Email__c = '{safe_email}'"
        return await self._query_many(soql)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _query_one(self, soql: str) -> Optional[Account]:
        results = await self._query_many(soql)
        return results[0] if results else None

    async def _query_many(self, soql: str) -> list[Account]:
        try:
            result = self._sf.query(soql)
        except Exception as exc:
            self._translate_sf_exception(exc, "query")
            return []

        records = result.get("records", [])

        # Handle Salesforce query pagination
        while not result.get("done", True):
            result = self._sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
            records.extend(result.get("records", []))

        accounts = []
        for rec in records:
            try:
                accounts.append(self._to_domain(rec))
            except Exception as exc:
                logger.warning("Failed to deserialise account record %s: %s", rec.get("Id"), exc)
        return accounts

    @staticmethod
    def _to_domain(rec: dict[str, Any]) -> Account:
        """Map a Salesforce API record dict to an Account domain entity."""
        from datetime import datetime

        def _parse_dt(val: Optional[str]) -> Optional[datetime]:
            if not val:
                return None
            from datetime import timezone
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt

        billing_address: Optional[Address] = None
        if rec.get("BillingCity") and rec.get("BillingCountry"):
            try:
                billing_address = Address(
                    street=rec.get("BillingStreet", ""),
                    city=rec.get("BillingCity", ""),
                    country_code=rec.get("BillingCountry", "US"),
                    state=rec.get("BillingState"),
                    postal_code=rec.get("BillingPostalCode"),
                )
            except Exception:
                pass

        shipping_address: Optional[Address] = None
        if rec.get("ShippingCity") and rec.get("ShippingCountry"):
            try:
                shipping_address = Address(
                    street=rec.get("ShippingStreet", ""),
                    city=rec.get("ShippingCity", ""),
                    country_code=rec.get("ShippingCountry", "US"),
                    state=rec.get("ShippingState"),
                    postal_code=rec.get("ShippingPostalCode"),
                )
            except Exception:
                pass

        contact_info: Optional[ContactInfo] = None
        if rec.get("Phone") or rec.get("Website"):
            contact_info = ContactInfo(
                phone=rec.get("Phone"),
                fax=rec.get("Fax"),
                website=rec.get("Website"),
            )

        primary_email: Optional[Email] = None
        if rec.get("Email__c"):
            primary_email = Email.try_parse(rec["Email__c"])

        sf_id: Optional[SalesforceId] = None
        if rec.get("Id"):
            sf_id = SalesforceId.try_parse(rec["Id"])

        # Coerce account_type
        raw_type = rec.get("Type") or "Prospect"
        try:
            account_type = AccountType(raw_type)
        except ValueError:
            account_type = AccountType.OTHER

        raw_industry = rec.get("Industry")
        industry: Optional[Industry] = None
        if raw_industry:
            try:
                industry = Industry(raw_industry)
            except ValueError:
                pass

        import uuid as _uuid
        now_str = rec.get("CreatedDate") or rec.get("LastModifiedDate") or ""
        created_at = _parse_dt(now_str) or __import__("datetime").datetime.utcnow().replace(tzinfo=__import__("datetime").timezone.utc)
        updated_at = _parse_dt(rec.get("LastModifiedDate")) or created_at

        return Account(
            account_id=_uuid.uuid4(),  # SF doesn't store our internal UUID; generate ephemeral
            legacy_id=rec.get(_EXTERNAL_ID_FIELD, rec.get("Id", "")),
            name=rec.get("Name", ""),
            account_type=account_type,
            status=AccountStatus.ACTIVE,
            industry=industry,
            billing_address=billing_address,
            shipping_address=shipping_address,
            contact_info=contact_info,
            primary_email=primary_email,
            annual_revenue=rec.get("AnnualRevenue"),
            number_of_employees=rec.get("NumberOfEmployees"),
            description=rec.get("Description"),
            salesforce_id=sf_id,
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _translate_sf_exception(exc: Exception, context: str) -> None:
        """Translate simple_salesforce exceptions to domain exceptions."""
        exc_str = str(exc)
        if "DUPLICATE_VALUE" in exc_str:
            raise DuplicateRecordError("Account", "Legacy_ID__c", context)
        if "ENTITY_IS_DELETED" in exc_str or "NOT_FOUND" in exc_str:
            raise AccountNotFound(account_id=context)
        # Extract SF error code if present
        sf_code = "SALESFORCE_ERROR"
        sf_msg = exc_str[:500]
        raise SalesforceApiError(sf_error_code=sf_code, sf_message=sf_msg)
