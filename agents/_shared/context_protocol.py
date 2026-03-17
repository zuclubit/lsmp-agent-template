"""Explicit context protocol for inter-agent communication and context serialization."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Final

from agents._shared.schemas import AgentRole, RequestContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid handoff graph
# ---------------------------------------------------------------------------

# Maps each AgentRole to the set of roles it is permitted to hand off to.
# The graph is acyclic at the agent-to-agent level; ORCHESTRATOR acts as the
# hub that receives results and decides subsequent routing.
VALID_HANDOFF_GRAPH: Final[dict[AgentRole, frozenset[AgentRole]]] = {
    AgentRole.ORCHESTRATOR: frozenset(
        {
            AgentRole.PLANNING,
            AgentRole.VALIDATION,
            AgentRole.SECURITY,
            AgentRole.EXECUTION,
            AgentRole.DEBUGGING,
        }
    ),
    # PLANNING must pass through a gate (VALIDATION or SECURITY) before
    # EXECUTION can be approved; it never dispatches directly to EXECUTION.
    AgentRole.PLANNING: frozenset(
        {
            AgentRole.VALIDATION,
            AgentRole.SECURITY,
        }
    ),
    # Gate agents return their verdict to the ORCHESTRATOR, which decides
    # whether to proceed or abort.
    AgentRole.VALIDATION: frozenset({AgentRole.ORCHESTRATOR}),
    AgentRole.SECURITY: frozenset({AgentRole.ORCHESTRATOR}),
    # EXECUTION reports back to the ORCHESTRATOR or escalates to DEBUGGING.
    AgentRole.EXECUTION: frozenset(
        {
            AgentRole.ORCHESTRATOR,
            AgentRole.DEBUGGING,
        }
    ),
    # DEBUGGING always returns to the ORCHESTRATOR with a remediation plan.
    AgentRole.DEBUGGING: frozenset({AgentRole.ORCHESTRATOR}),
}


# ---------------------------------------------------------------------------
# PII redaction patterns
# ---------------------------------------------------------------------------

# RFC 5322 simplified email pattern.
_EMAIL_RE: re.Pattern[str] = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Salesforce 15- and 18-character IDs (alphanumeric, case-sensitive prefix).
# Salesforce IDs start with a 3-character key prefix followed by 12 or 15
# more alphanumeric characters.
_SF_ID_RE: re.Pattern[str] = re.compile(
    r"\b[A-Za-z0-9]{3}(?:[A-Za-z0-9]{12}|[A-Za-z0-9]{15})\b"
)

_REDACTED: Final[str] = "[REDACTED]"


# ---------------------------------------------------------------------------
# Context validator
# ---------------------------------------------------------------------------


class InvalidHandoffError(ValueError):
    """Raised when a requested agent-to-agent handoff is not permitted."""

    def __init__(
        self,
        from_role: AgentRole,
        to_role: AgentRole,
        reason: str = "",
    ) -> None:
        base = (
            f"Invalid handoff: {from_role.value!r} -> {to_role.value!r} "
            f"is not in the allowed handoff graph."
        )
        super().__init__(f"{base} {reason}".strip())
        self.from_role = from_role
        self.to_role = to_role


class ContextValidator:
    """Validates agent-to-agent handoffs against the explicit handoff graph.

    This class enforces the architectural constraint that agents may only
    communicate through approved pathways, preventing ad-hoc coupling that
    bypasses the ORCHESTRATOR hub.

    Usage::

        validator = ContextValidator()
        validator.validate_handoff(AgentRole.PLANNING, AgentRole.VALIDATION, ctx)
    """

    def __init__(
        self,
        handoff_graph: dict[AgentRole, frozenset[AgentRole]] | None = None,
    ) -> None:
        """
        Args:
            handoff_graph: Override the default VALID_HANDOFF_GRAPH.
                           Useful for testing or extended pipeline topologies.
        """
        self._graph = handoff_graph or VALID_HANDOFF_GRAPH

    def validate_handoff(
        self,
        from_agent: AgentRole,
        to_agent: AgentRole,
        context: RequestContext,
    ) -> None:
        """Assert that the requested handoff is permitted by the handoff graph.

        Args:
            from_agent: The role initiating the handoff.
            to_agent:   The role that will receive control.
            context:    The current request context (used for trace logging).

        Raises:
            InvalidHandoffError: If the transition is not in the handoff graph.
            ValueError:          If ``from_agent`` is not registered in the graph.
        """
        allowed = self._graph.get(from_agent)
        if allowed is None:
            raise ValueError(
                f"AgentRole {from_agent.value!r} is not registered in the handoff "
                "graph. Ensure all roles are added to VALID_HANDOFF_GRAPH."
            )

        if to_agent not in allowed:
            logger.warning(
                "Blocked handoff: %s -> %s (trace_id=%s, allowed=%s)",
                from_agent.value,
                to_agent.value,
                context.trace_id,
                ", ".join(r.value for r in sorted(allowed, key=lambda r: r.value)),
            )
            raise InvalidHandoffError(
                from_agent,
                to_agent,
                reason=(
                    f"Permitted destinations from {from_agent.value!r}: "
                    + ", ".join(
                        repr(r.value)
                        for r in sorted(allowed, key=lambda r: r.value)
                    )
                    + "."
                ),
            )

        logger.debug(
            "Handoff approved: %s -> %s (trace_id=%s)",
            from_agent.value,
            to_agent.value,
            context.trace_id,
        )

    def allowed_targets(self, from_agent: AgentRole) -> frozenset[AgentRole]:
        """Return the set of roles that ``from_agent`` may hand off to.

        Args:
            from_agent: The role whose allowed targets are requested.

        Returns:
            A frozenset of permitted next roles (empty for unregistered roles).
        """
        return self._graph.get(from_agent, frozenset())


# ---------------------------------------------------------------------------
# Context serializer
# ---------------------------------------------------------------------------


class ContextSerializer:
    """Serialises and deserialises RequestContext for inter-process transport.

    Only the fields required for distributed tracing are included in the
    serialised form.  Sensitive fields (e.g. ``initiated_by``,
    ``max_budget_usd``) are intentionally omitted to minimise the attack
    surface when context is transmitted over the wire.

    PII redaction is provided as a utility for sanitising log lines and
    diagnostic output before they leave the system boundary.
    """

    # Fields included in the serialised envelope.
    _TRANSPORT_FIELDS: Final[tuple[str, ...]] = (
        "request_id",
        "tenant_id",
        "job_id",
        "trace_id",
    )

    def serialize(self, ctx: RequestContext) -> str:
        """Serialise a RequestContext to a compact JSON string.

        Only ``request_id``, ``tenant_id``, ``job_id``, and ``trace_id`` are
        included.  A ``serialized_at`` UTC timestamp is appended for ordering.

        Args:
            ctx: The context to serialise.

        Returns:
            A JSON string suitable for HTTP headers, message queues, or logs.
        """
        payload: dict[str, str] = {
            field: getattr(ctx, field) for field in self._TRANSPORT_FIELDS
        }
        payload["serialized_at"] = datetime.now(timezone.utc).isoformat()
        return json.dumps(payload, separators=(",", ":"))

    def deserialize(self, data: str) -> RequestContext:
        """Deserialise a JSON string produced by ``serialize`` into a RequestContext.

        The reconstructed context will have default values for fields that were
        not included in the serialised form (e.g. ``initiated_by`` is set to the
        placeholder ``"deserialized"``).

        Args:
            data: A JSON string as produced by ``serialize``.

        Returns:
            A RequestContext populated with the deserialized fields.

        Raises:
            ValueError: If ``data`` is not valid JSON or is missing required fields.
        """
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"ContextSerializer.deserialize received invalid JSON: {exc}"
            ) from exc

        missing = [f for f in self._TRANSPORT_FIELDS if f not in payload]
        if missing:
            raise ValueError(
                f"Serialised context is missing required fields: {missing}"
            )

        return RequestContext(
            request_id=payload["request_id"],
            tenant_id=payload["tenant_id"],
            job_id=payload["job_id"],
            trace_id=payload["trace_id"],
            # Safe defaults for fields omitted from the transport envelope.
            initiated_by=payload.get("initiated_by", "deserialized"),
        )

    @staticmethod
    def redact_pii(text: str) -> str:
        """Replace email addresses and Salesforce IDs in ``text`` with [REDACTED].

        This utility is intended for sanitising log lines, error messages, and
        diagnostic output before they are written to external sinks (log
        aggregators, alerting systems, support tickets).

        Patterns redacted:
        - Email addresses matching a simplified RFC 5322 pattern.
        - Salesforce 15- and 18-character alphanumeric IDs.

        Args:
            text: Raw text that may contain PII.

        Returns:
            A copy of ``text`` with all matched patterns replaced by
            the literal string ``[REDACTED]``.
        """
        # Redact emails first (more specific), then Salesforce IDs (more general).
        text = _EMAIL_RE.sub(_REDACTED, text)
        text = _SF_ID_RE.sub(_REDACTED, text)
        return text
