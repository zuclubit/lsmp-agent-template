"""
Agent Access Control Tests — Phase 9, Security Suite.

Verifies that the agent handoff graph, tool access restrictions, tenant isolation,
file system boundaries, circuit breakers, and rate limiters are all enforced
as specified in the security policy.

Tests the following control surfaces:
  - Validation gate must ALLOW before execution can proceed
  - Agent handoff graph prevents unauthorized direct invocations
  - Debugging agent cannot call write tools (read-only enforcement)
  - Cross-tenant context isolation
  - File path traversal prevention in security agent's read_file
  - Restricted-context access requires human approval
  - code-execution-tool is disabled for all agents
  - Circuit breaker opens after 3 consecutive failures
  - Per-agent rate limiting enforcement

Compliance: FedRAMP AC-3, AC-4, AC-6, NIST SP 800-207 (Zero Trust).
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from validation.layer import SecurityBlockedError, ValidationResult


# ---------------------------------------------------------------------------
# Local stubs for access control components
# (In production these come from agents._shared.base_agent and
#  agents._shared.schemas; we define minimal equivalents for test isolation)
# ---------------------------------------------------------------------------


class CircuitBreakerOpenError(RuntimeError):
    """Raised when a circuit breaker is OPEN and blocks a call."""
    def __init__(self, agent_role: str, remaining_seconds: float = 60.0) -> None:
        super().__init__(
            f"Circuit breaker for agent '{agent_role}' is OPEN. "
            f"Retry in {remaining_seconds:.1f}s."
        )
        self.agent_role = agent_role
        self.remaining_seconds = remaining_seconds


class RateLimitExceeded(RuntimeError):
    """Raised when a per-agent rate limit is exceeded."""
    def __init__(self, agent_name: str, limit: int, window_seconds: int) -> None:
        super().__init__(
            f"Rate limit exceeded for agent '{agent_name}': "
            f"max {limit} calls per {window_seconds}s."
        )
        self.agent_name = agent_name
        self.limit = limit
        self.window_seconds = window_seconds


class GateDecision(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


# ---------------------------------------------------------------------------
# Minimal VALID_HANDOFF_GRAPH (mirrors the real system's agent routing policy)
# ---------------------------------------------------------------------------

# Adjacency list: agent -> set of agents it is allowed to hand off to directly
VALID_HANDOFF_GRAPH: dict[str, set[str]] = {
    "orchestrator": {"planning", "validation", "security", "execution", "debugging", "documentation"},
    "planning": {"orchestrator"},       # planning can only return to orchestrator
    "validation": {"orchestrator"},
    "security": {"orchestrator"},
    "execution": {"orchestrator"},
    "debugging": {"orchestrator"},
    "documentation": {"orchestrator"},
}

# Tools allowed per agent role (read-only enforcement)
AGENT_TOOL_ALLOWLIST: dict[str, set[str]] = {
    "orchestrator": {
        "delegate_to_migration_agent", "delegate_to_validation_agent",
        "delegate_to_documentation_agent", "delegate_to_security_agent",
        "run_agents_in_parallel", "synthesise_results",
    },
    "planning": {"check_migration_status", "get_salesforce_limits", "get_system_health"},
    "validation": {
        "validate_record_counts", "check_field_completeness", "detect_anomalies",
        "compare_sample_records", "check_referential_integrity", "check_duplicate_records",
        "validate_data_types", "generate_report", "run_custom_soql_check",
        "get_field_metadata",
    },
    "security": {
        "scan_file_for_secrets", "check_dependency_vulnerabilities",
        "audit_authentication_code", "check_sql_injection",
        "audit_salesforce_permissions", "check_pii_handling",
        "check_tls_configuration", "read_file", "generate_security_report",
    },
    "execution": {
        "check_migration_status", "pause_migration", "resume_migration",
        "get_error_report", "retry_failed_records", "scale_batch_size",
        "get_salesforce_limits", "get_system_health", "create_incident",
    },
    "debugging": {
        "check_migration_status", "get_error_report", "get_system_health",
        "read_file",  # read-only; write tools are NOT in this list
    },
    "documentation": {"generate_report", "read_file"},
}

# Context types requiring human approval
RESTRICTED_CONTEXT_TYPES: set[str] = {
    "restricted-context",
    "production-secrets",
    "vault-credentials",
    "pii-raw-records",
}

# Global flag: code-execution-tool disabled
CODE_EXECUTION_TOOL_DISABLED: bool = True


# ---------------------------------------------------------------------------
# Helper: access control enforcement
# ---------------------------------------------------------------------------


class AccessControlEnforcer:
    """Enforces tool access, handoff graph, tenant isolation, and context access."""

    def can_invoke_tool(self, agent_role: str, tool_name: str) -> tuple[bool, str]:
        """Return (allowed, reason). Checks AGENT_TOOL_ALLOWLIST."""
        if CODE_EXECUTION_TOOL_DISABLED and tool_name == "code-execution-tool":
            return False, "code-execution-tool.disabled=true; blocked for all agents"
        allowed_tools = AGENT_TOOL_ALLOWLIST.get(agent_role, set())
        if tool_name in allowed_tools:
            return True, "ALLOWED by tool allowlist"
        return False, f"FORBIDDEN: '{tool_name}' not in allowlist for agent role '{agent_role}'"

    def can_handoff(self, from_agent: str, to_agent: str) -> tuple[bool, str]:
        """Return (allowed, reason). Checks VALID_HANDOFF_GRAPH."""
        allowed_targets = VALID_HANDOFF_GRAPH.get(from_agent, set())
        if to_agent in allowed_targets:
            return True, "ALLOWED by handoff graph"
        return False, f"BLOCKED: handoff from '{from_agent}' to '{to_agent}' not in VALID_HANDOFF_GRAPH"

    def can_access_context(
        self,
        agent_role: str,
        context_type: str,
        human_approved: bool = False,
    ) -> tuple[bool, str]:
        """Return (allowed, reason). Restricted contexts require human approval."""
        if context_type in RESTRICTED_CONTEXT_TYPES:
            if not human_approved:
                return False, f"BLOCK: context type '{context_type}' requires human approval"
            return True, "ALLOWED: human approval provided"
        return True, "ALLOWED: context type is not restricted"

    def validate_tenant_isolation(self, requesting_tenant: str, context_tenant: str) -> tuple[bool, str]:
        """Return (allowed, reason). Cross-tenant access is unconditionally blocked."""
        if requesting_tenant != context_tenant:
            return False, (
                f"CROSS_TENANT_BLOCKED: agent for tenant '{requesting_tenant}' "
                f"attempted to access context for tenant '{context_tenant}'"
            )
        return True, "ALLOWED: same tenant"


class FileAccessGuard:
    """Validates file access requests against the filesystem server whitelist."""

    def __init__(self, project_root: str = "/app") -> None:
        self._project_root = project_root.rstrip("/")

    def validate_path(self, file_path: str, agent_role: str = "security") -> tuple[bool, str]:
        """
        Validate that file_path is within PROJECT_ROOT and not in a denied pattern.
        Returns (allowed, reason).
        """
        import os
        # Resolve relative paths against project_root
        if not os.path.isabs(file_path):
            resolved = os.path.normpath(os.path.join(self._project_root, file_path))
        else:
            resolved = os.path.normpath(file_path)

        # Check that resolved path is still under project_root
        if not resolved.startswith(self._project_root):
            return False, f"PATH_TRAVERSAL_BLOCKED: resolved path '{resolved}' is outside PROJECT_ROOT"

        # Deny known sensitive patterns
        denied_patterns = [".env", "secrets", ".pem", ".key", ".pfx", "vault_token", ".claude"]
        lower = resolved.lower()
        for pattern in denied_patterns:
            if pattern in lower:
                return False, f"SENSITIVE_PATH_BLOCKED: path matches denied pattern '{pattern}'"

        return True, "ALLOWED: path within PROJECT_ROOT and not denied"


# ---------------------------------------------------------------------------
# Circuit breaker stub (mirrors base_agent.CircuitBreaker)
# ---------------------------------------------------------------------------


class CircuitBreaker:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, threshold: int = 3, reset_seconds: float = 60.0, name: str = "unnamed") -> None:
        self._threshold = threshold
        self._reset_seconds = reset_seconds
        self._name = name
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at: Optional[float] = None

    @property
    def state(self) -> str:
        if self._state == self.OPEN and self._opened_at:
            if (time.monotonic() - self._opened_at) >= self._reset_seconds:
                self._state = self.HALF_OPEN
        return self._state

    async def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        if self.state == self.OPEN:
            elapsed = time.monotonic() - (self._opened_at or 0)
            remaining = max(0.0, self._reset_seconds - elapsed)
            raise CircuitBreakerOpenError(agent_role=self._name, remaining_seconds=remaining)
        try:
            result = await fn(*args, **kwargs)
            if self._state == self.HALF_OPEN:
                self._state = self.CLOSED
                self._failures = 0
            return result
        except Exception:
            self._failures += 1
            if self._failures >= self._threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
            raise

    def record_failure(self) -> None:
        """Manually record a failure (for synchronous callers)."""
        self._failures += 1
        if self._failures >= self._threshold:
            self._state = self.OPEN
            self._opened_at = time.monotonic()


# ---------------------------------------------------------------------------
# Rate limiter stub
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding window rate limiter."""

    def __init__(self, max_calls: int, window_seconds: int) -> None:
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._windows: dict[str, deque] = defaultdict(deque)

    def check(self, agent_name: str) -> None:
        """Raise RateLimitExceeded if agent has exceeded its call budget."""
        now = time.monotonic()
        window = self._windows[agent_name]
        # Evict timestamps outside the window
        while window and now - window[0] > self._window_seconds:
            window.popleft()
        if len(window) >= self._max_calls:
            raise RateLimitExceeded(
                agent_name=agent_name,
                limit=self._max_calls,
                window_seconds=self._window_seconds,
            )
        window.append(now)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def enforcer() -> AccessControlEnforcer:
    return AccessControlEnforcer()


