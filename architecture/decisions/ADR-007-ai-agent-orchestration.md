# ADR-007: AI Agent Orchestration for Migration Validation and Remediation

**Status:** Accepted
**Date:** 2025-12-01
**Deciders:** Platform Architecture Team, AI/ML Engineering Lead, Security Architect, CISO
**Tags:** `ai`, `agents`, `claude`, `llm`, `orchestration`, `validation`, `human-in-the-loop`

---

## Table of Contents

1. [Context and Problem Statement](#1-context-and-problem-statement)
2. [Decision Drivers](#2-decision-drivers)
3. [Considered Options](#3-considered-options)
4. [Decision Outcome](#4-decision-outcome)
5. [Agent Architecture](#5-agent-architecture)
6. [Trust Boundaries and Human-in-the-Loop Gates](#6-trust-boundaries-and-human-in-the-loop-gates)
7. [Risk Analysis](#7-risk-analysis)
8. [Risk Mitigations](#8-risk-mitigations)
9. [Data Privacy with AI](#9-data-privacy-with-ai)
10. [Pros and Cons of Options](#10-pros-and-cons-of-options)
11. [Implementation Guidelines](#11-implementation-guidelines)
12. [Related Decisions](#12-related-decisions)

---

## 1. Context and Problem Statement

Migration projects of the scale targeted by this platform (50M–500M records, complex legacy schemas, government and enterprise clients) encounter thousands of unique failure scenarios that cannot be fully anticipated at design time:

**Representative Failure Scenarios from Client Engagements:**
- Legacy records with encoded business logic in free-text fields (e.g., `DESCRIPTION = "INACTIVE-2019-CFO OVERRIDE"`) that require contextual interpretation to determine disposition
- Character encoding issues where Oracle stored UTF-16 data in VARCHAR2 columns, causing mojibake that looks valid until Salesforce rejects it
- Salesforce validation rules that interact with field combinations in non-obvious ways (e.g., a custom validation rule rejects Accounts with `Type = 'Government'` unless `Government_Contract_Number__c` is populated — not documented anywhere)
- Referential integrity violations where 3% of Contact records reference Account IDs that were excluded from migration due to data quality rules (orphan handling strategy not defined)
- Date fields that changed semantics between legacy system versions (fiscal year vs. calendar year) requiring business context to interpret correctly

**Current Limitations of Deterministic Validation:**
The rule-based transformation engine (ADR-005) handles anticipated failure cases, but:
- Writing transformation rules for every edge case requires months of business analyst time per client
- Some failures require semantic understanding of the legacy data that cannot be expressed as deterministic rules
- Post-migration validation anomalies (e.g., `17.3% of Accounts have no related Contacts — is this expected?`) require contextual judgment
- DLQ remediation currently requires manual engineer review for every failed record category — at scale, this creates bottlenecks

**Opportunity:**
AI language models have demonstrated capability in:
- Understanding legacy data structure documentation and generating transformation logic
- Analyzing error patterns across thousands of failed records and proposing remediation strategies
- Generating data quality reports in natural language for non-technical stakeholders
- Validating migration completeness by analyzing statistical distributions of migrated data

**Decision Scope:**
This ADR covers the use of AI agents (specifically Claude API) for:
1. Automated analysis of DLQ (Dead Letter Queue) failures
2. Proposed remediation for failed record batches
3. Migration validation report generation
4. Transformation rule suggestions for new field mappings
5. Anomaly detection in migrated data distributions

This ADR explicitly does NOT cover:
- Autonomous writing of transformation rules that execute in production without human review
- Direct write access by AI agents to production Salesforce
- AI agents accessing raw PII data (see Section 9)

---

## 2. Decision Drivers

| Priority | Driver |
|----------|--------|
| P0 | AI agents must never autonomously modify production data (human approval required for all write operations) |
| P0 | PII data must not be sent to external AI APIs (Claude API is external) |
| P0 | All AI-generated suggestions must be human-reviewed before execution |
| P1 | Reduce DLQ remediation time from hours/days (manual) to minutes (AI-assisted, human-approved) |
| P1 | Generate migration completion reports automatically rather than manually |
| P1 | Reduce business analyst time for transformation rule authoring by 60%+ |
| P2 | Multi-agent architecture with specialized agents for different migration concerns |
| P2 | Agents must have auditable reasoning traces (not just final outputs) |
| P3 | Agents should improve over time through feedback on their suggestions (RAG over approved remediation history) |

---

## 3. Considered Options

1. **Multi-Agent System using Claude API with Specialized Agents** (selected)
2. **Single LLM Endpoint for Ad-Hoc Queries by Engineers**
3. **Fine-Tuned Open-Source Models (Llama, Mistral) Self-Hosted**
4. **Rule-Based Expert System (No LLM)**

---

## 4. Decision Outcome

**Chosen option: Multi-Agent System using Anthropic Claude API with specialized, narrowly-scoped agents operating under strict human-in-the-loop gates and data privacy controls.**

### Positive Consequences

- **DLQ throughput**: AI-powered triage categorizes and proposes remediation for DLQ failures within minutes instead of hours. Human review workload reduced from reviewing each record to approving categorized batch remediations.
- **Knowledge capture**: AI agents learn from the pattern of approved remediations (stored in vector DB, retrieved via RAG). Client-specific data patterns are captured without PII.
- **Report quality**: Migration completion reports generated by the Documentation Agent are comprehensive, consistent, and can be delivered immediately after migration completion rather than 2–3 days later.
- **Rule authoring acceleration**: The Rule Generation Agent generates initial YAML rule drafts from schema documentation. Estimated 60% reduction in business analyst time for standard field mappings.
- **Anomaly detection**: The Validation Agent flags statistical anomalies (e.g., "8% of migrated Opportunities have Amount = $0, which is 3× higher than the source system baseline of 2.7%") that deterministic checks would miss.
- **Auditability**: Each agent invocation is logged with the full prompt, response, action taken, and human reviewer identity. Claude API supports extended thinking output for complex reasoning traces.

### Negative Consequences

- **Hallucination risk**: Claude may propose incorrect remediation strategies that appear plausible. Human review gate mitigates this but does not eliminate it — reviewers may miss subtle errors in AI-generated logic.
- **API cost**: Claude API usage at scale (thousands of DLQ analyses per migration) costs money. Estimated $500–$5,000 per large migration. Must be factored into pricing model.
- **Latency**: AI analysis adds 5–30 seconds per DLQ batch. For the DLQ remediation flow, this is acceptable; for the critical path of the migration pipeline, AI must not be invoked.
- **Dependency on external API**: Claude API availability affects DLQ remediation throughput. Platform must gracefully degrade to manual review when API is unavailable.
- **Privacy architecture complexity**: Ensuring PII is stripped before Claude API calls requires a robust PII detection and masking layer. Implementation errors could expose client PII to Anthropic's API.
- **Trust calibration**: Engineers may over-trust AI-generated remediation suggestions, reducing the effectiveness of human review. Requires training and process enforcement.
- **Scope creep risk**: Once AI agents are in the platform, pressure may grow to expand their autonomy beyond approved scope. Governance processes must prevent this.

---

## 5. Agent Architecture

### 5.1 Agent Inventory

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        AI AGENT ORCHESTRATOR                                │
│   (Manages agent lifecycle, enforces trust boundaries, logs all invocations)│
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │
           ┌─────────────────────┼──────────────────────────────┐
           │                     │                              │
    ┌──────▼────────┐   ┌────────▼──────────┐   ┌──────────────▼──────┐
    │  DLQ TRIAGE   │   │  VALIDATION       │   │  DOCUMENTATION      │
    │  AGENT        │   │  AGENT            │   │  AGENT              │
    │               │   │                   │   │                     │
    │ Scope:        │   │ Scope:            │   │ Scope:              │
    │ - Analyze     │   │ - Statistical     │   │ - Migration         │
    │   DLQ failures│   │   distribution    │   │   completion report │
    │ - Categorize  │   │   analysis        │   │ - Executive summary │
    │   error types │   │ - Anomaly         │   │ - Data quality      │
    │ - Propose     │   │   detection       │   │   narrative         │
    │   remediation │   │ - Completeness    │   │ - Stakeholder       │
    │   (READ ONLY) │   │   checks          │   │   comms draft       │
    └───────────────┘   └───────────────────┘   └─────────────────────┘

    ┌──────▼────────┐   ┌────────▼──────────┐
    │  RULE         │   │  SECURITY AUDIT   │
    │  GENERATION   │   │  AGENT            │
    │  AGENT        │   │                   │
    │               │   │ Scope:            │
    │ Scope:        │   │ - Review proposed │
    │ - Generate    │   │   transformation  │
    │   YAML rule   │   │   rules for PII   │
    │   drafts from │   │   handling        │
    │   schema docs │   │ - Flag compliance │
    │ - Never       │   │   concerns        │
    │   deploys     │   │ - Read-only       │
    │   rules       │   │   analysis        │
    └───────────────┘   └───────────────────┘
```

### 5.2 DLQ Triage Agent

**Trigger:** When DLQ depth exceeds 100 records OR after 30 minutes of any DLQ accumulation.

**Data Provided to Agent (after PII stripping):**
```python
# DLQ analysis context sent to Claude API (NO PII)

dlq_context = {
    "job_id": "mig-20251201-abc",
    "tenant_id": "ent-acme-corp",
    "rule_set_id": "oracle-ebs-to-sf-account-v3",
    "rule_set_version": "3.2.1",
    "total_dlq_records": 1847,
    "error_distribution": {
        "SF_DUPLICATE_VALUE": 892,
        "REQUIRED_FIELD_NULL_Name": 412,
        "FIELD_TOO_LONG_BillingStreet": 289,
        "INVALID_CROSS_REFERENCE_KEY": 254
    },
    # Sample of 5 records with PII replaced by synthetic tokens
    "sample_records": [
        {
            "error_code": "SF_DUPLICATE_VALUE",
            "error_message": "duplicate value found: Legacy_ID__c duplicates value on record with id 0015f000001XXXXX",
            "source_entity": "HZ_PARTIES",
            "source_record_id_hash": "sha256:a3f8b2c1...",  # HASHED, not original
            # Field values with PII masked:
            "transformed_fields_masked": {
                "Name": "[MASKED_COMPANY_NAME]",
                "BillingStreet": "[MASKED_ADDRESS]",
                "Legacy_ID__c": "12345678",  # IDs are not PII
                "Phone": "[MASKED_PHONE]"
            }
        }
    ],
    "transformation_rule_context": {
        # Full YAML rule set (no PII in rules)
    },
    "salesforce_error_docs": "...",
    "historical_remediations": [
        # RAG-retrieved similar past remediations (approved, anonymized)
    ]
}
```

**System Prompt:**
```
You are a Salesforce migration expert assisting with error remediation.

You will receive:
1. A summary of DLQ (Dead Letter Queue) failures from a legacy-to-Salesforce migration
2. Sample failed records (with PII replaced by masked tokens)
3. The transformation rules that produced these records
4. Historical remediation patterns for similar errors

Your task:
1. Categorize each error type and explain the root cause
2. Propose specific, implementable remediation for each category
3. Estimate what % of DLQ records each remediation would resolve
4. Flag any patterns that indicate a systematic transformation rule issue vs. data quality issue
5. If a transformation rule change is needed, describe the change in natural language (do NOT write code)
6. Identify any records that should be permanently rejected (not remediable)

CRITICAL CONSTRAINTS:
- You are in read-only advisory mode. All suggestions require human approval before execution.
- Do not suggest actions that would modify production Salesforce data without explicit human review.
- If you are uncertain, say so explicitly rather than guessing.
- Your output will be reviewed by a migration engineer before any action is taken.
- Masked tokens ([MASKED_COMPANY_NAME], etc.) represent sensitive data you cannot see.
  Do not attempt to infer the actual values.
```

**Output Schema (structured):**
```json
{
  "analysis_id": "uuid",
  "timestamp": "ISO8601",
  "error_categories": [
    {
      "error_code": "SF_DUPLICATE_VALUE",
      "record_count": 892,
      "root_cause": "string — explanation of why duplicates are occurring",
      "confidence": "HIGH|MEDIUM|LOW",
      "proposed_remediation": {
        "type": "TRANSFORMATION_RULE_CHANGE|DATA_EXCLUSION|REQUEUE|MANUAL_REVIEW",
        "description": "string — human-readable description of the change",
        "estimated_records_resolved": 850,
        "risk_level": "LOW|MEDIUM|HIGH",
        "requires_human_approval": true
      }
    }
  ],
  "systematic_issues_detected": ["string"],
  "permanently_rejectable_count": 45,
  "reasoning_trace": "string — agent's step-by-step reasoning",
  "confidence_overall": "HIGH|MEDIUM|LOW",
  "agent_uncertainty_flags": ["string"]
}
```

### 5.3 Validation Agent

**Trigger:** After each migration job completes (all records loaded OR stopped).

**Input:** Aggregated statistics — no individual record data, no PII.
```python
validation_context = {
    "source_statistics": {
        "total_records": 2847391,
        "by_type": {"ORGANIZATION": 1293847, "PERSON": 1553544},
        "by_status": {"A": 2711982, "I": 135409},
        "avg_fields_populated": 14.7
    },
    "migrated_statistics": {
        "total_records": 2789044,
        "by_type": {"Customer - Direct": 891234, "Other": 1897810},
        "null_rates_by_field": {"Phone": 0.147, "Website": 0.631},
        "avg_fields_populated": 12.3
    },
    "rejection_summary": {
        "total_rejected": 58347,
        "by_reason": {"required_field_null": 41203, "quality_check": 12891}
    }
}
```

**Agent capability:** Identifies statistical anomalies, generates natural language quality assessment.

---

## 6. Trust Boundaries and Human-in-the-Loop Gates

### 6.1 Autonomy Levels

| Action | Agent Autonomy | Human Gate |
|--------|---------------|------------|
| Analyze DLQ failures | Full autonomy | None (read-only) |
| Generate error report | Full autonomy | None (informational) |
| Propose transformation rule change | Advisory only | Engineer approval required |
| Propose DLQ record re-queue | Advisory only | Migration engineer approval |
| Propose record permanent rejection | Advisory only | Migration engineer + client approval |
| Generate transformation rule YAML draft | Advisory only | 2-engineer review required before deployment |
| Flag compliance concern | Advisory only | Compliance officer notification |
| **Write to Salesforce** | **PROHIBITED** | **Architecturally enforced** |
| **Modify Kafka topic data** | **PROHIBITED** | **Architecturally enforced** |
| **Execute transformation rule** | **PROHIBITED** | **Architecturally enforced** |

### 6.2 Architectural Enforcement of Boundaries

AI agent workloads have SPIFFE identities with OPA policies that specifically deny write permissions:

```rego
# policies/authz/ai-agent-constraints.rego
package migration.authz.ai_agents

# AI agents CANNOT publish to any Kafka topic
deny[{"msg": "AI agents are prohibited from publishing to Kafka topics"}] {
    startswith(input.principal.spiffe_id,
        "spiffe://migration-platform.internal/ns/platform-ai-agents/")
    input.action == "kafka:publish"
}

# AI agents CANNOT call Salesforce API directly
deny[{"msg": "AI agents are prohibited from direct Salesforce API access"}] {
    startswith(input.principal.spiffe_id,
        "spiffe://migration-platform.internal/ns/platform-ai-agents/")
    input.resource.type == "salesforce_api"
}

# AI agents CANNOT access Vault secrets (only anonymized context via orchestrator)
deny[{"msg": "AI agents cannot access Vault secrets directly"}] {
    startswith(input.principal.spiffe_id,
        "spiffe://migration-platform.internal/ns/platform-ai-agents/")
    input.resource.type == "vault_secret"
}

# AI agents CAN read from specific read-only data endpoints
allow {
    startswith(input.principal.spiffe_id,
        "spiffe://migration-platform.internal/ns/platform-ai-agents/")
    input.action == "read"
    input.resource.type in [
        "migration_statistics",
        "dlq_summary",
        "transformation_rules",
        "approved_remediation_history"
    ]
}
```

### 6.3 Human Approval Workflow

```
AI Agent produces recommendation
         │
         ▼
Recommendation stored in approval_queue table
(PostgreSQL, immutable after insertion)
         │
         ▼
Migration Engineer receives notification (PagerDuty/Slack)
         │
         ▼
Engineer reviews in Migration Control UI:
  - AI reasoning trace (fully displayed)
  - Specific action proposed
  - Impact assessment
  - "Approve" / "Modify" / "Reject" controls
         │
    ┌────┴──────┐
    │           │
 APPROVE     REJECT
    │           │
    ▼           ▼
Orchestrator  Logged with
executes      rejection reason
approved      AI feedback loop
action
    │
    ▼
Action logged to immutable audit trail with:
- Engineer identity (from mTLS certificate)
- Timestamp
- AI recommendation (verbatim)
- Final approved action (may differ from AI recommendation)
- Engineer review duration
```

---

## 7. Risk Analysis

### Risk 1: AI Hallucination Leading to Data Loss or Corruption

**Likelihood:** Medium (LLMs can produce plausible but incorrect technical recommendations)
**Impact:** High (if hallucinated remediation is approved and executed, records may be corrupted or lost)
**Risk Level:** HIGH

### Risk 2: PII Exposure to Anthropic API

**Likelihood:** Medium (PII masking has edge cases; new PII patterns may not be detected)
**Impact:** Very High (GDPR violation, HIPAA violation, government contract breach)
**Risk Level:** CRITICAL

### Risk 3: Over-Trust in AI Recommendations (Automation Bias)

**Likelihood:** High (engineers under time pressure may rubber-stamp AI recommendations)
**Impact:** Medium-High (bad recommendations get approved, data quality issues reach Salesforce)
**Risk Level:** HIGH

### Risk 4: AI Agent SPIFFE Identity Compromise

**Likelihood:** Low (mTLS and short-lived SVIDs)
**Impact:** Low (even if compromised, OPA policies prevent write operations)
**Risk Level:** LOW

### Risk 5: Claude API Outage Blocking DLQ Remediation

**Likelihood:** Medium (external API dependency)
**Impact:** Medium (DLQ remediation slows to manual; migration continues, more records accumulate in DLQ)
**Risk Level:** MEDIUM

---

## 8. Risk Mitigations

### Mitigation for Risk 1 (Hallucination):

1. **Structured output enforcement**: Claude API called with strict JSON schema; the agent is instructed to include `confidence` and `agent_uncertainty_flags` fields. Low-confidence recommendations are highlighted to reviewers.
2. **Cross-agent validation**: For HIGH-impact recommendations (bulk record rejection, transformation rule changes), two independent agent invocations are made with different prompts. Only recommendations that agree in substance proceed to the approval queue.
3. **Historical pattern matching**: RAG over approved remediation history. If an AI recommendation matches an approved historical pattern, it is flagged as HIGH confidence. Novel recommendations get additional scrutiny.
4. **Human review quality**: Engineers are trained to review AI reasoning traces (not just the conclusion). Review includes verifying the agent's stated facts against known data.
5. **Post-execution sampling**: After an AI-recommended remediation is executed, a 10% sample of the remediated records is automatically verified against expected post-remediation state.

### Mitigation for Risk 2 (PII Exposure):

1. **PII Detection Layer**: All data passed to Claude API goes through a PII detection service using AWS Comprehend (government clients) or a self-hosted NLP model, combined with regex patterns for known PII formats.
2. **PII Masking**: Detected PII is replaced with type-consistent synthetic tokens (`[MASKED_EMAIL_1]`, `[MASKED_SSN_1]`) before API call. Tokens are consistent within a single request (same email address always maps to same token).
3. **Aggregate-only for statistics**: The Validation Agent receives only aggregated statistics — never individual records.
4. **Contractual DPA**: Anthropic Data Processing Agreement in place. Model training opt-out configured. API calls use `X-Anthropic-Do-Not-Train: true` header (pending Anthropic support for this header).
5. **Government clients**: For FedRAMP clients, Claude API calls are explicitly documented in the System Security Plan (SSP) as an external connection. If a government client prohibits external AI API calls, the AI agent features are disabled for that tenant and replaced with enhanced alerting to human reviewers.
6. **Audit log of all Claude API calls**: Every prompt sent to Claude API and every response received is logged (after PII masking verification) for compliance review.

### Mitigation for Risk 3 (Automation Bias):

1. **Review time floor**: Approval UI enforces a minimum 90-second review period before "Approve" becomes clickable. This discourages reflexive approval.
2. **Random deep review**: 10% of approved recommendations are flagged for post-approval audit review by the team's security/quality lead.
3. **Confidence disclosure**: When engineer approves a LOW-confidence recommendation, additional acknowledgement dialog requires them to confirm they understand the AI is uncertain.
4. **Accountability logging**: Approval logs include the engineer's identity and review duration. Review patterns (e.g., consistently <10-second reviews) are flagged in the weekly quality report.

### Mitigation for Risk 5 (API Outage):

1. **Graceful degradation**: When Claude API returns 5xx or times out, the DLQ record is flagged for human review with the error pattern pre-categorized by a local deterministic classifier.
2. **Local fallback model**: A small, self-hosted model (Mistral 7B) handles simple error categorization (the model is not used for complex remediation, only triage). This covers ~60% of DLQ errors without external API.
3. **Circuit breaker**: After 3 consecutive Claude API failures, all DLQ records are routed to human review. Circuit re-closes after successful test probe.

---

## 9. Data Privacy with AI

### 9.1 Data Classification for AI Processing

| Data Category | AI Processing Allowed | Mechanism |
|---------------|----------------------|-----------|
| Field names, data types, schema | Yes | Passed as-is |
| Transformation rule YAML | Yes | Passed as-is (no PII in rules) |
| Error codes and messages | Yes | Passed as-is |
| Statistical aggregates | Yes | Passed as-is |
| Record IDs (legacy system PKs) | Yes (non-PII) | Passed as-is |
| Individual record field values | Conditional | Passed only after PII masking |
| Names, addresses, emails, phone numbers | Masked only | `[MASKED_NAME]`, `[MASKED_EMAIL]` etc. |
| SSN, Tax ID, government IDs | Never | Entirely excluded from Claude API context |
| Credit card numbers | Never | Entirely excluded |
| Medical/health information | Never | Entirely excluded |

### 9.2 PII Detection Implementation

```python
# migration_platform/ai/pii_masking.py

from dataclasses import dataclass
from typing import Dict, Tuple
import re
import hashlib
import boto3  # For AWS Comprehend (government clients)

# Deterministic masking: same value always maps to same token within a session
# Token map is cleared after each Claude API call

class PIIMaskingService:
    """
    Detects and masks PII before data is sent to external AI APIs.
    Uses a combination of regex patterns, NLP-based NER, and field-name heuristics.
    """

    # Fields that are ALWAYS masked regardless of content
    ALWAYS_MASK_FIELDS = {
        'name', 'first_name', 'last_name', 'full_name',
        'email', 'email_address',
        'phone', 'phone_number', 'mobile', 'fax',
        'address', 'street', 'billing_street', 'shipping_street',
        'city', 'postal_code', 'zip_code',
        'ssn', 'tax_id', 'ein', 'itin',
        'credit_card', 'card_number', 'cvv',
        'date_of_birth', 'dob', 'birth_date',
        'passport', 'drivers_license', 'license_number'
    }

    # Fields containing IDs that are NOT PII (can pass through)
    SAFE_ID_FIELDS = {
        'party_id', 'account_id', 'legacy_id', 'record_id',
        'external_id', 'opportunity_id', 'contact_id'
    }

    # Regex patterns for PII detection in values
    PII_PATTERNS = {
        'SSN': re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
        'CREDIT_CARD': re.compile(r'\b(?:\d{4}[\s-]){3}\d{4}\b'),
        'EMAIL': re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
        'PHONE_US': re.compile(r'\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'),
        'ZIP': re.compile(r'\b\d{5}(?:-\d{4})?\b'),
    }

    def mask_record(
        self,
        record: Dict,
        token_map: Dict
    ) -> Tuple[Dict, Dict]:
        """
        Returns (masked_record, updated_token_map).
        token_map ensures the same PII value always maps to the same token within a session.
        """
        masked = {}
        for field, value in record.items():
            if value is None:
                masked[field] = None
                continue

            field_lower = field.lower()

            if field_lower in self.SAFE_ID_FIELDS:
                masked[field] = value
                continue

            if field_lower in self.ALWAYS_MASK_FIELDS:
                token, token_map = self._get_or_create_token(
                    str(value), f"MASKED_{field.upper()}", token_map
                )
                masked[field] = f"[{token}]"
                continue

            # Value-based PII detection for unclassified fields
            str_value = str(value)
            detected_type = self._detect_pii_in_value(str_value)
            if detected_type:
                token, token_map = self._get_or_create_token(
                    str_value, f"MASKED_{detected_type}", token_map
                )
                masked[field] = f"[{token}]"
            else:
                masked[field] = value

        return masked, token_map

    def _get_or_create_token(
        self,
        value: str,
        prefix: str,
        token_map: Dict
    ) -> Tuple[str, Dict]:
        value_hash = hashlib.sha256(value.encode()).hexdigest()[:8]
        existing = token_map.get(value_hash)
        if existing:
            return existing, token_map
        counter = sum(1 for k in token_map.values() if k.startswith(prefix))
        token = f"{prefix}_{counter + 1}"
        token_map[value_hash] = token
        return token, token_map

    def _detect_pii_in_value(self, value: str) -> str | None:
        for pii_type, pattern in self.PII_PATTERNS.items():
            if pattern.search(value):
                return pii_type
        return None
```

---

## 10. Pros and Cons of Options

### Option 2: Single LLM Endpoint for Ad-Hoc Engineer Queries

**Pros:** Simpler; no orchestration layer; engineers can ask arbitrary questions about migration state.

**Cons:** No audit trail of AI interactions; no structured output for reliable action items; inconsistent prompting leads to inconsistent recommendations; no PII controls enforced systematically; no human approval workflow; cannot scale to automated DLQ triage.

**Verdict:** Rejected. This is a tool, not an architecture. Acceptable as a supplementary capability for engineers, not as the primary AI integration.

---

### Option 3: Fine-Tuned Open-Source Models (Self-Hosted)

**Pros:** No PII exposure to external API; no per-call cost; lower latency; can be fine-tuned on migration-specific data; works in air-gapped government environments.

**Cons:** Fine-tuned models require significant ML engineering investment and ongoing maintenance; quality of open-source models for complex reasoning tasks is below Claude API (as of 2025-12); self-hosting adds infrastructure cost and operational burden; models become stale without retraining; government clients need to approve model training data; fine-tuning on client data creates data handling complications.

**Verdict:** Not selected for initial release. Planned as a parallel track for FedRAMP High clients who cannot use external APIs. A self-hosted Mistral 7B is used as the fallback model (Section 8, Risk 5 mitigation) — this is the seed for eventual full self-hosted deployment for government clients.

---

### Option 4: Rule-Based Expert System (No LLM)

**Pros:** Fully deterministic; auditable; no hallucination risk; works offline; no PII concerns.

**Cons:** Cannot handle novel error patterns not anticipated in rule design; requires significant expert knowledge encoding upfront; cannot generate natural language reports; does not learn from new patterns; significant engineering effort for diminishing coverage.

**Verdict:** Partially implemented as the deterministic fallback classifier. Insufficient as the sole solution for DLQ triage — cannot achieve the DLQ remediation throughput improvement that justifies the AI investment.

---

## 11. Implementation Guidelines

### 11.1 Claude API Client Configuration

```python
# migration_platform/ai/claude_client.py

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

class MigrationAIClient:
    """
    Managed Claude API client with retry logic, cost tracking, and audit logging.
    """

    def __init__(self, config: dict):
        self.client = anthropic.Anthropic(
            api_key=config['api_key'],  # Retrieved from Vault, not hardcoded
            max_retries=0  # We handle retries via tenacity
        )
        self.default_model = "claude-sonnet-4-6"
        self.cost_tracker = CostTracker(config['cost_limit_per_job_usd'])
        self.audit_logger = AuditLogger(config['audit_sink_url'])

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        reraise=True
    )
    def invoke_agent(
        self,
        agent_type: str,
        system_prompt: str,
        context: dict,
        schema: dict,
        job_id: str,
        tenant_id: str
    ) -> dict:
        # Cost guard: abort if job has exceeded AI budget
        self.cost_tracker.check_limit(job_id)

        response = self.client.messages.create(
            model=self.default_model,
            max_tokens=4096,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": f"Analysis context:\n{json.dumps(context, indent=2)}"
                }
            ],
            # Force structured JSON output
            tools=[{
                "name": "submit_analysis",
                "description": "Submit the structured analysis result",
                "input_schema": schema
            }],
            tool_choice={"type": "tool", "name": "submit_analysis"}
        )

        result = response.content[0].input

        # Log EVERY API call to immutable audit trail
        self.audit_logger.log_ai_invocation(
            agent_type=agent_type,
            job_id=job_id,
            tenant_id=tenant_id,
            prompt_tokens=response.usage.input_tokens,
            response_tokens=response.usage.output_tokens,
            # Context stored after PII masking verification
            masked_context_hash=hashlib.sha256(
                json.dumps(context).encode()
            ).hexdigest(),
            result_summary=result.get('confidence', 'UNKNOWN')
        )

        self.cost_tracker.record_usage(
            job_id=job_id,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens
        )

        return result
```

---

## 12. Related Decisions

- [ADR-003: Event-Driven Architecture](./ADR-003-event-driven-architecture.md) — AI agents consume from `failed` and `dlq` Kafka topics; approved remediations re-queue records via orchestrator
- [ADR-004: Zero Trust Security Model](./ADR-004-zero-trust-security-model.md) — AI agent SPIFFE identities with restricted OPA policies (read-only enforcement)
- [ADR-005: Data Transformation Strategy](./ADR-005-data-transformation-strategy.md) — Rule Generation Agent produces draft YAML for transformation rules

---

*Last reviewed: 2025-12-01*
*Next review due: 2026-03-01 (quarterly — AI landscape evolving rapidly)*
*Document owner: AI/ML Engineering Lead + Security Architect*
*Note: Hallucination risk mitigations must be re-evaluated each quarter as model capabilities evolve*
