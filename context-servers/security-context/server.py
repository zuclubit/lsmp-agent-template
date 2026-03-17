"""
Security Context Server — MCP Server for Security Policy and Access Control

Provides security policy information to agents via MCP protocol.

SECURITY CONTRACT:
  - This server NEVER returns actual secret values.
  - It only returns: allowed paths, Vault secret NAMES, RBAC roles, and permission decisions.
  - Actual secrets are fetched directly from Vault by the agent using the provided name.
  - All permission checks are logged to the audit trail.

MCP Resources:
  - security://paths/allowed                        — whitelisted file paths
  - security://vault/{tenant_id}/secret-names       — Vault secret names for tenant (NOT values)
  - security://rbac/{tenant_id}/roles               — RBAC roles assigned to tenant
  - security://policy/{operation}                   — policy for a given operation

MCP Tools:
  - list_allowed_paths()                            -> AllowedPaths
  - get_vault_secret_names(tenant_id)               -> VaultSecretNames (names only, never values)
  - get_rbac_roles(tenant_id)                       -> RBACRoles
  - check_permission(operator_id, tenant_id, operation, resource) -> PermissionDecision
  - validate_file_access(file_path, operation)      -> FileAccessDecision
  - validate_soql_query(query)                      -> SOQLValidationResult
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get(
    "SECURITY_CONTEXT_CONFIG",
    os.path.join(os.path.dirname(__file__), "config.json"),
)

def _load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as fh:
        return json.load(fh)

CONFIG: dict[str, Any] = _load_config()

# ---------------------------------------------------------------------------
# Domain Types
# ---------------------------------------------------------------------------

@dataclass
class AllowedPaths:
    paths: list[str]
    access_mode: str  # read_only | read_write
    max_file_size_bytes: int
    allowed_extensions: list[str]
    denied_patterns: list[str]


@dataclass
class VaultSecretNames:
    """
    Contains only the NAMES (paths) of Vault secrets — never the values.
    Agents use these names to fetch secrets directly from Vault using their own SVID.
    """
    tenant_id: str
    secret_names: dict[str, str]  # logical_name -> vault_path
    vault_mount: str
    vault_addr: str  # public Vault address, not token


@dataclass
class RBACRoles:
    tenant_id: str
    roles: list[str]
    permissions: dict[str, list[str]]  # role -> [permission]
    service_account_roles: dict[str, list[str]]  # service -> [role]


@dataclass
class PermissionDecision:
    operator_id: str
    tenant_id: str
    operation: str
    resource: str
    decision: str  # ALLOW | DENY
    reason: str
    requires_human_approval: bool
    audit_event_id: str
    evaluated_at: str


@dataclass
class FileAccessDecision:
    file_path: str
    resolved_path: str
    operation: str
    decision: str  # ALLOW | DENY
    reason: str
    is_within_allowed_directory: bool
    is_allowed_extension: bool
    is_within_size_limit: bool


@dataclass
class SOQLValidationResult:
    query: str
    is_valid: bool
    is_select_only: bool
    has_injection_risk: bool
    has_string_concatenation: bool
    has_wildcard_select: bool
    violations: list[dict[str, str]]
    sanitized_query: Optional[str]


# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------

class AuditTrail:
    """
    Appends all permission checks and access decisions to an audit log.
    In production this should write to a tamper-evident audit store.
    """

    def __init__(self, audit_log_path: str) -> None:
        self._path = audit_log_path
        os.makedirs(os.path.dirname(audit_log_path), exist_ok=True)

    def record(self, event_type: str, data: dict[str, Any]) -> str:
        event_id = hashlib.sha256(
            f"{event_type}:{time.time()}:{json.dumps(data, sort_keys=True)}".encode()
        ).hexdigest()[:16]

        record = {
            "event_id": event_id,
            "event_type": event_type,
            "timestamp": _utcnow(),
            **data,
        }
        try:
            with open(self._path, "a") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.error("Failed to write audit record: %s", exc)

        return event_id


_AUDIT_LOG_PATH = CONFIG.get("audit", {}).get("log_path", ".halcon/audit/security-context.jsonl")
_AUDIT = AuditTrail(_AUDIT_LOG_PATH)

# ---------------------------------------------------------------------------
# Allowed Paths Policy
# ---------------------------------------------------------------------------

_ALLOWED_DIRECTORIES: list[str] = CONFIG.get("allowed_directories", [
    "/Users/oscarvalois/Documents/Github/s-agent/agents",
    "/Users/oscarvalois/Documents/Github/s-agent/config",
    "/Users/oscarvalois/Documents/Github/s-agent/docs",
    "/Users/oscarvalois/Documents/Github/s-agent/security",
    "/Users/oscarvalois/Documents/Github/s-agent/halcon",
    "/Users/oscarvalois/Documents/Github/s-agent/tools",
    "/Users/oscarvalois/Documents/Github/s-agent/context-servers",
])

_ALLOWED_EXTENSIONS: list[str] = CONFIG.get("allowed_extensions", [
    ".py", ".yaml", ".yml", ".json", ".md", ".txt",
])

_DENIED_PATH_PATTERNS: list[str] = [
    r".*\.env$",
    r".*\.pem$",
    r".*\.key$",
    r".*\.p12$",
    r".*\.pfx$",
    r".*id_rsa.*",
    r".*credentials.*",
    r".*/\.git/.*",
    r".*/__pycache__/.*",
    r".*\.pyc$",
    r".*/secrets/.*",
    r".*\.secrets$",
]

_MAX_FILE_SIZE_BYTES: int = CONFIG.get("max_file_size_bytes", 1_048_576)  # 1MB


def _validate_file_path(file_path: str, operation: str = "read") -> FileAccessDecision:
    """Resolve realpath and validate against all security constraints."""
    try:
        resolved = os.path.realpath(file_path)
    except Exception:
        resolved = file_path

    # Check denied patterns
    for pattern in _DENIED_PATH_PATTERNS:
        if re.match(pattern, resolved):
            return FileAccessDecision(
                file_path=file_path,
                resolved_path=resolved,
                operation=operation,
                decision="DENY",
                reason=f"Path matches denied pattern: {pattern}",
                is_within_allowed_directory=False,
                is_allowed_extension=False,
                is_within_size_limit=True,
            )

    # Check allowed directories
    in_allowed = any(resolved.startswith(d) for d in _ALLOWED_DIRECTORIES)
    if not in_allowed:
        return FileAccessDecision(
            file_path=file_path,
            resolved_path=resolved,
            operation=operation,
            decision="DENY",
            reason=f"Path is not within any allowed directory",
            is_within_allowed_directory=False,
            is_allowed_extension=False,
            is_within_size_limit=True,
        )

    # Check extension
    _, ext = os.path.splitext(resolved)
    allowed_ext = ext.lower() in _ALLOWED_EXTENSIONS
    if not allowed_ext:
        return FileAccessDecision(
            file_path=file_path,
            resolved_path=resolved,
            operation=operation,
            decision="DENY",
            reason=f"Extension {ext!r} is not in allowed list",
            is_within_allowed_directory=True,
            is_allowed_extension=False,
            is_within_size_limit=True,
        )

    # Check file size if file exists
    within_size = True
    if os.path.exists(resolved):
        size = os.path.getsize(resolved)
        within_size = size <= _MAX_FILE_SIZE_BYTES
        if not within_size:
            return FileAccessDecision(
                file_path=file_path,
                resolved_path=resolved,
                operation=operation,
                decision="DENY",
                reason=f"File size {size} exceeds limit {_MAX_FILE_SIZE_BYTES}",
                is_within_allowed_directory=True,
                is_allowed_extension=True,
                is_within_size_limit=False,
            )

    return FileAccessDecision(
        file_path=file_path,
        resolved_path=resolved,
        operation=operation,
        decision="ALLOW",
        reason="All security checks passed",
        is_within_allowed_directory=True,
        is_allowed_extension=True,
        is_within_size_limit=True,
    )


# ---------------------------------------------------------------------------
# RBAC Policy
# ---------------------------------------------------------------------------

_RBAC_POLICY: dict[str, dict[str, list[str]]] = CONFIG.get("rbac_policy", {
    "operations": {
        "MIGRATE_CHUNK": ["migration_operator", "migration_admin"],
        "EXECUTE_MIGRATION": ["migration_operator", "migration_admin"],
        "ROLLBACK": ["migration_admin"],
        "HIGH_RISK_EXECUTION": ["migration_admin"],
        "FORCE_DELETE": ["migration_admin"],
        "OVERRIDE_GATE": ["migration_admin"],
        "VALIDATE_SOURCE": ["migration_operator", "migration_admin", "security_auditor"],
        "VALIDATE_TARGET": ["migration_operator", "migration_admin", "security_auditor"],
        "SECRETS_SCAN": ["security_auditor", "migration_admin"],
        "DEPENDENCY_AUDIT": ["security_auditor", "migration_admin"],
        "READ_LOGS": ["migration_operator", "migration_admin", "security_auditor", "readonly"],
        "READ_SCHEMA": ["migration_operator", "migration_admin", "security_auditor"],
    }
})

_HIGH_RISK_OPERATIONS = {"ROLLBACK", "HIGH_RISK_EXECUTION", "FORCE_DELETE", "OVERRIDE_GATE"}


def _check_permission(
    operator_id: str,
    tenant_id: str,
    operation: str,
    resource: str,
    operator_roles: list[str],
) -> PermissionDecision:
    """Evaluate RBAC permission for an operation."""
    allowed_roles = _RBAC_POLICY.get("operations", {}).get(operation, [])
    has_permission = any(role in allowed_roles for role in operator_roles)
    requires_human = operation in _HIGH_RISK_OPERATIONS

    decision = "ALLOW" if has_permission else "DENY"
    reason = (
        f"Operator has role(s) {operator_roles} which include one of {allowed_roles}"
        if has_permission
        else f"None of operator roles {operator_roles} are in required roles {allowed_roles} for {operation}"
    )

    event_id = _AUDIT.record("permission_check", {
        "operator_id": operator_id,
        "tenant_id": tenant_id,
        "operation": operation,
        "resource": resource,
        "decision": decision,
        "roles": operator_roles,
    })

    return PermissionDecision(
        operator_id=operator_id,
        tenant_id=tenant_id,
        operation=operation,
        resource=resource,
        decision=decision,
        reason=reason,
        requires_human_approval=requires_human and has_permission,
        audit_event_id=event_id,
        evaluated_at=_utcnow(),
    )


# ---------------------------------------------------------------------------
# SOQL Validation
# ---------------------------------------------------------------------------

_SOQL_NON_SELECT_PATTERN = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|UPSERT|MERGE|UNDELETE)\s+",
    re.IGNORECASE,
)
_SOQL_CONCAT_PATTERNS = [
    re.compile(r"""soql.*?\+.*?['"]"""),
    re.compile(r'''f["']SELECT.*?\{[^}]+\}'''),
    re.compile(r'''\%\s+.*?(SELECT|WHERE|FROM)''', re.IGNORECASE),
]
_SOQL_WILDCARD_PATTERN = re.compile(r"SELECT\s+\*\s+FROM", re.IGNORECASE)