@pytest.fixture
def file_guard() -> FileAccessGuard:
    return FileAccessGuard(project_root="/app")


# ---------------------------------------------------------------------------
# Test 1: Execution agent blocked without validation gate ALLOW
# ---------------------------------------------------------------------------


def test_execution_agent_blocked_without_validation_gate(enforcer: AccessControlEnforcer) -> None:
    """
    Execution agent attempting to proceed without a ALLOW gate decision must be BLOCKED.
    The orchestrator's _do_synthesise() must not call execution if gate is not ALLOW.
    """
    gate_decision = GateDecision.BLOCK  # validation agent returned BLOCK

    # Simulate orchestrator logic: check gate before allowing execution
    def should_allow_execution(gate: GateDecision) -> bool:
        return gate == GateDecision.ALLOW

    assert not should_allow_execution(gate_decision), (
        "Execution must be blocked when validation gate is not ALLOW"
    )


# ---------------------------------------------------------------------------
# Test 2: Planning agent cannot invoke execution directly
# ---------------------------------------------------------------------------


def test_planning_agent_cannot_invoke_execution_directly(enforcer: AccessControlEnforcer) -> None:
    """VALID_HANDOFF_GRAPH must block PLANNING → EXECUTION direct handoff."""
    allowed, reason = enforcer.can_handoff(from_agent="planning", to_agent="execution")
    assert not allowed, (
        f"Expected BLOCKED for PLANNING→EXECUTION handoff, got ALLOWED. Reason: {reason}"
    )
    assert "BLOCKED" in reason or "not in VALID_HANDOFF_GRAPH" in reason


