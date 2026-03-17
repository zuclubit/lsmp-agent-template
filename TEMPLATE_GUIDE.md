# Template Guide — Using LSMP as Your Agent System Foundation

This repository is a **production-ready template** for enterprise AI agent systems built on Claude. Fork it to accelerate your own migration or automation project.

---

## What This Template Provides

| Component | What you get |
|-----------|-------------|
| **6 specialized agents** | Orchestrator, Planning, Validation, Security, Execution, Debugging |
| **Safety gates** | BlockingGate protocol — BLOCK stops the pipeline before damage |
| **Circuit breakers** | On every external API call — prevents cascading failures |
| **Compliance controls** | SOX, GDPR, FedRAMP scaffolding ready to configure |
| **Test suite** | 40 test files across unit, integration, contract, failure scenarios |
| **Halcon observability** | Retrospective metrics after every agent session |
| **MCP context servers** | 4 context servers for project, docs, runtime, security state |
| **Shared infrastructure** | BaseAgent, schemas, context protocol, PII redaction |

---

## Adapting for Your Migration

### Step 1 — Fork and Configure

```bash
gh repo fork zuclubit/lsmp-agent-template --clone
cd lsmp-agent-template
cp .env.example .env
# Fill in ANTHROPIC_API_KEY, your source DB, target Salesforce org
```

### Step 2 — Replace Source Extractors

Edit `migration/legacy_extractors/` — replace the Oracle Siebel and SAP CRM connectors with your source system:

```python
# migration/legacy_extractors/your_system_extractor.py
class YourSystemExtractor:
    def extract_batch(self, offset: int, limit: int) -> list[dict]:
        # Connect to your source — replace this implementation
        ...
```

### Step 3 — Update Field Mappings

Edit `migration/data_transformations/account_transformer.py`:

```python
ACCOUNT_FIELD_MAP = {
    "your_legacy_field": "Salesforce_Field__c",
    # Add your mappings here
}
```

### Step 4 — Configure Validation Thresholds

Edit `config/agents.yaml` — the validation thresholds are the most important config:

```yaml
validation-agent:
  thresholds:
    record_count_tolerance: 0.001   # adjust for your data quality expectations
    required_field_null_rate_max: 0.01
    referential_integrity_min: 0.99
```

### Step 5 — Configure Salesforce Target

Update `.env` with your Salesforce org details, and review `config/agents.yaml` for SF-specific settings. Update the external ID field (`Legacy_ID__c`) to match your SF org's custom field.

### Step 6 — Adjust Compliance Scope

Remove compliance controls that don't apply to your project:
- Not government? Remove `security/compliance/fedramp_controls.md`
- Not financial? Disable SOX dual-authorization in `security/policies.yaml`
- No EU data? Simplify `security/compliance/gdpr_controls.md`

---

## Agent Customization Guide

### What to change per agent

| Agent | Safe to change | Do NOT change |
|-------|---------------|---------------|
| `planning-agent` | Step names, duration estimates, risk thresholds | Step ordering (EXTRACT must come before LOAD) |
| `validation-agent` | Thresholds, field lists, sample rates | Default-to-FAILED behavior, gate blocking logic |
| `security-agent` | Path whitelist, entropy threshold | SOQL SELECT-only enforcement, PII detection |
| `execution-agent` | API endpoints, retry counts | Idempotency check, gate requirement |
| `debugging-agent` | Log sources, metric names | Read-only constraint (never add write tools) |
| `orchestrator-agent` | Agent routing, timeout values | BlockingGate enforcement, VALID_HANDOFF_GRAPH |

---

## Context for AI Coding Agents

If you use Claude Code or another AI coding assistant to work on this codebase, here's what to tell it:

### Files to Read First (priority order)

1. `CLAUDE.md` — complete system context, critical rules, known issues
2. `config/agents.yaml` — single source of truth for all agent behavior
3. `agents/_shared/schemas.py` — all Pydantic v2 data contracts
4. `agents/orchestrator-agent/agent.md` — orchestrator spec and blocking gate rules
5. The relevant specialist `agent.md` for the agent you're modifying

### Critical Rules for AI Agents Working on This Codebase

1. **Never disable BlockingGate** — the `BLOCK` decision must always stop the pipeline
2. **Never use stub data** — no `random.*`, no hardcoded fake results in validation
3. **Default to FAILED** — validation results must prove data is good, not assume it
4. **Check `config/agents.yaml`** before hardcoding any threshold — all thresholds live there
5. **Run `pytest tests/ -m unit`** after any change to agent tools or schemas
6. **Update `CLAUDE.md`** if you change agent behavior or add a new agent
7. **Never commit secrets** — even test credentials belong in `.env` only

### Halcon Metrics

Every agent modification that changes behavior should be reflected in Halcon metrics. The target ranges are:

```
convergence_efficiency:  > 0.70  (useful work / total tokens)
decision_density:        > 0.50  (decisions per 1000 tokens)
adaptation_utilization:  0.2–0.6 (fraction of iterations that changed approach)
final_utility:           > 0.80  (outcome quality)
```

---

## Template File Index

| File | Purpose |
|------|---------|
| `CLAUDE.md` | AI agent system context — read at every session start |
| `README.md` | Project overview for humans and agents |
| `TEMPLATE_GUIDE.md` | This file — how to adapt the template |
| `CONTRIBUTING.md` | Code standards and PR process |
| `config/agents.yaml` | All agent model/threshold/tool configuration |
| `config/halcon.yaml` | Retrospective metrics configuration |
| `agents/_shared/schemas.py` | Central data contracts (Pydantic v2) |
| `agents/_shared/base_agent.py` | Circuit breaker, agentic loop, Halcon emission |
| `agents/_shared/context_protocol.py` | Handoff graph, PII redaction |
| `security/policies.yaml` | Gate enforcement, tool permissions, SOX rules |
| `monitoring/agent_observability.py` | Prometheus, OTel, HalconEmitter |

---

*Template version: 1.0.0 | Updated: 2026-03-17 | Maintained by zuclubit/platform-engineering*
