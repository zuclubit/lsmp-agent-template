"""
Migration Orchestration Agent – powered by Anthropic Claude API.

This agent monitors active Salesforce migration runs, interprets errors and
anomalies, and takes autonomous corrective actions (pause, resize batches,
retry records, open incidents) using the Anthropic tool-calling API.

Architecture
------------
1. MigrationAgent.run(task)   – entry point for a single task
2. The agent loop:
   a. Send the user task + conversation history to Claude
   b. If Claude returns tool_use blocks → dispatch tools concurrently
   c. Append tool results → loop back to (a)
   d. Stop when Claude returns a final text response (no tool calls)
3. Structured output: decisions are logged and emitted as AgentDecisionEvents

Usage
-----
    agent = MigrationAgent()

    # Check on a running migration
    result = asyncio.run(agent.run(
        "Migration run abc-123 has a 15% error rate. "
        "Investigate and take appropriate action."
    ))
    print(result.final_answer)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from .tools import TOOL_SCHEMAS, dispatch_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")
MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "4096"))
MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "20"))
TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.1"))

_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "prompts", "system_prompt.md"
)


def _load_system_prompt() -> str:
    try:
        with open(_SYSTEM_PROMPT_PATH, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        logger.warning("system_prompt.md not found – using inline fallback")
        return (
            "You are an expert migration orchestration agent for Salesforce data migrations. "
            "Analyse migration runs, detect issues, and take corrective actions using the "
            "available tools. Always explain your reasoning before acting."
        )


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AgentResult:
    """Encapsulates the outcome of a single agent run."""

    task: str
    final_answer: str
    tool_calls_made: List[Dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None
    decided_actions: List[str] = field(default_factory=list)


@dataclass
class ToolCallRecord:
    tool_name: str
    tool_input: Dict[str, Any]
    tool_result: Any
    duration_ms: float
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class MigrationAgent:
    """
    Claude-powered agent that autonomously manages Salesforce migration runs.

    The agent uses an agentic loop with tool calling:
    - It can inspect migration status, error reports, SF limits, and system health
    - It can pause, resume, cancel, or retry migrations
    - It escalates via incident creation when human intervention is required
    - All decisions are logged with rationale for auditability

    Instantiation::

        agent = MigrationAgent(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model="claude-opus-4-5",
        )

    Running a task::

        result = await agent.run(
            "Run XYZ has 20% error rate. Investigate and remediate."
        )
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL,
        max_tokens: int = MAX_TOKENS,
        max_iterations: int = MAX_ITERATIONS,
        temperature: float = TEMPERATURE,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"]
        )
        self._model = model
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._temperature = temperature
        self._system_prompt = _load_system_prompt()
        self._tool_call_history: List[ToolCallRecord] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
    ) -> AgentResult:
        """
        Execute a migration management task using the Claude agent loop.

        Args:
            task:                  Natural-language description of what to investigate
                                   or accomplish.
            context:               Optional key-value context injected into the first
                                   user message (e.g. run_id, error_count).
            conversation_history:  Prior messages to prepend for multi-turn scenarios.

        Returns:
            :class:`AgentResult` with the final answer and audit trail.
        """
        start_ts = time.perf_counter()
        self._tool_call_history = []

        user_message = self._build_user_message(task, context)
        messages: List[Dict[str, Any]] = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})

        logger.info(
            "MigrationAgent starting task: %s (model=%s max_iter=%d)",
            task[:120],
            self._model,
            self._max_iterations,
        )

        final_answer = ""
        decided_actions: List[str] = []
        iteration = 0
        error: Optional[str] = None

        try:
            async for iteration, response in self._agent_loop(messages):
                # Collect any text responses as the running answer
                for block in response.content:
                    if block.type == "text" and block.text:
                        final_answer = block.text
                        actions = self._extract_decisions(block.text)
                        decided_actions.extend(actions)

        except anthropic.APIStatusError as exc:
            error = f"Anthropic API error {exc.status_code}: {exc.message}"
            logger.error("Agent API error: %s", error)
            final_answer = f"Agent encountered an API error: {error}"
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            logger.error("Agent unexpected error: %s", exc, exc_info=True)
            final_answer = f"Agent encountered an unexpected error: {error}"

        duration = time.perf_counter() - start_ts

        result = AgentResult(
            task=task,
            final_answer=final_answer,
            tool_calls_made=[
                {
                    "tool": r.tool_name,
                    "input": r.tool_input,
                    "result_summary": str(r.tool_result)[:300],
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                }
                for r in self._tool_call_history
            ],
            iterations=iteration,
            duration_seconds=round(duration, 2),
            error=error,
            decided_actions=decided_actions,
        )

        logger.info(
            "MigrationAgent completed: iterations=%d duration=%.2fs tools_called=%d",
            iteration,
            duration,
            len(self._tool_call_history),
        )
        return result

    # ------------------------------------------------------------------
    # Agent loop
    # ------------------------------------------------------------------

    async def _agent_loop(
        self, messages: List[Dict[str, Any]]
    ):
        """
        Core agentic loop: call Claude → handle tool use → repeat.

        Yields (iteration_number, response) for each Claude response.
        """
        for iteration in range(1, self._max_iterations + 1):
            logger.debug("Agent loop iteration %d", iteration)

            response = await self._call_claude(messages)

            # Always yield the response (even if it contains tool calls) so the
            # caller can extract text blocks
            yield iteration, response

            # Determine if we're done
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_use_blocks or response.stop_reason == "end_turn":
                logger.debug("Agent loop finished (no more tool calls)")
                break

            if response.stop_reason == "max_tokens":
                logger.warning("Claude hit max_tokens limit – stopping loop")
                break

            # Append assistant response to history
            messages.append({"role": "assistant", "content": response.content})

            # Execute all tool calls concurrently
            tool_results = await self._execute_tools_concurrently(tool_use_blocks)

            # Append tool results as user message
            messages.append({
                "role": "user",
                "content": tool_results,
            })

        else:
            logger.warning(
                "Agent loop hit max_iterations=%d without a final answer",
                self._max_iterations,
            )

    # ------------------------------------------------------------------
    # Claude API call
    # ------------------------------------------------------------------

    async def _call_claude(
        self, messages: List[Dict[str, Any]]
    ) -> anthropic.types.Message:
        """Send the current conversation to Claude and return the response."""
        return await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=self._system_prompt,
            tools=TOOL_SCHEMAS,
            messages=messages,
            temperature=self._temperature,
        )

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tools_concurrently(
        self, tool_use_blocks: List[Any]
    ) -> List[Dict[str, Any]]:
        """
        Execute all tool-use blocks concurrently and return formatted results.

        Each result is a ``tool_result`` content block consumed by the next
        Claude call.
        """
        tasks = [
            asyncio.create_task(self._execute_single_tool(block))
            for block in tool_use_blocks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        tool_result_blocks: List[Dict[str, Any]] = []
        for block, result in zip(tool_use_blocks, results):
            if isinstance(result, Exception):
                content = json.dumps({"error": str(result)})
                is_error = True
                logger.error("Tool %s raised: %s", block.name, result)
            else:
                content = json.dumps(result, default=str)
                is_error = False

            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
                "is_error": is_error,
            })

        return tool_result_blocks

    async def _execute_single_tool(self, block: Any) -> Any:
        """Execute one tool call and record the result."""
        tool_name = block.name
        tool_input = block.input or {}
        start_ts = time.perf_counter()
        error: Optional[str] = None
        result: Any = None

        try:
            result = await dispatch_tool(tool_name, tool_input)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            result = {"error": error}
            logger.error("Tool execution failed tool=%s: %s", tool_name, exc)

        duration_ms = (time.perf_counter() - start_ts) * 1000
        self._tool_call_history.append(
            ToolCallRecord(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_result=result,
                duration_ms=round(duration_ms, 1),
                error=error,
            )
        )
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_message(task: str, context: Optional[Dict[str, Any]]) -> str:
        if not context:
            return task
        ctx_lines = "\n".join(f"  {k}: {v}" for k, v in context.items())
        return f"{task}\n\nContext:\n{ctx_lines}"

    @staticmethod
    def _extract_decisions(text: str) -> List[str]:
        """Heuristically extract action decisions from agent text."""
        decisions = []
        keywords = ["I will", "I am going to", "I recommend", "Action:", "Decision:"]
        for line in text.splitlines():
            if any(line.strip().startswith(kw) for kw in keywords):
                decisions.append(line.strip())
        return decisions


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """
    Simple CLI for ad-hoc agent invocations.

    Usage:
        python -m agents.migration-agent.agent \
            "Run abc-123 has a 25% error rate on Account records. Investigate."
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    task = " ".join(sys.argv[1:]) or (
        "Perform a health check on all active migration runs and report any issues."
    )

    agent = MigrationAgent()
    result = await agent.run(task)

    print("\n" + "=" * 70)
    print("MIGRATION AGENT RESULT")
    print("=" * 70)
    print(f"Task:       {result.task}")
    print(f"Duration:   {result.duration_seconds}s")
    print(f"Iterations: {result.iterations}")
    print(f"Tools used: {len(result.tool_calls_made)}")
    if result.error:
        print(f"Error:      {result.error}")
    print("\nFINAL ANSWER:")
    print(result.final_answer)
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
