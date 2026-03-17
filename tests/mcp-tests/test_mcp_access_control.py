"""
MCP Server Access Control Tests — Phase 9, Security Suite.

Verifies that all four MCP servers (filesystem, documentation, api, memory)
enforce their access control policies correctly, including:

  - Filesystem server: denies writes, denies .env/secrets paths, allows whitelisted reads
  - Documentation server: returns runbooks, never includes credentials in results
  - API server: blocks DELETE on data endpoints and DML SOQL
  - Memory server: cross-tenant isolation, automatic TTL purge
  - Rate limiting and authentication enforcement (MCP-layer)

Compliance: FedRAMP AC-3, AC-4, FedRAMP IA-3 (Device Identification / Authentication).
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import pytest

from validation.layer import SOQLValidator, ValidationResult


# ---------------------------------------------------------------------------
# MCP server simulation stubs
# These stubs replicate the policy logic from mcp-servers/*/policies.yaml
# ---------------------------------------------------------------------------


class MCPDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class MCPStatusCode(int, Enum):
    OK = 200
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    TOO_MANY_REQUESTS = 429
    METHOD_NOT_ALLOWED = 405


@dataclass
class MCPResponse:
    status: MCPStatusCode
    body: Any = None
    denial_reason: str = ""
    alert_generated: bool = False


# ---------------------------------------------------------------------------
# Filesystem MCP Server
# ---------------------------------------------------------------------------

_FS_WRITE_OPERATIONS = {"write_file", "delete_file", "create_file", "chmod", "chown", "rename"}
_FS_SENSITIVE_PATTERNS = [".env", "secrets", ".pem", ".key", ".pfx", "vault_token", ".claude/"]
_FS_ALLOWED_PATHS_PREFIX = [
    "./docs/",
    "./agents/",
    "./migration/",
    "./integrations/",
    "./security/",
    "./monitoring/",
]


class FilesystemMCPServer:
    """
    Simulates the filesystem MCP server policy from
    mcp-servers/filesystem-server/policies.yaml.

    Default: DENY. Explicit ALLOW rules must match for access.
    """

    def __init__(self, project_root: str = "/app") -> None:
        self._project_root = project_root

    def handle(
        self,
        operation: str,
        resource_path: str,
        agent_name: str,
        svid_token: Optional[str] = "valid-svid-token",
    ) -> MCPResponse:
        """Process an MCP request through the policy engine."""
        # Authentication check
        if not svid_token:
            return MCPResponse(
                status=MCPStatusCode.UNAUTHORIZED,
                denial_reason="MCP_AUTH_REQUIRED: No SVID token provided",
            )

        # Rule FS-003: Block ALL write operations (priority 1 — always applied first)
        if operation in _FS_WRITE_OPERATIONS:
            return MCPResponse(
                status=MCPStatusCode.METHOD_NOT_ALLOWED,
                body={"error": f"Write operation '{operation}' is unconditionally denied"},
                denial_reason=f"FS-003: write operations blocked for all principals",
            )

        # Rule FS-004: Block access to sensitive paths (priority 1)
        lower_path = resource_path.lower()
        for pattern in _FS_SENSITIVE_PATTERNS:
            if pattern in lower_path:
                return MCPResponse(
                    status=MCPStatusCode.FORBIDDEN,
                    body={"error": f"Access to sensitive path denied"},
                    denial_reason=f"FS-004: path matches sensitive pattern '{pattern}'",
                    alert_generated=True,  # Any access to sensitive path → immediate alert
                )

        # Check if path is under an allowed prefix
        for allowed_prefix in _FS_ALLOWED_PATHS_PREFIX:
            if resource_path.startswith(allowed_prefix) or resource_path.lstrip("./").startswith(
                allowed_prefix.lstrip("./")
            ):
                return MCPResponse(status=MCPStatusCode.OK, body={"content": f"<content of {resource_path}>"})

        # Default DENY
        return MCPResponse(
            status=MCPStatusCode.FORBIDDEN,
            body={"error": "Access denied: path not in allowlist"},
            denial_reason="FS-default: path not matched by any ALLOW rule",
        )


# ---------------------------------------------------------------------------
# Documentation MCP Server
# ---------------------------------------------------------------------------

_RUNBOOKS: dict[str, str] = {
    "migration_stall": (
        "# Migration Stall Runbook\n\n"
        "## Symptoms\n- run status stuck in 'running' for >30 min\n"
        "- No error rate increase but progress=0%\n\n"
        "## Immediate Actions\n"
        "1. Call check_migration_status to get current batch cursor\n"
        "2. Check Salesforce API limits with get_salesforce_limits\n"
        "3. If limits depleted, call scale_batch_size(new_size=50)\n"
        "4. If DB connection lost, call pause_migration then create_incident(P2)\n\n"
        "## Escalation\n- P1 if > 1M records blocked\n- Contact: migration-ops@agency.gov\n"
    ),
    "high_error_rate": (
        "# High Error Rate Runbook\n\n"
        "## Threshold: Error rate > 5%\n\n"
        "## Investigation Steps\n"
        "1. get_error_report(run_id) to identify failing record types\n"
        "2. compare_sample_records on 10 failing records\n"
        "3. Check for field mapping issues or validation failures\n"
        "4. If root cause identified: retry_failed_records\n"
        "5. If systemic: pause_migration and create_incident(P1)\n"
    ),
}

# Credential patterns that must NEVER appear in documentation output
import re
_DOC_CREDENTIAL_PATTERNS = [
    re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}"),
    re.compile(r"hvs\.[a-zA-Z0-9_\-]{20,}"),
    re.compile(r"00D[a-zA-Z0-9]{12,15}![a-zA-Z0-9_.]{40,}"),
    re.compile(r"-----BEGIN.*PRIVATE KEY-----"),
    re.compile(r"password\s*[:=]\s*['\"]?\S+['\"]?", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]


def _contains_credentials(text: str) -> bool:
    return any(pattern.search(text) for pattern in _DOC_CREDENTIAL_PATTERNS)


class DocumentationMCPServer:
    """Simulates the documentation MCP server."""

    def get_runbook(self, runbook_name: str, svid_token: Optional[str] = "valid-svid-token") -> MCPResponse:
        if not svid_token:
            return MCPResponse(status=MCPStatusCode.UNAUTHORIZED, denial_reason="AUTH_REQUIRED")
        content = _RUNBOOKS.get(runbook_name)
        if content is None:
            return MCPResponse(
                status=MCPStatusCode.FORBIDDEN,
                denial_reason=f"Runbook '{runbook_name}' not found",
            )
        # Safety check: runbook content must not contain credentials
        if _contains_credentials(content):
            return MCPResponse(
                status=MCPStatusCode.FORBIDDEN,
                denial_reason="CONTENT_POLICY: credential pattern detected in runbook",
                alert_generated=True,
            )
        return MCPResponse(status=MCPStatusCode.OK, body={"content": content})


# ---------------------------------------------------------------------------
# API MCP Server
# ---------------------------------------------------------------------------

_API_DELETE_PATHS_BLOCKED = [
    "/api/v1/migrations/records",
    "/api/v1/data/",
    "/api/v1/accounts/",
    "/api/v1/contacts/",
]

_SOQL_VALIDATOR = SOQLValidator()


class APIMCPServer:
    """Simulates the API MCP server policy."""

    def handle_http(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        svid_token: Optional[str] = "valid-svid-token",
    ) -> MCPResponse:
        if not svid_token:
            return MCPResponse(status=MCPStatusCode.UNAUTHORIZED, denial_reason="AUTH_REQUIRED")

        # Block DELETE on data endpoints
        if method.upper() == "DELETE":
            for blocked_path in _API_DELETE_PATHS_BLOCKED:
                if path.startswith(blocked_path):
                    return MCPResponse(
                        status=MCPStatusCode.METHOD_NOT_ALLOWED,
                        denial_reason=f"DELETE on data endpoint '{path}' is unconditionally denied",
                    )

        # Block DML SOQL in body
        if body and "soql" in body:
            soql = body["soql"]
            result, rule_id = _SOQL_VALIDATOR.validate(soql)
            if result == ValidationResult.BLOCK:
                return MCPResponse(
                    status=MCPStatusCode.FORBIDDEN,
                    denial_reason=f"DML_SOQL_BLOCKED: rule {rule_id}",
                )

        return MCPResponse(status=MCPStatusCode.OK, body={"status": "ok"})


# ---------------------------------------------------------------------------
# Memory MCP Server
# ---------------------------------------------------------------------------


@dataclass
class MemoryEntry:
    value: Any
    agent_id: str
    tenant_id: str
    session_id: str
    expires_at: float  # monotonic time


class MemoryMCPServer:
    """Simulates the memory MCP server with tenant isolation and TTL enforcement."""

    def __init__(self) -> None:
        # key format: "session:{sid}:agent:{aid}:WORKING:{entry_key}"
        self._store: dict[str, MemoryEntry] = {}

    def write(
        self,
        session_id: str,
        agent_id: str,
        tenant_id: str,
        entry_key: str,
        value: Any,
        ttl_seconds: float = 3600.0,
        svid_token: Optional[str] = "valid-svid-token",
    ) -> MCPResponse:
        if not svid_token:
            return MCPResponse(status=MCPStatusCode.UNAUTHORIZED, denial_reason="AUTH_REQUIRED")
        key = f"session:{session_id}:agent:{agent_id}:WORKING:{entry_key}"
        self._store[key] = MemoryEntry(
            value=value,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            expires_at=time.monotonic() + ttl_seconds,
        )
        return MCPResponse(status=MCPStatusCode.OK, body={"written": True})

    def read(
        self,
        session_id: str,
        requesting_agent_id: str,
        requesting_tenant_id: str,
        target_agent_id: str,
        target_tenant_id: str,
        entry_key: str,
        svid_token: Optional[str] = "valid-svid-token",
    ) -> MCPResponse:
        if not svid_token:
            return MCPResponse(status=MCPStatusCode.UNAUTHORIZED, denial_reason="AUTH_REQUIRED")

        # Cross-tenant isolation: unconditional DENY
        if requesting_tenant_id != target_tenant_id:
            return MCPResponse(
                status=MCPStatusCode.FORBIDDEN,
                denial_reason=(
                    f"CROSS_TENANT_BLOCKED: agent '{requesting_agent_id}' (tenant '{requesting_tenant_id}') "
                    f"cannot read memory of tenant '{target_tenant_id}'"
                ),
                alert_generated=True,
            )

        # Cross-agent isolation: DENY unless orchestrator
        if requesting_agent_id != target_agent_id and requesting_agent_id != "orchestrator-agent":
            return MCPResponse(
                status=MCPStatusCode.FORBIDDEN,
                denial_reason=(
                    f"CROSS_AGENT_BLOCKED: agent '{requesting_agent_id}' cannot read "
                    f"'{target_agent_id}' working memory (MEM-ISO-002)"
                ),
                alert_generated=True,
            )

        key = f"session:{session_id}:agent:{target_agent_id}:WORKING:{entry_key}"
        entry = self._store.get(key)

        if entry is None:
            return MCPResponse(status=MCPStatusCode.FORBIDDEN, denial_reason="ENTRY_NOT_FOUND")

        # TTL check
        if time.monotonic() > entry.expires_at:
            del self._store[key]
            return MCPResponse(status=MCPStatusCode.FORBIDDEN, denial_reason="ENTRY_EXPIRED")

        return MCPResponse(status=MCPStatusCode.OK, body={"value": entry.value})


# ---------------------------------------------------------------------------
# Rate limiter for MCP servers
# ---------------------------------------------------------------------------


class MCPRateLimiter:
    def __init__(self, max_requests: int = 100, window_seconds: int = 60) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._windows: dict[str, deque] = defaultdict(deque)

    def check(self, agent_name: str) -> MCPResponse:
        now = time.monotonic()
        window = self._windows[agent_name]
        while window and now - window[0] > self._window_seconds:
            window.popleft()
        if len(window) >= self._max_requests:
            return MCPResponse(
                status=MCPStatusCode.TOO_MANY_REQUESTS,
                denial_reason=f"RATE_LIMIT: agent '{agent_name}' exceeded {self._max_requests} req/{self._window_seconds}s",
            )
        window.append(now)
        return MCPResponse(status=MCPStatusCode.OK)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fs_server() -> FilesystemMCPServer:
    return FilesystemMCPServer(project_root="/app")


@pytest.fixture
def doc_server() -> DocumentationMCPServer:
    return DocumentationMCPServer()


@pytest.fixture
def api_server() -> APIMCPServer:
    return APIMCPServer()


@pytest.fixture
def mem_server() -> MemoryMCPServer:
    return MemoryMCPServer()


@pytest.fixture
def rate_limiter() -> MCPRateLimiter:
    return MCPRateLimiter(max_requests=5, window_seconds=60)


# ---------------------------------------------------------------------------
# Test 1: Filesystem server denies write operations
# ---------------------------------------------------------------------------


def test_filesystem_server_denies_write(fs_server: FilesystemMCPServer) -> None:
    """Any write operation (write_file, delete_file, etc.) must return DENY."""
    write_operations = ["write_file", "delete_file", "create_file", "chmod", "chown"]
    for op in write_operations:
        response = fs_server.handle(operation=op, resource_path="./docs/report.md", agent_name="documentation-agent")
        assert response.status == MCPStatusCode.METHOD_NOT_ALLOWED, (
            f"Expected METHOD_NOT_ALLOWED for write op '{op}', got {response.status}"
        )
        assert response.denial_reason, f"denial_reason must be set for blocked write op '{op}'"


# ---------------------------------------------------------------------------
# Test 2: Filesystem server denies .env file read
# ---------------------------------------------------------------------------


def test_filesystem_server_denies_env_file(fs_server: FilesystemMCPServer) -> None:
    """Reading '.env' must be DENIED and must trigger an alert."""
    response = fs_server.handle(operation="read_file", resource_path=".env", agent_name="security-agent")
    assert response.status == MCPStatusCode.FORBIDDEN, (
        f"Expected FORBIDDEN for .env read, got {response.status}"
    )
    assert response.alert_generated, "Reading .env must trigger an alert"


# ---------------------------------------------------------------------------
# Test 3: Filesystem server denies secrets path
# ---------------------------------------------------------------------------


def test_filesystem_server_denies_secrets_path(fs_server: FilesystemMCPServer) -> None:
    """Read of '/var/secrets/vault_token' must be DENIED."""
    response = fs_server.handle(
        operation="read_file",
        resource_path="/var/secrets/vault_token",
        agent_name="security-agent",
    )
    assert response.status == MCPStatusCode.FORBIDDEN, (
        f"Expected FORBIDDEN for vault_token path, got {response.status}"
    )
    assert response.alert_generated


# ---------------------------------------------------------------------------
# Test 4: Filesystem server allows whitelisted read
# ---------------------------------------------------------------------------


def test_filesystem_server_allows_whitelisted_read(fs_server: FilesystemMCPServer) -> None:
    """Reading './docs/api/openapi.yaml' must be ALLOWED."""
    response = fs_server.handle(
        operation="read_file",
        resource_path="./docs/api/openapi.yaml",
        agent_name="security-agent",
    )
    assert response.status == MCPStatusCode.OK, (
        f"Expected OK for whitelisted read, got {response.status}. "
        f"Denial reason: {response.denial_reason}"
    )


# ---------------------------------------------------------------------------
# Test 5: Documentation server returns runbook content
# ---------------------------------------------------------------------------


def test_documentation_server_returns_runbook(doc_server: DocumentationMCPServer) -> None:
    """get_runbook('migration_stall') must return non-empty content."""
    response = doc_server.get_runbook("migration_stall")
    assert response.status == MCPStatusCode.OK, (
        f"Expected OK for known runbook, got {response.status}"
    )
    assert response.body is not None
    assert "content" in response.body
    assert len(response.body["content"]) > 100, "Runbook content must be substantial"
    assert "migration" in response.body["content"].lower()


# ---------------------------------------------------------------------------
# Test 6: Documentation server — no credentials in runbook results
# ---------------------------------------------------------------------------


def test_documentation_server_no_credentials_in_results(doc_server: DocumentationMCPServer) -> None:
    """Runbook content must never contain API keys, tokens, or passwords."""
    for runbook_name in ["migration_stall", "high_error_rate"]:
        response = doc_server.get_runbook(runbook_name)
        if response.status == MCPStatusCode.OK:
            content = response.body.get("content", "")
            assert not _contains_credentials(content), (
                f"Runbook '{runbook_name}' must not contain credentials. "
                f"Found credential pattern in content."
            )


# ---------------------------------------------------------------------------
# Test 7: API server blocks DELETE on data endpoints
# ---------------------------------------------------------------------------


def test_api_server_blocks_delete_on_data(api_server: APIMCPServer) -> None:
    """DELETE /api/v1/migrations/records must be DENIED."""
    response = api_server.handle_http(
        method="DELETE",
        path="/api/v1/migrations/records",
        body=None,
    )
    assert response.status == MCPStatusCode.METHOD_NOT_ALLOWED, (
        f"Expected METHOD_NOT_ALLOWED for DELETE on data, got {response.status}"
    )


# ---------------------------------------------------------------------------
# Test 8: API server blocks DML SOQL
# ---------------------------------------------------------------------------


def test_api_server_blocks_dml_soql(api_server: APIMCPServer) -> None:
    """SOQL body containing DELETE must be DENIED."""
    response = api_server.handle_http(
        method="POST",
        path="/api/v1/query",
        body={"soql": "DELETE FROM Account WHERE Legacy_ID__c = null"},
    )
    assert response.status == MCPStatusCode.FORBIDDEN, (
        f"Expected FORBIDDEN for DML SOQL, got {response.status}"
    )
    assert "DML_SOQL_BLOCKED" in response.denial_reason or "DELETE" in response.denial_reason


# ---------------------------------------------------------------------------
# Test 9: Memory server cross-tenant isolation
# ---------------------------------------------------------------------------


def test_memory_server_cross_tenant_isolation(mem_server: MemoryMCPServer) -> None:
    """Agent from tenant A cannot read tenant B memory."""
    # Write tenant B's data
    mem_server.write(
        session_id="sess-001",
        agent_id="validation-agent",
        tenant_id="tenant-B",
        entry_key="migration_state",
        value={"status": "running", "sensitive": "data"},
    )

    # Tenant A's agent attempts to read tenant B's memory
    response = mem_server.read(
        session_id="sess-001",
        requesting_agent_id="validation-agent",
        requesting_tenant_id="tenant-A",
        target_agent_id="validation-agent",
        target_tenant_id="tenant-B",
        entry_key="migration_state",
    )
    assert response.status == MCPStatusCode.FORBIDDEN, (
        f"Expected FORBIDDEN for cross-tenant read, got {response.status}"
    )
    assert response.alert_generated, "Cross-tenant attempt must trigger an alert"
    assert "CROSS_TENANT_BLOCKED" in response.denial_reason


# ---------------------------------------------------------------------------
# Test 10: Memory server auto-purge after TTL
# ---------------------------------------------------------------------------


def test_memory_server_auto_purge_after_ttl(mem_server: MemoryMCPServer) -> None:
    """Memory entry with expired TTL must be purged and return ENTRY_EXPIRED."""
    # Write with a very short TTL (0.01 seconds — effectively immediate expiry)
    mem_server.write(
        session_id="sess-002",
        agent_id="planning-agent",
        tenant_id="tenant-C",
        entry_key="temp_plan",
        value={"plan": "draft"},
        ttl_seconds=0.001,  # expire almost immediately
    )

    # Wait for TTL to expire
    time.sleep(0.05)

    # Attempt to read the expired entry
    response = mem_server.read(
        session_id="sess-002",
        requesting_agent_id="planning-agent",
        requesting_tenant_id="tenant-C",
        target_agent_id="planning-agent",
        target_tenant_id="tenant-C",
        entry_key="temp_plan",
    )
    assert response.status == MCPStatusCode.FORBIDDEN, (
        f"Expected FORBIDDEN for expired entry, got {response.status}"
    )
    assert "EXPIRED" in response.denial_reason


# ---------------------------------------------------------------------------
# Test 11: MCP rate limit enforced
# ---------------------------------------------------------------------------


def test_mcp_rate_limit_enforced(rate_limiter: MCPRateLimiter) -> None:
    """Exceeding per-agent rate limit must return 429 Too Many Requests."""
    agent = "security-agent"

    # First 5 requests succeed
    for i in range(5):
        response = rate_limiter.check(agent)
        assert response.status == MCPStatusCode.OK, (
            f"Request {i + 1} must succeed, got {response.status}"
        )

    # 6th request exceeds limit
    response = rate_limiter.check(agent)
    assert response.status == MCPStatusCode.TOO_MANY_REQUESTS, (
        f"Expected 429 after exceeding rate limit, got {response.status}"
    )
    assert "RATE_LIMIT" in response.denial_reason


# ---------------------------------------------------------------------------
# Test 12: MCP requires authentication (SVID token)
# ---------------------------------------------------------------------------


def test_mcp_requires_authentication(fs_server: FilesystemMCPServer) -> None:
    """Request without SVID token must be rejected with 401 Unauthorized."""
    response = fs_server.handle(
        operation="read_file",
        resource_path="./docs/api/openapi.yaml",
        agent_name="security-agent",
        svid_token=None,  # No token
    )
    assert response.status == MCPStatusCode.UNAUTHORIZED, (
        f"Expected 401 UNAUTHORIZED for missing SVID token, got {response.status}"
    )


def test_mcp_requires_authentication_memory_server(mem_server: MemoryMCPServer) -> None:
    """Memory server read without SVID token must return 401."""
    response = mem_server.read(
        session_id="sess-003",
        requesting_agent_id="planning-agent",
        requesting_tenant_id="tenant-X",
        target_agent_id="planning-agent",
        target_tenant_id="tenant-X",
        entry_key="plan",
        svid_token=None,
    )
    assert response.status == MCPStatusCode.UNAUTHORIZED


def test_mcp_requires_authentication_api_server(api_server: APIMCPServer) -> None:
    """API server request without SVID token must return 401."""
    response = api_server.handle_http(
        method="GET",
        path="/api/v1/migrations/runs/run-001",
        svid_token=None,
    )
    assert response.status == MCPStatusCode.UNAUTHORIZED


def test_cross_agent_memory_read_blocked(mem_server: MemoryMCPServer) -> None:
    """Non-orchestrator agent cannot read another agent's WORKING memory."""
    mem_server.write(
        session_id="sess-004",
        agent_id="execution-agent",
        tenant_id="tenant-X",
        entry_key="state",
        value={"batch_cursor": 5000},
    )
    # validation-agent tries to read execution-agent's memory
    response = mem_server.read(
        session_id="sess-004",
        requesting_agent_id="validation-agent",
        requesting_tenant_id="tenant-X",
        target_agent_id="execution-agent",
        target_tenant_id="tenant-X",
        entry_key="state",
    )
    assert response.status == MCPStatusCode.FORBIDDEN
    assert "CROSS_AGENT_BLOCKED" in response.denial_reason


def test_orchestrator_can_read_agent_memory(mem_server: MemoryMCPServer) -> None:
    """Orchestrator agent may read any other agent's WORKING memory (synthesis gate)."""
    mem_server.write(
        session_id="sess-005",
        agent_id="validation-agent",
        tenant_id="tenant-X",
        entry_key="validation_result",
        value={"grade": "A", "score": 0.97},
    )
    response = mem_server.read(
        session_id="sess-005",
        requesting_agent_id="orchestrator-agent",  # orchestrator
        requesting_tenant_id="tenant-X",
        target_agent_id="validation-agent",
        target_tenant_id="tenant-X",
        entry_key="validation_result",
    )
    assert response.status == MCPStatusCode.OK
    assert response.body["value"]["grade"] == "A"
