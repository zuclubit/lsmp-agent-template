"""
RBAC Configuration and Enforcement for Legacy to Salesforce Migration
=====================================================================
Implements Role-Based Access Control with:
  - Decorator-based permission enforcement
  - Role assignment and revocation
  - Permission checking with wildcard support
  - Audit logging of all authorization decisions
  - JWT token validation
  - Contextual access (justification, time-window)

Author: Security Team
Version: 1.2.0
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Optional, TypeVar, ParamSpec

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
P = ParamSpec("P")
R = TypeVar("R")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WILDCARD = "*"
PERMISSION_SEPARATOR = ":"

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Role(str, Enum):
    MIGRATION_ADMIN = "migration-admin"
    MIGRATION_OPERATOR = "migration-operator"
    MIGRATION_VIEWER = "migration-viewer"
    API_SERVICE = "api-service"
    AUDIT_READER = "audit-reader"
    SECURITY_ANALYST = "security-analyst"


class Permission(str, Enum):
    # Migration jobs
    JOBS_READ = "migration:jobs:read"
    JOBS_CREATE = "migration:jobs:create"
    JOBS_UPDATE = "migration:jobs:update"
    JOBS_DELETE = "migration:jobs:delete"
    JOBS_EXECUTE = "migration:jobs:execute"
    JOBS_APPROVE = "migration:jobs:approve"

    # Data
    DATA_READ_CONFIDENTIAL = "data:read:confidential"
    DATA_READ_RESTRICTED = "data:read:restricted"
    DATA_EXPORT = "data:export"
    DATA_TRANSFORM_CONFIGURE = "data:transform:configure"

    # Configuration
    CONFIG_READ = "config:read"
    CONFIG_WRITE = "config:write"
    CONFIG_SECRETS_READ = "config:secrets:read"

    # Monitoring
    MONITORING_METRICS_READ = "monitoring:metrics:read"
    MONITORING_LOGS_READ = "monitoring:logs:read"
    MONITORING_ALERTS_MANAGE = "monitoring:alerts:manage"

    # Audit
    AUDIT_LOGS_READ = "audit:logs:read"
    AUDIT_REPORTS_GENERATE = "audit:reports:generate"

    # Admin
    ADMIN_USERS_MANAGE = "admin:users:manage"
    ADMIN_SYSTEM_MANAGE = "admin:system:manage"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RoleDefinition:
    """Defines the permissions and constraints for a role."""
    name: Role
    description: str
    permissions: list[str]  # Supports wildcards e.g. "migration:jobs:*"
    require_mfa: bool = False
    session_timeout_minutes: int = 480
    require_justification_for: list[str] = field(default_factory=list)
    is_service_account: bool = False
    max_access_duration_days: Optional[int] = None


@dataclass
class UserContext:
    """Represents the authenticated user/service making a request."""
    user_id: str
    username: str
    roles: list[Role]
    is_service_account: bool = False
    mfa_verified: bool = False
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    authenticated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    justifications: dict[str, str] = field(default_factory=dict)


@dataclass
class AuthorizationDecision:
    """Result of an authorization check."""
    allowed: bool
    user_id: str
    permission: str
    resource: Optional[str]
    reason: str
    matched_role: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Role Permission Definitions
# ---------------------------------------------------------------------------

ROLE_DEFINITIONS: dict[Role, RoleDefinition] = {
    Role.MIGRATION_ADMIN: RoleDefinition(
        name=Role.MIGRATION_ADMIN,
        description="Full system administrator for migration platform",
        permissions=[
            "migration:jobs:*",
            "data:read:confidential",
            "data:read:restricted",
            "data:export",
            "data:transform:configure",
            "config:*",
            "monitoring:*",
            "audit:*",
            "admin:*",
        ],
        require_mfa=True,
        session_timeout_minutes=60,
        require_justification_for=["data:read:restricted", "data:export"],
    ),
    Role.MIGRATION_OPERATOR: RoleDefinition(
        name=Role.MIGRATION_OPERATOR,
        description="Day-to-day migration operations",
        permissions=[
            "migration:jobs:read",
            "migration:jobs:create",
            "migration:jobs:update",
            "migration:jobs:execute",
            "data:read:confidential",
            "data:transform:configure",
            "config:read",
            "monitoring:metrics:read",
            "monitoring:logs:read",
        ],
        require_mfa=True,
        session_timeout_minutes=480,
    ),
    Role.MIGRATION_VIEWER: RoleDefinition(
        name=Role.MIGRATION_VIEWER,
        description="Read-only monitoring and status viewing",
        permissions=[
            "migration:jobs:read",
            "config:read",
            "monitoring:metrics:read",
            "monitoring:logs:read",
        ],
        require_mfa=False,
        session_timeout_minutes=480,
    ),
    Role.API_SERVICE: RoleDefinition(
        name=Role.API_SERVICE,
        description="Service-to-service API role",
        permissions=[
            "migration:jobs:read",
            "migration:jobs:create",
            "migration:jobs:update",
            "data:read:confidential",
            "config:read",
        ],
        require_mfa=False,
        session_timeout_minutes=-1,
        is_service_account=True,
    ),
    Role.AUDIT_READER: RoleDefinition(
        name=Role.AUDIT_READER,
        description="External auditor read-only access",
        permissions=[
            "migration:jobs:read",
            "audit:logs:read",
            "audit:reports:generate",
            "monitoring:metrics:read",
        ],
        require_mfa=True,
        session_timeout_minutes=240,
        max_access_duration_days=30,
    ),
}


# ---------------------------------------------------------------------------
# Permission Matching Engine
# ---------------------------------------------------------------------------

class PermissionMatcher:
    """Matches requested permissions against granted permissions with wildcard support."""

    @staticmethod
    def matches(granted: str, requested: str) -> bool:
        """
        Check if a granted permission covers a requested permission.

        Supports wildcards at the end of permission strings:
          - "migration:jobs:*" matches "migration:jobs:read"
          - "migration:*" matches "migration:jobs:read"
          - "*" matches anything

        Args:
            granted: The permission the user has been granted.
            requested: The permission being requested.

        Returns:
            True if granted covers requested.
        """
        if granted == WILDCARD:
            return True

        granted_parts = granted.split(PERMISSION_SEPARATOR)
        requested_parts = requested.split(PERMISSION_SEPARATOR)

        for i, part in enumerate(granted_parts):
            if part == WILDCARD:
                return True
            if i >= len(requested_parts):
                return False
            if part != requested_parts[i]:
                return False

        return len(granted_parts) == len(requested_parts)

    @classmethod
    def has_permission(cls, granted_permissions: list[str], requested: str) -> bool:
        """Check if any granted permission covers the requested one."""
        return any(cls.matches(g, requested) for g in granted_permissions)


# ---------------------------------------------------------------------------
# RBAC Engine
# ---------------------------------------------------------------------------

class RBACEngine:
    """Core RBAC enforcement engine."""

    def __init__(
        self,
        role_definitions: dict[Role, RoleDefinition] | None = None,
        audit_logger: "AuditLogger | None" = None,
    ) -> None:
        self._roles = role_definitions or ROLE_DEFINITIONS
        self._audit_logger = audit_logger
        self._permission_cache: dict[str, bool] = {}
        self._cache_ttl_seconds = 300

    def _get_all_permissions(self, roles: list[Role]) -> list[str]:
        """Aggregate all permissions from all assigned roles."""
        permissions: list[str] = []
        for role in roles:
            role_def = self._roles.get(role)
            if role_def:
                permissions.extend(role_def.permissions)
        return list(set(permissions))

    def _check_mfa_requirement(self, user: UserContext, roles: list[Role]) -> bool:
        """Return True if MFA requirement is satisfied."""
        requires_mfa = any(
            self._roles[r].require_mfa
            for r in roles
            if r in self._roles
        )
        if requires_mfa and not user.mfa_verified:
            return False
        return True

    def _check_justification_requirement(
        self,
        user: UserContext,
        roles: list[Role],
        permission: str,
    ) -> bool:
        """Return True if justification requirement is satisfied."""
        for role in roles:
            role_def = self._roles.get(role)
            if not role_def:
                continue
            for required_perm in role_def.require_justification_for:
                if PermissionMatcher.matches(required_perm, permission):
                    if permission not in user.justifications:
                        return False
        return True

    def _check_session_validity(self, user: UserContext, roles: list[Role]) -> bool:
        """Return True if the session is still valid."""
        for role in roles:
            role_def = self._roles.get(role)
            if not role_def:
                continue
            if role_def.session_timeout_minutes == -1:
                continue  # Service accounts don't expire
            timeout = timedelta(minutes=role_def.session_timeout_minutes)
            if datetime.now(timezone.utc) - user.authenticated_at > timeout:
                return False
        return True

    def authorize(
        self,
        user: UserContext,
        permission: str,
        resource: str | None = None,
    ) -> AuthorizationDecision:
        """
        Core authorization check.

        Args:
            user: The authenticated user context.
            permission: The permission being requested.
            resource: Optional specific resource being accessed.

        Returns:
            AuthorizationDecision with allow/deny and reasoning.
        """
        correlation_id = str(uuid.uuid4())

        # Session validity check
        if not self._check_session_validity(user, user.roles):
            decision = AuthorizationDecision(
                allowed=False,
                user_id=user.user_id,
                permission=permission,
                resource=resource,
                reason="Session expired",
                correlation_id=correlation_id,
            )
            self._emit_audit(user, decision)
            return decision

        # MFA check
        if not self._check_mfa_requirement(user, user.roles):
            decision = AuthorizationDecision(
                allowed=False,
                user_id=user.user_id,
                permission=permission,
                resource=resource,
                reason="MFA required but not verified",
                correlation_id=correlation_id,
            )
            self._emit_audit(user, decision)
            return decision

        # Get all effective permissions
        all_permissions = self._get_all_permissions(user.roles)

        # Check if user has the permission
        if not PermissionMatcher.has_permission(all_permissions, permission):
            decision = AuthorizationDecision(
                allowed=False,
                user_id=user.user_id,
                permission=permission,
                resource=resource,
                reason=f"Permission '{permission}' not granted to any assigned role",
                correlation_id=correlation_id,
            )
            self._emit_audit(user, decision)
            return decision

        # Justification check
        if not self._check_justification_requirement(user, user.roles, permission):
            decision = AuthorizationDecision(
                allowed=False,
                user_id=user.user_id,
                permission=permission,
                resource=resource,
                reason=f"Justification required for '{permission}' but not provided",
                correlation_id=correlation_id,
            )
            self._emit_audit(user, decision)
            return decision

        # Find the matching role for audit purposes
        matched_role = None
        for role in user.roles:
            role_def = self._roles.get(role)
            if role_def and PermissionMatcher.has_permission(role_def.permissions, permission):
                matched_role = role.value
                break

        decision = AuthorizationDecision(
            allowed=True,
            user_id=user.user_id,
            permission=permission,
            resource=resource,
            reason="Permission granted",
            matched_role=matched_role,
            correlation_id=correlation_id,
        )
        self._emit_audit(user, decision)
        return decision

    def _emit_audit(self, user: UserContext, decision: AuthorizationDecision) -> None:
        """Emit authorization decision to audit log."""
        audit_event = {
            "event_type": "authorization_decision",
            "correlation_id": decision.correlation_id,
            "timestamp": decision.timestamp.isoformat(),
            "user_id": user.user_id,
            "username": user.username,
            "session_id": user.session_id,
            "ip_address": user.ip_address,
            "roles": [r.value for r in user.roles],
            "permission_requested": decision.permission,
            "resource": decision.resource,
            "decision": "ALLOW" if decision.allowed else "DENY",
            "reason": decision.reason,
            "matched_role": decision.matched_role,
            "mfa_verified": user.mfa_verified,
        }

        if self._audit_logger:
            self._audit_logger.log(audit_event)
        else:
            # Fall back to structured logging
            level = logging.INFO if decision.allowed else logging.WARNING
            logger.log(level, "Authorization decision", extra={"audit": audit_event})


# ---------------------------------------------------------------------------
# JWT Token Management
# ---------------------------------------------------------------------------

class JWTManager:
    """Manages JWT token creation and validation."""

    ALGORITHM = "RS256"
    TOKEN_EXPIRY_MINUTES = 60

    def __init__(self, public_key: str, private_key: str | None = None) -> None:
        self._public_key = public_key
        self._private_key = private_key
        self._issuer = "migration-platform"
        self._audience = "migration-api"

    def decode_token(self, token: str) -> dict[str, Any]:
        """
        Decode and validate a JWT token.

        Raises:
            jwt.ExpiredSignatureError: Token has expired.
            jwt.InvalidTokenError: Token is invalid.
        """
        return jwt.decode(
            token,
            self._public_key,
            algorithms=[self.ALGORITHM],
            audience=self._audience,
            issuer=self._issuer,
            options={
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )

    def create_token(
        self,
        user_id: str,
        username: str,
        roles: list[str],
        mfa_verified: bool = False,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """Create a signed JWT token."""
        if not self._private_key:
            raise ValueError("Private key required to create tokens")

        now = datetime.now(timezone.utc)
        payload = {
            "sub": user_id,
            "username": username,
            "roles": roles,
            "mfa": mfa_verified,
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "exp": now + timedelta(minutes=self.TOKEN_EXPIRY_MINUTES),
            "jti": str(uuid.uuid4()),
        }
        if extra_claims:
            payload.update(extra_claims)

        return jwt.encode(payload, self._private_key, algorithm=self.ALGORITHM)

    def token_to_user_context(self, token_data: dict[str, Any], request: Request | None = None) -> UserContext:
        """Convert decoded JWT claims to a UserContext."""
        roles = [Role(r) for r in token_data.get("roles", []) if r in Role._value2member_map_]
        return UserContext(
            user_id=token_data["sub"],
            username=token_data.get("username", token_data["sub"]),
            roles=roles,
            mfa_verified=token_data.get("mfa", False),
            authenticated_at=datetime.fromtimestamp(token_data["iat"], tz=timezone.utc),
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )


# ---------------------------------------------------------------------------
# Role Assignment Manager
# ---------------------------------------------------------------------------

class RoleAssignmentManager:
    """Manages dynamic role assignments (in production backed by database)."""

    def __init__(self) -> None:
        # In production, this would be backed by a database
        self._assignments: dict[str, list[Role]] = {}
        self._assignment_history: list[dict[str, Any]] = []

    def assign_role(
        self,
        user_id: str,
        role: Role,
        assigned_by: str,
        justification: str,
        expires_at: datetime | None = None,
    ) -> None:
        """Assign a role to a user."""
        if user_id not in self._assignments:
            self._assignments[user_id] = []

        if role not in self._assignments[user_id]:
            self._assignments[user_id].append(role)

        self._assignment_history.append({
            "action": "assign",
            "user_id": user_id,
            "role": role.value,
            "assigned_by": assigned_by,
            "justification": justification,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(
            "Role assigned",
            extra={
                "user_id": user_id,
                "role": role.value,
                "assigned_by": assigned_by,
            },
        )

    def revoke_role(self, user_id: str, role: Role, revoked_by: str, reason: str) -> None:
        """Revoke a role from a user."""
        if user_id in self._assignments and role in self._assignments[user_id]:
            self._assignments[user_id].remove(role)

        self._assignment_history.append({
            "action": "revoke",
            "user_id": user_id,
            "role": role.value,
            "revoked_by": revoked_by,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(
            "Role revoked",
            extra={
                "user_id": user_id,
                "role": role.value,
                "revoked_by": revoked_by,
            },
        )

    def get_user_roles(self, user_id: str) -> list[Role]:
        """Get all roles assigned to a user."""
        return self._assignments.get(user_id, [])

    def get_assignment_history(self, user_id: str) -> list[dict[str, Any]]:
        """Get role assignment history for a user (for audit)."""
        return [e for e in self._assignment_history if e["user_id"] == user_id]


# ---------------------------------------------------------------------------
# FastAPI Dependency Injection
# ---------------------------------------------------------------------------

security_scheme = HTTPBearer(auto_error=True)

# Global instances (initialized at startup)
_rbac_engine: RBACEngine | None = None
_jwt_manager: JWTManager | None = None


def init_rbac(jwt_public_key: str, jwt_private_key: str | None = None) -> None:
    """Initialize the global RBAC engine. Call at application startup."""
    global _rbac_engine, _jwt_manager
    _rbac_engine = RBACEngine()
    _jwt_manager = JWTManager(
        public_key=jwt_public_key,
        private_key=jwt_private_key,
    )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> UserContext:
    """
    FastAPI dependency: extract and validate user from Bearer token.

    Raises:
        HTTPException 401: Invalid or expired token.
    """
    if _jwt_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service not initialized",
        )

    try:
        token_data = _jwt_manager.decode_token(credentials.credentials)
        user = _jwt_manager.token_to_user_context(token_data, request)
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        logger.warning("Invalid JWT token", extra={"error": str(e), "ip": request.client.host if request.client else None})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Decorator-based Permission Enforcement
# ---------------------------------------------------------------------------

def require_permission(
    permission: str | Permission,
    resource_param: str | None = None,
    require_justification: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Decorator that enforces permission checks on FastAPI route handlers.

    Args:
        permission: The permission required to access this endpoint.
        resource_param: Name of the path/query parameter that identifies the resource.
        require_justification: If True, caller must provide X-Justification header.

    Usage:
        @router.get("/jobs/{job_id}")
        @require_permission(Permission.JOBS_READ, resource_param="job_id")
        async def get_job(job_id: str, user: UserContext = Depends(get_current_user)):
            ...
    """
    perm_str = permission.value if isinstance(permission, Permission) else permission

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if _rbac_engine is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authorization service not initialized",
                )

            # Extract user from kwargs (FastAPI injects it)
            user: UserContext | None = kwargs.get("user") or kwargs.get("current_user")
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User context not found",
                )

            # Extract resource if specified
            resource = str(kwargs.get(resource_param)) if resource_param else None

            # Check justification header if required
            if require_justification:
                request: Request | None = kwargs.get("request")
                if request:
                    justification = request.headers.get("X-Justification")
                    if not justification:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"X-Justification header required for permission '{perm_str}'",
                        )
                    user.justifications[perm_str] = justification

            decision = _rbac_engine.authorize(user, perm_str, resource)

            if not decision.allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied: {decision.reason}",
                    headers={"X-Correlation-ID": decision.correlation_id},
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_role(*roles: Role) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Decorator that requires the user to have at least one of the specified roles.

    Usage:
        @require_role(Role.MIGRATION_ADMIN, Role.MIGRATION_OPERATOR)
        async def admin_or_operator_endpoint(user: UserContext = Depends(get_current_user)):
            ...
    """
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            user: UserContext | None = kwargs.get("user") or kwargs.get("current_user")
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User context not found",
                )

            has_role = any(r in user.roles for r in roles)
            if not has_role:
                logger.warning(
                    "Role check failed",
                    extra={
                        "user_id": user.user_id,
                        "required_roles": [r.value for r in roles],
                        "user_roles": [r.value for r in user.roles],
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Required role(s): {[r.value for r in roles]}",
                )
            return await func(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# AuditLogger stub (real implementation in audit/audit_logger.py)
# ---------------------------------------------------------------------------

class AuditLogger:
    """Minimal audit logger interface. Replace with full implementation."""

    def log(self, event: dict[str, Any]) -> None:
        logger.info("AUDIT", extra={"audit_event": event})


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def get_user_effective_permissions(user: UserContext) -> list[str]:
    """Return all effective permissions for a user across all their roles."""
    permissions: set[str] = set()
    for role in user.roles:
        role_def = ROLE_DEFINITIONS.get(role)
        if role_def:
            permissions.update(role_def.permissions)
    return sorted(permissions)


def compute_permission_hash(permissions: list[str]) -> str:
    """Compute a deterministic hash of a permission set for caching."""
    normalized = sorted(permissions)
    return hashlib.sha256(json.dumps(normalized).encode()).hexdigest()[:16]