def _validate_soql(query: str) -> SOQLValidationResult:
    violations: list[dict[str, str]] = []
    is_select_only = not bool(_SOQL_NON_SELECT_PATTERN.match(query))
    has_injection = False
    has_concat = False
    has_wildcard = bool(_SOQL_WILDCARD_PATTERN.search(query))

    if not is_select_only:
        violations.append({
            "type": "NON_SELECT_QUERY",
            "severity": "CRITICAL",
            "description": "Only SELECT statements are permitted",
        })
        has_injection = True

    for pattern in _SOQL_CONCAT_PATTERNS:
        if pattern.search(query):
            has_concat = True
            violations.append({
                "type": "STRING_CONCATENATION",
                "severity": "CRITICAL",
                "description": "String concatenation in SOQL is a potential injection vector",
            })
            has_injection = True
            break

    if has_wildcard:
        violations.append({
            "type": "WILDCARD_SELECT",
            "severity": "MEDIUM",
            "description": "SELECT * is not permitted; use explicit field names",
        })

    if len(query) > 20000:
        violations.append({
            "type": "QUERY_TOO_LONG",
            "severity": "HIGH",
            "description": f"Query length {len(query)} exceeds Salesforce 20,000 char limit",
        })

    is_valid = len([v for v in violations if v["severity"] in ("CRITICAL", "HIGH")]) == 0

    return SOQLValidationResult(
        query=query[:100] + "..." if len(query) > 100 else query,  # truncate for safety
        is_valid=is_valid,
        is_select_only=is_select_only,
        has_injection_risk=has_injection,
        has_string_concatenation=has_concat,
        has_wildcard_select=has_wildcard,
        violations=violations,
        sanitized_query=None,  # We don't attempt sanitization — fix the query instead
    )


