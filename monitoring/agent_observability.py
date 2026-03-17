"""
Agent Observability Module — Migration Platform.

Provides unified Prometheus metrics, OpenTelemetry tracing, and Halcon
retrospective metrics emission for all agents in the migration platform.

Components:
- AgentMetrics:   Prometheus counters, histograms, and gauges for agent activity
- AgentTracer:    OpenTelemetry span management for distributed tracing
- HalconEmitter:  Structured retrospective metrics written to sessions.jsonl
                  and pushed to Prometheus gauges

Usage::

    from monitoring.agent_observability import AgentMetrics, AgentTracer, HalconEmitter

    metrics = AgentMetrics()
    tracer = AgentTracer(service_name="orchestrator-agent")
    halcon = HalconEmitter(sessions_path=Path(".halcon/retrospectives/sessions.jsonl"))

    # Record a tool call
    metrics.tool_calls.labels(
        agent_name="orchestrator-agent",
        tool_name="delegate_to_validation_agent",
        status="success",
    ).inc()

    # Emit Halcon metrics after orchestration
    halcon.emit(HalconMetrics(...))
"""
from __future__ import annotations

import json
import logging
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependencies — graceful degradation if not installed
# ---------------------------------------------------------------------------

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        CollectorRegistry,
        REGISTRY as DEFAULT_REGISTRY,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROMETHEUS_AVAILABLE = False
    Counter = Gauge = Histogram = CollectorRegistry = None

try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import Span, SpanKind, StatusCode
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False
    Span = None

# ---------------------------------------------------------------------------
# HalconMetrics dataclass
# ---------------------------------------------------------------------------


@dataclass
class HalconMetrics:
    """
    Retrospective efficiency metrics for a single agent session.
    Written to .halcon/retrospectives/sessions.jsonl and pushed to Prometheus.

    Field definitions match config/halcon.yaml metric descriptions.
    """
    session_id: str
    agent_name: str
    timestamp_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Core Halcon metrics (all normalised 0.0–1.0 unless noted)
    convergence_efficiency: float = 0.0
    """Ratio of useful tokens to total tokens consumed. Target: > 0.7"""

    decision_density: float = 0.0
    """Number of gate decisions per 1000 tokens. Target: > 0.5"""

    adaptation_utilization: float = 0.0
    """Fraction of iterations that changed approach. Target: 0.2–0.6"""

    final_utility: float = 0.0
    """Outcome quality 0.0–1.0. Target: > 0.8"""

    peak_utility: float = 0.0
    """Peak utility score reached during the session (before potential decline)."""

    inferred_problem_class: Optional[str] = None
    """Heuristic classification: deterministic-linear, convergent, adaptive-iterative, exploratory-divergent."""

    # Supporting data
    total_iterations: int = 0
    useful_iterations: int = 0
    wasted_rounds: int = 0
    gate_decisions_made: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # Failure analysis
    dominant_failure_mode: Optional[str] = None
    evidence_trajectory: Optional[str] = None  # "rising", "flat", "declining"
    structural_instability_score: float = 0.0

    # Session metadata
    orchestration_id: Optional[str] = None
    run_id: Optional[str] = None
    tenant_id: Optional[str] = None
    environment: str = "production"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HalconMetrics":
        # Filter to only known fields
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def validate(self) -> List[str]:
        """Returns a list of validation errors (empty = valid)."""
        errors = []
        for attr in ("convergence_efficiency", "final_utility", "adaptation_utilization"):
            val = getattr(self, attr)
            if not (0.0 <= val <= 1.0):
                errors.append(f"{attr}={val} is outside [0.0, 1.0]")
        if not self.session_id:
            errors.append("session_id must not be empty")
        if not self.agent_name:
            errors.append("agent_name must not be empty")
        return errors


# ---------------------------------------------------------------------------
# AgentMetrics — Prometheus metrics
# ---------------------------------------------------------------------------


