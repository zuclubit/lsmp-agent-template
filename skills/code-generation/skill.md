---
name: code-generation
description: Generates production-ready Python, Apex, and transformation rule code for migration pipeline components
type: skill
version: 2.0.0
agent: code-generation-agent
---

# Code Generation Skill

**Version**: 2.0.0
**Agent**: code-generation-agent
**Last Updated**: 2026-03

---

## Purpose

The `code-generation` skill produces production-ready code for the migration platform. It
accepts a plain-language task description and optional context files, then emits syntactically
valid, fully typed, and immediately usable source code.

Supported output types:
1. **Python** — migration adapters, transformers, validators, tools, and pipeline utilities
2. **Apex** — Salesforce Apex classes, triggers, and batch jobs
3. **Transformation rules** — YAML/JSON field mapping rules consumed by the transformation engine
4. **Migration scripts** — SQL extraction scripts for legacy source systems

---

## Output Type Details

### Python Code

- Full PEP 484 type annotations on all public functions, methods, and class attributes
- Pydantic v2 models for data validation
- `asyncio` and `httpx.AsyncClient` for I/O-bound operations
- Exponential backoff retry using `_http_request_with_retry` pattern
- No `exec`, `eval`, `compile`, or dynamic code execution
- No `subprocess` with `shell=True`
- No hardcoded secrets or credentials
- File writes scoped to `/var/data/migration/` and `/tmp/migration-work/` only

### Apex Code

- Follows Salesforce Apex best practices (bulkification, governor limit awareness)
- All DML inside `try/catch` blocks with rollback on error
- `@AuraEnabled` methods for LWC integration
- SOQL queries with bind variables — no dynamic SOQL with string concatenation
- Test classes included with `@isTest` annotation and `Test.startTest()` / `Test.stopTest()`
- Minimum 75% code coverage requirement

### Transformation Rules

- YAML format, one mapping per rule
- Each rule specifies: `source_field`, `target_field`, `transform_fn`, `null_handling`, `validation_regex`
- Supports: direct mapping, value lookup, concatenation, conditional mapping, regex extract
- Null handling options: `preserve_null`, `default_value`, `skip_record`, `raise_error`

### Migration Scripts (SQL)

- Standard SQL compatible with: PostgreSQL, Oracle, SQL Server, MySQL
- Parameterised queries — no string interpolation of user input
- Read-only: SELECT statements only, no DML
- Include EXPLAIN plan hints for large extracts (> 100k rows)
- Batch size recommendations based on estimated row counts

---

## Code Quality Guardrails

All generated code is validated against these guardrails before output is returned.
Violations cause the generation to fail with an error — no partial output is returned.

| Guardrail | Description |
|-----------|-------------|
| No `exec`/`eval` | Dynamic code execution is never generated |
| No shell injection | `subprocess(shell=True)` and `os.system()` are never generated |
| No hardcoded secrets | All credentials must come from environment variables or secrets manager |
| Type hints required | All public Python functions must have PEP 484 annotations |
| No unauthorised paths | File writes scoped to approved paths only |
| Apex bulkification | Apex code must operate on collections, not single records |
| SOQL injection-safe | Apex SOQL must use bind variables, not string concatenation |

---

## Input Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_description` | string | Yes | Plain-language description of the code to generate |
| `output_type` | enum | Yes | `python`, `apex`, `transformation_rules`, `migration_script` |
| `target_module` | string | Conditional | Dotted module path (required for Python, e.g. `migration.adapters.salesforce`) |
| `apex_class_name` | string | Conditional | Apex class name (required for Apex output) |
| `context_files` | string[] | No | Paths to existing files the generator should use as context |
| `entity_names` | string[] | No | Entity names relevant to the generated code |

---

## Output Schema

| Field | Type | Description |
|-------|------|-------------|
| `generated_code` | string | Complete, syntactically valid source code |
| `file_path` | string | Recommended file path relative to repository root |
| `language` | string | Language of the generated code |
| `imports` | string[] | Import statements required (Python only) |
| `test_suggestions` | string[] | Test case suggestions for adequate coverage |
| `guardrail_violations` | string[] | Any guardrail violations found (non-empty means generation failed) |

---

## Example Invocations

### Python Adapter

```python
from agents.code_generation_agent.agent import generate_code

result = await generate_code(
    task_description=(
        "Implement a retry-aware Salesforce Bulk API v2 job poller that polls "
        "every 30 seconds, raises SalesforceJobFailedError on terminal failure, "
        "and returns a BulkJobResult dataclass on success."
    ),
    output_type="python",
    target_module="migration.adapters.salesforce.bulk_poller",
    context_files=[
        "migration/adapters/salesforce/models.py",
        "migration/adapters/salesforce/errors.py",
    ],
)

if not result.guardrail_violations:
    with open(result.file_path, "w") as f:
        f.write(result.generated_code)
```

### Apex Batch Class

```python
result = await generate_code(
    task_description=(
        "Apex Batch class that queries Account records migrated in the last 24 hours, "
        "validates that all required custom fields are populated, "
        "and sends a Platform Event for each invalid record."
    ),
    output_type="apex",
    apex_class_name="MigrationAccountValidationBatch",
    entity_names=["Account"],
)
```

### Transformation Rules

```python
result = await generate_code(
    task_description=(
        "Generate field mapping rules to transform Oracle EBS customer records "
        "to Salesforce Account objects. Source fields: PARTY_NAME, EMAIL_ADDRESS, "
        "PHONE_NUMBER, STATUS. Target fields: Name, Email__c, Phone, Active__c."
    ),
    output_type="transformation_rules",
    entity_names=["Account"],
)
```

---

## When to Use

Use the code-generation skill when:
- Building new migration adapters for source systems
- Generating Apex validation or post-migration processing code
- Creating field mapping transformation rules from schema documentation
- Producing extraction SQL scripts for legacy databases

Do NOT use the code-generation skill when:
- Modifying existing production code without review (generate to a staging path, review first)
- Generating code that will run with elevated privileges
- Generating authentication or encryption primitives (use established libraries)

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Model for code generation |
| `CODE_GEN_MAX_TOKENS` | `8192` | Max tokens for generated code response |
