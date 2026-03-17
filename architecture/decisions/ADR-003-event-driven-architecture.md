# ADR-003: Event-Driven Architecture for Migration Pipeline

**Status:** Accepted
**Date:** 2025-11-14
**Deciders:** Platform Architecture Team, Engineering Lead, Security Architect
**Supersedes:** N/A
**Superseded by:** N/A
**Tags:** `architecture`, `messaging`, `kafka`, `migration-pipeline`, `audit`

---

## Table of Contents

1. [Context and Problem Statement](#1-context-and-problem-statement)
2. [Decision Drivers](#2-decision-drivers)
3. [Considered Options](#3-considered-options)
4. [Decision Outcome](#4-decision-outcome)
5. [Pros and Cons of the Options](#5-pros-and-cons-of-the-options)
6. [Implementation Notes](#6-implementation-notes)
7. [Reference Architecture](#7-reference-architecture)
8. [Compliance and Governance Considerations](#8-compliance-and-governance-considerations)
9. [Metrics and Observability](#9-metrics-and-observability)
10. [Related Decisions](#10-related-decisions)

---

## 1. Context and Problem Statement

The Legacy-to-Salesforce migration platform must orchestrate a complex, multi-phase data pipeline involving:

- **Extraction** from heterogeneous legacy systems (Oracle EBS, SAP, custom SQL Server databases, flat-file archives)
- **Transformation** through a rule-based engine with field mappings, data cleansing, and enrichment
- **Validation** via AI-assisted agents and deterministic schema checks
- **Loading** into Salesforce CRM via Bulk API 2.0 and REST API

The migration workloads span government clients (with strict FISMA/FedRAMP requirements) and private enterprise clients. Individual migration jobs can involve 50M–500M records, run over days or weeks, and must support pause/resume without data loss.

Key requirements driving this architectural decision:

- **Audit trail immutability**: Every record transformation event must be captured for compliance and dispute resolution. Government contracts mandate a 7-year retention of audit logs (NARA guidelines).
- **Decoupled pipeline stages**: Extraction, transformation, validation, and loading must be independently scalable and deployable. A failure in the loading stage must not require re-extraction.
- **Exactly-once semantics**: In migration contexts, duplicate records in Salesforce are a critical defect. The architecture must provide exactly-once delivery guarantees or robust idempotency.
- **Backpressure and rate limiting**: Salesforce API governor limits (10,000 bulk API calls per 24 hours per org) require the pipeline to be throttleable without message loss.
- **Replay capability**: When transformation rules change post-extraction, the platform must replay raw events through the new rule set without re-querying the legacy system (which may be decommissioned).

The current synchronous, direct-call architecture chains pipeline stages via REST callbacks, leading to:

- Tight coupling between extraction and transformation services
- No audit trail below the application log level
- Inability to replay failed batches without full re-extraction
- Cascading failures when downstream services are slow (Salesforce rate limiting cascades back to extraction)
- No backpressure mechanism — extraction floods transformation memory

---

## 2. Decision Drivers

| Priority | Driver |
|----------|--------|
| P0 | Immutable, replayable audit log for government compliance (NIST SP 800-92, FISMA) |
| P0 | Exactly-once or idempotent record loading (no duplicate Salesforce records) |
| P1 | Independent scalability of pipeline stages |
| P1 | Pause/resume of long-running migrations without data loss |
| P1 | Backpressure from Salesforce API rate limits must not propagate to upstream stages |
| P2 | Multi-tenant isolation of event streams |
| P2 | Support for event replay when transformation rules are updated |
| P3 | Operational tooling maturity (monitoring, offset management, consumer group management) |
| P3 | Ecosystem integration with Kafka Connect for legacy source connectors |

---

## 3. Considered Options

1. **Apache Kafka** (selected)
2. **RabbitMQ with durable queues**
3. **Azure Service Bus with Service Bus Sessions**
4. **Direct database staging tables with polling**
5. **AWS EventBridge + SQS/SNS**

---

## 4. Decision Outcome

**Chosen option: Apache Kafka**, deployed on Confluent Platform (self-managed on-premises for FedRAMP High environments; Confluent Cloud for private enterprise clients).

Kafka's combination of a distributed commit log, configurable retention, consumer group offset management, and exactly-once transaction support uniquely satisfies the audit trail immutability and replay requirements that are non-negotiable for government contracts.

### Positive Consequences

- **Immutable event log**: Kafka topics with log compaction disabled and retention set to 7 years (compressed, tiered storage) serve as the audit log with no additional infrastructure.
- **Replay without re-extraction**: Consumers can reset offsets to replay any segment of the extraction log through updated transformation logic.
- **Independent stage scaling**: Each pipeline stage is a consumer group. Loading consumers can be scaled to 0 (pausing loads to Salesforce) without affecting extraction or transformation throughput.
- **Native backpressure**: Consumer groups naturally buffer — if loading slows due to Salesforce rate limits, the Kafka partition lag increases but extraction continues at its own pace.
- **Ecosystem**: Kafka Connect has production-grade connectors for Oracle, SQL Server, and flat files (Debezium CDC, JDBC Source Connector), reducing custom extraction code.
- **Exactly-once transactions**: Kafka's transactional producer API enables exactly-once writes when combined with idempotent Salesforce upsert operations using external ID fields.
- **Multi-tenant isolation**: Per-client topic naming convention (`tenant.{client_id}.migration.{job_id}.{stage}`) with ACL-based access control provides hard isolation between clients.

### Negative Consequences

- **Operational complexity**: Kafka requires dedicated operational expertise. A team of 2+ engineers must be trained or hired with deep Kafka/Confluent knowledge. Estimated 3-month ramp time.
- **Infrastructure overhead**: A minimum production Kafka cluster (3 brokers, 3 ZooKeeper nodes, Schema Registry, Kafka Connect cluster) requires significant compute. Estimated cost: $8,000–$15,000/month for on-premises hardware or cloud VMs.
- **Latency floor**: Kafka's batch-oriented design introduces 5–50ms latency per message hop. For migration workloads processing records in bulk, this is acceptable; for real-time bidirectional sync, a different solution would be needed.
- **Schema evolution complexity**: Using Confluent Schema Registry with Avro requires disciplined schema governance. Incompatible schema changes break consumers. This requires process enforcement.
- **Storage requirements**: Retaining 7 years of migration events for large clients (500M records) requires significant cold storage. Tiered storage (S3/Azure Blob) is mandatory. Estimated 2–10TB per large migration job.
- **ZooKeeper dependency**: Older Kafka versions (pre-KRaft mode) require ZooKeeper, adding operational surface. Migration to KRaft mode is planned for Kafka 3.5+ to eliminate this dependency.

---

## 5. Pros and Cons of the Options

### Option 1: Apache Kafka

**Pros:**
- Distributed commit log provides true immutability and audit trail natively
- Consumer offset management enables precise pause/resume at the record level
- Exactly-once transactions with transactional producers (Kafka 0.11+)
- Unlimited consumer groups read the same data independently (audit, monitoring, alerting consumers can run alongside migration consumers)
- Log compaction enables efficient state snapshots for large key spaces
- Confluent Platform adds Schema Registry, KSQL, Control Center for enterprise operations
- KIP-405 Tiered Storage enables cost-effective 7-year retention on object storage
- Kafka Connect ecosystem has 200+ connectors including Debezium for CDC from Oracle/SQL Server
- Industry-standard for large-scale data pipeline (LinkedIn processes 7 trillion messages/day)

**Cons:**
- Highest operational complexity of all options
- Requires ZooKeeper (or KRaft) ensemble management
- Schema registry adds a dependency that becomes a single point of failure
- Harder to reason about consumer lag and backpressure than queue-based systems
- Partition rebalancing can cause consumer group pauses (mitigated with static membership)
- No built-in dead letter queue concept — must implement manually or use Kafka Streams error handling

**Verdict:** Best fit for requirements. Complexity is justified by audit trail and replay capabilities.

---

### Option 2: RabbitMQ with Durable Queues

**Pros:**
- Simpler operational model than Kafka
- Native dead letter exchanges for failed message handling
- AMQP protocol is widely supported
- Lower latency than Kafka for single-message routing
- Topic exchanges provide flexible routing patterns
- RabbitMQ Streams (3.9+) adds log-based storage closer to Kafka semantics

**Cons:**
- Messages are consumed and deleted — no immutable audit log without a separate system
- No native replay capability without message archiving infrastructure
- Exchange/queue topology becomes complex for multi-tenant scenarios
- Cluster scaling requires manual queue mirroring configuration
- Queue depth limits require careful capacity planning (memory-backed queues)
- No exactly-once semantics — at-least-once with consumer acknowledgement
- RabbitMQ Streams is immature compared to Kafka's decade-long production hardening
- Cannot serve as the audit log — a separate audit database would be needed, duplicating storage

**Verdict:** Rejected. Inability to serve as both message bus and audit log requires duplicated infrastructure. The audit log requirement is non-negotiable for government compliance.

---

### Option 3: Azure Service Bus with Sessions

**Pros:**
- Managed service — no cluster operations
- Sessions provide ordered, exactly-once processing per session key
- Dead letter queues built-in
- 80GB/topic message retention (configurable)
- Geo-redundancy built-in for Premium tier
- Azure Active Directory integration aligns with government cloud identity

**Cons:**
- Maximum 80GB storage per topic is insufficient for 7-year audit retention
- No offset management for replay — once consumed, messages are gone
- Proprietary to Azure — lock-in risk for multi-cloud government environments
- Cannot serve as replay log without custom archiving to Azure Storage
- Limited to Azure regions — FedRAMP High requires Azure Government, which has limited feature availability
- No equivalent to Kafka Connect ecosystem for legacy source systems
- Message TTL max 14 days — incompatible with 7-year audit requirement

**Verdict:** Rejected. Message TTL limitation and lack of replay without separate archiving infrastructure are disqualifying for government compliance requirements.

---

### Option 4: Direct Database Staging Tables with Polling

**Pros:**
- Zero new infrastructure — uses existing database investments
- ACID transactions eliminate exactly-once concerns
- SQL queries for monitoring and debugging are familiar to all engineers
- No schema registry needed
- Straightforward rollback with DELETE/TRUNCATE

**Cons:**
- Database becomes bottleneck — staging tables under high write load contend with operational queries
- Polling introduces latency and unnecessary database load
- No multi-consumer concept — requires complex locking for parallel consumers
- No event ordering guarantees across distributed writers
- Database storage is expensive compared to object storage for 7-year retention
- Tight coupling — all pipeline stages depend on shared database schema
- No backpressure mechanism — writers flood staging tables, consumer falls behind
- Operational complexity of managing partition-like constructs manually
- No ecosystem connectors — all source extraction must be custom-coded

**Verdict:** Rejected. Suitable for small migrations (<1M records) but does not scale to the required volumes. The shared database creates coupling that violates the independent scalability requirement.

---

### Option 5: AWS EventBridge + SQS/SNS

**Pros:**
- Fully managed serverless — no cluster operations
- EventBridge Archive enables limited replay
- SQS provides natural backpressure with visibility timeouts
- SNS fan-out to multiple SQS queues for parallel consumers
- Native AWS IAM for access control

**Cons:**
- EventBridge Archive maximum retention is 1 year (insufficient for 7-year requirement)
- SQS message retention maximum 14 days
- No exactly-once between EventBridge and SQS (at-least-once)
- AWS-proprietary — government clients on Azure Government or on-premises cannot use this
- EventBridge throughput limits (10,000 events/second default) require quota increases for large migrations
- No consumer group offset management — position tracking requires custom implementation
- Fragmented: EventBridge + SQS + SNS + S3 for archiving is 4 services to operate vs. 1 Kafka cluster

**Verdict:** Rejected. Multi-cloud and on-premises requirements exclude AWS-proprietary services. Retention limits are incompatible with compliance requirements.

---

## 6. Implementation Notes

### 6.1 Kafka Cluster Topology

**Development/Test:**
```
1 broker (KRaft mode, no ZooKeeper)
Schema Registry: 1 instance
Kafka Connect: 1 worker
Replication factor: 1
Min ISR: 1
```

**Staging/Pre-Production:**
```
3 brokers, 3 KRaft controllers
Schema Registry: 2 instances (HA)
Kafka Connect: 3 workers
Replication factor: 3
Min ISR: 2
```

**Production (FedRAMP High):**
```
6 brokers across 3 availability zones (2 per AZ)
3 dedicated KRaft controllers
Schema Registry: 3 instances (cluster mode)
Kafka Connect: 6 workers (3 per pipeline stage)
Replication factor: 3
Min ISR: 2
Encryption: TLS 1.3 in-transit, AES-256 at-rest
Authentication: mTLS with SPIFFE-issued certificates (see ADR-004)
Authorization: Kafka ACLs enforced via OPA (see ADR-004)
```

### 6.2 Topic Naming Convention

```
{environment}.{tenant_id}.migration.{job_id}.{stage}

Examples:
prod.gov-dod-001.migration.mig-20251114-abc.extracted
prod.gov-dod-001.migration.mig-20251114-abc.transformed
prod.gov-dod-001.migration.mig-20251114-abc.validated
prod.gov-dod-001.migration.mig-20251114-abc.loaded
prod.gov-dod-001.migration.mig-20251114-abc.failed
prod.gov-dod-001.migration.mig-20251114-abc.dlq

prod.ent-acme-corp.migration.mig-20251115-xyz.extracted
prod.ent-acme-corp.migration.mig-20251115-xyz.transformed
```

**Partition count:** `ceil(expected_records / 1_000_000)`, minimum 12, maximum 120. This ensures each partition holds 1M–10M records for predictable processing time estimates.

**Retention:** 7 years (tiered storage) for audit topics. 30 days for operational topics (DLQ, monitoring).

### 6.3 Message Schema (Avro)

```json
{
  "namespace": "com.s_agent.migration.events",
  "type": "record",
  "name": "MigrationRecord",
  "fields": [
    {
      "name": "event_id",
      "type": "string",
      "doc": "UUID v4 unique to this event"
    },
    {
      "name": "correlation_id",
      "type": "string",
      "doc": "Ties all events for a single source record across pipeline stages"
    },
    {
      "name": "job_id",
      "type": "string",
      "doc": "Migration job identifier"
    },
    {
      "name": "tenant_id",
      "type": "string",
      "doc": "Client/tenant identifier for multi-tenant isolation"
    },
    {
      "name": "source_system",
      "type": "string",
      "doc": "Legacy source system name (e.g., oracle-ebs-prod)"
    },
    {
      "name": "source_entity",
      "type": "string",
      "doc": "Source table or object name"
    },
    {
      "name": "source_record_id",
      "type": "string",
      "doc": "Primary key in legacy system"
    },
    {
      "name": "payload",
      "type": "bytes",
      "doc": "Serialized record data (Avro-encoded nested schema)"
    },
    {
      "name": "payload_schema_id",
      "type": "int",
      "doc": "Confluent Schema Registry schema ID for payload decoding"
    },
    {
      "name": "stage",
      "type": {
        "type": "enum",
        "name": "PipelineStage",
        "symbols": ["EXTRACTED", "TRANSFORMED", "VALIDATED", "LOADED", "FAILED"]
      }
    },
    {
      "name": "timestamp_ms",
      "type": "long",
      "logicalType": "timestamp-millis",
      "doc": "Event creation time in epoch milliseconds"
    },
    {
      "name": "sequence_number",
      "type": "long",
      "doc": "Monotonically increasing sequence number per source_record_id"
    },
    {
      "name": "checksum",
      "type": "string",
      "doc": "SHA-256 of payload bytes for integrity verification"
    },
    {
      "name": "metadata",
      "type": {
        "type": "map",
        "values": "string"
      },
      "default": {},
      "doc": "Extensible key-value metadata (transformation rule version, validator version, etc.)"
    }
  ]
}
```

### 6.4 Producer Configuration (Exactly-Once)

```python
# migration_platform/infrastructure/kafka/producer.py

from confluent_kafka import Producer, KafkaException
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer

PRODUCER_CONFIG = {
    # Exactly-once semantics
    "enable.idempotence": True,
    "acks": "all",                    # Wait for all ISR acknowledgement
    "retries": 2147483647,            # Integer.MAX_VALUE
    "max.in.flight.requests.per.connection": 5,  # Safe with idempotence enabled

    # Transactional producer ID (unique per producer instance)
    "transactional.id": "migration-extractor-{tenant_id}-{job_id}",

    # Performance
    "batch.size": 65536,              # 64KB batches
    "linger.ms": 20,                  # Wait up to 20ms to fill batch
    "compression.type": "lz4",        # Fast compression for migration data

    # Security (mTLS - see ADR-004)
    "security.protocol": "SSL",
    "ssl.certificate.location": "/var/run/spiffe/certs/client.crt",
    "ssl.key.location": "/var/run/spiffe/certs/client.key",
    "ssl.ca.location": "/var/run/spiffe/certs/ca-bundle.crt",

    # Schema Registry
    "schema.registry.url": "https://schema-registry.internal:8081",
}
```

### 6.5 Consumer Configuration (Migration Loader)

```python
# Consumer group configuration for Salesforce loading stage

LOADER_CONSUMER_CONFIG = {
    "group.id": "migration-loader-{tenant_id}-{job_id}",
    "auto.offset.reset": "earliest",

    # Manual offset commit for exactly-once with Salesforce
    # Offsets are committed only AFTER successful Salesforce upsert
    "enable.auto.commit": False,

    # Static membership to avoid rebalancing during long Salesforce API calls
    "group.instance.id": "loader-{pod_name}",
    "session.timeout.ms": 300000,     # 5 minutes (long Salesforce calls)
    "heartbeat.interval.ms": 10000,   # 10 seconds

    # Fetch tuning for large batches
    "fetch.min.bytes": 1048576,       # 1MB minimum fetch
    "fetch.max.wait.ms": 500,
    "max.poll.records": 500,          # Match Salesforce Bulk API batch size

    # Security
    "security.protocol": "SSL",
    "ssl.certificate.location": "/var/run/spiffe/certs/client.crt",
    "ssl.key.location": "/var/run/spiffe/certs/client.key",
    "ssl.ca.location": "/var/run/spiffe/certs/ca-bundle.crt",
}
```

### 6.6 Dead Letter Queue (DLQ) Strategy

Failed records are routed to a per-job DLQ topic with enriched error context:

```python
DLQ_ENVELOPE_SCHEMA = {
    "original_event": "<MigrationRecord>",
    "failure_stage": "TRANSFORMATION | VALIDATION | LOADING",
    "failure_reason": "string",
    "failure_code": "string (e.g., SF_DUPLICATE_VALUE, VALIDATION_REQUIRED_FIELD_NULL)",
    "retry_count": "int",
    "first_failure_timestamp_ms": "long",
    "last_failure_timestamp_ms": "long",
    "error_detail": "string (stack trace or API error response)",
    "remediation_hint": "string (populated by AI validation agent - see ADR-007)"
}
```

DLQ processing policy:
- Automatic retry: 3 attempts with exponential backoff (1s, 4s, 16s)
- After 3 failures: record enters DLQ topic, alert fires, AI remediation agent evaluates
- Human review required before re-queuing from DLQ
- DLQ records included in migration completion report with failure categorization

---

## 7. Reference Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     LEGACY-TO-SALESFORCE MIGRATION PLATFORM                     │
│                          Event-Driven Pipeline Architecture                      │
└─────────────────────────────────────────────────────────────────────────────────┘

  LEGACY SOURCES                    KAFKA CLUSTER (6 Brokers, 3 AZ)
  ┌─────────────┐                   ┌──────────────────────────────────────────┐
  │ Oracle EBS  │──[Debezium CDC]──▶│  Topic: .migration.{job}.extracted       │
  │ SQL Server  │──[JDBC Src]──────▶│  Partitions: 12-120                      │
  │ SAP HANA    │──[Kafka Conn]────▶│  Retention: 7 years (tiered S3)          │
  │ CSV/FlatFile│──[File Src]──────▶│  Replication: 3, min ISR: 2              │
  └─────────────┘                   │  Schema: Avro + Confluent Registry        │
                                    └────────────────┬─────────────────────────┘
                                                     │ Consumer Group:
                                                     │ migration-transformer
                                                     ▼
                                    ┌──────────────────────────────────────────┐
  ┌─────────────────────┐           │  TRANSFORMATION SERVICE                  │
  │  Rule Engine        │◀──────────│  - Applies transformation rules          │
  │  Schema Registry    │           │  - Field mapping, data cleansing         │
  │  (ADR-005)          │──────────▶│  - Enrichment lookups                    │
  └─────────────────────┘           │  - Publishes to .transformed topic       │
                                    └────────────────┬─────────────────────────┘
                                                     │
                                    ┌────────────────▼─────────────────────────┐
                                    │  Topic: .migration.{job}.transformed     │
                                    │  Partitions: same as extracted           │
                                    │  Retention: 7 years                      │
                                    └────────────────┬─────────────────────────┘
                                                     │ Consumer Group:
                                                     │ migration-validator
                                                     ▼
                                    ┌──────────────────────────────────────────┐
  ┌─────────────────────┐           │  VALIDATION SERVICE                      │
  │  AI Validation      │◀──────────│  - Schema validation                     │
  │  Agent (ADR-007)    │           │  - Business rule checks                  │
  │                     │──────────▶│  - Duplicate detection                   │
  └─────────────────────┘           │  - Publishes to .validated or .failed    │
                                    └────────────────┬─────────────────────────┘
                                                     │
                          ┌──────────────────────────┼──────────────────────┐
                          │                          │                      │
               ┌──────────▼──────┐      ┌────────────▼────────┐  ┌────────▼────────┐
               │ .validated      │      │ .failed             │  │ .dlq            │
               │ topic           │      │ topic               │  │ topic           │
               └──────────┬──────┘      └────────────┬────────┘  └────────┬────────┘
                          │                          │                    │
                          │ Consumer Group:          │ AI Remediation     │ Human
                          │ migration-loader         │ Agent              │ Review
                          ▼                                               │
               ┌──────────────────────┐                                  │
               │  LOADING SERVICE     │◀─────────────────────────────────┘
               │  - Salesforce Bulk   │    (after human approval)
               │    API 2.0 upsert    │
               │  - Rate limiting     │
               │  - Idempotent via    │
               │    External ID field │
               │  - Publishes to      │
               │    .loaded topic     │
               └──────────┬───────────┘
                          │
                          ▼
               ┌──────────────────────┐
               │ SALESFORCE ORG       │
               │  - Accounts          │
               │  - Contacts          │
               │  - Opportunities     │
               │  - Custom Objects    │
               └──────────────────────┘

  CROSS-CUTTING CONSUMERS (read all topics, independent consumer groups):
  ┌─────────────────────────────────────────────────────────────────────┐
  │  AUDIT SERVICE: Writes all events to immutable audit log (S3+Glue)  │
  │  METRICS SERVICE: Computes lag, throughput, error rates (Prometheus) │
  │  ALERTING SERVICE: Fires PagerDuty/OpsGenie alerts on error rates   │
  │  REPORT SERVICE: Builds migration completion reports (async)         │
  └─────────────────────────────────────────────────────────────────────┘
```

---

## 8. Compliance and Governance Considerations

### 8.1 FISMA / FedRAMP High

- Kafka cluster must be deployed in FedRAMP High authorized cloud environment (GovCloud) or on-premises in government data center
- All Kafka topic data at rest must use FIPS 140-2 validated encryption (AES-256-GCM)
- TLS 1.3 required for all broker-to-broker and client-to-broker communication
- Kafka audit logs (authentication, authorization events) must be forwarded to SIEM within 5 minutes
- Kafka service accounts must be managed via PIV/CAC-backed identity provider where mandated

### 8.2 NIST SP 800-92 (Log Management)

- Kafka retention policy for audit topics: `log.retention.ms = 220903200000` (7 years in milliseconds)
- Tiered storage must replicate to a WORM (Write Once Read Many) S3 bucket with Object Lock enabled
- Log integrity: each retained event includes SHA-256 checksum of payload; batch checksums computed at segment roll

### 8.3 Data Residency

- Topics for EU-based clients must be stored only on brokers in EU regions
- Kafka MirrorMaker 2 replication for disaster recovery must not cross jurisdictional boundaries unless explicitly approved
- Per-tenant key encryption using tenant-specific DEKs (Data Encryption Keys) in HashiCorp Vault (see ADR-004)

### 8.4 Change Management

- Kafka topic configurations (partition count, retention) are version-controlled in Terraform
- Schema Registry schema changes require PR review by 2 engineers; breaking changes require migration window approval
- Consumer group offset manipulation requires change ticket and second approval

---

## 9. Metrics and Observability

### Key Metrics to Monitor

| Metric | Source | Alert Threshold |
|--------|--------|-----------------|
| `kafka_consumer_group_lag` | Kafka JMX → Prometheus | > 100,000 records for > 5 min |
| `kafka_broker_under_replicated_partitions` | Kafka JMX | > 0 for > 1 min |
| `migration_records_extracted_total` | Custom counter | N/A (informational) |
| `migration_records_failed_total` | Custom counter | Error rate > 0.1% |
| `migration_dlq_depth` | Custom gauge | > 1,000 records |
| `kafka_producer_record_error_rate` | Kafka JMX | > 0 |
| `schema_registry_request_error_rate` | Schema Registry metrics | > 0.01% |

### Kafka Consumer Lag Dashboard

Grafana dashboard: `monitoring/dashboards/kafka-migration-pipeline.json`

Key panels:
- Consumer group lag per partition (heatmap)
- Records/second per pipeline stage
- DLQ depth over time
- Error rate by failure category
- End-to-end latency (extraction timestamp → Salesforce confirmation)

---

## 10. Related Decisions

- [ADR-004: Zero Trust Security Model](./ADR-004-zero-trust-security-model.md) — Defines mTLS and SPIFFE certificate management for Kafka clients
- [ADR-005: Data Transformation Strategy](./ADR-005-data-transformation-strategy.md) — Defines the transformation rule engine that consumes from `extracted` topics
- [ADR-006: Multi-Tenant Deployment](./ADR-006-multi-tenant-deployment.md) — Defines Kubernetes namespace isolation that maps to Kafka ACL tenant boundaries
- [ADR-007: AI Agent Orchestration](./ADR-007-ai-agent-orchestration.md) — Defines the AI agents that consume from `failed` and `dlq` topics for remediation

---

*Last reviewed: 2025-11-14*
*Next review due: 2026-05-14 (semi-annual review cycle)*
*Document owner: Platform Architecture Team*