# ---------------------------------------------------------------------------
# Vault Secret Names (never values)
# ---------------------------------------------------------------------------

_VAULT_SECRET_MANIFEST: dict[str, dict[str, str]] = CONFIG.get("vault_secret_manifest", {
    "_global": {
        "migration_api_token": "secret/services/migration-api/token",
        "kafka_bootstrap_servers": "secret/kafka/bootstrap-servers",
        "kafka_sasl_credentials": "secret/kafka/sasl-credentials",
    }
})

VAULT_ADDR = os.environ.get("VAULT_ADDR", "https://vault.internal:8200")


def _get_vault_secret_names(tenant_id: str) -> VaultSecretNames:
    global_secrets = _VAULT_SECRET_MANIFEST.get("_global", {})
    tenant_secrets = _VAULT_SECRET_MANIFEST.get(tenant_id, {
        "source_db_credentials": f"secret/migration/{tenant_id}/source-db",
        "salesforce_credentials": f"secret/migration/{tenant_id}/salesforce",
        "sf_connected_app": f"secret/migration/{tenant_id}/sf-connected-app",
    })

    all_secrets = {**global_secrets, **tenant_secrets}

    _AUDIT.record("vault_names_accessed", {"tenant_id": tenant_id, "secret_count": len(all_secrets)})

    return VaultSecretNames(
        tenant_id=tenant_id,
        secret_names=all_secrets,
        vault_mount="secret",
        vault_addr=VAULT_ADDR,
    )


