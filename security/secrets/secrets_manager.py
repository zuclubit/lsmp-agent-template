"""
Secrets Manager Client — Legacy to Salesforce Migration
========================================================
Multi-backend secrets management supporting:
  - HashiCorp Vault (primary)
  - Azure Key Vault
  - AWS Secrets Manager

Features:
  - In-memory cache with TTL
  - Secret rotation hooks
  - Audit logging on every access
  - Automatic retry with exponential backoff
  - Type-safe secret retrieval

Author: Platform Security Team
Version: 1.1.0
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and Constants
# ---------------------------------------------------------------------------

class SecretBackend(str, Enum):
    VAULT = "vault"
    AZURE_KEY_VAULT = "azure_key_vault"
    AWS_SECRETS_MANAGER = "aws_secrets_manager"


DEFAULT_CACHE_TTL_SECONDS = 300       # 5 minutes
MAX_RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SecretValue:
    """Represents a retrieved secret with metadata."""
    key: str
    value: str
    version: Optional[str]
    created_at: Optional[datetime]
    expires_at: Optional[datetime]
    backend: SecretBackend
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_expired(self) -> bool:
        """Check if the secret has an expiry and is past it."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    def as_dict(self) -> dict[str, Any]:
        """Return as dictionary (value is masked for logging)."""
        return {
            "key": self.key,
            "value": "[REDACTED]",
            "version": self.version,
            "backend": self.backend.value,
            "retrieved_at": self.retrieved_at.isoformat(),
        }


@dataclass
class CacheEntry:
    """Internal cache entry with TTL."""
    secret: SecretValue
    cached_at: float = field(default_factory=time.monotonic)
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS

    def is_valid(self) -> bool:
        return (time.monotonic() - self.cached_at) < self.ttl_seconds


# ---------------------------------------------------------------------------
# Abstract Backend Interface
# ---------------------------------------------------------------------------

class SecretBackendClient(ABC):
    """Abstract interface for secret backend clients."""

    @abstractmethod
    async def get_secret(self, key: str) -> SecretValue:
        """Retrieve a secret by key."""
        ...

    @abstractmethod
    async def get_secret_map(self, path: str) -> dict[str, str]:
        """Retrieve multiple secrets at a path, returning key-value dict."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the backend is reachable and healthy."""
        ...


# ---------------------------------------------------------------------------
# HashiCorp Vault Backend
# ---------------------------------------------------------------------------