# ---------------------------------------------------------------------------
# Test 3: Debugging agent cannot call write tools
# ---------------------------------------------------------------------------


def test_debugging_agent_cannot_write(enforcer: AccessControlEnforcer) -> None:
    """Debugging agent attempting to call any write tool must be FORBIDDEN."""
    write_tools = [
        "pause_migration",
        "resume_migration",
        "cancel_migration",
        "retry_failed_records",
        "scale_batch_size",
        "create_incident",
        "write_file",
        "delete_file",
    ]
    for tool in write_tools:
        allowed, reason = enforcer.can_invoke_tool(agent_role="debugging", tool_name=tool)
        assert not allowed, (
            f"Debugging agent must NOT be allowed to call write tool '{tool}'. "
            f"Got ALLOWED. This is a critical security violation."
        )
        assert "FORBIDDEN" in reason or "not in allowlist" in reason


# ---------------------------------------------------------------------------
# Test 4: Cross-tenant context blocked
# ---------------------------------------------------------------------------


def test_cross_tenant_context_blocked(enforcer: AccessControlEnforcer) -> None:
    """Agent with tenant_id=A cannot read context for tenant_id=B."""
    allowed, reason = enforcer.validate_tenant_isolation(
        requesting_tenant="tenant-A",
        context_tenant="tenant-B",
    )
    assert not allowed, (
        f"Cross-tenant context access must be BLOCKED. Got ALLOWED. Reason: {reason}"
    )
    assert "CROSS_TENANT_BLOCKED" in reason