# ---------------------------------------------------------------------------
# Security Context Server
# ---------------------------------------------------------------------------

class SecurityContextServer:
    """
    MCP-compatible context server for security policy and access control.
    Never returns actual secret values — only names, paths, and decisions.
    """

    SERVER_ID = "security-context"
    SERVER_VERSION = "2.0.0"

    def handle_mcp_request(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        try:
            if method == "tools/call":
                tool_name = params["name"]
                arguments = params.get("arguments", {})
                result = self._dispatch_tool(tool_name, arguments)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}

            elif method == "resources/read":
                uri = params["uri"]
                result = self._read_resource(uri, params)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(result)}]}}

            elif method == "tools/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOL_DEFINITIONS}}

            elif method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": self.SERVER_ID, "version": self.SERVER_VERSION},
                        "capabilities": {"resources": {}, "tools": {}},
                    },
                }

            else:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": str(exc)}}
        except Exception:
            logger.exception("Unhandled error in security context server")
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "Internal error"}}

    def _dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "list_allowed_paths":
            return asdict(AllowedPaths(
                paths=_ALLOWED_DIRECTORIES,
                access_mode="read_only",
                max_file_size_bytes=_MAX_FILE_SIZE_BYTES,
                allowed_extensions=_ALLOWED_EXTENSIONS,
                denied_patterns=_DENIED_PATH_PATTERNS,
            ))

        elif tool_name == "get_vault_secret_names":
            tenant_id = args["tenant_id"]
            return asdict(_get_vault_secret_names(tenant_id))

        elif tool_name == "get_rbac_roles":
            tenant_id = args["tenant_id"]
            return asdict(RBACRoles(
                tenant_id=tenant_id,
                roles=list(_RBAC_POLICY.get("operations", {}).keys()),
                permissions={op: roles for op, roles in _RBAC_POLICY.get("operations", {}).items()},
                service_account_roles={
                    "migration-agent": ["migration_operator"],
                    "security-agent": ["security_auditor"],
                    "orchestrator-agent": ["migration_admin"],
                    "validation-agent": ["migration_operator"],
                },
            ))

        elif tool_name == "check_permission":
            return asdict(_check_permission(
                operator_id=args["operator_id"],
                tenant_id=args["tenant_id"],
                operation=args["operation"],
                resource=args.get("resource", ""),
                operator_roles=args.get("operator_roles", ["agent_service"]),
            ))

        elif tool_name == "validate_file_access":
            result = _validate_file_path(args["file_path"], args.get("operation", "read"))
            _AUDIT.record("file_access_check", {
                "file_path": args["file_path"],
                "resolved_path": result.resolved_path,
                "decision": result.decision,
            })
            return asdict(result)

        elif tool_name == "validate_soql_query":
            return asdict(_validate_soql(args["query"]))

        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    def _read_resource(self, uri: str, params: dict[str, Any]) -> dict[str, Any]:
        if uri == "security://paths/allowed":
            return asdict(AllowedPaths(
                paths=_ALLOWED_DIRECTORIES,
                access_mode="read_only",
                max_file_size_bytes=_MAX_FILE_SIZE_BYTES,
                allowed_extensions=_ALLOWED_EXTENSIONS,
                denied_patterns=_DENIED_PATH_PATTERNS,
            ))
        elif uri.startswith("security://vault/") and uri.endswith("/secret-names"):
            tenant_id = uri.split("/")[3]
            return asdict(_get_vault_secret_names(tenant_id))
        else:
            raise ValueError(f"Unknown resource URI: {uri}")


