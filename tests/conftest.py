"""
Pytest Configuration and Shared Fixtures
=========================================
Shared fixtures for all test modules:
  - Database connections (testcontainers)
  - Mock Salesforce client
  - Test data factories
  - Application settings overrides
  - Authentication helpers

Author: QA/Testing Team
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

# Force test environment before importing any app code
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LEGACY_DB_PASSWORD", "test_password")
os.environ.setdefault("POSTGRES_PASSWORD", "test_password")
os.environ.setdefault("SALESFORCE_CLIENT_SECRET", "test_secret")
os.environ.setdefault("SALESFORCE_PASSWORD", "test_password")
os.environ.setdefault("SALESFORCE_SECURITY_TOKEN", "test_token")
os.environ.setdefault("DEV_ENCRYPTION_SEED", "test-seed-for-unit-tests")


# ---------------------------------------------------------------------------
# pytest configuration
# ---------------------------------------------------------------------------

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: fast unit tests (no I/O)")
    config.addinivalue_line("markers", "integration: integration tests (require services)")
    config.addinivalue_line("markers", "contract: contract tests for external APIs")
    config.addinivalue_line("markers", "migration_validation: post-migration validation tests")
    config.addinivalue_line("markers", "slow: tests that take more than 10 seconds")


# ---------------------------------------------------------------------------
# Event loop fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Settings override
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_settings_cache():
    """Clear settings cache before each test to allow env var overrides."""
    try:
        from config.settings import get_settings
        get_settings.cache_clear()
    except ImportError:
        pass
    yield
    try:
        from config.settings import get_settings
        get_settings.cache_clear()
    except ImportError:
        pass


@pytest.fixture
def test_settings():
    """Return a test-configured Settings instance."""
    try:
        from config.settings import Settings, Environment
        return Settings(
            environment=Environment.TEST,
            debug=True,
            log_level="WARNING",
        )
    except ImportError:
        return MagicMock()


# ---------------------------------------------------------------------------
# Salesforce Client Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_sf_response_account():
    """Standard successful Salesforce Account response."""
    return {
        "id": "001xx000003GYkZAAW",
        "success": True,
        "errors": [],
    }


@pytest.fixture
def mock_sf_bulk_job_response():
    """Mock Salesforce Bulk API v2 job response."""
    return {
        "id": f"750xx{uuid.uuid4().hex[:10].upper()}",
        "operation": "upsert",
        "object": "Account",
        "state": "JobComplete",
        "numberRecordsProcessed": 100,
        "numberRecordsFailed": 0,
        "totalProcessingTime": 5000,
        "apiActiveProcessingTime": 4500,
        "apexProcessingTime": 0,
        "createdDate": "2026-03-16T00:00:00.000+0000",
        "systemModstamp": "2026-03-16T00:00:05.000+0000",
    }


@pytest.fixture
def mock_salesforce_client():
    """Mock Salesforce client with standard responses."""
    client = AsyncMock()

    client.authenticate = AsyncMock(return_value=None)
    client.is_authenticated = True

    # Query responses
    client.query = AsyncMock(return_value={
        "totalSize": 2,
        "done": True,
        "records": [
            {"Id": "001xx000001", "Name": "Test Account 1", "Type": "Customer"},
            {"Id": "001xx000002", "Name": "Test Account 2", "Type": "Prospect"},
        ],
    })

    # Bulk insert
    client.bulk_insert = AsyncMock(return_value={
        "job_id": "750xx000001",
        "state": "JobComplete",
        "records_processed": 100,
        "records_failed": 0,
    })

    # Bulk upsert
    client.bulk_upsert = AsyncMock(return_value={
        "job_id": "750xx000002",
        "state": "JobComplete",
        "records_processed": 100,
        "records_failed": 0,
    })

    # Rate limit info
    client.get_remaining_api_calls = AsyncMock(return_value=15000)

    # Error simulation helpers
    client.simulate_auth_failure = lambda: setattr(client, "is_authenticated", False)
    client.simulate_rate_limit = lambda: client.bulk_upsert.side_effect(
        Exception("REQUEST_LIMIT_EXCEEDED: TotalRequests Limit exceeded")
    )

    return client


# ---------------------------------------------------------------------------
# Database Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def postgres_container():
    """
    Start a PostgreSQL testcontainer for integration tests.

    Requires Docker and testcontainers library.
    Scoped to session to avoid repeated container starts.
    """
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:15-alpine") as container:
        yield container


@pytest.fixture(scope="session")
async def db_engine(postgres_container):
    """Create an async SQLAlchemy engine connected to the test container."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import create_async_engine

    url = postgres_container.get_connection_url().replace("psycopg2", "asyncpg")
    engine = create_async_engine(url, echo=False)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    """Provide a transactional database session that rolls back after each test."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import AsyncSession

    async with db_engine.begin() as conn:
        async with AsyncSession(bind=conn) as session:
            yield session
            await session.rollback()


@pytest.fixture
def mock_db_session():
    """Mock database session for unit tests."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock()
    session.scalars = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ---------------------------------------------------------------------------