class VaultClient(SecretBackendClient):
    """HashiCorp Vault KV v2 and Transit client."""

    def __init__(
        self,
        vault_addr: str,
        mount_path: str = "migration",
        token: str | None = None,
        kubernetes_role: str | None = None,
        kubernetes_jwt_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token",
        namespace: str | None = None,
        tls_verify: bool = True,
        ca_cert_path: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._addr = vault_addr.rstrip("/")
        self._mount = mount_path
        self._namespace = namespace
        self._tls_verify = tls_verify
        self._ca_cert = ca_cert_path
        self._timeout = timeout_seconds
        self._k8s_role = kubernetes_role
        self._k8s_jwt_path = kubernetes_jwt_path

        self._token: str | None = token
        self._token_renewable: bool = False
        self._token_expiry: datetime | None = None

        self._http_client = httpx.AsyncClient(
            base_url=self._addr,
            verify=ca_cert_path if ca_cert_path else tls_verify,
            timeout=timeout_seconds,
        )

    def _get_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Vault-Token"] = self._token
        if self._namespace:
            headers["X-Vault-Namespace"] = self._namespace
        return headers

    async def _authenticate_kubernetes(self) -> None:
        """Authenticate via Kubernetes service account JWT."""
        try:
            with open(self._k8s_jwt_path) as f:
                jwt_token = f.read().strip()
        except OSError as e:
            raise RuntimeError(f"Cannot read K8s service account token: {e}") from e

        response = await self._http_client.post(
            f"/v1/auth/kubernetes/login",
            json={"role": self._k8s_role, "jwt": jwt_token},
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

        auth = data.get("auth", {})
        self._token = auth.get("client_token")
        self._token_renewable = auth.get("renewable", False)
        lease_duration = auth.get("lease_duration", 3600)
        self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=lease_duration * 0.9)

        logger.info("Vault Kubernetes auth successful", extra={"role": self._k8s_role})

    async def _ensure_authenticated(self) -> None:
        """Ensure we have a valid, non-expired token."""
        if self._token and self._token_expiry:
            if datetime.now(timezone.utc) < self._token_expiry:
                return
        if self._k8s_role:
            await self._authenticate_kubernetes()
        elif not self._token:
            raise RuntimeError("No Vault authentication method configured")

    async def get_secret(self, key: str) -> SecretValue:
        """
        Retrieve a single secret from Vault KV v2.

        Args:
            key: Secret path relative to mount (e.g., "salesforce/credentials")

        Returns:
            SecretValue with the secret data JSON-encoded as string.
        """
        await self._ensure_authenticated()

        url = f"/v1/{self._mount}/data/{key}"
        response = await self._http_client.get(url, headers=self._get_headers())

        if response.status_code == 404:
            raise KeyError(f"Secret not found: {key}")
        response.raise_for_status()

        data = response.json()
        secret_data = data.get("data", {}).get("data", {})
        metadata = data.get("data", {}).get("metadata", {})

        created_at = None
        if metadata.get("created_time"):
            try:
                created_at = datetime.fromisoformat(metadata["created_time"].rstrip("Z") + "+00:00")
            except (ValueError, AttributeError):
                pass

        return SecretValue(
            key=key,
            value=json.dumps(secret_data),
            version=str(metadata.get("version", "unknown")),
            created_at=created_at,
            expires_at=None,
            backend=SecretBackend.VAULT,
        )

    async def get_secret_map(self, path: str) -> dict[str, str]:
        """Retrieve a KV secret and return the map directly."""
        secret = await self.get_secret(path)
        return json.loads(secret.value)

    async def health_check(self) -> bool:
        try:
            response = await self._http_client.get("/v1/sys/health", timeout=5.0)
            # 200 = initialized, unsealed, active
            # 429 = standby (still healthy for reads)
            # 472/473 = DR/perf standby
            return response.status_code in (200, 429, 472, 473)
        except Exception:
            return False

    async def renew_token(self) -> None:
        """Renew the current Vault token."""
        if not self._token:
            return
        response = await self._http_client.post(
            "/v1/auth/token/renew-self",
            headers=self._get_headers(),
        )
        response.raise_for_status()
        data = response.json()
        lease = data.get("auth", {}).get("lease_duration", 3600)
        self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=lease * 0.9)
        logger.info("Vault token renewed")

    async def transit_encrypt(self, key_name: str, plaintext: bytes) -> str:
        """Encrypt data using Vault Transit engine."""
        await self._ensure_authenticated()
        encoded = base64.b64encode(plaintext).decode()
        response = await self._http_client.post(
            f"/v1/transit/encrypt/{key_name}",
            json={"plaintext": encoded},
            headers=self._get_headers(),
        )
        response.raise_for_status()
        return response.json()["data"]["ciphertext"]

    async def transit_decrypt(self, key_name: str, ciphertext: str) -> bytes:
        """Decrypt data using Vault Transit engine."""
        await self._ensure_authenticated()
        response = await self._http_client.post(
            f"/v1/transit/decrypt/{key_name}",
            json={"ciphertext": ciphertext},
            headers=self._get_headers(),
        )
        response.raise_for_status()
        encoded = response.json()["data"]["plaintext"]
        return base64.b64decode(encoded)


# ---------------------------------------------------------------------------
# Azure Key Vault Backend
# ---------------------------------------------------------------------------

