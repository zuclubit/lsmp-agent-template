# ADR-002: Hexagonal Architecture (Ports & Adapters) as Primary Application Architecture Pattern

**Status:** Accepted
**Date:** 2025-09-03
**Deciders:** Data Architect, Platform Engineering Lead, Engineering Team Leads
**Reviewed By:** Program Director (for cost/timeline implications)
**Classification:** Internal — Restricted

---

## Context

The LSMP must integrate with six heterogeneous external systems across three fundamentally different integration protocols:
- Oracle Siebel CRM (JDBC, Oracle Wallet authentication, Oracle-specific SQL dialect)
- SAP CRM 7.0 (RFC/BAPI, SAP SSO2 tokens, ABAP data structures)
- PostgreSQL Legacy DB (JDBC, async PostgreSQL driver, logical replication)
- Salesforce GC+ (REST API, Bulk API 2.0, OAuth 2.0)
- HashiCorp Vault (HTTPS API, AppRole authentication, dynamic secrets)
- AWS S3 (AWS SDK, S3 presigned URLs, multipart upload)

The initial architecture proposal (August 2025) used a "pragmatic layered" approach — services imported source-specific libraries directly in business logic functions. This approach was prototyped during Phase 1 Foundation work.

### Problems Observed with the Pragmatic Layered Approach

During Phase 1 Foundation development, the team encountered the following issues:

**Testing difficulty.** Unit tests for the extraction use case required a live Oracle database connection. Setting up and maintaining Oracle test instances for each developer's local environment took 2–3 hours and frequently failed due to network connectivity issues. Test suite execution time was 12 minutes for 180 tests (average 4 seconds each due to DB round trips).

**Adapter coupling.** When the PyRFC library (SAP connector) updated from version 2.7 to 2.8, the API change broke 14 functions across 4 different use case modules. The PyRFC import had leaked from the adapter layer into transformation logic and error handling.

**Dry-run mode.** Program management requested a "dry-run" capability — run the full pipeline but write to a mock Salesforce instead of the real Salesforce GC+. Implementing this required modifying 8 functions across the load service because the Salesforce client was instantiated inline rather than injected.

**SAP unavailability.** During a SAP BASIS maintenance window, the entire extraction service was non-functional — not just the SAP adapter. The Oracle Siebel adapter could have continued extracting, but the service was structured such that SAP unavailability caused all extractions to queue.

**Compliance test coverage.** The ISSO requested proof that PII data never flows from the transformation layer back to the source-reading layer. This was difficult to assert because the layers were not cleanly separated.

### Architecture Patterns Considered

| Pattern | Description | Key Trade-off |
|---|---|---|
| **Pragmatic Layered (status quo)** | Business logic calls framework/library code directly | Simple to start; becomes tightly coupled; testing requires live dependencies |
| **Hexagonal Architecture (Ports & Adapters)** | Business logic defines ports (interfaces); adapters implement ports; DI wires them | More upfront structure; enables swappable adapters; clean testability |
| **Clean Architecture** | Strict layering with dependency inversion; Hexagonal is a specific implementation | Same benefits as Hexagonal; Clean Architecture is the family, Hexagonal is the specific implementation we'd use |
| **Plugin Architecture** | External systems as dynamically loaded plugins | Overcomplicated for this team size; runtime loading increases security surface |
| **Functional Core / Imperative Shell** | Pure functions for business logic; I/O at the edges | Excellent for transformation logic; less natural for stateful extraction/load workflows |

### Why Hexagonal Architecture Specifically

Hexagonal Architecture (also called Ports & Adapters, coined by Alistair Cockburn) was chosen over generic Clean Architecture because:
1. The concept of "ports" (interfaces) and "adapters" (implementations) maps directly and intuitively to the LSMP's integration challenge
2. The team had previous familiarity with the pattern from a prior federal migration project
3. The literature for testing hexagonal systems (test adapters, mock adapters) is well-established

---

## Decision

**All application containers in the LSMP will be structured according to Hexagonal Architecture (Ports & Adapters).**

### Architectural Rules (Enforced by CI)