class AgentMetrics:
    """
    Prometheus metric definitions for agent observability.

    All metrics are registered in a dedicated CollectorRegistry to allow
    test isolation without conflicting with the default global registry.

    Metric naming convention: agent_<component>_<measurement>_<unit>
    """

    def __init__(self, registry: Optional[Any] = None):
        if not _PROMETHEUS_AVAILABLE:
            logger.warning(
                "prometheus_client not installed — AgentMetrics will use no-op stubs"
            )
            self._init_noop()
            return

        self._registry = registry or CollectorRegistry()
        self._init_prometheus()

    def _init_prometheus(self) -> None:
        """Initialise all Prometheus metrics."""

        # ------------------------------------------------------------------
        # Counter: total tool calls by agent, tool, and outcome
        # ------------------------------------------------------------------
        self.tool_calls = Counter(
            name="agent_tool_calls_total",
            documentation=(
                "Total number of tool calls made by agents, "
                "labeled by agent_name, tool_name, and status (success|error|blocked)"
            ),
            labelnames=["agent_name", "tool_name", "status"],
            registry=self._registry,
        )

        # ------------------------------------------------------------------
        # Histogram: agent session (full run) duration
        # ------------------------------------------------------------------
        self.session_duration = Histogram(
            name="agent_session_duration_seconds",
            documentation=(
                "Duration of a complete agent session from start to end_turn, "
                "in seconds. Labeled by agent_name."
            ),
            labelnames=["agent_name"],
            buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600],
            registry=self._registry,
        )

        # ------------------------------------------------------------------
        # Counter: validation/security gate decisions
        # ------------------------------------------------------------------
        self.gate_decisions = Counter(
            name="agent_gate_decisions_total",
            documentation=(
                "Total gate decisions made during agent sessions. "
                "Labels: agent_name, gate_name (validation|security), "
                "decision (ALLOW|WARN|BLOCK)"
            ),
            labelnames=["agent_name", "gate_name", "decision"],
            registry=self._registry,
        )

        # ------------------------------------------------------------------
        # Gauge: circuit breaker state per downstream service
        # State encoding: 0=CLOSED, 1=HALF_OPEN, 2=OPEN
        # ------------------------------------------------------------------
        self.circuit_breaker_state = Gauge(
            name="agent_circuit_breaker_state",
            documentation=(
                "Current circuit breaker state for downstream services. "
                "0=CLOSED (healthy), 1=HALF_OPEN (probing), 2=OPEN (tripped). "
                "Labeled by service_name."
            ),
            labelnames=["service_name"],
            registry=self._registry,
        )

        # ------------------------------------------------------------------
        # Gauge: Halcon convergence efficiency per agent
        # ------------------------------------------------------------------
        self.halcon_convergence_efficiency = Gauge(
            name="halcon_convergence_efficiency",
            documentation=(
                "Halcon convergence efficiency for the most recent agent session: "
                "ratio of useful tokens to total tokens consumed (0.0–1.0). "
                "Labeled by agent_name. Target: > 0.7"
            ),
            labelnames=["agent_name"],
            registry=self._registry,
        )

        # ------------------------------------------------------------------
        # Gauge: Halcon final utility (outcome quality) per agent
        # ------------------------------------------------------------------
        self.halcon_final_utility = Gauge(
            name="halcon_final_utility",
            documentation=(
                "Halcon final utility for the most recent agent session: "
                "outcome quality score 0.0–1.0. "
                "Labeled by agent_name. Target: > 0.8"
            ),
            labelnames=["agent_name"],
            registry=self._registry,
        )

        # ------------------------------------------------------------------
        # Gauge: Halcon adaptation utilization per agent
        # ------------------------------------------------------------------
        self.halcon_adaptation_utilization = Gauge(
            name="halcon_adaptation_utilization",
            documentation=(
                "Fraction of agent iterations that changed approach (0.0–1.0). "
                "Labeled by agent_name. Target range: 0.2–0.6"
            ),
            labelnames=["agent_name"],
            registry=self._registry,
        )

        # ------------------------------------------------------------------
        # Counter: human-in-the-loop confirmation events
        # ------------------------------------------------------------------
        self.human_confirmations = Counter(
            name="agent_human_confirmations_total",
            documentation=(
                "Total human-in-the-loop confirmation events. "
                "Labels: action (rollback|force_complete|pause_all), "
                "outcome (approved|rejected|timeout)"
            ),
            labelnames=["action", "outcome"],
            registry=self._registry,
        )

        # ------------------------------------------------------------------
        # Gauge: current agent iteration count (for loop detection)
        # ------------------------------------------------------------------
        self.agent_iterations = Gauge(
            name="agent_current_iterations",
            documentation=(
                "Current iteration count for running agent sessions. "
                "Exceeding max_iterations triggers circuit breaker. "
                "Labeled by agent_name, session_id."
            ),
            labelnames=["agent_name", "session_id"],
            registry=self._registry,
        )

    def _init_noop(self) -> None:
        """No-op metric stubs for environments without prometheus_client."""
        class _NoOpMetric:
            def labels(self, **kwargs) -> "_NoOpMetric":
                return self
            def inc(self, amount: float = 1) -> None: pass
            def set(self, value: float) -> None: pass
            def observe(self, value: float) -> None: pass

        class _NoOpHistogram(_NoOpMetric):
            @contextmanager
            def time(self) -> Generator:
                yield

        self.tool_calls = _NoOpMetric()
        self.session_duration = _NoOpHistogram()
        self.gate_decisions = _NoOpMetric()
        self.circuit_breaker_state = _NoOpMetric()
        self.halcon_convergence_efficiency = _NoOpMetric()
        self.halcon_final_utility = _NoOpMetric()
        self.halcon_adaptation_utilization = _NoOpMetric()
        self.human_confirmations = _NoOpMetric()
        self.agent_iterations = _NoOpMetric()

    @contextmanager
    def time_session(self, agent_name: str) -> Generator[None, None, None]:
        """Context manager to time and record a complete agent session."""
        start = time.perf_counter()
        try:
            yield
        finally:
            duration = time.perf_counter() - start
            self.session_duration.labels(agent_name=agent_name).observe(duration)

    def record_circuit_breaker_open(self, service_name: str) -> None:
        """Record that a circuit breaker has tripped to OPEN state."""
        self.circuit_breaker_state.labels(service_name=service_name).set(2)
        logger.warning(
            "circuit_breaker_open service=%s", service_name
        )

    def record_circuit_breaker_closed(self, service_name: str) -> None:
        """Record that a circuit breaker has returned to CLOSED state."""
        self.circuit_breaker_state.labels(service_name=service_name).set(0)

    def record_circuit_breaker_half_open(self, service_name: str) -> None:
        """Record that a circuit breaker is in HALF_OPEN probe state."""
        self.circuit_breaker_state.labels(service_name=service_name).set(1)


