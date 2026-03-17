"""
Structured Audit Log — tamper-evident, HMAC-chained JSON log for the agent system.

Every agent action, tool call, MCP access, gate decision, and security event
is written here. Satisfies: FedRAMP AU-2, SOX ITGC, GDPR Art. 5(2) accountability.

Design principles:
  - HMAC chain: each entry contains the HMAC-SHA256 of the previous entry, making
    log tampering detectable by any independent auditor.
  - Hash-not-raw: input fields are stored as SHA-256 hashes, never as raw values.
    Output fields are stored as sanitized summaries only.
  - Non-blocking: the local JSONL write always completes before any Splunk shipping.
  - Fail-safe: if Splunk is unavailable, local log still written (never silent loss).

Tamper detection: run AuditLogger.verify_chain() to replay the HMAC chain and
identify any entry that has been modified, inserted, or deleted.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_lib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AuditEventType(str, Enum):
    """Event categories for the agent audit log."""
    AGENT_INVOCATION = "AGENT_INVOCATION"
    TOOL_CALL = "TOOL_CALL"
    MCP_ACCESS = "MCP_ACCESS"
    GATE_DECISION = "GATE_DECISION"
    CONTEXT_LOAD = "CONTEXT_LOAD"
    SECURITY_BLOCK = "SECURITY_BLOCK"
    REDACTION_APPLIED = "REDACTION_APPLIED"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"


# ---------------------------------------------------------------------------
# AuditEntry dataclass
# ---------------------------------------------------------------------------


@dataclass
class AuditEntry:
    """
    A single tamper-evident audit record.

    Fields:
      - input_hash:      SHA-256 of the raw input string — never the raw input itself.
      - output_summary:  A sanitized (redacted) summary — never raw output.
      - hmac_chain:      HMAC-SHA256 of (previous_entry_hash + this_entry_content).
                         Enables sequential chain verification.
      - entry_id:        SHA-256 of this entry's serialized content (before chain fields).
    """
    event_type: AuditEventType
    timestamp_utc: str
    trace_id: str
    agent_name: str
    tenant_id: str
    job_id: str
    action: str
    input_hash: str          # SHA-256 of input — never raw input
    output_summary: str      # sanitized summary — never raw output
    result: str              # PASS / BLOCK / ALLOW / DENY / ERROR / etc.
    duration_ms: int
    rule_id: Optional[str]   # redaction rule or gate that triggered
    tool_calls: list[dict]   # list of {tool_name, success, duration_ms}
    gate_decisions: list[dict]
    tokens_used: int
    hmac_chain: str = ""     # HMAC of previous entry + current content (tamper detection)
    entry_id: str = ""       # SHA-256 of this entry's serializable content

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, sort_keys=True)

    def content_for_hashing(self) -> str:
        """
        Serialization of the entry excluding chain fields (hmac_chain, entry_id).
        Used to compute the entry_id and to verify HMAC integrity.
        """
        d = self.to_dict()
        d.pop("hmac_chain", None)
        d.pop("entry_id", None)
        return json.dumps(d, default=str, sort_keys=True)


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


class AuditLogger:
    """
    HMAC-chained audit logger for the agent system.

    Each entry contains the HMAC of the previous entry, making log tampering
    detectable. The chain is initialized from the last entry in the JSONL log
    file on startup, so chain continuity is maintained across restarts.

    Write to:  .audit/agent_audit.jsonl  (configurable via log_path)
    Also ships to: Splunk HEC if SPLUNK_HEC_URL is configured.

    Thread safety: all writes are protected by a re-entrant lock.

    Compliance: FedRAMP AU-2 (Auditable Events), AU-9 (Protection of Audit Info).
    """

    def __init__(
        self,
        log_path: Path = Path(".audit/agent_audit.jsonl"),
        hmac_key: Optional[bytes] = None,
    ) -> None:
        self._log_path = log_path
        self._hmac_key = self._resolve_hmac_key(hmac_key)
        self._last_entry_hash: str = ""
        self._lock = threading.RLock()

        # Ensure the log directory exists
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Load the last entry hash from existing log for chain continuity
        self._load_last_hash()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def log(self, entry: AuditEntry) -> str:
        """
        Log an audit entry to the JSONL file and optionally ship to Splunk.

        Steps:
        1. Compute entry_id = SHA-256 of the entry's content (excl. chain fields)
        2. Compute hmac_chain = HMAC(key, previous_hash + content)
        3. Stamp entry_id and hmac_chain onto the entry
        4. Write to JSONL file (atomic: write then flush)
        5. Update _last_entry_hash for the next entry
        6. Non-blocking Splunk ship (best-effort)

        Returns:
            entry_id (str) — use for downstream correlation
        """
        with self._lock:
            content = entry.content_for_hashing()
            entry_id = hashlib.sha256(content.encode("utf-8")).hexdigest()
            entry.entry_id = entry_id

            hmac_chain = self._compute_hmac(content=content, previous_hash=self._last_entry_hash)
            entry.hmac_chain = hmac_chain

            # Write to JSONL
            try:
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(entry.to_json() + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                # Local log write failed — this is a hard failure (audit must not be lost)
                logger.error(
                    "AUDIT LOG WRITE FAILED: %s — entry_id=%s",
                    exc,
                    entry_id,
                    exc_info=True,
                )
                raise

            # Update chain state
            self._last_entry_hash = entry_id

        # Non-blocking Splunk shipping (outside lock to avoid blocking writers)
        self._ship_to_splunk(entry)

        return entry_id

    def verify_chain(self) -> tuple[bool, list[str]]:
        """
        Verify the HMAC chain integrity by replaying all entries in the log.

        Algorithm:
        1. Read all entries in order
        2. For each entry, recompute content_hash and HMAC
        3. Verify entry_id matches content_hash
        4. Verify hmac_chain matches HMAC(key, previous_entry_id + content)
        5. Collect all violations

        Returns:
            (is_valid: bool, violations: list[str])
            violations is empty if is_valid is True.
        """
        violations: list[str] = []

        if not self._log_path.exists():
            return True, []

        previous_hash = ""
        line_number = 0

        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line_number += 1
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue

                    try:
                        data = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        violations.append(
                            f"Line {line_number}: JSON parse error — {exc}"
                        )
                        continue

                    stored_entry_id = data.get("entry_id", "")
                    stored_hmac = data.get("hmac_chain", "")

                    # Recompute content (exclude chain fields)
                    content_data = {k: v for k, v in data.items()
                                    if k not in ("hmac_chain", "entry_id")}
                    content = json.dumps(content_data, default=str, sort_keys=True)

                    # Verify entry_id
                    expected_entry_id = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if stored_entry_id != expected_entry_id:
                        violations.append(
                            f"Line {line_number} (entry_id={stored_entry_id[:16]}...): "
                            f"entry_id mismatch — expected {expected_entry_id[:16]}..., "
                            f"got {stored_entry_id[:16]}... — POSSIBLE TAMPERING"
                        )

                    # Verify HMAC chain
                    expected_hmac = self._compute_hmac(content=content, previous_hash=previous_hash)
                    if stored_hmac != expected_hmac:
                        violations.append(
                            f"Line {line_number} (entry_id={stored_entry_id[:16]}...): "
                            f"HMAC chain broken — expected {expected_hmac[:16]}..., "
                            f"got {stored_hmac[:16]}... — POSSIBLE TAMPERING or insertion"
                        )

                    previous_hash = stored_entry_id

        except OSError as exc:
            violations.append(f"Cannot read audit log: {exc}")

        is_valid = len(violations) == 0
        return is_valid, violations

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _load_last_hash(self) -> None:
        """
        Load the last entry_id from the log file to seed the HMAC chain.

        This ensures that entries written after a restart correctly chain
        onto the previous session's last entry, providing continuity.
        """
        if not self._log_path.exists():
            self._last_entry_hash = ""
            return

        last_entry_id = ""
        try:
            # Efficiently find the last non-empty line
            with self._log_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        last_entry_id = data.get("entry_id", "")
                    except json.JSONDecodeError:
                        continue
        except OSError:
            last_entry_id = ""

        self._last_entry_hash = last_entry_id

    def _compute_hmac(self, content: str, previous_hash: str) -> str:
        """
        Compute HMAC-SHA256 of (previous_hash + "|" + content).

        The chain input concatenates the previous entry's hash with the
        current content, ensuring each HMAC depends on the full prior chain.

        Returns:
            Hex-encoded HMAC digest (64 characters).
        """
        message = (previous_hash + "|" + content).encode("utf-8")
        return hmac_lib.new(self._hmac_key, message, hashlib.sha256).hexdigest()

    def _ship_to_splunk(self, entry: AuditEntry) -> None:
        """
        Non-blocking Splunk HEC shipping via a background thread.

        Local log is ALWAYS written first. Splunk shipping is best-effort:
        failures are logged at WARNING level but do not raise.

        Requires environment variable: SPLUNK_HEC_URL
        Optional:                       SPLUNK_HEC_TOKEN
        """
        splunk_url = os.environ.get("SPLUNK_HEC_URL", "")
        if not splunk_url:
            return  # Splunk not configured — skip silently

        def _ship() -> None:
            try:
                import urllib.request
                splunk_token = os.environ.get("SPLUNK_HEC_TOKEN", "")
                payload = json.dumps(
                    {
                        "time": time.time(),
                        "source": "agent-audit-log",
                        "sourcetype": "_json",
                        "event": entry.to_dict(),
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    splunk_url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Splunk {splunk_token}" if splunk_token else "Splunk anonymous",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status not in (200, 201):
                        logger.warning(
                            "Splunk HEC returned non-200 status %d for entry %s",
                            resp.status,
                            entry.entry_id[:16],
                        )
            except Exception as exc:
                logger.warning(
                    "Splunk HEC shipping failed for entry %s: %s",
                    entry.entry_id[:16],
                    exc,
                )

        thread = threading.Thread(target=_ship, daemon=True)
        thread.start()

    @staticmethod
    def _resolve_hmac_key(provided_key: Optional[bytes]) -> bytes:
        """
        Resolve the HMAC key.

        Priority:
        1. Explicitly provided key (test injection)
        2. AUDIT_HMAC_KEY environment variable (production)
        3. Fallback dev key (only for local development — warns on use)
        """
        if provided_key:
            return provided_key

        env_key = os.environ.get("AUDIT_HMAC_KEY", "")
        if env_key:
            return env_key.encode("utf-8")

        logger.warning(
            "AUDIT_HMAC_KEY not set — using default dev key. "
            "HMAC chain will not be verifiable across different instances. "
            "Set AUDIT_HMAC_KEY in production."
        )
        return b"default-dev-key-not-for-production"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_logger: Optional[AuditLogger] = None
_singleton_lock = threading.Lock()


def _get_default_logger() -> AuditLogger:
    """Return the module-level default AuditLogger (lazy-initialized)."""
    global _default_logger
    if _default_logger is None:
        with _singleton_lock:
            if _default_logger is None:
                _default_logger = AuditLogger(
                    log_path=Path(
                        os.environ.get("AUDIT_LOG_PATH", ".audit/agent_audit.jsonl")
                    )
                )
    return _default_logger


def _make_entry(
    event_type: AuditEventType,
    agent_name: str,
    trace_id: str,
    tenant_id: str,
    job_id: str,
    action: str,
    input_hash: str,
    result: str,
    duration_ms: int = 0,
    tokens_used: int = 0,
    rule_id: Optional[str] = None,
    output_summary: str = "",
    tool_calls: Optional[list[dict]] = None,
    gate_decisions: Optional[list[dict]] = None,
) -> AuditEntry:
    return AuditEntry(
        event_type=event_type,
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        trace_id=trace_id or str(uuid.uuid4()),
        agent_name=agent_name,
        tenant_id=tenant_id or "unknown",
        job_id=job_id or "unknown",
        action=action,
        input_hash=input_hash,
        output_summary=output_summary,
        result=result,
        duration_ms=duration_ms,
        rule_id=rule_id,
        tool_calls=tool_calls or [],
        gate_decisions=gate_decisions or [],
        tokens_used=tokens_used,
    )


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def log_agent_invocation(
    agent_name: str,
    trace_id: str,
    tenant_id: str,
    job_id: str,
    input_hash: str,
    result: str,
    duration_ms: int,
    tokens_used: int,
) -> str:
    """
    Log an AGENT_INVOCATION event.

    Returns entry_id for correlation.
    """
    entry = _make_entry(
        event_type=AuditEventType.AGENT_INVOCATION,
        agent_name=agent_name,
        trace_id=trace_id,
        tenant_id=tenant_id,
        job_id=job_id,
        action="agent_invocation",
        input_hash=input_hash,
        result=result,
        duration_ms=duration_ms,
        tokens_used=tokens_used,
    )
    return _get_default_logger().log(entry)


def log_security_block(
    agent_name: str,
    trace_id: str,
    rule_id: str,
    blocked_content_hash: str,
    action: str,
) -> str:
    """
    Log a SECURITY_BLOCK event.

    Returns entry_id for correlation.
    """
    entry = _make_entry(
        event_type=AuditEventType.SECURITY_BLOCK,
        agent_name=agent_name,
        trace_id=trace_id,
        tenant_id="unknown",
        job_id="unknown",
        action=action,
        input_hash=blocked_content_hash,
        result="BLOCK",
        rule_id=rule_id,
        output_summary=f"Blocked by rule {rule_id}",
    )
    return _get_default_logger().log(entry)


def log_gate_decision(
    agent_name: str,
    trace_id: str,
    gate_name: str,
    decision: str,
    reason: str,
) -> str:
    """
    Log a GATE_DECISION event.

    Returns entry_id for correlation.
    """
    input_hash = hashlib.sha256(f"{gate_name}:{reason}".encode()).hexdigest()
    entry = _make_entry(
        event_type=AuditEventType.GATE_DECISION,
        agent_name=agent_name,
        trace_id=trace_id,
        tenant_id="unknown",
        job_id="unknown",
        action=f"gate:{gate_name}",
        input_hash=input_hash,
        result=decision,
        output_summary=reason[:200],  # max 200 chars in summary
        gate_decisions=[{"gate_name": gate_name, "decision": decision, "reason": reason[:200]}],
    )
    return _get_default_logger().log(entry)


def log_mcp_access(
    agent_name: str,
    trace_id: str,
    server_name: str,
    operation: str,
    resource: str,
    allowed: bool,
) -> str:
    """
    Log an MCP_ACCESS event.

    Returns entry_id for correlation.
    """
    resource_hash = hashlib.sha256(resource.encode()).hexdigest()
    entry = _make_entry(
        event_type=AuditEventType.MCP_ACCESS,
        agent_name=agent_name,
        trace_id=trace_id,
        tenant_id="unknown",
        job_id="unknown",
        action=f"mcp:{server_name}:{operation}",
        input_hash=resource_hash,
        result="ALLOW" if allowed else "DENY",
        output_summary=f"server={server_name} op={operation} resource_hash={resource_hash[:16]}",
    )
    return _get_default_logger().log(entry)
