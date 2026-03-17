"""
Anomaly Detector — real-time behavioral anomaly detection for agent activity.

Uses sliding-window counters (collections.deque) to detect rate spikes, auth
failures, unusual file access volumes, and cross-tenant attempts without
requiring external state stores.

All detections:
1. Emit a Prometheus counter increment
2. Write to the AuditLogger with event_type=ANOMALY_DETECTED
3. Optionally dispatch to a webhook (ANOMALY_WEBHOOK_URL env var)

Compliance: FedRAMP AU-6 (Audit Review, Analysis, and Reporting),
            FedRAMP SI-4 (Information System Monitoring).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from monitoring.structured_audit_log import (
    AuditEntry,
    AuditEventType,
    AuditLogger,
    _get_default_logger,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Prometheus metrics
# ---------------------------------------------------------------------------

try:
    from prometheus_client import Counter, Gauge, REGISTRY
    _PROMETHEUS_AVAILABLE = True

    _anomaly_total = Counter(
        "agent_anomaly_detections_total",
        "Total number of anomaly detections by type and agent",
        ["anomaly_type", "agent_name"],
    )
    _tool_call_rate = Gauge(
        "agent_tool_calls_per_minute",
        "Current tool call rate per agent (calls in last 60s)",
        ["agent_name"],
    )
    _block_rate = Gauge(
        "agent_security_block_rate_per_hour",
        "Security BLOCK decisions in last 3600s",
        [],
    )
    _auth_failure_rate = Gauge(
        "agent_auth_failure_rate_per_minute",
        "Auth failures per agent in last 60s",
        ["agent_name"],
    )
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    _anomaly_total = None
    _tool_call_rate = None
    _block_rate = None
    _auth_failure_rate = None


# ---------------------------------------------------------------------------
# AnomalyAlert dataclass
# ---------------------------------------------------------------------------


@dataclass
class AnomalyAlert:
    """Structured alert produced when an anomaly threshold is crossed."""
    anomaly_type: str
    agent_name: str
    trace_id: str
    description: str
    threshold: int
    observed_count: int
    window_seconds: int
    timestamp_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    additional_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anomaly_type": self.anomaly_type,
            "agent_name": self.agent_name,
            "trace_id": self.trace_id,
            "description": self.description,
            "threshold": self.threshold,
            "observed_count": self.observed_count,
            "window_seconds": self.window_seconds,
            "timestamp_utc": self.timestamp_utc,
            "additional_context": self.additional_context,
        }


# ---------------------------------------------------------------------------
# AnomalyDetector
# ---------------------------------------------------------------------------


class AnomalyDetector:
    """
    Real-time anomaly detection using sliding-window counters.

    All detectors maintain per-agent deques of (monotonic_timestamp) events.
    Events older than the detection window are evicted on each check.

    Thread safety: all state mutations are protected by per-agent RLocks.
    """

    def __init__(
        self,
        audit_logger: Optional[AuditLogger] = None,
        webhook_url: Optional[str] = None,
    ) -> None:
        self._audit_logger = audit_logger or _get_default_logger()
        self._webhook_url = webhook_url or os.environ.get("ANOMALY_WEBHOOK_URL", "")

        # Sliding windows — keyed by agent_name
        # Each entry is a monotonic timestamp (float)
        self._tool_call_windows: dict[str, deque] = defaultdict(deque)
        self._auth_failure_windows: dict[str, deque] = defaultdict(deque)
        self._file_read_windows: dict[str, deque] = defaultdict(deque)
        self._block_window: deque = deque()  # global block rate

        # Per-agent locks to prevent race conditions
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._global_lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Detection methods                                                    #
    # ------------------------------------------------------------------ #

    def detect_tool_call_spike(
        self,
        agent_name: str,
        window_seconds: int = 60,
        threshold: int = 10,
        trace_id: str = "",
    ) -> Optional[AnomalyAlert]:
        """
        Record a tool call event for agent_name. If the count within the last
        window_seconds exceeds threshold, generate and dispatch an alert.

        Returns:
            AnomalyAlert if threshold exceeded, None otherwise.
        """
        with self._locks[agent_name]:
            now = time.monotonic()
            window = self._tool_call_windows[agent_name]
            # Evict stale events
            while window and now - window[0] > window_seconds:
                window.popleft()
            window.append(now)

            count = len(window)

            # Update Prometheus gauge
            if _PROMETHEUS_AVAILABLE and _tool_call_rate:
                _tool_call_rate.labels(agent_name=agent_name).set(count)

            if count > threshold:
                alert = AnomalyAlert(
                    anomaly_type="TOOL_CALL_SPIKE",
                    agent_name=agent_name,
                    trace_id=trace_id or _generate_trace_id(),
                    description=(
                        f"Agent '{agent_name}' made {count} tool calls in the last "
                        f"{window_seconds}s (threshold: {threshold})"
                    ),
                    threshold=threshold,
                    observed_count=count,
                    window_seconds=window_seconds,
                    additional_context={"window_size": len(window)},
                )
                self._dispatch_alert(alert)
                return alert

        return None

    def detect_block_rate_spike(
        self,
        window_seconds: int = 3600,
        threshold: int = 3,
        trace_id: str = "",
    ) -> Optional[AnomalyAlert]:
        """
        Record a global security BLOCK event. If the count within the last
        window_seconds exceeds threshold, generate an alert.

        Returns:
            AnomalyAlert if threshold exceeded, None otherwise.
        """
        with self._global_lock:
            now = time.monotonic()
            while self._block_window and now - self._block_window[0] > window_seconds:
                self._block_window.popleft()
            self._block_window.append(now)

            count = len(self._block_window)

            if _PROMETHEUS_AVAILABLE and _block_rate:
                _block_rate.set(count)

            if count > threshold:
                alert = AnomalyAlert(
                    anomaly_type="BLOCK_RATE_SPIKE",
                    agent_name="global",
                    trace_id=trace_id or _generate_trace_id(),
                    description=(
                        f"Security BLOCK decisions exceeded threshold: {count} "
                        f"blocks in the last {window_seconds}s (threshold: {threshold})"
                    ),
                    threshold=threshold,
                    observed_count=count,
                    window_seconds=window_seconds,
                )
                self._dispatch_alert(alert)
                return alert

        return None

    def detect_unusual_file_volume(
        self,
        agent_name: str,
        window_seconds: int = 60,
        threshold: int = 50,
        trace_id: str = "",
    ) -> Optional[AnomalyAlert]:
        """
        Record a file read event for agent_name. If the count within the last
        window_seconds exceeds threshold, generate an alert.

        Returns:
            AnomalyAlert if threshold exceeded, None otherwise.
        """
        with self._locks[agent_name]:
            now = time.monotonic()
            window = self._file_read_windows[agent_name]
            while window and now - window[0] > window_seconds:
                window.popleft()
            window.append(now)

            count = len(window)

            if count > threshold:
                alert = AnomalyAlert(
                    anomaly_type="UNUSUAL_FILE_VOLUME",
                    agent_name=agent_name,
                    trace_id=trace_id or _generate_trace_id(),
                    description=(
                        f"Agent '{agent_name}' read {count} files in the last "
                        f"{window_seconds}s (threshold: {threshold})"
                    ),
                    threshold=threshold,
                    observed_count=count,
                    window_seconds=window_seconds,
                )
                self._dispatch_alert(alert)
                return alert

        return None

    def detect_repeated_auth_failures(
        self,
        agent_name: str,
        window_seconds: int = 60,
        threshold: int = 5,
        trace_id: str = "",
    ) -> Optional[AnomalyAlert]:
        """
        Record an authentication failure for agent_name. If the count within
        the last window_seconds exceeds threshold, generate an alert.

        Returns:
            AnomalyAlert if threshold exceeded, None otherwise.
        """
        with self._locks[agent_name]:
            now = time.monotonic()
            window = self._auth_failure_windows[agent_name]
            while window and now - window[0] > window_seconds:
                window.popleft()
            window.append(now)

            count = len(window)

            if _PROMETHEUS_AVAILABLE and _auth_failure_rate:
                _auth_failure_rate.labels(agent_name=agent_name).set(count)

            if count > threshold:
                alert = AnomalyAlert(
                    anomaly_type="REPEATED_AUTH_FAILURES",
                    agent_name=agent_name,
                    trace_id=trace_id or _generate_trace_id(),
                    description=(
                        f"Agent '{agent_name}' recorded {count} authentication "
                        f"failures in the last {window_seconds}s (threshold: {threshold})"
                    ),
                    threshold=threshold,
                    observed_count=count,
                    window_seconds=window_seconds,
                )
                self._dispatch_alert(alert)
                return alert

        return None

    def detect_cross_tenant_attempt(
        self,
        trace_id: str,
        agent_name: str = "unknown",
        requesting_tenant: str = "",
        target_tenant: str = "",
    ) -> AnomalyAlert:
        """
        Record and immediately alert on any cross-tenant access attempt.

        Cross-tenant attempts are always an immediate CRITICAL alert regardless
        of frequency — there is no threshold.

        Returns:
            AnomalyAlert (always, since any occurrence is an alert)
        """
        alert = AnomalyAlert(
            anomaly_type="CROSS_TENANT_ACCESS_ATTEMPT",
            agent_name=agent_name,
            trace_id=trace_id or _generate_trace_id(),
            description=(
                f"CRITICAL: Agent '{agent_name}' (tenant '{requesting_tenant}') "
                f"attempted to access context for tenant '{target_tenant}'. "
                "Cross-tenant access is unconditionally forbidden."
            ),
            threshold=1,
            observed_count=1,
            window_seconds=0,
            additional_context={
                "requesting_tenant": requesting_tenant,
                "target_tenant": target_tenant,
                "severity": "CRITICAL",
            },
        )
        self._dispatch_alert(alert)
        return alert

    # ------------------------------------------------------------------ #
    # Internal dispatch                                                    #
    # ------------------------------------------------------------------ #

    def _dispatch_alert(self, alert: AnomalyAlert) -> None:
        """
        Dispatch an anomaly alert to:
        1. AuditLogger (ANOMALY_DETECTED event)
        2. Prometheus counter
        3. Optional webhook (non-blocking background thread)
        """
        # 1. Write to audit log
        input_hash = hashlib.sha256(alert.description.encode()).hexdigest()
        entry = AuditEntry(
            event_type=AuditEventType.ANOMALY_DETECTED,
            timestamp_utc=alert.timestamp_utc,
            trace_id=alert.trace_id,
            agent_name=alert.agent_name,
            tenant_id="unknown",
            job_id="unknown",
            action=alert.anomaly_type,
            input_hash=input_hash,
            output_summary=alert.description[:500],
            result="ALERT",
            duration_ms=0,
            rule_id=alert.anomaly_type,
            tool_calls=[],
            gate_decisions=[],
            tokens_used=0,
        )
        try:
            self._audit_logger.log(entry)
        except Exception as exc:
            logger.error("Failed to write anomaly alert to audit log: %s", exc)

        # 2. Prometheus counter
        if _PROMETHEUS_AVAILABLE and _anomaly_total:
            try:
                _anomaly_total.labels(
                    anomaly_type=alert.anomaly_type,
                    agent_name=alert.agent_name,
                ).inc()
            except Exception:
                pass

        # 3. Webhook (non-blocking)
        if self._webhook_url:
            self._dispatch_webhook(alert)

    def _dispatch_webhook(self, alert: AnomalyAlert) -> None:
        """Non-blocking webhook dispatch in a daemon thread."""
        url = self._webhook_url

        def _send() -> None:
            try:
                payload = json.dumps(alert.to_dict()).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5):
                    pass
            except Exception as exc:
                logger.warning("Anomaly webhook dispatch failed: %s", exc)

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _generate_trace_id() -> str:
    """Generate a random trace ID for anomaly events without an existing trace."""
    import uuid
    return str(uuid.uuid4())