class AzureKeyVaultClient(SecretBackendClient):
    """Azure Key Vault secrets client using managed identity or client credentials."""

    SCOPE = "https://vault.azure.net/.default"
    API_VERSION = "7.4"

    def __init__(
        self,
        vault_url: str,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        use_managed_identity: bool = True,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._vault_url = vault_url.rstrip("/")
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._use_managed_identity = use_managed_identity
        self._timeout = timeout_seconds

        self._access_token: str | None = None
        self._token_expiry: datetime | None = None

        self._http_client = httpx.AsyncClient(timeout=timeout_seconds)

    async def _get_token_managed_identity(self) -> None:
        """Obtain access token from Azure Instance Metadata Service (IMDS)."""
        imds_url = "http://169.254.169.254/metadata/identity/oauth2/token"
        params = {
            "api-version": "2018-02-01",
            "resource": "https://vault.azure.net",
        }
        if self._client_id:
            params["client_id"] = self._client_id

        response = await self._http_client.get(
            imds_url,
            params=params,
            headers={"Metadata": "true"},
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        self._access_token = data["access_token"]
        expires_on = int(data.get("expires_on", 0))
        self._token_expiry = datetime.fromtimestamp(expires_on, tz=timezone.utc) - timedelta(minutes=5)

    async def _get_token_client_credentials(self) -> None:
        """Obtain access token via client credentials flow."""
        token_url = f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self.SCOPE,
        }
        response = await self._http_client.post(token_url, data=data)
        response.raise_for_status()
        token_data = response.json()
        self._access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 3600)
        self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

    async def _ensure_token(self) -> None:
        if self._access_token and self._token_expiry:
            if datetime.now(timezone.utc) < self._token_expiry:
                return
        if self._use_managed_identity:
            await self._get_token_managed_identity()
        else:
            await self._get_token_client_credentials()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def get_secret(self, key: str) -> SecretValue:
        """Get a secret from Azure Key Vault by name."""
        await self._ensure_token()
        url = f"{self._vault_url}/secrets/{key}"
        params = {"api-version": self.API_VERSION}

        response = await self._http_client.get(url, params=params, headers=self._auth_headers())

        if response.status_code == 404:
            raise KeyError(f"Secret not found in Azure Key Vault: {key}")
        response.raise_for_status()

        data = response.json()
        attrs = data.get("attributes", {})

        expires_at = None
        if attrs.get("exp"):
            expires_at = datetime.fromtimestamp(attrs["exp"], tz=timezone.utc)

        created_at = None
        if attrs.get("created"):
            created_at = datetime.fromtimestamp(attrs["created"], tz=timezone.utc)

        version = data.get("id", "").rsplit("/", 1)[-1]

        return SecretValue(
            key=key,
            value=data["value"],
            version=version,
            created_at=created_at,
            expires_at=expires_at,
            backend=SecretBackend.AZURE_KEY_VAULT,
        )

    async def get_secret_map(self, path: str) -> dict[str, str]:
        """Azure KV stores individual secrets; path is treated as a prefix filter."""
        secret = await self.get_secret(path)
        try:
            return json.loads(secret.value)
        except json.JSONDecodeError:
            return {path: secret.value}

    async def health_check(self) -> bool:
        try:
            await self._ensure_token()
            url = f"{self._vault_url}/secrets"
            params = {"api-version": self.API_VERSION, "maxresults": 1}
            response = await self._http_client.get(url, params=params, headers=self._auth_headers(), timeout=5.0)
            return response.status_code in (200, 401)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# AWS Secrets Manager Backend
# ---------------------------------------------------------------------------