# ---------------------------------------------------------------------------
# MCP Tool Definitions
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS = [
    {
        "name": "list_allowed_paths",
        "description": "Return whitelisted directories and file access policy",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_vault_secret_names",
        "description": "Return Vault secret NAMES for a tenant (never actual values)",
        "inputSchema": {
            "type": "object",
            "properties": {"tenant_id": {"type": "string"}},
            "required": ["tenant_id"],
        },
    },
    {
        "name": "get_rbac_roles",
        "description": "Return RBAC role assignments for a tenant",
        "inputSchema": {
            "type": "object",
            "properties": {"tenant_id": {"type": "string"}},
            "required": ["tenant_id"],
        },
    },
    {
        "name": "check_permission",
        "description": "Check whether an operator has permission to perform an operation",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operator_id": {"type": "string"},
                "tenant_id": {"type": "string"},
                "operation": {"type": "string"},
                "resource": {"type": "string"},
                "operator_roles": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["operator_id", "tenant_id", "operation"],
        },
    },
    {
        "name": "validate_file_access",
        "description": "Check whether a file path is allowed under security policy",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "operation": {"type": "string", "enum": ["read", "list"]},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "validate_soql_query",
        "description": "Validate a SOQL query for injection risks and policy compliance",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    import datetime
    return datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    server = SecurityContextServer()
    logger.info("SecurityContextServer starting — reading from stdin")

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