1. **The Dependency Rule:** Source code dependencies can only point inward. The Domain layer has no external dependencies. The Application layer depends on Domain only. The Infrastructure layer depends on Application (via interfaces) and Domain.

2. **Ports are defined in the Application layer.** Every external interaction is expressed as a Python Abstract Base Class (ABC) in `application/ports/`. Examples:
   - `SourceReaderPort[T]` — defines `read_batch(params) → Iterable[T]`
   - `RecordWriterPort[T]` — defines `write_records(records: Iterable[T]) → WriteResult`
   - `SecretProviderPort` — defines `get_secret(path: str) → str`
   - `AuditEmitterPort` — defines `emit(event: AuditEvent) → None`
   - `StagingWriterPort` — defines `write_parquet(df: DataFrame, path: str) → Manifest`

3. **Adapters live in the Infrastructure layer.** Each external system has exactly one adapter that implements a port. Examples:
   - `SiebelAdapter(SourceReaderPort[SiebelRecord])` — in `infrastructure/adapters/siebel_adapter.py`
   - `SalesforceAdapter(RecordWriterPort[SalesforceRecord])` — in `infrastructure/adapters/salesforce_adapter.py`
   - `VaultAdapter(SecretProviderPort)` — in `infrastructure/adapters/vault_adapter.py`
   - `DryRunAdapter(RecordWriterPort[SalesforceRecord])` — mock adapter for dry-run mode

4. **Use Cases are in the Application layer.** Use cases receive adapters via constructor injection. They only import Port ABCs, never concrete adapters.

5. **Domain is pure Python.** No framework imports (FastAPI, SQLAlchemy, boto3, etc.) in `domain/`. All domain objects are Python dataclasses or simple classes.

6. **Enforcement via `import-linter`.** The following rules are enforced in CI:
   ```ini
   [importlinter:contract:domain-isolation]
   name = Domain must not import infrastructure or application
   type = forbidden
   source_modules = domain
   forbidden_modules = infrastructure, application, fastapi, sqlalchemy, boto3, aiokafka

   [importlinter:contract:application-isolation]
   name = Application must not import infrastructure
   type = forbidden
   source_modules = application
   forbidden_modules = infrastructure, fastapi, sqlalchemy, boto3, aiokafka, pyrfc, cx_Oracle
   ```

### File Structure Enforced

```
{service}/
├── domain/
│   ├── entities/          # MigrationJob, MigrationRecord, etc.
│   ├── value_objects/     # BatchId, RecordCount, Checksum, etc.
│   └── events/            # MigrationJobStarted, etc. (dataclasses, no infra deps)
├── application/
│   ├── ports/             # Abstract interfaces (SourceReaderPort, AuditEmitterPort, etc.)
│   └── use_cases/         # ExtractEntityUseCase, LoadRecordsUseCase, etc.
├── infrastructure/
│   └── adapters/          # SiebelAdapter, SalesforceAdapter, VaultAdapter, etc.
└── main.py                # Dependency injection wiring (only file that imports all layers)
```

### Test Adapter Pattern

For every production adapter, a corresponding test adapter is provided:

```python
# application/ports/source_reader_port.py
from abc import ABC, abstractmethod
from typing import Iterable, TypeVar, Generic

T = TypeVar('T')

class SourceReaderPort(ABC, Generic[T]):
    @abstractmethod
    def read_batch(self, partition: PartitionRange) -> Iterable[T]:
        ...

# infrastructure/adapters/siebel_adapter.py  (production)
class SiebelAdapter(SourceReaderPort[SiebelRecord]):
    def read_batch(self, partition: PartitionRange) -> Iterable[SiebelRecord]:
        # Real JDBC connection to Siebel
        ...

# tests/adapters/in_memory_source_adapter.py  (test double)
class InMemorySourceAdapter(SourceReaderPort[dict]):
    def __init__(self, records: list[dict]):
        self._records = records

    def read_batch(self, partition: PartitionRange) -> Iterable[dict]:
        return iter(self._records)  # Returns test data instantly

# tests/test_extract_use_case.py  (unit test — no DB required)
def test_extract_creates_manifest():
    fake_records = [{"ROW_ID": "1-ABC", "NAME": "Test Agency"}, ...]
    use_case = ExtractEntityUseCase(
        source_reader=InMemorySourceAdapter(fake_records),  # injected
        staging_writer=InMemoryS3Writer(),                  # injected
        audit_emitter=InMemoryAuditEmitter(),               # injected
    )
    result = use_case.execute(ExtractionParams(entity_type="Account", batch_id="test-batch"))
    assert result.record_count == len(fake_records)
    assert result.manifest.checksum is not None
```