# Test Data Factories
# ---------------------------------------------------------------------------

class AccountFactory:
    """Factory for generating test Account records."""

    @staticmethod
    def build(
        legacy_id: str | None = None,
        name: str | None = None,
        account_type: str = "Customer",
        **overrides,
    ) -> dict[str, Any]:
        _id = legacy_id or f"LEGACY-ACC-{uuid.uuid4().hex[:8].upper()}"
        return {
            "LegacyAccountId": _id,
            "AccountName": name or f"Test Company {_id[-8:]}",
            "AccountType": account_type,
            "BillingStreet": "123 Main St",
            "BillingCity": "Testville",
            "BillingState": "CA",
            "BillingPostalCode": "90210",
            "BillingCountry": "US",
            "Phone": "555-0100",
            "Website": "https://testcompany.example.com",
            "AnnualRevenue": 1_000_000.00,
            "NumberOfEmployees": 50,
            "Industry": "Technology",
            "CreatedDate": datetime(2020, 1, 1, tzinfo=timezone.utc),
            "ModifiedDate": datetime(2024, 6, 1, tzinfo=timezone.utc),
            **overrides,
        }

    @staticmethod
    def build_batch(count: int, **overrides) -> list[dict[str, Any]]:
        return [AccountFactory.build(**overrides) for _ in range(count)]

    @staticmethod
    def build_invalid(issue: str = "missing_name") -> dict[str, Any]:
        base = AccountFactory.build()
        if issue == "missing_name":
            del base["AccountName"]
        elif issue == "null_legacy_id":
            base["LegacyAccountId"] = None
        elif issue == "invalid_email":
            base["Email"] = "not-a-valid-email"
        elif issue == "negative_revenue":
            base["AnnualRevenue"] = -1000
        return base


class ContactFactory:
    """Factory for generating test Contact records."""

    @staticmethod
    def build(
        legacy_id: str | None = None,
        account_legacy_id: str | None = None,
        **overrides,
    ) -> dict[str, Any]:
        _id = legacy_id or f"LEGACY-CON-{uuid.uuid4().hex[:8].upper()}"
        return {
            "LegacyContactId": _id,
            "LegacyAccountId": account_legacy_id or "LEGACY-ACC-00000001",
            "FirstName": "John",
            "LastName": f"Doe-{_id[-4:]}",
            "Email": f"john.doe.{_id[-4:].lower()}@testcompany.example.com",
            "Phone": "555-0200",
            "Title": "VP Engineering",
            "Department": "Engineering",
            "MailingStreet": "123 Main St",
            "MailingCity": "Testville",
            "MailingState": "CA",
            "MailingPostalCode": "90210",
            "MailingCountry": "US",
            "CreatedDate": datetime(2021, 3, 1, tzinfo=timezone.utc),
            "ModifiedDate": datetime(2024, 8, 1, tzinfo=timezone.utc),
            **overrides,
        }

    @staticmethod
    def build_batch(count: int, **overrides) -> list[dict[str, Any]]:
        return [ContactFactory.build(**overrides) for _ in range(count)]


