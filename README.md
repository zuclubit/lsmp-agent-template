# Legacy-to-Salesforce Migration Platform (LSMP)

**Enterprise AI Agent System — FedRAMP High | FISMA Moderate | SOX | GDPR**

> Autonomous AI agents that migrate 4.2M customer records, 18M case records, and 1.1M opportunity records from Oracle Siebel 8.1, SAP CRM 7.0, and PostgreSQL into Salesforce Government Cloud+. Powered by Claude claude-opus-4-6 / claude-sonnet-4-6. RPO = 0.

---

## Key Features

- 6 specialized autonomous agents (orchestrator, planning, validation, security, execution, debugging)
- Three validation gates with real DB/Salesforce API checks — no stubs, no mocks in production
- Zero-stub data policy — all validation uses live data sources
- BlockingGate protocol — a FAIL result from any gate halts the entire pipeline
- Halcon retrospective metrics emitted after every agent session
- Multi-tenant isolation via per-tenant HashiCorp Vault credentials and Kafka topics
- FedRAMP High, FISMA Moderate, SOX, and GDPR compliance controls built in
- SPIFFE/SPIRE mTLS workload identity for all inter-service communication
- OpenTelemetry distributed tracing across all agents and tools
- Circuit breaker on every external API call (Salesforce, Vault, migration control-plane)

---

## Architecture

### Agent Dependency Graph

```
orchestrator-agent (claude-opus-4-6)
  ├── planning-agent    (claude-sonnet-4-6)
  ├── validation-agent  (claude-sonnet-4-6)  <-- BLOCKS execution if FAIL
  ├── security-agent    (claude-sonnet-4-6)  <-- BLOCKS if risk_score > 0.7
  ├── execution-agent   (claude-sonnet-4-6)  <-- only runs with ALLOW gate
  └── debugging-agent   (claude-sonnet-4-6)  <-- read-only, invoked on failure
```

### ETL Pipeline

```
Legacy CRM Sources
  ├── Oracle Siebel 8.1
  ├── SAP CRM 7.0
  └── PostgreSQL
        │
        ▼
   Kafka Topics (per-tenant, mTLS)
        │
        ▼
   Transform Layer  (migration/data_transformations/)
        │
        ▼
   Validation Gate  (validation-agent — real SF API checks)
        │
        ▼
   Security Gate    (security-agent — OWASP + secrets + CVE)
        │
        ▼
   Salesforce Bulk API 2.0
   (Government Cloud+)
```

---

## Directory Structure

```
s-agent/
├── agents/                    # All AI agent implementations
│   ├── orchestrator-agent/    # Supervisor — routes tasks, synthesises results
│   ├── planning-agent/        # Migration plan generation and scheduling
│   ├── validation-agent/      # Data quality gating (BlockingGate)
│   ├── security-agent/        # OWASP scanning, secrets detection, CVE checks
│   ├── execution-agent/       # Migration run lifecycle control
│   ├── debugging-agent/       # Read-only failure investigation
│   └── _shared/               # Shared schemas, base classes, utilities
├── skills/                    # Reusable tool/skill implementations
├── context-servers/           # MCP context server adapters
├── tools/                     # Low-level API wrappers (SF, Vault, Kafka)
├── migration/                 # ETL pipeline: extractors, transformers, loaders
│   ├── legacy_extractors/     # Source system connectors
│   └── data_transformations/  # Field mapping and transformation logic
├── halcon/                    # Halcon observability SDK integration
├── config/                    # agents.yaml, thresholds, model config
├── security/                  # RBAC, audit logging, encryption, compliance
│   ├── policies/              # security_policy.md, data_classification.md
│   ├── rbac/                  # OPA roles.yaml, rbac_config.py
│   ├── audit/                 # HMAC-chained audit logger (Splunk sink)
│   ├── secrets/               # Vault integration (secrets_manager.py)
│   ├── encryption/            # AES-256-GCM encryption service
│   └── compliance/            # FedRAMP, FISMA, SOX, GDPR controls
├── monitoring/                # Prometheus metrics, OTel traces, Halcon emitter
├── tests/                     # Full test suite
│   ├── agent-tests/           # Unit tests per agent (mocked Claude API)
│   ├── skill-tests/           # Individual tool/skill tests
│   ├── integration-tests/     # Full pipeline + Halcon integration
│   ├── failure-scenarios/     # Adversarial and tool failure tests
│   └── fixtures/              # Shared test fixtures (no real PII)
├── docs/                      # Auto-generated runbooks, field maps, reports
├── architecture/              # ADRs, diagrams, design documents
├── ci-cd/                     # GitHub Actions workflows, deployment manifests
└── infrastructure/            # Kubernetes, Terraform, Helm charts
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- `ANTHROPIC_API_KEY` with access to Claude claude-opus-4-6 / claude-sonnet-4-6
- Running migration control-plane (or use `MIGRATION_API_BASE_URL=http://localhost:8000`)