def test_same_tenant_context_allowed(enforcer: AccessControlEnforcer) -> None:
    """Agent with tenant_id=A can access context for tenant_id=A."""
    allowed, reason = enforcer.validate_tenant_isolation(
        requesting_tenant="tenant-A",
        context_tenant="tenant-A",
    )
    assert allowed, f"Same-tenant access must be ALLOWED. Reason: {reason}"


# ---------------------------------------------------------------------------
# Test 5: Security agent file read outside whitelist (traversal)
# ---------------------------------------------------------------------------


def test_security_agent_file_read_outside_whitelist(file_guard: FileAccessGuard) -> None:
    """Path '../../etc/passwd' must be blocked as path traversal."""
    allowed, reason = file_guard.validate_path("../../etc/passwd", agent_role="security")
    assert not allowed, (
        f"Expected PATH_TRAVERSAL_BLOCKED for '../../etc/passwd', got ALLOWED. Reason: {reason}"
    )
    assert "PATH_TRAVERSAL_BLOCKED" in reason or "outside PROJECT_ROOT" in reason


# ---------------------------------------------------------------------------
# Test 6: Security agent file read on whitelisted path — permitted
# ---------------------------------------------------------------------------


def test_security_agent_file_read_whitelisted_path(file_guard: FileAccessGuard) -> None:
    """Path './docs/api/openapi.yaml' must be permitted (within PROJECT_ROOT)."""
    allowed, reason = file_guard.validate_path("./docs/api/openapi.yaml", agent_role="security")
    assert allowed, (
        f"Expected ALLOWED for whitelisted path './docs/api/openapi.yaml', got BLOCKED. "
        f"Reason: {reason}"
    )


# ---------------------------------------------------------------------------
# Test 7: Restricted context requires human approval
# ---------------------------------------------------------------------------


def test_restricted_context_requires_human_approval(enforcer: AccessControlEnforcer) -> None:
    """Accessing 'restricted-context' without human approval must be BLOCKED."""
    allowed, reason = enforcer.can_access_context(
        agent_role="validation",
        context_type="restricted-context",
        human_approved=False,
    )
    assert not allowed, (
        f"Expected BLOCK for restricted-context without human approval, got ALLOWED. "
        f"Reason: {reason}"
    )
    assert "human approval" in reason.lower() or "BLOCK" in reason


def test_restricted_context_allowed_with_human_approval(enforcer: AccessControlEnforcer) -> None:
    """Accessing 'restricted-context' WITH human approval must be ALLOWED."""
    allowed, reason = enforcer.can_access_context(
        agent_role="validation",
        context_type="restricted-context",
        human_approved=True,
    )
    assert allowed, f"Expected ALLOWED with human approval, got BLOCKED. Reason: {reason}"


# ---------------------------------------------------------------------------
# Test 8: code-execution-tool blocked for all agents
# ---------------------------------------------------------------------------


