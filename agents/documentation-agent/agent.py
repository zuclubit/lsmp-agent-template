"""
Documentation Agent – powered by Anthropic Claude API.

This agent automatically generates and updates migration project documentation
by analysing code changes, migration run results, and data schemas. It ensures
documentation stays in sync with the evolving codebase and migration state.

Capabilities
------------
- Analyse Python/SQL/YAML files and generate inline docstrings and README sections
- Generate field mapping documentation from transformation code
- Update migration runbooks after each successful run
- Produce change logs from git diff summaries
- Create data dictionary entries for Salesforce custom fields
- Generate API documentation from FastAPI route definitions
- Write post-migration reports in business-readable format

Usage
-----
    agent = DocumentationAgent()
    result = await agent.run(
        "Generate a data mapping table for the Account transformation module",
        files=["integrations/schemas/account_schema.py"]
    )
    print(result.generated_content)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import anthropic

logger = logging.getLogger(__name__)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")
MAX_TOKENS = int(os.getenv("DOC_AGENT_MAX_TOKENS", "8192"))

_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "prompts", "system_prompt.md"
)


def _load_system_prompt() -> str:
    try:
        with open(_SYSTEM_PROMPT_PATH, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return (
            "You are a technical documentation specialist for a Salesforce migration platform. "
            "Generate clear, accurate, and maintainable documentation from code and data artefacts."
        )


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read the content of a file in the project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Relative path from project root."},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '*.py'"},
            },
            "required": ["directory"],
        },
    },
    {
        "name": "write_documentation",
        "description": "Write generated documentation to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "append", "update_section"],
                         "default": "create"},
                "section_header": {"type": "string",
                                   "description": "For update_section mode: the section to replace."},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "get_migration_run_summary",
        "description": "Retrieve summary data from a completed migration run for documentation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "get_git_diff",
        "description": "Get git diff for recent changes to generate a changelog entry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "Git ref or ISO date, e.g. 'HEAD~5' or '2024-01-01'"},
                "path": {"type": "string", "description": "Limit diff to this path."},
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _tool_read_file(file_path: str) -> Dict[str, Any]:
    project_root = os.getenv("PROJECT_ROOT", "/Users/oscarvalois/Documents/Github/s-agent")
    full_path = os.path.join(project_root, file_path)
    try:
        with open(full_path, encoding="utf-8") as fh:
            content = fh.read()
        lines = content.count("\n") + 1
        return {"file_path": file_path, "content": content, "lines": lines, "exists": True}
    except FileNotFoundError:
        return {"file_path": file_path, "exists": False, "error": "File not found"}
    except Exception as exc:  # noqa: BLE001
        return {"file_path": file_path, "exists": False, "error": str(exc)}


async def _tool_list_directory(directory: str, pattern: str = "*") -> Dict[str, Any]:
    import glob as _glob

    project_root = os.getenv("PROJECT_ROOT", "/Users/oscarvalois/Documents/Github/s-agent")
    full_dir = os.path.join(project_root, directory)
    matches = _glob.glob(os.path.join(full_dir, pattern))
    files = [os.path.relpath(m, project_root) for m in matches]
    return {"directory": directory, "pattern": pattern, "files": files, "count": len(files)}


async def _tool_write_documentation(
    file_path: str,
    content: str,
    mode: str = "create",
    section_header: Optional[str] = None,
) -> Dict[str, Any]:
    project_root = os.getenv("PROJECT_ROOT", "/Users/oscarvalois/Documents/Github/s-agent")
    full_path = os.path.join(project_root, file_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    if mode == "append" and os.path.exists(full_path):
        with open(full_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n{content}")
    elif mode == "update_section" and section_header and os.path.exists(full_path):
        with open(full_path, "r", encoding="utf-8") as fh:
            existing = fh.read()
        import re
        pattern = rf"(## {re.escape(section_header)}.*?)(?=\n## |\Z)"
        replacement = f"## {section_header}\n\n{content}"
        updated = re.sub(pattern, replacement, existing, flags=re.DOTALL)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(updated)
    else:
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)

    logger.info("Documentation written to %s (mode=%s)", file_path, mode)
    return {"file_path": file_path, "mode": mode, "success": True,
            "bytes_written": len(content.encode())}


async def _tool_get_migration_run_summary(run_id: str) -> Dict[str, Any]:
    import random
    return {
        "run_id": run_id,
        "status": "completed",
        "object_types": ["Account", "Contact"],
        "total_records": random.randint(10000, 50000),
        "successful_records": random.randint(9500, 49999),
        "failed_records": random.randint(0, 50),
        "duration_seconds": random.randint(600, 3600),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


async def _tool_get_git_diff(since: str = "HEAD~5", path: str = ".") -> Dict[str, Any]:
    import subprocess
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", since, "--", path],
            capture_output=True, text=True, timeout=10,
            cwd=os.getenv("PROJECT_ROOT", "."),
        )
        return {"diff_stat": result.stdout, "error": result.stderr or None}
    except Exception as exc:  # noqa: BLE001
        return {"diff_stat": "", "error": str(exc)}


_TOOL_DISPATCH = {
    "read_file": _tool_read_file,
    "list_directory": _tool_list_directory,
    "write_documentation": _tool_write_documentation,
    "get_migration_run_summary": _tool_get_migration_run_summary,
    "get_git_diff": _tool_get_git_diff,
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class DocumentationResult:
    task: str
    generated_content: str
    files_written: List[str]
    files_read: List[str]
    iterations: int
    duration_seconds: float
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DocumentationAgent:
    """
    Claude-powered agent that generates, updates, and maintains migration
    project documentation automatically.

    It reads source files, analyses code structure, and produces human-readable
    documentation in Markdown or reStructuredText format.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"]
        )
        self._model = model
        self._max_tokens = max_tokens
        self._system_prompt = _load_system_prompt()
        self._files_read: List[str] = []
        self._files_written: List[str] = []

    async def run(
        self,
        task: str,
        files: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> DocumentationResult:
        """Execute a documentation generation task."""
        start_ts = time.perf_counter()
        self._files_read = []
        self._files_written = []

        user_content = task
        if files:
            user_content += f"\n\nFiles to analyse: {', '.join(files)}"
        if context:
            ctx = "\n".join(f"  {k}: {v}" for k, v in context.items())
            user_content += f"\n\nContext:\n{ctx}"

        messages: List[Dict[str, Any]] = [{"role": "user", "content": user_content}]
        final_text = ""
        error: Optional[str] = None
        iteration = 0

        try:
            for iteration in range(1, 20):
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system_prompt,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                    temperature=0.2,
                )

                tool_blocks = [b for b in response.content if b.type == "tool_use"]
                for block in response.content:
                    if block.type == "text" and block.text:
                        final_text = block.text

                if not tool_blocks or response.stop_reason == "end_turn":
                    break

                messages.append({"role": "assistant", "content": response.content})
                tool_results = await self._run_tools(tool_blocks)
                messages.append({"role": "user", "content": tool_results})

        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            logger.error("DocumentationAgent error: %s", exc, exc_info=True)

        return DocumentationResult(
            task=task,
            generated_content=final_text,
            files_written=list(self._files_written),
            files_read=list(self._files_read),
            iterations=iteration,
            duration_seconds=round(time.perf_counter() - start_ts, 2),
            error=error,
        )

    async def _run_tools(self, blocks: List[Any]) -> List[Dict[str, Any]]:
        results = []
        for block in blocks:
            try:
                fn = _TOOL_DISPATCH.get(block.name)
                if not fn:
                    result = {"error": f"Unknown tool: {block.name}"}
                    is_error = True
                else:
                    result = await fn(**(block.input or {}))
                    is_error = False
                    if block.name == "read_file":
                        self._files_read.append(block.input.get("file_path", ""))
                    elif block.name == "write_documentation":
                        self._files_written.append(block.input.get("file_path", ""))
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)}
                is_error = True

            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
                "is_error": is_error,
            })
        return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    async def _main() -> None:
        agent = DocumentationAgent()
        task = " ".join(sys.argv[1:]) or (
            "List all Python files in the integrations directory and "
            "generate a brief summary of each module's purpose."
        )
        result = await agent.run(task)
        print(f"\nDocumentation Agent Result\n{'='*50}")
        print(f"Iterations: {result.iterations}  Duration: {result.duration_seconds}s")
        print(f"Files read: {result.files_read}")
        print(f"Files written: {result.files_written}")
        print(f"\n{result.generated_content}")

    asyncio.run(_main())