# ---------------------------------------------------------------------------
# AgentTracer — OpenTelemetry span management
# ---------------------------------------------------------------------------


class AgentTracer:
    """
    OpenTelemetry span management for agent distributed tracing.

    Provides agent-aware wrappers around the standard OTel API that
    automatically add agent-specific span attributes.
    """

    def __init__(
        self,
        service_name: str = "migration-agent-platform",
        tracer_name: Optional[str] = None,
    ):
        self.service_name = service_name
        self._tracer_name = tracer_name or service_name
        self._tracer = self._init_tracer()

    def _init_tracer(self) -> Any:
        if not _OTEL_AVAILABLE:
            logger.warning("opentelemetry not installed — AgentTracer will use no-op spans")
            return None
        return otel_trace.get_tracer(self._tracer_name)

    def start_agent_span(
        self,
        agent_name: str,
        request_id: str,
        trace_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Any:
        """
        Start a new OTel span for an agent session.

        Args:
            agent_name:  Name of the agent (e.g. 'orchestrator-agent').
            request_id:  Unique ID for this invocation.
            trace_id:    Distributed trace ID (propagated from the HTTP request).
            session_id:  Orchestration session ID.

        Returns:
            OpenTelemetry Span (or a no-op stub if OTel not available).
        """
        if not _OTEL_AVAILABLE or self._tracer is None:
            return _NoOpSpan(agent_name=agent_name, request_id=request_id)

        span = self._tracer.start_span(
            name=f"{agent_name}.session",
            kind=SpanKind.INTERNAL,
        )
        span.set_attribute("agent.name", agent_name)
        span.set_attribute("agent.request_id", request_id)
        if trace_id:
            span.set_attribute("agent.trace_id", trace_id)
        if session_id:
            span.set_attribute("agent.session_id", session_id)
        return span

    def record_tool_call(
        self,
        span: Any,
        tool_name: str,
        duration_ms: float,
        success: bool,
        error_code: Optional[str] = None,
    ) -> None:
        """
        Record a tool call on an existing span as a span event.

        Args:
            span:         The active agent span.
            tool_name:    Name of the tool that was called.
            duration_ms:  How long the tool call took in milliseconds.
            success:      Whether the tool call succeeded.
            error_code:   Error code if the tool call failed.
        """
        if isinstance(span, _NoOpSpan) or not _OTEL_AVAILABLE:
            return
        span.add_event(
            name="tool_call",
            attributes={
                "tool.name": tool_name,
                "tool.duration_ms": duration_ms,
                "tool.success": success,
                "tool.error_code": error_code or "",
            },
        )
        if not success and error_code:
            span.set_attribute(f"tool.last_error", error_code)

    def record_gate_decision(
        self,
        span: Any,
        gate_name: str,
        decision: str,
        reason: Optional[str] = None,
    ) -> None:
        """
        Record a gate decision (ALLOW/WARN/BLOCK) on an existing span.

        Args:
            span:       The active agent span.
            gate_name:  Name of the gate (e.g. 'validation', 'security').
            decision:   One of ALLOW, WARN, BLOCK.
            reason:     Optional human-readable reason for the decision.
        """
        if isinstance(span, _NoOpSpan) or not _OTEL_AVAILABLE:
            return
        span.add_event(
            name="gate_decision",
            attributes={
                "gate.name": gate_name,
                "gate.decision": decision,
                "gate.reason": reason or "",
            },
        )
        # A BLOCK decision is a notable event — mark the span for alerting
        if decision == "BLOCK":
            span.set_attribute("gate.blocked", True)
            span.set_attribute("gate.blocked_by", gate_name)

    @contextmanager
    def agent_session_span(
        self,
        agent_name: str,
        request_id: str,
        trace_id: Optional[str] = None,
    ) -> Generator[Any, None, None]:
        """
        Context manager that starts an agent span and ends it on exit.

        Usage::

            with tracer.agent_session_span("validation-agent", req_id) as span:
                tracer.record_tool_call(span, "validate_record_counts", 250.0, True)
        """
        span = self.start_agent_span(
            agent_name=agent_name,
            request_id=request_id,
            trace_id=trace_id,
        )
        start = time.perf_counter()
        try:
            yield span
            if _OTEL_AVAILABLE and not isinstance(span, _NoOpSpan):
                span.set_status(StatusCode.OK)
        except Exception as exc:
            if _OTEL_AVAILABLE and not isinstance(span, _NoOpSpan):
                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            if _OTEL_AVAILABLE and not isinstance(span, _NoOpSpan):
                span.set_attribute("agent.duration_ms", duration_ms)
                span.end()


class _NoOpSpan:
    """Stub span used when OpenTelemetry is not installed."""

    def __init__(self, agent_name: str = "", request_id: str = ""):
        self.agent_name = agent_name
        self.request_id = request_id

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        pass

    def set_status(self, *args, **kwargs) -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass

    def end(self) -> None:
        pass


# ---------------------------------------------------------------------------
# HalconEmitter
# ---------------------------------------------------------------------------


class HalconEmitter:
    """
    Emits Halcon retrospective metrics to the sessions.jsonl file and
    optionally pushes them to Prometheus gauges for real-time monitoring.

    The sessions.jsonl file is the primary store for post-run analysis.
    Prometheus gauges provide real-time visibility in Grafana dashboards.

    File format: append-only JSONL (one JSON object per line).
    Never overwrites — each call appends exactly one record.
    """

    def __init__(
        self,
        sessions_path: Optional[Path] = None,
        agent_metrics: Optional[AgentMetrics] = None,
    ):
        self.sessions_path = sessions_path or Path(
            ".halcon/retrospectives/sessions.jsonl"
        )
        self._metrics = agent_metrics

    def emit(self, metrics: HalconMetrics) -> None:
        """
        Append a HalconMetrics session to the JSONL file and push to Prometheus.

        This method is designed to be idempotent within a session — if called
        multiple times with the same session_id, all records are preserved
        (each is a point-in-time snapshot).

        Args:
            metrics: The HalconMetrics to record.

        Raises:
            ValueError: If metrics validation fails.
        """
        errors = metrics.validate()
        if errors:
            raise ValueError(
                f"HalconMetrics validation failed: {'; '.join(errors)}"
            )

        # Write to JSONL (append-only)
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        record = metrics.to_dict()
        try:
            with self.sessions_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            logger.error(
                "HalconEmitter: failed to write session %s: %s",
                metrics.session_id,
                exc,
            )
            raise

        # Push to Prometheus gauges (fire-and-forget — don't fail if metrics unavailable)
        self._push_to_prometheus(metrics)

        logger.debug(
            "halcon_emitted session_id=%s agent=%s convergence=%.3f utility=%.3f",
            metrics.session_id,
            metrics.agent_name,
            metrics.convergence_efficiency,
            metrics.final_utility,
        )

    def _push_to_prometheus(self, metrics: HalconMetrics) -> None:
        """Push Halcon metrics to Prometheus gauges."""
        if self._metrics is None:
            return
        try:
            agent = metrics.agent_name
            self._metrics.halcon_convergence_efficiency.labels(
                agent_name=agent
            ).set(metrics.convergence_efficiency)
            self._metrics.halcon_final_utility.labels(
                agent_name=agent
            ).set(metrics.final_utility)
            self._metrics.halcon_adaptation_utilization.labels(
                agent_name=agent
            ).set(metrics.adaptation_utilization)
        except Exception as exc:  # pragma: no cover — prometheus errors must not halt the run
            logger.warning("Failed to push Halcon metrics to Prometheus: %s", exc)

    def load_session_history(self) -> List[HalconMetrics]:
        """
        Load all past sessions from the JSONL file.

        Returns:
            List of HalconMetrics, oldest first.
            Returns [] if the file doesn't exist or is empty.
        """
        if not self.sessions_path.exists():
            return []

        sessions = []
        line_number = 0
        try:
            for line in self.sessions_path.read_text(encoding="utf-8").splitlines():
                line_number += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    sessions.append(HalconMetrics.from_dict(data))
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning(
                        "HalconEmitter: skipping malformed line %d: %s",
                        line_number,
                        exc,
                    )
        except OSError as exc:
            logger.error("HalconEmitter: failed to read sessions: %s", exc)

        return sessions

    def load_session_history_raw(self) -> List[Dict[str, Any]]:
        """
        Load all past sessions as raw dicts (no deserialisation).
        Useful for schema-agnostic queries.
        """
        if not self.sessions_path.exists():
            return []
        sessions = []
        for line in self.sessions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    sessions.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return sessions

    def get_dominant_failure_mode(self) -> Optional[str]:
        """
        Returns the most common dominant_failure_mode across all stored sessions.

        This is used by the debugging agent and the Halcon retrospective system
        to identify systemic failure patterns that should be addressed.

        Returns:
            The most common failure mode string, or None if no sessions with
            a failure mode exist.
        """
        sessions = self.load_session_history_raw()
        mode_counts: Dict[str, int] = {}
        for session in sessions:
            mode = session.get("dominant_failure_mode")
            if mode:
                mode_counts[mode] = mode_counts.get(mode, 0) + 1

        if not mode_counts:
            return None
        return max(mode_counts, key=lambda k: mode_counts[k])

    def compute_aggregate_stats(self) -> Dict[str, Any]:
        """
        Compute aggregate statistics across all stored sessions.

        Returns a summary dict useful for monitoring dashboards and
        retrospective analysis.
        """
        sessions = self.load_session_history_raw()
        if not sessions:
            return {
                "session_count": 0,
                "avg_convergence_efficiency": None,
                "avg_final_utility": None,
                "avg_adaptation_utilization": None,
                "dominant_failure_mode": None,
            }

        def _mean(key: str) -> Optional[float]:
            values = [s[key] for s in sessions if key in s and s[key] is not None]
            return sum(values) / len(values) if values else None

        return {
            "session_count": len(sessions),
            "avg_convergence_efficiency": _mean("convergence_efficiency"),
            "avg_final_utility": _mean("final_utility"),
            "avg_adaptation_utilization": _mean("adaptation_utilization"),
            "avg_decision_density": _mean("decision_density"),
            "dominant_failure_mode": self.get_dominant_failure_mode(),
            "agents_recorded": list({s.get("agent_name") for s in sessions if s.get("agent_name")}),
        }

    def get_sessions_for_agent(self, agent_name: str) -> List[Dict[str, Any]]:
        """Return all sessions for a specific agent, in chronological order."""
        return [
            s for s in self.load_session_history_raw()
            if s.get("agent_name") == agent_name
        ]


# ---------------------------------------------------------------------------
# Module-level convenience instances
# ---------------------------------------------------------------------------

# Default singleton instances — import and use directly in agents
# These are lazy-initialised to avoid import-time Prometheus registration conflicts

_default_metrics: Optional[AgentMetrics] = None
_default_tracer: Optional[AgentTracer] = None
_default_emitter: Optional[HalconEmitter] = None


def get_agent_metrics() -> AgentMetrics:
    """Return the module-level AgentMetrics singleton."""
    global _default_metrics
    if _default_metrics is None:
        _default_metrics = AgentMetrics()
    return _default_metrics


def get_agent_tracer(service_name: str = "migration-agent-platform") -> AgentTracer:
    """Return the module-level AgentTracer singleton."""
    global _default_tracer
    if _default_tracer is None:
        _default_tracer = AgentTracer(service_name=service_name)
    return _default_tracer


def get_halcon_emitter(
    sessions_path: Optional[Path] = None,
) -> HalconEmitter:
    """Return the module-level HalconEmitter singleton."""
    global _default_emitter
    if _default_emitter is None:
        _default_emitter = HalconEmitter(
            sessions_path=sessions_path or Path(".halcon/retrospectives/sessions.jsonl"),
            agent_metrics=get_agent_metrics(),
        )
    return _default_emitter


# ---------------------------------------------------------------------------
# AgentRunContext — lightweight run context used by observe_agent_run
# ---------------------------------------------------------------------------


@dataclass
class AgentRunContext:
    """
    Context object that captures agent run metrics for Halcon emission.

    Created by observe_agent_run() and populated during the run via
    record_completion() or automatically on exception.
    """

    orchestration_id: str
    agent: str
    task: str
    total_iterations: int = 0
    tool_calls: int = 0
    wasted_rounds: int = 0
    final_utility_score: float = 0.0
    peak_utility_score: float = 0.0
    error_rate: float = 0.0
    success: bool = False
    error: Optional[str] = None
    duration_seconds: float = 0.0

    def record_completion(self, result: Any) -> None:
        """
        Populate this context from a completed agent result.

        Accepts any object with the following optional attributes:
        - error (str or None)
        - iterations (int)
        - tool_calls_made (list or int)
        - overall_score (float)
        """
        if result is None:
            return
        self.error = getattr(result, "error", None)
        self.success = self.error is None
        iterations = getattr(result, "iterations", None)
        if iterations is not None:
            self.total_iterations = int(iterations)
        tool_calls_made = getattr(result, "tool_calls_made", None)
        if tool_calls_made is not None:
            if isinstance(tool_calls_made, (list, tuple)):
                self.tool_calls = len(tool_calls_made)
            else:
                self.tool_calls = int(tool_calls_made)
        score = getattr(result, "overall_score", None)
        if score is not None:
            self.final_utility_score = float(score)
            self.peak_utility_score = float(score)

    def to_halcon_metrics(self, session_id: Optional[str] = None) -> HalconMetrics:
        """
        Convert this AgentRunContext into a HalconMetrics record.

        Computes derived Halcon fields:
        - convergence_efficiency = 1 - (wasted_rounds / total_iterations)
          clamped to [0.0, 1.0]; 1.0 when total_iterations == 0
        - decision_density = tool_calls / max(total_iterations, 1) normalised to [0,1]
        - adaptation_utilization = wasted_rounds / max(total_iterations, 1)
        - structural_instability_score = error_rate clamped to [0.0, 1.0]
        - dominant_failure_mode: derived from error string or 'none' on success
        - evidence_trajectory:
            'monotonic'    — successful run (success=True)
            'flat'         — failed run with 0 final score
            'non-monotonic'— failed run with non-zero partial score
        - inferred_problem_class: heuristic based on iteration/tool-call ratio
        """
        total = max(self.total_iterations, 1)

        convergence_efficiency = max(0.0, min(1.0, 1.0 - (self.wasted_rounds / total)))
        decision_density = max(0.0, min(1.0, self.tool_calls / (total * 10 + 1)))
        adaptation_utilization = max(0.0, min(1.0, self.wasted_rounds / total))
        structural_instability = max(0.0, min(1.0, self.error_rate))

        # dominant_failure_mode
        if self.success or not self.error:
            dominant_failure_mode = None
        elif "timeout" in (self.error or "").lower():
            dominant_failure_mode = "api_timeout"
        elif "connection" in (self.error or "").lower():
            dominant_failure_mode = "connection_error"
        elif "rate" in (self.error or "").lower() or "429" in (self.error or ""):
            dominant_failure_mode = "rate_limit"
        else:
            dominant_failure_mode = "tool_error"

        # evidence_trajectory
        if self.success:
            evidence_trajectory = "monotonic"
        elif self.final_utility_score == 0.0:
            evidence_trajectory = "flat"
        else:
            evidence_trajectory = "non-monotonic"

        # inferred_problem_class: heuristic
        if self.tool_calls == 0:
            inferred_problem_class = "deterministic-linear"
        elif self.wasted_rounds == 0:
            inferred_problem_class = "convergent"
        elif self.wasted_rounds > total * 0.5:
            inferred_problem_class = "exploratory-divergent"
        else:
            inferred_problem_class = "adaptive-iterative"

        return HalconMetrics(
            session_id=session_id or self.orchestration_id,
            agent_name=self.agent,
            convergence_efficiency=convergence_efficiency,
            decision_density=decision_density,
            adaptation_utilization=adaptation_utilization,
            final_utility=max(0.0, min(1.0, self.final_utility_score)),
            peak_utility=max(0.0, min(1.0, self.peak_utility_score)),
            total_iterations=self.total_iterations,
            useful_iterations=max(0, self.total_iterations - self.wasted_rounds),
            wasted_rounds=self.wasted_rounds,
            dominant_failure_mode=dominant_failure_mode,
            evidence_trajectory=evidence_trajectory,
            structural_instability_score=structural_instability,
            inferred_problem_class=inferred_problem_class,
            orchestration_id=self.orchestration_id,
        )


# ---------------------------------------------------------------------------
# HalconEmitter — overloaded to accept sessions_file str parameter
# ---------------------------------------------------------------------------

# Patch HalconEmitter.__init__ to also accept sessions_file (str) for test compatibility.
# The integration tests call: HalconEmitter(sessions_file=str(halcon_temp_file))
_orig_halcon_init = HalconEmitter.__init__


def _halcon_init_compat(
    self,
    sessions_path: Optional[Path] = None,
    agent_metrics: Optional[AgentMetrics] = None,
    sessions_file: Optional[str] = None,  # alternate name used in tests
) -> None:
    if sessions_file is not None and sessions_path is None:
        sessions_path = Path(sessions_file)
    _orig_halcon_init(self, sessions_path=sessions_path, agent_metrics=agent_metrics)


HalconEmitter.__init__ = _halcon_init_compat  # type: ignore[method-assign]


# Patch HalconEmitter.emit to accept AgentRunContext directly (used by integration tests)
_orig_halcon_emit = HalconEmitter.emit


async def _halcon_emit_compat(self, ctx_or_metrics: Any) -> None:  # type: ignore[override]
    """
    Accept either a HalconMetrics or an AgentRunContext.
    When an AgentRunContext is passed, convert it first.
    Also supports being called as a coroutine (tests use `await halcon_emitter.emit(ctx)`).
    """
    if isinstance(ctx_or_metrics, AgentRunContext):
        metrics = ctx_or_metrics.to_halcon_metrics()
    else:
        metrics = ctx_or_metrics
    _orig_halcon_emit(self, metrics)


HalconEmitter.emit = _halcon_emit_compat  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# observe_agent_run — async context manager
# ---------------------------------------------------------------------------


from contextlib import asynccontextmanager
import asyncio as _asyncio


@asynccontextmanager
async def observe_agent_run(
    agent: str,
    task: str,
    halcon_emitter: Optional[HalconEmitter] = None,
    orchestration_id: Optional[str] = None,
    agent_metrics: Optional[AgentMetrics] = None,
) -> Any:
    """
    Async context manager that observes an agent run and emits Halcon metrics.

    Emits Halcon metrics on both success and exception (failure metrics on error).

    Usage::

        async with observe_agent_run(
            agent="migration",
            task="Health check for run-001",
            halcon_emitter=emitter,
        ) as ctx:
            result = await agent.run(task)
            ctx.record_completion(result)

    The context yields an AgentRunContext. Call ctx.record_completion(result)
    after the agent finishes to populate metrics from the result object.
    On exception, failure metrics are automatically derived.
    """
    import uuid as _uuid

    orch_id = orchestration_id or f"observe-{_uuid.uuid4().hex[:12]}"
    ctx = AgentRunContext(
        orchestration_id=orch_id,
        agent=agent,
        task=task,
    )
    start = _asyncio.get_event_loop().time() if _asyncio.get_event_loop().is_running() else 0.0

    emitter = halcon_emitter or get_halcon_emitter()
    exc_to_raise = None

    try:
        yield ctx
    except Exception as exc:
        ctx.success = False
        ctx.error = str(exc)
        ctx.final_utility_score = 0.0
        ctx.peak_utility_score = 0.0
        exc_to_raise = exc
    finally:
        try:
            end = _asyncio.get_event_loop().time() if _asyncio.get_event_loop().is_running() else 0.0
            ctx.duration_seconds = end - start
        except Exception:
            pass
        try:
            await emitter.emit(ctx)
        except Exception as emit_err:
            logger.warning("observe_agent_run: failed to emit Halcon metrics: %s", emit_err)

    if exc_to_raise is not None:
        raise exc_to_raise