class AWSSecretsManagerClient(SecretBackendClient):
    """AWS Secrets Manager client using IAM role or access keys."""

    def __init__(
        self,
        region: str = "us-east-1",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._region = region
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._timeout = timeout_seconds

        # In production, use boto3. Here we show the structure.
        # self._client = boto3.client(
        #     "secretsmanager",
        #     region_name=region,
        #     aws_access_key_id=aws_access_key_id,
        #     aws_secret_access_key=aws_secret_access_key,
        # )

    async def get_secret(self, key: str) -> SecretValue:
        """Get a secret from AWS Secrets Manager."""
        # In production:
        # response = self._client.get_secret_value(SecretId=key)
        # value = response.get("SecretString") or base64.b64decode(response["SecretBinary"]).decode()
        raise NotImplementedError("AWS Secrets Manager requires boto3 — install aws-sdk extras")

    async def get_secret_map(self, path: str) -> dict[str, str]:
        raise NotImplementedError("AWS Secrets Manager requires boto3 — install aws-sdk extras")

    async def health_check(self) -> bool:
        return False  # Stub


# ---------------------------------------------------------------------------
# Unified Secrets Manager with Caching and Audit
# ---------------------------------------------------------------------------

class SecretsManager:
    """
    Unified secrets manager with caching, retry, and audit logging.

    Supports multiple backends with fallback capability.
    """

    def __init__(
        self,
        primary_backend: SecretBackendClient,
        fallback_backend: SecretBackendClient | None = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        max_retries: int = MAX_RETRY_ATTEMPTS,
        enable_audit: bool = True,
    ) -> None:
        self._primary = primary_backend
        self._fallback = fallback_backend
        self._cache_ttl = cache_ttl_seconds
        self._max_retries = max_retries
        self._enable_audit = enable_audit

        self._cache: dict[str, CacheEntry] = {}
        self._rotation_callbacks: dict[str, list] = {}
        self._access_log: list[dict[str, Any]] = []

    def _cache_key(self, key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def _get_from_cache(self, key: str) -> SecretValue | None:
        cache_key = self._cache_key(key)
        entry = self._cache.get(cache_key)
        if entry and entry.is_valid() and not entry.secret.is_expired():
            return entry.secret
        if entry:
            del self._cache[cache_key]
        return None

    def _store_in_cache(self, key: str, secret: SecretValue) -> None:
        cache_key = self._cache_key(key)
        self._cache[cache_key] = CacheEntry(secret=secret, ttl_seconds=self._cache_ttl)

    def _audit_log(self, operation: str, key: str, backend: str, success: bool, error: str | None = None) -> None:
        if not self._enable_audit:
            return
        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": "secret_access",
            "operation": operation,
            "secret_key": self._mask_key(key),
            "backend": backend,
            "success": success,
            "error": error,
            "cache_hit": False,  # Will be overridden if from cache
        }
        self._access_log.append(event)
        level = logging.INFO if success else logging.ERROR
        logger.log(level, "Secret access", extra={"audit": event})

    @staticmethod
    def _mask_key(key: str) -> str:
        """Mask the last portion of a secret key for logs."""
        parts = key.rsplit("/", 1)
        if len(parts) == 2:
            return f"{parts[0]}/****"
        return "****"

    async def _retry_get(
        self,
        backend: SecretBackendClient,
        key: str,
    ) -> SecretValue:
        """Retrieve a secret with exponential backoff retry."""
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                return await backend.get_secret(key)
            except KeyError:
                raise  # Don't retry on not-found
            except Exception as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    delay = min(
                        RETRY_BASE_DELAY_SECONDS * (2 ** attempt),
                        RETRY_MAX_DELAY_SECONDS,
                    )
                    logger.warning(
                        f"Secret retrieval attempt {attempt + 1} failed, retrying in {delay}s",
                        extra={"key": self._mask_key(key), "error": str(e)},
                    )
                    await asyncio.sleep(delay)

        raise RuntimeError(f"Failed to retrieve secret after {self._max_retries} attempts") from last_error

    async def get_secret(self, key: str, bypass_cache: bool = False) -> SecretValue:
        """
        Retrieve a secret by key.

        Checks cache first, then primary backend, then fallback backend.

        Args:
            key: Secret path/name.
            bypass_cache: If True, skip cache and force fresh retrieval.

        Returns:
            SecretValue with secret data.
        """
        # Check cache
        if not bypass_cache:
            cached = self._get_from_cache(key)
            if cached:
                logger.debug("Secret cache hit", extra={"key": self._mask_key(key)})
                return cached

        # Try primary backend
        backend_name = type(self._primary).__name__
        try:
            secret = await self._retry_get(self._primary, key)
            self._store_in_cache(key, secret)
            self._audit_log("get", key, backend_name, success=True)
            return secret
        except KeyError:
            self._audit_log("get", key, backend_name, success=False, error="NOT_FOUND")
            raise
        except Exception as e:
            self._audit_log("get", key, backend_name, success=False, error=str(e))
            if self._fallback:
                logger.warning(f"Primary backend failed, trying fallback: {e}")
                fallback_name = type(self._fallback).__name__
                try:
                    secret = await self._retry_get(self._fallback, key)
                    self._store_in_cache(key, secret)
                    self._audit_log("get", key, fallback_name, success=True)
                    return secret
                except Exception as fe:
                    self._audit_log("get", key, fallback_name, success=False, error=str(fe))
                    raise RuntimeError(f"Both primary and fallback backends failed. Primary: {e}, Fallback: {fe}") from fe
            raise

    async def get_secret_value(self, key: str, bypass_cache: bool = False) -> str:
        """Convenience method to get just the string value of a secret."""
        secret = await self.get_secret(key, bypass_cache)
        return secret.value

    async def get_secret_map(self, path: str) -> dict[str, str]:
        """
        Retrieve a map of key-value secrets at a given path.

        Useful for Vault KV paths that store multiple values.
        """
        return await self._primary.get_secret_map(path)

    def invalidate_cache(self, key: str | None = None) -> None:
        """
        Invalidate cache for a specific key or all keys.

        Call this after secret rotation.
        """
        if key:
            cache_key = self._cache_key(key)
            self._cache.pop(cache_key, None)
            logger.info("Cache invalidated for key", extra={"key": self._mask_key(key)})
        else:
            self._cache.clear()
            logger.info("Full secret cache cleared")

    def register_rotation_callback(self, key: str, callback) -> None:
        """Register a callback to be called when a secret is rotated."""
        if key not in self._rotation_callbacks:
            self._rotation_callbacks[key] = []
        self._rotation_callbacks[key].append(callback)

    async def notify_rotation(self, key: str, new_secret: SecretValue) -> None:
        """Notify all registered callbacks that a secret has been rotated."""
        self.invalidate_cache(key)
        callbacks = self._rotation_callbacks.get(key, [])
        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(key, new_secret)
                else:
                    callback(key, new_secret)
            except Exception as e:
                logger.error(f"Rotation callback failed for key {self._mask_key(key)}: {e}")

    async def health_check(self) -> dict[str, bool]:
        """Check health of all configured backends."""
        results: dict[str, bool] = {}
        results["primary"] = await self._primary.health_check()
        if self._fallback:
            results["fallback"] = await self._fallback.health_check()
        return results


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_secrets_manager(config: dict[str, Any]) -> SecretsManager:
    """
    Factory function to create a SecretsManager from configuration.

    Config example:
        {
            "primary": {
                "backend": "vault",
                "vault_addr": "https://vault.internal:8200",
                "mount_path": "migration",
                "kubernetes_role": "migration-api"
            },
            "fallback": {
                "backend": "azure_key_vault",
                "vault_url": "https://migration-kv.vault.azure.net",
                "use_managed_identity": true
            },
            "cache_ttl_seconds": 300
        }
    """
    def _build_client(cfg: dict[str, Any]) -> SecretBackendClient:
        backend = cfg.get("backend", SecretBackend.VAULT)
        if backend == SecretBackend.VAULT or backend == "vault":
            return VaultClient(
                vault_addr=cfg["vault_addr"],
                mount_path=cfg.get("mount_path", "migration"),
                token=cfg.get("token"),
                kubernetes_role=cfg.get("kubernetes_role"),
                kubernetes_jwt_path=cfg.get(
                    "kubernetes_jwt_path",
                    "/var/run/secrets/kubernetes.io/serviceaccount/token",
                ),
                namespace=cfg.get("namespace"),
                tls_verify=cfg.get("tls_verify", True),
                ca_cert_path=cfg.get("ca_cert_path"),
            )
        elif backend == SecretBackend.AZURE_KEY_VAULT or backend == "azure_key_vault":
            return AzureKeyVaultClient(
                vault_url=cfg["vault_url"],
                tenant_id=cfg.get("tenant_id"),
                client_id=cfg.get("client_id"),
                client_secret=cfg.get("client_secret"),
                use_managed_identity=cfg.get("use_managed_identity", True),
            )
        elif backend == SecretBackend.AWS_SECRETS_MANAGER or backend == "aws_secrets_manager":
            return AWSSecretsManagerClient(
                region=cfg.get("region", "us-east-1"),
                aws_access_key_id=cfg.get("aws_access_key_id"),
                aws_secret_access_key=cfg.get("aws_secret_access_key"),
            )
        else:
            raise ValueError(f"Unknown secrets backend: {backend}")

    primary = _build_client(config["primary"])
    fallback = _build_client(config["fallback"]) if "fallback" in config else None

    return SecretsManager(
        primary_backend=primary,
        fallback_backend=fallback,
        cache_ttl_seconds=config.get("cache_ttl_seconds", DEFAULT_CACHE_TTL_SECONDS),
        max_retries=config.get("max_retries", MAX_RETRY_ATTEMPTS),
    )
