"""
Project Context Server — MCP Server for Migration Platform Knowledge

Provides structured project knowledge to agents via MCP protocol.
Agents query this server for: migration configs, tenant info, schema mappings,
transformation rule sets, historical migration outcomes.

MCP Resources:
  - migration://{migration_id}              — current migration state
  - tenant://{tenant_id}/config             — tenant configuration
  - schema://{source_system}/{entity}       — source schema definitions
  - ruleset://{ruleset_id}                  — transformation rule definitions
  - history://{tenant_id}/migrations        — past migration outcomes

MCP Tools:
  - lookup_migration_config(migration_id)                       -> MigrationConfig
  - get_tenant_settings(tenant_id)                              -> TenantSettings
  - find_similar_migrations(source_system, target_objects)      -> list[MigrationSummary]
  - get_transformation_ruleset(ruleset_id)                      -> TransformationRuleset
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "PROJECT_CONTEXT_CONFIG",
    os.path.join(os.path.dirname(__file__), "config.json"),
)

MIGRATION_API_BASE = os.environ.get("MIGRATION_API_BASE", "http://api.migration.internal")
VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://vault.internal:8200")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "")  # Injected via SPIRE/env — never hard-coded


def _load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


CONFIG: dict[str, Any] = _load_config()

# ---------------------------------------------------------------------------
# Domain Types
# ---------------------------------------------------------------------------

@dataclass
class MigrationConfig:
    migration_id: str
    tenant_id: str
    source_system: str
    target_org: str
    source_objects: list[str]
    target_objects: list[str]
    transformation_ruleset_id: str
    chunk_size: int
    dlq_threshold: int
    risk_level: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TenantSettings:
    tenant_id: str
    display_name: str
    salesforce_org_id: str
    salesforce_api_version: str
    max_concurrent_migrations: int
    rate_limit_records_per_second: int
    allowed_source_systems: list[str]
    contact_email: str
    tier: str  # standard | enterprise | enterprise_plus
    feature_flags: dict[str, bool] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SchemaDefinition:
    source_system: str
    entity: str
    version: str
    fields: list[dict[str, Any]]
    primary_key: str
    foreign_keys: list[dict[str, str]]
    indexes: list[str]
    estimated_row_count: int
    last_synced_at: str


@dataclass
class TransformationRuleset:
    ruleset_id: str
    name: str
    source_system: str
    target_system: str
    version: str
    rules: list[dict[str, Any]]
    created_by: str
    approved_by: Optional[str]
    created_at: str
    updated_at: str


@dataclass
class MigrationSummary:
    migration_id: str
    tenant_id: str
    source_system: str
    target_objects: list[str]
    status: str
    records_migrated: int
    success_rate: float
    duration_minutes: int
    executed_at: str
    final_utility: float


# ---------------------------------------------------------------------------
# TTL Cache
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class TTLCache:
    """Simple in-process TTL cache with tenant-scoped key isolation."""

    def __init__(self, default_ttl_seconds: int = 300, max_entries: int = 1000):
        self._store: dict[str, _CacheEntry] = {}
        self.default_ttl = default_ttl_seconds
        self.max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def _make_key(self, tenant_id: str, key: str) -> str:
        """Scope cache keys to tenant to prevent cross-tenant leakage."""
        raw = f"{tenant_id}:{key}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, tenant_id: str, key: str) -> Optional[Any]:
        cache_key = self._make_key(tenant_id, key)
        entry = self._store.get(cache_key)
        if entry is None:
            self._misses += 1
            return None
        if time.monotonic() > entry.expires_at:
            del self._store[cache_key]
            self._misses += 1
            return None
        self._hits += 1
        return entry.value

    def set(self, tenant_id: str, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        if len(self._store) >= self.max_entries:
            self._evict_expired()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        cache_key = self._make_key(tenant_id, key)
        self._store[cache_key] = _CacheEntry(
            value=value,
            expires_at=time.monotonic() + ttl,
        )

    def invalidate(self, tenant_id: str, key: str) -> None:
        cache_key = self._make_key(tenant_id, key)
        self._store.pop(cache_key, None)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._store.items() if now > v.expires_at]
        for k in expired:
            del self._store[k]

    @property
    def stats(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._store)}


# ---------------------------------------------------------------------------
# Access Control
# ---------------------------------------------------------------------------

class AccessController:
    """
    Enforces tenant isolation and RBAC rules.
    Operators may only access resources belonging to their tenant(s).
    Service accounts have broader access scoped by role.
    """

    ROLE_PERMISSIONS: dict[str, set[str]] = {
        "migration_operator": {"read:migration", "read:tenant", "read:schema", "read:ruleset", "read:history"},
        "migration_admin": {"*"},
        "security_auditor": {"read:migration", "read:schema", "read:ruleset"},
        "readonly": {"read:migration", "read:history"},
        "agent_service": {"read:migration", "read:tenant", "read:schema", "read:ruleset", "read:history"},
    }

    def __init__(self) -> None:
        self._token_cache: dict[str, dict[str, Any]] = {}

    def validate_access(
        self,
        caller_tenant_id: str,
        resource_tenant_id: str,
        required_permission: str,
        caller_role: str = "agent_service",
    ) -> bool:
        """
        Returns True if the caller is permitted to access the resource.
        Cross-tenant access is only allowed for migration_admin role.
        """
        if caller_tenant_id != resource_tenant_id and caller_role != "migration_admin":
            logger.warning(
                "Cross-tenant access denied: caller_tenant=%s resource_tenant=%s role=%s",
                caller_tenant_id,
                resource_tenant_id,
                caller_role,
            )
            return False

        permissions = self.ROLE_PERMISSIONS.get(caller_role, set())
        if "*" in permissions:
            return True
        return required_permission in permissions

    def assert_access(
        self,
        caller_tenant_id: str,
        resource_tenant_id: str,
        required_permission: str,
        caller_role: str = "agent_service",
    ) -> None:
        if not self.validate_access(caller_tenant_id, resource_tenant_id, required_permission, caller_role):
            raise PermissionError(
                f"Access denied: caller_tenant={caller_tenant_id} "
                f"resource_tenant={resource_tenant_id} "
                f"permission={required_permission} role={caller_role}"
            )


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

class MigrationIndex:
    """
    In-memory index for fast lookup of migrations by source system and target objects.
    Rebuilt from API on startup and refreshed on TTL expiry.
    """

    def __init__(self) -> None:
        self._by_source_system: dict[str, list[str]] = {}  # source_system -> [migration_id]
        self._by_target_object: dict[str, list[str]] = {}  # target_object -> [migration_id]
        self._last_built: float = 0.0
        self._ttl = CONFIG.get("indexing", {}).get("rebuild_interval_seconds", 900)

    def is_stale(self) -> bool:
        return (time.monotonic() - self._last_built) > self._ttl

    def rebuild(self, summaries: list[MigrationSummary]) -> None:
        self._by_source_system.clear()
        self._by_target_object.clear()
        for s in summaries:
            self._by_source_system.setdefault(s.source_system, []).append(s.migration_id)
            for obj in s.target_objects:
                self._by_target_object.setdefault(obj, []).append(s.migration_id)
        self._last_built = time.monotonic()
        logger.info("MigrationIndex rebuilt with %d summaries", len(summaries))

    def find(self, source_system: str, target_objects: list[str]) -> list[str]:
        """Return migration IDs matching source_system and any of the target_objects."""
        by_source = set(self._by_source_system.get(source_system, []))
        by_target: set[str] = set()
        for obj in target_objects:
            by_target.update(self._by_target_object.get(obj, []))
        return list(by_source & by_target)


# ---------------------------------------------------------------------------
# Data Access Layer
# ---------------------------------------------------------------------------

class ProjectContextDataAccess:
    """
    Fetches migration platform data from internal APIs.
    All reads are via authenticated HTTP — no direct DB access from this server.
    """

    def __init__(self, http_client: httpx.Client) -> None:
        self._client = http_client

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{MIGRATION_API_BASE}{path}"
        try:
            resp = self._client.get(url, params=params, timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("API error GET %s: %s %s", path, exc.response.status_code, exc.response.text[:200])
            raise
        except httpx.RequestError as exc:
            logger.error("Request error GET %s: %s", path, exc)
            raise

    def fetch_migration_config(self, migration_id: str) -> MigrationConfig:
        data = self._get(f"/api/v1/migrations/{migration_id}/config")
        return MigrationConfig(**data)

    def fetch_tenant_settings(self, tenant_id: str) -> TenantSettings:
        data = self._get(f"/api/v1/tenants/{tenant_id}/settings")
        return TenantSettings(**data)

    def fetch_schema(self, source_system: str, entity: str) -> SchemaDefinition:
        data = self._get(f"/api/v1/schemas/{source_system}/{entity}")
        return SchemaDefinition(**data)

    def fetch_ruleset(self, ruleset_id: str) -> TransformationRuleset:
        data = self._get(f"/api/v1/rulesets/{ruleset_id}")
        return TransformationRuleset(**data)

    def fetch_migration_history(self, tenant_id: str, limit: int = 50) -> list[MigrationSummary]:
        data = self._get(f"/api/v1/tenants/{tenant_id}/migrations", params={"limit": limit, "status": "COMPLETED"})
        return [MigrationSummary(**item) for item in data.get("items", [])]

    def fetch_all_completed_summaries(self, limit: int = 500) -> list[MigrationSummary]:
        data = self._get("/api/v1/migrations", params={"status": "COMPLETED", "limit": limit})
        return [MigrationSummary(**item) for item in data.get("items", [])]


# ---------------------------------------------------------------------------
# Project Context Server
# ---------------------------------------------------------------------------

class ProjectContextServer:
    """
    MCP-compatible context server for migration platform knowledge.

    Implements MCP Resources and Tools protocol.
    All data is fetched from internal APIs, cached with TTL,
    and access-controlled per tenant.
    """

    SERVER_ID = "project-context"
    SERVER_VERSION = "2.0.0"

    def __init__(self) -> None:
        self._cache = TTLCache(
            default_ttl_seconds=CONFIG.get("caching", {}).get("default_ttl_seconds", 300),
            max_entries=CONFIG.get("caching", {}).get("max_entries", 1000),
        )
        self._access = AccessController()
        self._index = MigrationIndex()
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {VAULT_TOKEN}", "X-Service-ID": self.SERVER_ID},
            timeout=15.0,
        )
        self._data = ProjectContextDataAccess(self._http)

    # ------------------------------------------------------------------
    # MCP Resource handlers
    # ------------------------------------------------------------------

    def read_resource(
        self,
        uri: str,
        caller_tenant_id: str,
        caller_role: str = "agent_service",
    ) -> dict[str, Any]:
        """
        Dispatch MCP resource read requests by URI scheme.

        Supported URIs:
          migration://{migration_id}
          tenant://{tenant_id}/config
          schema://{source_system}/{entity}
          ruleset://{ruleset_id}
          history://{tenant_id}/migrations
        """
        if uri.startswith("migration://"):
            migration_id = uri[len("migration://"):]
            return self._resource_migration(migration_id, caller_tenant_id, caller_role)

        elif uri.startswith("tenant://"):
            rest = uri[len("tenant://"):]
            parts = rest.split("/", 1)
            tenant_id = parts[0]
            return self._resource_tenant(tenant_id, caller_tenant_id, caller_role)

        elif uri.startswith("schema://"):
            rest = uri[len("schema://"):]
            parts = rest.split("/", 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid schema URI: {uri}")
            source_system, entity = parts
            return self._resource_schema(source_system, entity, caller_tenant_id, caller_role)

        elif uri.startswith("ruleset://"):
            ruleset_id = uri[len("ruleset://"):]
            return self._resource_ruleset(ruleset_id, caller_tenant_id, caller_role)

        elif uri.startswith("history://"):
            rest = uri[len("history://"):]
            tenant_id = rest.split("/")[0]
            return self._resource_history(tenant_id, caller_tenant_id, caller_role)

        else:
            raise ValueError(f"Unknown resource URI scheme: {uri}")

    def _resource_migration(
        self, migration_id: str, caller_tenant_id: str, caller_role: str
    ) -> dict[str, Any]:
        cached = self._cache.get(caller_tenant_id, f"migration:{migration_id}")
        if cached:
            return cached
        config = self._data.fetch_migration_config(migration_id)
        self._access.assert_access(caller_tenant_id, config.tenant_id, "read:migration", caller_role)
        result = asdict(config)
        self._cache.set(caller_tenant_id, f"migration:{migration_id}", result)
        return result

    def _resource_tenant(
        self, tenant_id: str, caller_tenant_id: str, caller_role: str
    ) -> dict[str, Any]:
        self._access.assert_access(caller_tenant_id, tenant_id, "read:tenant", caller_role)
        cached = self._cache.get(tenant_id, f"tenant:{tenant_id}:config")
        if cached:
            return cached
        settings = self._data.fetch_tenant_settings(tenant_id)
        result = asdict(settings)
        self._cache.set(tenant_id, f"tenant:{tenant_id}:config", result)
        return result

    def _resource_schema(
        self, source_system: str, entity: str, caller_tenant_id: str, caller_role: str
    ) -> dict[str, Any]:
        self._access.assert_access(caller_tenant_id, caller_tenant_id, "read:schema", caller_role)
        cache_key = f"schema:{source_system}:{entity}"
        cached = self._cache.get(caller_tenant_id, cache_key)
        if cached:
            return cached
        schema = self._data.fetch_schema(source_system, entity)
        result = asdict(schema)
        self._cache.set(caller_tenant_id, cache_key, result, ttl_seconds=1800)
        return result

    def _resource_ruleset(
        self, ruleset_id: str, caller_tenant_id: str, caller_role: str
    ) -> dict[str, Any]:
        self._access.assert_access(caller_tenant_id, caller_tenant_id, "read:ruleset", caller_role)
        cached = self._cache.get(caller_tenant_id, f"ruleset:{ruleset_id}")
        if cached:
            return cached
        ruleset = self._data.fetch_ruleset(ruleset_id)
        result = asdict(ruleset)
        self._cache.set(caller_tenant_id, f"ruleset:{ruleset_id}", result, ttl_seconds=3600)
        return result

    def _resource_history(
        self, tenant_id: str, caller_tenant_id: str, caller_role: str
    ) -> dict[str, Any]:
        self._access.assert_access(caller_tenant_id, tenant_id, "read:history", caller_role)
        cached = self._cache.get(tenant_id, f"history:{tenant_id}")
        if cached:
            return cached
        summaries = self._data.fetch_migration_history(tenant_id)
        result = {"tenant_id": tenant_id, "migrations": [asdict(s) for s in summaries]}
        self._cache.set(tenant_id, f"history:{tenant_id}", result, ttl_seconds=300)
        return result

    # ------------------------------------------------------------------
    # MCP Tool handlers
    # ------------------------------------------------------------------

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        caller_tenant_id: str,
        caller_role: str = "agent_service",
    ) -> dict[str, Any]:
        """Dispatch MCP tool calls by name."""
        dispatch: dict[str, Any] = {
            "lookup_migration_config": self._tool_lookup_migration_config,
            "get_tenant_settings": self._tool_get_tenant_settings,
            "find_similar_migrations": self._tool_find_similar_migrations,
            "get_transformation_ruleset": self._tool_get_transformation_ruleset,
        }
        handler = dispatch.get(tool_name)
        if handler is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        return handler(arguments, caller_tenant_id, caller_role)

    def _tool_lookup_migration_config(
        self, args: dict[str, Any], caller_tenant_id: str, caller_role: str
    ) -> dict[str, Any]:
        migration_id = args["migration_id"]
        if not migration_id or not isinstance(migration_id, str):
            raise ValueError("migration_id must be a non-empty string")
        return self._resource_migration(migration_id, caller_tenant_id, caller_role)

    def _tool_get_tenant_settings(
        self, args: dict[str, Any], caller_tenant_id: str, caller_role: str
    ) -> dict[str, Any]:
        tenant_id = args["tenant_id"]
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id must be a non-empty string")
        return self._resource_tenant(tenant_id, caller_tenant_id, caller_role)

    def _tool_find_similar_migrations(
        self, args: dict[str, Any], caller_tenant_id: str, caller_role: str
    ) -> dict[str, Any]:
        source_system = args["source_system"]
        target_objects = args.get("target_objects", [])
        if not isinstance(target_objects, list):
            raise ValueError("target_objects must be a list")

        if self._index.is_stale():
            summaries = self._data.fetch_all_completed_summaries()
            self._index.rebuild(summaries)

        migration_ids = self._index.find(source_system, target_objects)

        # Fetch configs and filter to caller's tenant
        results = []
        for mid in migration_ids[:20]:  # Limit to 20 results
            try:
                config = self._data.fetch_migration_config(mid)
                if config.tenant_id == caller_tenant_id or caller_role == "migration_admin":
                    results.append(asdict(config))
            except Exception:
                pass

        return {"similar_migrations": results, "count": len(results)}

    def _tool_get_transformation_ruleset(
        self, args: dict[str, Any], caller_tenant_id: str, caller_role: str
    ) -> dict[str, Any]:
        ruleset_id = args["ruleset_id"]
        if not ruleset_id or not isinstance(ruleset_id, str):
            raise ValueError("ruleset_id must be a non-empty string")
        return self._resource_ruleset(ruleset_id, caller_tenant_id, caller_role)

    # ------------------------------------------------------------------
    # MCP Protocol envelope
    # ------------------------------------------------------------------

    def handle_mcp_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """
        Handle a raw MCP JSON-RPC request.
        Returns a JSON-RPC response envelope.
        """
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})
        caller_tenant_id = params.get("_caller_tenant_id", "unknown")
        caller_role = params.get("_caller_role", "agent_service")

        try:
            if method == "resources/read":
                uri = params["uri"]
                result = self.read_resource(uri, caller_tenant_id, caller_role)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(result)}]}}

            elif method == "tools/call":
                tool_name = params["name"]
                arguments = params.get("arguments", {})
                result = self.call_tool(tool_name, arguments, caller_tenant_id, caller_role)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}

            elif method == "tools/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOL_DEFINITIONS}}

            elif method == "resources/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": _RESOURCE_DEFINITIONS}}

            elif method == "initialize":
                return {"jsonrpc": "2.0", "id": req_id, "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": self.SERVER_ID, "version": self.SERVER_VERSION},
                    "capabilities": {"resources": {"subscribe": False}, "tools": {}},
                }}

            else:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

        except PermissionError as exc:
            logger.warning("Permission denied: %s", exc)
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "Permission denied", "data": str(exc)}}
        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid params", "data": str(exc)}}
        except Exception as exc:
            logger.exception("Unhandled error in MCP request id=%s method=%s", req_id, method)
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "Internal error"}}


# ---------------------------------------------------------------------------
# MCP Metadata
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS = [
    {
        "name": "lookup_migration_config",
        "description": "Retrieve full migration configuration by migration_id",
        "inputSchema": {
            "type": "object",
            "properties": {"migration_id": {"type": "string", "description": "Unique migration run identifier"}},
            "required": ["migration_id"],
        },
    },
    {
        "name": "get_tenant_settings",
        "description": "Retrieve tenant configuration and feature flags",
        "inputSchema": {
            "type": "object",
            "properties": {"tenant_id": {"type": "string"}},
            "required": ["tenant_id"],
        },
    },
    {
        "name": "find_similar_migrations",
        "description": "Find past migrations with matching source system and target objects",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_system": {"type": "string"},
                "target_objects": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["source_system"],
        },
    },
    {
        "name": "get_transformation_ruleset",
        "description": "Retrieve transformation ruleset definition by ID",
        "inputSchema": {
            "type": "object",
            "properties": {"ruleset_id": {"type": "string"}},
            "required": ["ruleset_id"],
        },
    },
]

_RESOURCE_DEFINITIONS = [
    {"uri": "migration://{migration_id}", "name": "Migration State", "mimeType": "application/json"},
    {"uri": "tenant://{tenant_id}/config", "name": "Tenant Configuration", "mimeType": "application/json"},
    {"uri": "schema://{source_system}/{entity}", "name": "Source Schema Definition", "mimeType": "application/json"},
    {"uri": "ruleset://{ruleset_id}", "name": "Transformation Ruleset", "mimeType": "application/json"},
    {"uri": "history://{tenant_id}/migrations", "name": "Migration History", "mimeType": "application/json"},
]


# ---------------------------------------------------------------------------
# Entrypoint (stdio MCP transport)
# ---------------------------------------------------------------------------

def main() -> None:
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    server = ProjectContextServer()
    logger.info("ProjectContextServer starting — reading from stdin")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}
            print(json.dumps(response), flush=True)
            continue

        response = server.handle_mcp_request(request)
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