class MigrationJobFactory:
    """Factory for generating test MigrationJob records."""

    @staticmethod
    def build(
        job_id: str | None = None,
        source_object: str = "Account",
        status: str = "pending",
        **overrides,
    ) -> dict[str, Any]:
        return {
            "id": job_id or str(uuid.uuid4()),
            "source_object": source_object,
            "status": status,
            "phase": "extraction",
            "environment": "test",
            "total_records": 1000,
            "processed_records": 0,
            "failed_records": 0,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "created_by": "test-user",
            **overrides,
        }


# ---------------------------------------------------------------------------
# Auth fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_jwt_keys(tmp_path):
    """Generate temporary RSA key pair for JWT testing."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )
        public_key = private_key.public_key()

        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        private_key_path = tmp_path / "jwt_private.pem"
        public_key_path = tmp_path / "jwt_public.pem"
        private_key_path.write_bytes(private_pem)
        public_key_path.write_bytes(public_pem)

        return {
            "private_key": private_pem.decode(),
            "public_key": public_pem.decode(),
            "private_key_path": private_key_path,
            "public_key_path": public_key_path,
        }
    except ImportError:
        return {
            "private_key": "MOCK_PRIVATE_KEY",
            "public_key": "MOCK_PUBLIC_KEY",
            "private_key_path": tmp_path / "jwt_private.pem",
            "public_key_path": tmp_path / "jwt_public.pem",
        }


@pytest.fixture
def admin_user_context():
    """Admin UserContext fixture."""
    try:
        from security.rbac.rbac_config import UserContext, Role
        return UserContext(
            user_id="user-admin-001",
            username="admin@company.com",
            roles=[Role.MIGRATION_ADMIN],
            mfa_verified=True,
            ip_address="10.0.0.1",
        )
    except ImportError:
        return MagicMock(user_id="user-admin-001", roles=["migration-admin"])


@pytest.fixture
def viewer_user_context():
    """Viewer UserContext fixture."""
    try:
        from security.rbac.rbac_config import UserContext, Role
        return UserContext(
            user_id="user-viewer-001",
            username="viewer@company.com",
            roles=[Role.MIGRATION_VIEWER],
            mfa_verified=False,
            ip_address="10.0.0.2",
        )
    except ImportError:
        return MagicMock(user_id="user-viewer-001", roles=["migration-viewer"])


# ---------------------------------------------------------------------------
# HTTP client fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def async_http_client():
    """
    Async HTTP client for API integration tests.
    Import and configure the FastAPI app here.
    """
    try:
        from fastapi.testclient import TestClient
        # App import would go here: from src.api.main import app
        # For now, yield a mock
        yield AsyncMock()
    except ImportError:
        yield AsyncMock()


# ---------------------------------------------------------------------------
# Encryption fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def encryption_service():
    """Create a test EncryptionService with a deterministic master key."""
    try:
        from security.encryption.encryption_service import EncryptionService
        import os
        master_key = bytes.fromhex("0" * 64)  # 32-byte all-zeros key for testing
        return EncryptionService(master_key=master_key)
    except ImportError:
        return MagicMock()


# ---------------------------------------------------------------------------
# Audit logger fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_audit_logger():
    """Mock audit logger that captures events."""
    logger = AsyncMock()
    logger.log_event = AsyncMock()
    logger.log_auth = AsyncMock()
    logger.log_authz = AsyncMock()
    logger.log_data_access = AsyncMock()
    logger.log_migration_event = AsyncMock()
    logger.events = []

    async def capture_event(event):
        logger.events.append(event)

    logger.log_event.side_effect = capture_event
    return logger
