"""
Abstract Account repository interface (port).

This module defines the contract that infrastructure adapters must fulfil.
It lives in the domain layer and contains zero infrastructure dependencies.

Following Hexagonal Architecture, the domain defines the port; the adapters
layer provides the concrete plug (implementation).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID

from domain.entities.account import Account, AccountStatus, AccountType, Industry
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId


# ---------------------------------------------------------------------------
# Query / filter criteria value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountCriteria:
    """
    Encapsulates optional filter predicates for querying accounts.

    All fields are optional.  Only non-None fields are applied as filters.
    Designed to be passed directly to repository query methods, keeping the
    query API stable as requirements evolve.
    """

    legacy_ids: Optional[frozenset[str]] = None
    statuses: Optional[frozenset[AccountStatus]] = None
    account_types: Optional[frozenset[AccountType]] = None
    industries: Optional[frozenset[Industry]] = None
    is_migrated: Optional[bool] = None          # True = has SF id, False = does not
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    updated_after: Optional[datetime] = None
    name_contains: Optional[str] = None
    limit: int = 1000
    offset: int = 0
    order_by: str = "created_at"
    order_asc: bool = True


@dataclass(frozen=True)
class PagedResult:
    """Generic paged result wrapper."""

    items: list[Account]
    total_count: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return (self.offset + self.limit) < self.total_count

    @property
    def next_offset(self) -> Optional[int]:
        if self.has_more:
            return self.offset + self.limit
        return None


# ---------------------------------------------------------------------------
# Abstract repository (Port)
# ---------------------------------------------------------------------------


class AccountRepository(ABC):
    """
    Port defining the persistence contract for Account aggregates.

    Implementations live in the adapters layer (e.g.
    adapters.outbound.salesforce_account_repository.SalesforceAccountRepository,
    adapters.outbound.postgres_account_repository.PostgresAccountRepository).

    All methods are async to support non-blocking I/O in the application layer.
    The domain itself never calls these methods; only application services do.
    """

    @abstractmethod
    async def find_by_id(self, account_id: UUID) -> Optional[Account]:
        """
        Return the Account with the given domain ID, or None if not found.

        Parameters
        ----------
        account_id:
            The UUID assigned by the domain when the aggregate was created.
        """
        ...

    @abstractmethod
    async def find_by_legacy_id(self, legacy_id: str) -> Optional[Account]:
        """
        Return the Account matching the legacy system's natural key, or None.

        Parameters
        ----------
        legacy_id:
            The primary key used in the source (legacy) system.
        """
        ...

    @abstractmethod
    async def find_by_salesforce_id(self, salesforce_id: SalesforceId) -> Optional[Account]:
        """
        Return the Account already migrated to the given Salesforce record, or None.
        """
        ...

    @abstractmethod
    async def find_all(self, limit: int = 100, offset: int = 0) -> list[Account]:
        """
        Return a page of all accounts.

        Prefer find_by_criteria for filtered queries; this method is intended
        for bulk export / reconciliation scenarios.
        """
        ...

    @abstractmethod
    async def find_by_criteria(self, criteria: AccountCriteria) -> PagedResult:
        """
        Return accounts matching the supplied criteria, with pagination.

        Parameters
        ----------
        criteria:
            A value object encapsulating all filter and pagination parameters.
        """
        ...

    @abstractmethod
    async def find_unmigrated(self, limit: int = 200) -> list[Account]:
        """
        Return accounts that have not yet been migrated (salesforce_id is None).

        Implementations should order by legacy_id ascending for deterministic
        batching.
        """
        ...

    @abstractmethod
    async def count_unmigrated(self) -> int:
        """Return the total number of accounts not yet migrated."""
        ...

    @abstractmethod
    async def save(self, account: Account) -> Account:
        """
        Persist a new or updated Account.

        - For new aggregates, perform an INSERT.
        - For existing aggregates, perform an UPDATE using optimistic locking
          on the `version` field; raise ConcurrencyConflict if the stored
          version does not match.

        Parameters
        ----------
        account:
            The aggregate to persist.  If account.version == 0 and no record
            exists with account.account_id, it is treated as a new entity.

        Returns
        -------
        Account
            The persisted aggregate (may have a refreshed version field).
        """
        ...

    @abstractmethod
    async def save_batch(self, accounts: list[Account]) -> list[Account]:
        """
        Persist multiple accounts in a single database round-trip.

        Implementations should use bulk INSERT/UPSERT for efficiency.
        Returns the saved aggregates with updated version numbers.
        """
        ...

    @abstractmethod
    async def delete(self, account_id: UUID) -> bool:
        """
        Remove the account from the store.

        Returns True if an account was found and deleted, False if not found.
        Note: in most migration scenarios, hard-deletes should be avoided;
        prefer deactivating the account via Account.deactivate().
        """
        ...

    @abstractmethod
    async def exists(self, account_id: UUID) -> bool:
        """Return True if an Account with the given id exists in the store."""
        ...

    @abstractmethod
    async def find_by_email(self, email: Email) -> list[Account]:
        """
        Return all accounts whose primary_email matches.

        May return multiple accounts in legacy systems where email was not
        enforced as unique.
        """
        ...
