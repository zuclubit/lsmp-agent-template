"""
Application Settings — Legacy to Salesforce Migration Platform
=============================================================
Pydantic Settings class loading configuration from:
  1. Environment variables (highest priority)
  2. .env file
  3. Default values

All settings are type-validated on startup.

Author: Platform Engineering Team
Version: 1.1.0
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Environment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogFormat(str, Enum):
    JSON = "json"
    TEXT = "text"


class StorageProvider(str, Enum):
    LOCAL = "local"
    AZURE = "azure"
    AWS = "aws"


class SecretBackend(str, Enum):
    VAULT = "vault"
    AZURE_KEY_VAULT = "azure_key_vault"
    AWS_SECRETS_MANAGER = "aws_secrets_manager"
    ENVIRONMENT = "environment"


# ---------------------------------------------------------------------------
# Settings Groups
# ---------------------------------------------------------------------------

class DatabaseSettings(BaseSettings):
    """Legacy source database configuration."""
    model_config = SettingsConfigDict(env_prefix="LEGACY_DB_")

    host: str = "localhost"
    port: int = 1433
    name: str = "legacy_crm"
    username: str = "migration_user"
    password: SecretStr = SecretStr("CHANGE_ME")
    ssl_mode: str = "disable"
    ssl_cert_path: Optional[Path] = None
    pool_min: int = Field(default=5, ge=1, le=100)
    pool_max: int = Field(default=25, ge=1, le=200)
    pool_timeout_seconds: int = 30
    connection_timeout_seconds: int = 15
    replica_host: Optional[str] = None

    @property
    def connection_string(self) -> str:
        return (
            f"mssql+aioodbc://{self.username}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class PostgresSettings(BaseSettings):
    """Migration state database configuration."""
    model_config = SettingsConfigDict(env_prefix="POSTGRES_")

    host: str = "localhost"
    port: int = 5432
    db: str = "migration_state"
    user: str = "migration_app"
    password: SecretStr = SecretStr("CHANGE_ME")
    ssl_mode: str = "disable"
    ssl_cert_path: Optional[Path] = None
    pool_min: int = Field(default=5, ge=1, le=100)
    pool_max: int = Field(default=50, ge=1, le=500)
    replica_host: Optional[str] = None

    @property
    def dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.db}"
        )


class SalesforceSettings(BaseSettings):
    """Salesforce connectivity configuration."""
    model_config = SettingsConfigDict(env_prefix="SALESFORCE_")

    instance_url: str = "https://test.salesforce.com"
    client_id: SecretStr = SecretStr("CHANGE_ME")
    client_secret: SecretStr = SecretStr("CHANGE_ME")
    username: str = "migration@example.com"
    password: SecretStr = SecretStr("CHANGE_ME")
    security_token: SecretStr = SecretStr("CHANGE_ME")
    api_version: str = "59.0"
    bulk_api_version: int = 2
    max_connections: int = Field(default=10, ge=1, le=50)
    timeout_seconds: int = 120
    retry_attempts: int = Field(default=5, ge=1, le=10)
    bulk_batch_size: int = Field(default=10000, ge=100, le=150_000_000)
    rate_limit_buffer: int = 1000
    concurrent_jobs_max: int = Field(default=5, ge=1, le=10)

    @field_validator("instance_url")
    @classmethod
    def validate_instance_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("Salesforce instance URL must use HTTPS")
        return v.rstrip("/")


class KafkaSettings(BaseSettings):
    """Kafka messaging configuration."""
    model_config = SettingsConfigDict(env_prefix="KAFKA_")

    bootstrap_servers: str = "localhost:9092"
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str = ""
    sasl_username: SecretStr = SecretStr("")
    sasl_password: SecretStr = SecretStr("")
    ssl_ca_cert_path: Optional[Path] = None
    group_id: str = "migration-consumer"
    auto_offset_reset: str = "earliest"
    max_poll_records: int = 500
    session_timeout_ms: int = 30000
    topics_migration_events: str = "migration-events"
    topics_error_events: str = "migration-errors"
    topics_audit_events: str = "migration-audit"
    topics_dlq: str = "migration-dlq"


class RedisSettings(BaseSettings):
    """Redis cache configuration."""
    model_config = SettingsConfigDict(env_prefix="REDIS_")

    host: str = "localhost"
    port: int = 6379
    password: SecretStr = SecretStr("")
    db: int = 0
    tls_enabled: bool = False
    tls_cert_path: Optional[Path] = None
    max_connections: int = 100
    timeout_seconds: int = 5
    cache_default_ttl_seconds: int = 300
    cluster_mode: bool = False
    cluster_nodes: str = ""  # Comma-separated host:port list in cluster mode

    @property
    def url(self) -> str:
        scheme = "rediss" if self.tls_enabled else "redis"
        pw = self.password.get_secret_value()
        auth = f":{pw}@" if pw else ""
        return f"{scheme}://{auth}{self.host}:{self.port}/{self.db}"


class VaultSettings(BaseSettings):
    """HashiCorp Vault configuration."""
    model_config = SettingsConfigDict(env_prefix="VAULT_")

    addr: str = "http://localhost:8200"
    token: SecretStr = SecretStr("")
    mount_path: str = "migration"
    namespace: str = ""
    tls_verify: bool = True
    ca_cert_path: Optional[Path] = None
    k8s_role: str = ""
    cache_ttl_seconds: int = 300

    @property
    def use_kubernetes_auth(self) -> bool:
        return bool(self.k8s_role) and not self.token.get_secret_value()


class SecuritySettings(BaseSettings):
    """Security and authentication configuration."""
    model_config = SettingsConfigDict(env_prefix="AUTH_")

    jwt_public_key_path: Optional[Path] = None
    jwt_private_key_path: Optional[Path] = None
    jwt_algorithm: str = "RS256"
    jwt_expiry_minutes: int = Field(default=60, ge=5, le=1440)
    mfa_required: bool = False
    session_timeout_minutes: int = Field(default=480, ge=5, le=1440)

    @field_validator("jwt_algorithm")
    @classmethod
    def validate_algorithm(cls, v: str) -> str:
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if v not in allowed:
            raise ValueError(f"JWT algorithm must be one of: {allowed}")
        return v


class EncryptionSettings(BaseSettings):
    """Data encryption configuration."""
    model_config = SettingsConfigDict(env_prefix="ENCRYPTION_")

    master_key_b64: SecretStr = SecretStr("")
    key_id: str = "master-key-v1"
    key_rotation_days: int = 90
    dev_seed: str = Field(
        default="dev-seed-change-me",
        alias="DEV_ENCRYPTION_SEED",
    )


class MigrationSettings(BaseSettings):
    """Migration engine configuration."""
    model_config = SettingsConfigDict(env_prefix="MIGRATION_")

    batch_size: int = Field(default=2000, ge=100, le=10000)
    max_parallel_jobs: int = Field(default=3, ge=1, le=20)
    retry_attempts: int = Field(default=5, ge=1, le=10)
    retry_delay_seconds: int = Field(default=10, ge=1, le=3600)
    retry_max_delay_seconds: int = Field(default=300, ge=10, le=3600)
    timeout_hours: int = Field(default=72, ge=1, le=168)
    checkpoint_interval_records: int = Field(default=10000, ge=100)
    dry_run: bool = False
    enable_rollback: bool = True
    require_approval: bool = True
    approval_timeout_hours: int = Field(default=4, ge=1, le=72)
    max_error_rate_percent: float = Field(default=2.0, ge=0.0, le=100.0)


class MonitoringSettings(BaseSettings):
    """Observability configuration."""
    model_config = SettingsConfigDict(env_prefix="")

    prometheus_enabled: bool = True
    prometheus_port: int = 9090
    otel_tracing_enabled: bool = False
    otel_exporter_type: str = "console"
    otel_exporter_otlp_endpoint: str = ""
    otel_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    otel_tenant_id: str = "migration"
    log_debug_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Root Application Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """
    Root application settings.

    Loads from environment variables, .env file, and defaults.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Identity
    environment: Environment = Environment.DEVELOPMENT
    service_name: str = "migration-platform"
    service_version: str = "unknown"
    debug: bool = False

    # Logging
    log_level: LogLevel = LogLevel.INFO
    log_format: LogFormat = LogFormat.TEXT

    # API
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_workers: int = Field(default=1, ge=1, le=64)
    api_timeout_seconds: int = 120
    api_max_request_size_mb: int = 50
    cors_origins: str = "http://localhost:3000"

    # Config paths
    config_dir: Path = Path("/etc/migration/config")
    app_config_path: Path = Path("config/app_config.yaml")
    feature_flags_path: Path = Path("config/feature_flags.yaml")

    # Secret backend
    secret_backend: SecretBackend = SecretBackend.ENVIRONMENT

    # Nested settings
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    salesforce: SalesforceSettings = Field(default_factory=SalesforceSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    vault: VaultSettings = Field(default_factory=VaultSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    encryption: EncryptionSettings = Field(default_factory=EncryptionSettings)
    migration: MigrationSettings = Field(default_factory=MigrationSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)

    # Notification
    slack_webhook_url: SecretStr = SecretStr("")
    pagerduty_routing_key: SecretStr = SecretStr("")
    alert_email_recipients: str = ""

    # Feature flags (loaded from feature_flags.yaml, overridable via env)
    feature_bulk_api_v2: bool = True
    feature_parallel_transformation: bool = False
    feature_ai_validation: bool = False
    feature_incremental_migration: bool = False
    feature_field_level_encryption: bool = False
    feature_audit_chain: bool = False
    feature_distributed_tracing: bool = False

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str) -> str:
        return v

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        return self.environment == Environment.DEVELOPMENT

    @property
    def is_test(self) -> bool:
        return self.environment == Environment.TEST

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """Enforce security requirements in production."""
        if self.is_production:
            if self.debug:
                raise ValueError("DEBUG must be False in production")
            if not self.security.mfa_required:
                # Allow but warn
                import warnings
                warnings.warn("MFA_REQUIRED is False in production environment", stacklevel=2)
        return self

    def get_feature_flag(self, flag_name: str) -> bool:
        """Get a feature flag value, checking env var override first."""
        env_key = f"FEATURE_{flag_name.upper()}"
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return env_val.lower() in ("1", "true", "yes", "on")
        return getattr(self, f"feature_{flag_name.lower()}", False)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the global Settings singleton.

    The result is cached via lru_cache.
    In tests, call get_settings.cache_clear() before overriding with mocks.
    """
    return Settings()
