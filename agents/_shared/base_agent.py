"""Abstract base agent with circuit breaker, agentic loop, and Halcon metrics."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

import anthropic

from agents._shared.schemas import (
    AgentInput,
    AgentResult,
    AgentRole,
    HalconMetrics,
    RequestContext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Circuit-breaker configuration
# ---------------------------------------------------------------------------

CIRCUIT_BREAKER_THRESHOLD: int = 3
"""Number of consecutive failures before the circuit opens."""

_CIRCUIT_BREAKER_RESET_SECONDS: float = 60.0
"""Seconds after which an open circuit transitions to HALF_OPEN."""


# ---------------------------------------------------------------------------
# Circuit-breaker implementation
# ---------------------------------------------------------------------------


class CircuitBreakerState(str, Enum):
    """Operating states of the circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(RuntimeError):
    """Raised when a call is attempted while the circuit is OPEN."""

    def __init__(self, agent_role: str, remaining_seconds: float) -> None:
        super().__init__(
            f"Circuit breaker for agent '{agent_role}' is OPEN. "
            f"Retry in {remaining_seconds:.1f}s."
        )
        self.agent_role = agent_role
        self.remaining_seconds = remaining_seconds


class CircuitBreaker:
    """Circuit breaker with automatic half-open probe after a reset timeout.

    State transitions:
        CLOSED    -> OPEN      when consecutive_failures >= threshold
        OPEN      -> HALF_OPEN when reset_seconds have elapsed since last failure
        HALF_OPEN -> CLOSED    on a successful call
        HALF_OPEN -> OPEN      on a failed call
    """

    def __init__(
        self,
        threshold: int = CIRCUIT_BREAKER_THRESHOLD,
        reset_seconds: float = _CIRCUIT_BREAKER_RESET_SECONDS,
        name: str = "unnamed",
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self._threshold = threshold
        self._reset_seconds = reset_seconds
        self._name = name
        self._state: CircuitBreakerState = CircuitBreakerState.CLOSED
        self._consecutive_failures: int = 0
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> CircuitBreakerState:
        self._maybe_transition_to_half_open()
        return self._state

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitBreakerState.CLOSED
        logger.debug("CircuitBreaker[%s] success recorded -> CLOSED", self._name)

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        self._last_failure_time = time.monotonic()
        if self._consecutive_failures >= self._threshold:
            self._state = CircuitBreakerState.OPEN
            logger.warning(
                "CircuitBreaker[%s] opened after %d consecutive failures.",
                self._name,
                self._consecutive_failures,
            )

    def check(self) -> None:
        """Raise CircuitBreakerOpenError if the circuit is currently OPEN."""
        self._maybe_transition_to_half_open()
        if self._state is CircuitBreakerState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            remaining = max(0.0, self._reset_seconds - elapsed)
            raise CircuitBreakerOpenError(self._name, remaining)

    def _maybe_transition_to_half_open(self) -> None:
        if self._state is CircuitBreakerState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._reset_seconds:
                self._state = CircuitBreakerState.HALF_OPEN
                logger.info(
                    "CircuitBreaker[%s] reset timeout elapsed -> HALF_OPEN", self._name
                )


# ---------------------------------------------------------------------------
# Abstract base agent
# ---------------------------------------------------------------------------


class BaseAgent(ABC):
    """Abstract base class for all agents in the s-agent system.

    Subclasses must implement ``execute()``.  The class provides:
    - A shared Anthropic client (``self._client``)
    - A per-instance circuit breaker (``self._circuit_breaker``)
    - An agentic tool-loop helper (``_run_tool_loop``)
    - Halcon metrics computation (``_emit_halcon_metrics``)
    - Request context validation (``_validate_request_context``)
    """

    def __init__(
        self,
        role: AgentRole,
        model: str = "claude-sonnet-4-5",
        max_tokens: int = 8192,
        max_iterations: int = 10,
    ) -> None:
        """Initialise the base agent.

        Args:
            role:           The functional role this agent fulfils.
            model:          Anthropic model identifier to use for LLM calls.
            max_tokens:     Maximum tokens per individual LLM response.
            max_iterations: Hard cap on agentic loop iterations.
        """
        self._role = role
        self._model = model
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._client: anthropic.Anthropic = anthropic.Anthropic()
        self._circuit_breaker: CircuitBreaker = CircuitBreaker(
            threshold=CIRCUIT_BREAKER_THRESHOLD,
            name=role.value,
        )
        # Tracks tool-call count across the most recent _run_tool_loop call.
        self._loop_tool_call_count: int = 0
        logger.debug(
            "BaseAgent initialised: role=%s model=%s max_tokens=%d max_iterations=%d",
            role.value, model, max_tokens, max_iterations,
        )

    # ------------------------------------------------------------------
    # Abstract interface — subclasses must implement
    # ------------------------------------------------------------------

    @abstractmethod
    async def execute(self, input: AgentInput) -> AgentResult:  # noqa: A002
        """Execute the agent's primary task.

        Args:
            input: Fully validated agent input payload.

        Returns:
            AgentResult describing success/failure, output, and metrics.
        """

    # ------------------------------------------------------------------
    # Protected helpers
    # ------------------------------------------------------------------

    async def _run_tool_loop(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]],
    ) -> tuple[str, int]:
        """Run the Anthropic agentic tool-use loop.

        Iterates until the model produces a final text response or
        ``max_iterations`` is reached.  The circuit breaker is checked before
        every API call; failures are recorded automatically.

        Args:
            system_prompt: Static system context for the model.
            user_message:  Initial user-turn message.
            tools:         Tool definitions in Anthropic schema format.
                           Pass an empty list to disable tool use entirely.

        Returns:
            ``(final_text, total_tokens)`` where final_text is the model's
            last plain-text response and total_tokens is the cumulative count.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN at call time.
            RuntimeError:            If the iteration cap is exceeded.
            anthropic.APIError:      Re-raised after recording the failure.
        """
        self._circuit_breaker.check()
        self._loop_tool_call_count = 0
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        total_tokens: int = 0

        for iteration in range(1, self._max_iterations + 1):
            logger.debug(
                "Agent[%s] tool-loop iteration %d/%d",
                self._role.value, iteration, self._max_iterations,
            )
            try:
                response = await asyncio.to_thread(
                    self._client.messages.create,
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=system_prompt,
                    messages=messages,
                    **({"tools": tools} if tools else {}),
                )
            except anthropic.APIError as exc:
                self._circuit_breaker.record_failure()
                logger.error(
                    "Agent[%s] Anthropic API error on iteration %d: %s",
                    self._role.value, iteration, exc,
                )
                raise

            total_tokens += response.usage.input_tokens + response.usage.output_tokens
            tool_use_blocks: list[anthropic.types.ToolUseBlock] = []
            text_blocks: list[str] = []
            for block in response.content:
                if isinstance(block, anthropic.types.TextBlock):
                    text_blocks.append(block.text)
                elif isinstance(block, anthropic.types.ToolUseBlock):
                    tool_use_blocks.append(block)

            # No tool calls remaining — the model has finished.
            if not tool_use_blocks or response.stop_reason == "end_turn":
                final_text = " ".join(text_blocks).strip()
                self._circuit_breaker.record_success()
                logger.debug(
                    "Agent[%s] tool-loop complete after %d iteration(s), %d tokens",
                    self._role.value, iteration, total_tokens,
                )
                return final_text, total_tokens

            messages.append({"role": "assistant", "content": response.content})
            self._loop_tool_call_count += len(tool_use_blocks)
            tool_results: list[dict[str, Any]] = []
            for tool_block in tool_use_blocks:
                logger.debug(
                    "Agent[%s] executing tool '%s' (id=%s)",
                    self._role.value, tool_block.name, tool_block.id,
                )
                try:
                    result = await self._dispatch_tool(tool_block.name, tool_block.input)
                    content = str(result)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Agent[%s] tool '%s' raised %s: %s",
                        self._role.value, tool_block.name, type(exc).__name__, exc,
                    )
                    content = f"Tool error: {type(exc).__name__}: {exc}"
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tool_block.id, "content": content}
                )
            messages.append({"role": "user", "content": tool_results})

        # Iteration cap exceeded without a terminal response.
        self._circuit_breaker.record_failure()
        raise RuntimeError(
            f"Agent[{self._role.value}] exceeded max_iterations={self._max_iterations} "
            "without producing a final text response."
        )

    async def _dispatch_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> Any:
        """Dispatch a tool call by name. Override in subclasses.

        Args:
            tool_name:  The tool name as returned by the model.
            tool_input: Parsed JSON input for the tool.

        Returns:
            The tool result (coerced to ``str`` by the caller).
        """
        raise NotImplementedError(
            f"Agent[{self._role.value}] has no handler for tool '{tool_name}'. "
            "Override _dispatch_tool() in the subclass."
        )

    def _emit_halcon_metrics(
        self,
        session_id: str,
        result: AgentResult,
        tool_call_count: int = 0,
        available_tool_count: int = 0,
        evidence_trajectory: list[str] | None = None,
    ) -> HalconMetrics:
        """Compute and return Halcon observability metrics for a completed execution.

        Metric definitions:
        - ``convergence_efficiency``: estimated output / total tokens.
        - ``decision_density``: tool calls per 1 000 tokens consumed.
        - ``adaptation_utilization``: fraction of offered tools actually invoked.
        - ``final_utility``: harmonic mean of convergence_efficiency and success.

        If ``tool_call_count`` is omitted, the count accumulated during the most
        recent ``_run_tool_loop`` call is used automatically.

        Args:
            session_id:             Unique identifier for this execution session.
            result:                 The completed AgentResult.
            tool_call_count:        Total tool-use blocks dispatched (0 = auto).
            available_tool_count:   Number of tools offered to the model.
            evidence_trajectory:    Chronological key decision / evidence events.

        Returns:
            A populated HalconMetrics instance.
        """
        effective_tool_calls = tool_call_count or self._loop_tool_call_count
        tokens = max(result.tokens_used, 1)
        _output_ratio = 0.30
        convergence_efficiency = min(
            max(_output_ratio if result.success else _output_ratio * 0.5, 0.0), 1.0
        )
        decision_density = (effective_tool_calls / tokens) * 1_000
        if available_tool_count > 0:
            adaptation_utilization = (
                min(effective_tool_calls, available_tool_count) / available_tool_count
            )
        else:
            adaptation_utilization = 0.0
        dominant_failure_mode: str | None = None
        if result.error:
            el = result.error.lower()
            if "timeout" in el:
                dominant_failure_mode = "timeout"
            elif "token" in el:
                dominant_failure_mode = "token_limit"
            elif "circuit" in el:
                dominant_failure_mode = "circuit_breaker"
            elif "validation" in el:
                dominant_failure_mode = "validation_error"
            else:
                dominant_failure_mode = "unknown"
        success_score = 1.0 if result.success else 0.0
        if convergence_efficiency + success_score > 0:
            final_utility = (2.0 * convergence_efficiency * success_score) / (
                convergence_efficiency + success_score
            )
        else:
            final_utility = 0.0
        metrics = HalconMetrics(
            session_id=session_id,
            convergence_efficiency=round(convergence_efficiency, 4),
            decision_density=round(decision_density, 4),
            adaptation_utilization=round(adaptation_utilization, 4),
            dominant_failure_mode=dominant_failure_mode,
            evidence_trajectory=evidence_trajectory or [],
            final_utility=round(final_utility, 4),
        )
        logger.info(
            "HalconMetrics[%s] session=%s convergence=%.3f density=%.3f utility=%.3f",
            self._role.value, session_id,
            metrics.convergence_efficiency, metrics.decision_density, metrics.final_utility,
        )
        return metrics

    def _validate_request_context(self, ctx: RequestContext) -> None:
        """Validate that the request context carries the required identifiers.

        This method should be called at the start of every ``execute()``
        implementation to fail fast on malformed inputs before incurring LLM
        costs.

        Args:
            ctx: The RequestContext to validate.

        Raises:
            ValueError: If ``tenant_id`` or ``job_id`` is empty or whitespace-only.
        """
        if not ctx.tenant_id or not ctx.tenant_id.strip():
            raise ValueError(
                f"RequestContext.tenant_id must not be empty (request_id={ctx.request_id})."
            )
        if not ctx.job_id or not ctx.job_id.strip():
            raise ValueError(
                f"RequestContext.job_id must not be empty (request_id={ctx.request_id})."
            )
        logger.debug(
            "RequestContext validated: tenant=%s job=%s request=%s",
            ctx.tenant_id, ctx.job_id, ctx.request_id,
        )