```bash
# Install dependencies
pip install -r agents/requirements.txt

# Configure environment
cp .env.example .env
export ANTHROPIC_API_KEY=your-key-here
export MIGRATION_API_BASE_URL=http://localhost:8000
export SF_INSTANCE_URL=https://your-org.my.salesforce.com
export VAULT_ADDR=https://vault.internal:8200

# Run the full orchestrated pipeline
python -m agents.orchestrator-agent.agent "Migrate tenant ACME from legacy CRM"

# Run validation only
python -m agents.validation-agent.agent --job-id JOB_ID --stages Account Contact

# Run security audit on a target directory
python -m agents.security-agent.agent --target integrations/

# Run all tests
pytest tests/ -v --timeout=60

# Run only fast unit tests (no I/O, no network)
pytest tests/ -m unit -v

# Run agent-specific tests
pytest tests/agent-tests/ -v

# Run integration tests (requires ANTHROPIC_API_KEY)
pytest tests/integration-tests/ -v -m integration
```

---

## Agent System

| Agent | Model | Purpose | Blocks Pipeline On |
|---|---|---|---|
| `orchestrator-agent` | claude-opus-4-6 | Decomposes tasks, delegates to specialists, synthesises results | Any specialist returns error or BLOCKED |
| `planning-agent` | claude-sonnet-4-6 | Generates migration execution plans, schedules batches | Plan validation failure |
| `validation-agent` | claude-sonnet-4-6 | Data quality gate — record counts, field completeness, referential integrity | Grade D or F |
| `security-agent` | claude-sonnet-4-6 | OWASP scanning, secrets detection, CVE checks, PII handling | Any CRITICAL or HIGH finding |
| `execution-agent` | claude-sonnet-4-6 | Migration run lifecycle — pause, resume, retry, batch tuning | ALLOW gate not present |
| `debugging-agent` | claude-sonnet-4-6 | Read-only failure investigation, root cause analysis | N/A (read-only) |

---

## Using This as a Template

This repository is designed to be forked and adapted for any enterprise migration project.

1. **Fork the repository** and update the project name in `CLAUDE.md`, `config/agents.yaml`, and `pyproject.toml`.

2. **Replace source system extractors** — swap out `migration/legacy_extractors/siebel_extractor.py` and `migration/legacy_extractors/sap_extractor.py` with connectors for your source systems.

3. **Update field mappings** — edit the transformation files in `migration/data_transformations/` to match your source schema to your target Salesforce object model.

4. **Configure your Salesforce org** — set `SF_INSTANCE_URL`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET` in your `.env` file (or inject via Vault).

5. **Adjust validation thresholds** — edit `config/agents.yaml` to set acceptable error rates, completeness thresholds, and record count tolerances for your data.

6. **Update compliance controls** — review `security/compliance/` and remove or adjust controls that do not apply to your regulatory scope.

7. **Update `CLAUDE.md`** — keep the system context file accurate so AI coding agents have the correct mental model of your deployment.

---

## Configuration

| Environment Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key with access to claude-opus-4-6 |
| `MIGRATION_API_BASE_URL` | Yes | Base URL of the migration control-plane API |
| `INTERNAL_SERVICE_TOKEN` | Yes | Injected by Vault Agent at runtime — do not hardcode |
| `SF_INSTANCE_URL` | Yes | Salesforce org URL (Government Cloud+) |
| `VAULT_ADDR` | Yes | HashiCorp Vault server address |
| `PROJECT_ROOT` | Yes | Absolute path to repo root (used by security agent path validation) |
| `ANTHROPIC_MODEL` | No | Override default model (default: `claude-opus-4-6`) |
| `AGENT_MAX_ITERATIONS` | No | Max agent loop iterations (default: 20) |
| `ENVIRONMENT` | No | `development`, `staging`, or `production` |

---

## Compliance

| Framework | Scope | Reference |
|---|---|---|
| FedRAMP High | All components processing CUI/PII | `security/compliance/fedramp_controls.md` |
| FISMA Moderate | Federal information systems | `security/compliance/fisma_controls.md` |
| SOX | Financial data processing and audit trails | `security/compliance/sox_controls.md` |
| GDPR | EU data subject records (Article 17, 20, 25) | `security/compliance/gdpr_controls.md` |

All audit events are HMAC-chained and forwarded to Splunk via `security/audit/audit_logger.py`. The chain cannot be tampered with without detection.

---

## Halcon Retrospective Metrics

After every agent session, the system emits a structured metrics record to `.halcon/retrospectives/sessions.jsonl`. These records capture convergence efficiency, final utility, decision density, and failure modes to support continuous improvement of agent behavior.

Key metrics:
- `convergence_efficiency` — ratio of productive to total iterations
- `final_utility` — quality score weighted by error rate
- `decision_density` — tool calls per iteration (higher = more decisive agent)
- `dominant_failure_mode` — most common failure pattern in the session

Use `HalconEmitter` from `monitoring/agent_observability.py` to emit metrics. Never write to `sessions.jsonl` directly.

See `halcon/` for the full SDK and `.halcon/retrospectives/sessions.jsonl` for historical data.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, testing requirements, and the PR checklist.

---

## License

MIT License. See [LICENSE](LICENSE).