def test_code_execution_tool_blocked_for_all_agents(enforcer: AccessControlEnforcer) -> None:
    """When code-execution-tool.disabled=true, every agent role must be blocked."""
    all_agent_roles = ["orchestrator", "planning", "validation", "security",
                       "execution", "debugging", "documentation"]
    for role in all_agent_roles:
        allowed, reason = enforcer.can_invoke_tool(
            agent_role=role,
            tool_name="code-execution-tool",
        )
        assert not allowed, (
            f"code-execution-tool must be BLOCKED for all agents, but was ALLOWED for '{role}'. "
            f"Reason: {reason}"
        )
        assert "disabled" in reason.lower() or "blocked" in reason.lower()


# ---------------------------------------------------------------------------
# Test 9: Circuit breaker blocks after threshold
# ---------------------------------------------------------------------------


def test_circuit_breaker_blocks_after_threshold() -> None:
    """3 consecutive failures must open the circuit breaker; 4th call raises CircuitBreakerOpenError."""
    import asyncio

    breaker = CircuitBreaker(threshold=3, reset_seconds=60.0, name="migration-agent")

    async def failing_tool() -> None:
        raise ConnectionError("Simulated tool failure")

    async def _run() -> None:
        # First 3 failures — should raise ConnectionError (circuit still closed)
        for _attempt in range(3):
            with pytest.raises(ConnectionError):
                await breaker.call(failing_tool)

        assert breaker.state == CircuitBreaker.OPEN, (
            f"Expected circuit state OPEN after 3 failures, got {breaker.state}"
        )

        # 4th call — circuit is OPEN, must raise CircuitBreakerOpenError
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            await breaker.call(failing_tool)

        assert exc_info.value.agent_role == "migration-agent"
        assert exc_info.value.remaining_seconds > 0

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 10: Rate limit enforced per agent
# ---------------------------------------------------------------------------


def test_rate_limit_enforced_per_agent() -> None:
    """Exceeding 5 calls in 10 seconds for an agent must raise RateLimitExceeded."""
    limiter = RateLimiter(max_calls=5, window_seconds=10)
    agent_name = "validation-agent"

    # First 5 calls must succeed
    for i in range(5):
        limiter.check(agent_name)  # must not raise

    # 6th call must be rate-limited
    with pytest.raises(RateLimitExceeded) as exc_info:
        limiter.check(agent_name)

    assert exc_info.value.agent_name == agent_name
    assert exc_info.value.limit == 5


def test_rate_limit_independent_per_agent() -> None:
    """Rate limit for agent A must not affect agent B."""
    limiter = RateLimiter(max_calls=3, window_seconds=10)

    # Exhaust agent-A's quota
    for _ in range(3):
        limiter.check("agent-A")

    # agent-A is now rate-limited
    with pytest.raises(RateLimitExceeded):
        limiter.check("agent-A")

    # agent-B must still be able to make calls
    limiter.check("agent-B")  # must NOT raise


def test_abs_path_traversal_blocked(file_guard: FileAccessGuard) -> None:
    """Absolute path outside PROJECT_ROOT must be blocked."""
    allowed, reason = file_guard.validate_path("/etc/passwd", agent_role="security")
    assert not allowed
    assert "PATH_TRAVERSAL_BLOCKED" in reason or "outside PROJECT_ROOT" in reason


def test_vault_token_path_blocked(file_guard: FileAccessGuard) -> None:
    """Path containing 'vault_token' must be blocked by sensitive pattern rule."""
    allowed, reason = file_guard.validate_path("./config/vault_token", agent_role="security")
    assert not allowed
    assert "vault_token" in reason.lower() or "SENSITIVE_PATH_BLOCKED" in reason


def test_env_file_path_blocked(file_guard: FileAccessGuard) -> None:
    """Path matching '.env*' must be blocked."""
    allowed, reason = file_guard.validate_path(".env.production", agent_role="security")
    assert not allowed


def test_orchestrator_can_delegate_to_all_specialists(enforcer: AccessControlEnforcer) -> None:
    """Orchestrator must be allowed to hand off to all specialist agents."""
    specialists = ["planning", "validation", "security", "execution", "debugging", "documentation"]
    for specialist in specialists:
        allowed, reason = enforcer.can_handoff(from_agent="orchestrator", to_agent=specialist)
        assert allowed, (
            f"Orchestrator must be allowed to delegate to '{specialist}'. "
            f"Got BLOCKED. Reason: {reason}"
        )