---

## Consequences

### Positive Consequences

**Testability (measured impact):**
- After refactoring to Hexagonal: unit test execution dropped from 12 minutes to 47 seconds for equivalent coverage
- Test data setup eliminated entirely — no live DB required for unit tests
- Coverage increased from 61% to 87% line coverage (easier to test edge cases with in-memory adapters)
- 1,200 unit tests now run in < 60 seconds in CI

**Dry-run mode implemented in 4 hours:** By swapping `SalesforceAdapter` for `DryRunAdapter` at startup (controlled by `DRY_RUN_MODE` feature flag), the full pipeline runs without touching Salesforce. This was previously a multi-day refactoring effort.

**SAP bulkhead isolation:** The SAP RFC extraction adapter is now isolated in `infrastructure/adapters/sap_adapter.py`. Its failure does not affect `SiebelAdapter` or `PostgreSQLAdapter` — they are independent implementations of the same port.

**Compliance auditability:** `import-linter` provides a CI-enforced proof that no PII-handling code in the Domain or Application layers directly touches storage adapters. The dependency graph is machine-verifiable.

**Parallel adapter development:** During Phase 1, the SAP RFC Adapter and Siebel JDBC Adapter were developed by two engineers simultaneously against the same `SourceReaderPort` interface. Integration testing connected both adapters to the same use case in week 6 with zero rework.

### Negative Consequences

**Upfront boilerplate:** Each new external system requires:
1. Defining or extending a Port ABC
2. Creating an adapter class
3. Creating a test adapter
4. Wiring in `main.py`

This is approximately 30–60 minutes of additional setup per new integration vs. the pragmatic approach.

**Indirection for simple adapters:** For simple integrations (e.g., the Config Service client, which only has one method), the full adapter pattern adds indirection that some engineers find unnecessary. Addressed by: team convention that adapters < 50 lines may omit the intermediate ABC if there is only ever one implementation and tests use `unittest.mock.MagicMock`.

**Learning curve for junior engineers:** Engineers unfamiliar with dependency injection and interface-based design needed 2–3 days to become productive with the pattern. Addressed by: pair programming sessions in first two weeks, architecture kata exercises, annotated example adapters in the codebase.

**`main.py` becomes complex:** The dependency injection wiring in `main.py` grows with each adapter. Addressed by: factory functions (`create_extraction_service()`, `create_load_service()`) that group related wiring, keeping `main.py` under 100 lines.

### Metrics to Validate the Decision

The following metrics are tracked monthly and reviewed at each phase retrospective:

| Metric | Baseline (Layered) | Target | Current |
|---|---|---|---|
| Unit test execution time | 12 minutes | < 90 seconds | 47 seconds |
| Line test coverage | 61% | ≥ 85% | 87% |
| Time to implement new dry-run mode | 3+ days (estimated) | < 1 day | 4 hours |
| CVEs due to adapter library leaking into domain | High risk | 0 confirmed leaks | 0 (import-linter enforced) |
| Onboarding time for new engineer | N/A (pre-decision) | < 1 week to first PR | 4 days average |

---

## Notes

This decision applies to all containers developed as part of the LSMP. It does not apply to:
- Infrastructure-as-Code (Terraform modules)
- Airflow DAG definitions (which are inherently infrastructure-coupled)
- Simple utility scripts (< 200 lines, no domain logic)

The Transformation Engine (Spark) uses a modified application of this pattern where the "adapter" concept maps to Spark UDFs and DataFrameReader/Writer configurations, rather than classical interface injection. This variation is documented in the Transformation Engine component diagram.

---

*ADR maintained in Git at `architecture/decisions/ADR-002-hexagonal-architecture.md`. This decision is in effect for all LSMP service development. Deviations require Architecture Board review and a documented justification in the relevant service's README.*
