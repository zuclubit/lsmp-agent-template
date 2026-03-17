---
name: documentation
description: Generates and updates technical documentation from code, schemas, and API specs
type: skill
version: 2.0.0
agent: documentation-agent
---

# Documentation Skill

**Version**: 2.0.0
**Agent**: documentation-agent
**Last Updated**: 2026-03

---

## Purpose

The `documentation` skill generates technical documentation artefacts from source code,
JSON schemas, and API specifications. It targets engineers who need accurate, up-to-date
runbooks, API references, architecture decision records, and agent specification documents.

All generated documentation is written to approved paths only. Source directories are
never modified.

---

## Supported Document Types

### `runbook`

Operational runbook for a specific failure scenario or maintenance procedure.

**Template structure**:
```
# Runbook: {title}
## Purpose
## Prerequisites
## When to Use This Runbook
## Step-by-Step Procedure
## Verification
## Rollback
## Escalation Path
## Related Alerts
## Last Updated
```

**Persisted under**: `monitoring/runbooks/` only
**Format**: GitHub-flavoured Markdown

---

### `api`

API reference document or OpenAPI 3.1.0 specification.

**Generated from**:
- Annotated Python source code (FastAPI/Pydantic models)
- Existing `schema.json` files
- Partial OpenAPI fragments

**Output**: OpenAPI 3.1.0 YAML with:
- `info`, `servers`, `paths`, `components/schemas`, `security`
- Request/response examples
- Error responses (400, 401, 403, 404, 422, 500)

**Persisted under**: `docs/api/`
**Format**: OpenAPI 3.1.0 YAML

---

### `architecture`

Architecture narrative describing a system component or integration pattern.

**Template structure**:
```
# Architecture: {component_name}
## Overview
## Responsibilities
## Design Decisions
## Component Interactions
## Data Flow
## Failure Modes
## Operational Considerations
## Related Documents
```

**Persisted under**: `docs/architecture/`
**Format**: GitHub-flavoured Markdown

---

### `adr`

Architecture Decision Record following the MADR (Markdown Any Decision Record) template.

**Template structure**:
```
# ADR-{number}: {decision_title}
## Status
## Context
## Decision
## Consequences
## Alternatives Considered
## References
```

**Persisted under**: `docs/adr/`
**Format**: GitHub-flavoured Markdown

---

### `agent_spec`

Agent specification document following the project's agent.md format.

**Template structure**:
```
# {Agent Name} Specification
## Purpose
## Input Schema
## Output Schema
## Tools
## Execution Workflow
## Environment Variables
## Example Invocations
## Limitations
```

**Persisted under**: `docs/agents/` or `agents/{agent-name}/` (if updating existing)
**Format**: GitHub-flavoured Markdown

---

## Path Restrictions

**Writes are permitted only under approved directories.**

| doc_type | Permitted output_path prefixes |
|----------|-------------------------------|
| `runbook` | `monitoring/runbooks/` |
| `api` | `docs/api/` |
| `architecture` | `docs/architecture/` |
| `adr` | `docs/adr/` |
| `agent_spec` | `docs/agents/`, `agents/` |

Rules enforced:
- `output_path` must start with one of the permitted prefixes for the doc_type
- Path traversal sequences (`../`) are rejected
- Absolute paths are rejected
- Writes to `src/`, `migration/`, `agents/*.py`, `tests/`, or any source code directory are rejected

---

## Input Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_type` | string | Yes | What the input represents: `code`, `schema`, `api`, `text` |
| `source_content` | string | Yes | Raw content to generate documentation from |
| `doc_type` | string | Yes | Documentation type: `runbook`, `api`, `architecture`, `adr`, `agent_spec` |
| `output_path` | string | Yes | Relative path from repository root. Must be within permitted prefix. |
| `title` | string | No | Document title (auto-derived from source if omitted) |

---

## Output Schema

| Field | Type | Description |
|-------|------|-------------|
| `content` | string | Generated documentation content |
| `word_count` | integer | Number of words in the content |
| `sections` | string[] | Top-level section headings in document order |
| `output_path` | string | Resolved output path (validated, normalised) |
| `format` | string | Output format: `markdown`, `yaml` |

---

## Example Invocations

### Runbook from Failure Scenario

```python
from agents.documentation_agent.agent import generate_docs

result = await generate_docs(
    source_type="text",
    source_content=(
        "The migration step net_timeout failure occurs when the database connection "
        "times out during extraction. Recovery requires increasing the timeout "
        "configuration and retrying the step."
    ),
    doc_type="runbook",
    output_path="monitoring/runbooks/net-timeout-recovery.md",
    title="Network Timeout Recovery",
)

print(f"Generated {result.word_count} words with sections: {result.sections}")
```

### API Reference from Schema

```python
result = await generate_docs(
    source_type="schema",
    source_content=open("agents/security-agent/schema.json").read(),
    doc_type="api",
    output_path="docs/api/security-agent.yaml",
    title="Security Agent API Reference",
)
```

### ADR from Decision Context

```python
result = await generate_docs(
    source_type="text",
    source_content=(
        "We need to decide whether to use file-backed checkpoints or API-backed "
        "checkpoints for execution agent idempotency. File-backed is simpler but "
        "not distributed. API-backed requires the migration API to be available."
    ),
    doc_type="adr",
    output_path="docs/adr/0003-execution-agent-checkpoint-strategy.md",
    title="Execution Agent Checkpoint Strategy",
)
```

---

## Quality Standards

Generated documentation must meet these standards:
- **Accuracy**: All field names, types, and values match the actual source code or schema
- **Completeness**: All required template sections are populated — no empty sections
- **No hallucination**: The generator never invents API fields, endpoints, or behaviours not present in the source
- **Code block formatting**: All code examples are in fenced code blocks with language specifier
- **Table formatting**: All tables use GitHub-flavoured Markdown table syntax

---

## When to Use

Use the documentation skill when:
- A new agent or skill is created and needs an agent.md specification
- A schema.json is updated and the API reference doc needs regenerating
- A post-mortem reveals a missing runbook that should be created
- An architectural decision is made and needs to be recorded as an ADR

Do NOT use the documentation skill when:
- Generating code (use code-generation skill)
- Generating test files (use testing skill)
- Updating code comments or docstrings (edit the source file directly)

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Model for documentation generation |
| `DOCS_AGENT_MAX_TOKENS` | `8192` | Max tokens for documentation response |
