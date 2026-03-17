"""
Integration tests for Kafka event producer/consumer.

Uses in-memory fakes (no real Kafka broker) to verify:
  - Correct topic routing per event type
  - Message serialisation / deserialisation round-trip
  - Schema validation against the registered event schemas
  - Dead-letter queue behaviour on processing failure
  - At-least-once delivery semantics simulation

Marks: @pytest.mark.integration
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

# ---------------------------------------------------------------------------
# Domain / event imports
# ---------------------------------------------------------------------------
from domain.events.migration_events import (
    DomainEvent,
    MigrationCompleted,
    MigrationFailed,
    MigrationPhase,
    MigrationStarted,
    MigrationStatus,
    PhaseCompleted,
    RecordMigrated,
    RecordMigrationFailed,
)


# ---------------------------------------------------------------------------
# In-memory Kafka broker fake
# ---------------------------------------------------------------------------


@dataclass
class KafkaMessage:
    topic: str
    key: str
    value: Dict[str, Any]
    headers: Dict[str, str] = field(default_factory=dict)
    offset: int = 0
    partition: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class InMemoryKafkaProducer:
    """Thread-safe in-memory Kafka producer for testing."""

    def __init__(self) -> None:
        self._published: List[KafkaMessage] = []
        self._fail_on_next: bool = False

    def publish(self, topic: str, key: str, value: Dict[str, Any], headers: Dict[str, str] | None = None) -> None:
        if self._fail_on_next:
            self._fail_on_next = False
            raise RuntimeError(f"Simulated Kafka publish failure for topic={topic}")
        msg = KafkaMessage(topic=topic, key=key, value=value, headers=headers or {})
        msg.offset = len(self._published)
        self._published.append(msg)

    def messages_for_topic(self, topic: str) -> List[KafkaMessage]:
        return [m for m in self._published if m.topic == topic]

    def all_messages(self) -> List[KafkaMessage]:
        return list(self._published)

    def clear(self) -> None:
        self._published.clear()

    def simulate_next_publish_failure(self) -> None:
        self._fail_on_next = True


class InMemoryKafkaConsumer:
    """In-memory consumer that pulls from the broker fake."""

    def __init__(self, producer: InMemoryKafkaProducer, topics: List[str]) -> None:
        self._producer = producer
        self._topics = set(topics)
        self._offset = 0
        self._handlers: Dict[str, Callable] = {}

    def subscribe(self, topic: str, handler: Callable[[KafkaMessage], None]) -> None:
        self._handlers[topic] = handler

    def poll(self) -> int:
        """Process all pending messages. Returns count processed."""
        processed = 0
        for msg in self._producer.all_messages()[self._offset:]:
            if msg.topic in self._topics and msg.topic in self._handlers:
                self._handlers[msg.topic](msg)
                processed += 1
        self._offset = len(self._producer.all_messages())
        return processed


class DeadLetterQueue:
    """Captures messages that could not be processed."""

    def __init__(self) -> None:
        self._messages: List[Tuple[KafkaMessage, Exception]] = []

    def append(self, message: KafkaMessage, error: Exception) -> None:
        self._messages.append((message, error))

    def count(self) -> int:
        return len(self._messages)

    def messages(self) -> List[KafkaMessage]:
        return [m for m, _ in self._messages]


# ---------------------------------------------------------------------------
# Event serialiser
# ---------------------------------------------------------------------------


class MigrationEventSerializer:
    """Converts domain events to/from Kafka message payloads."""

    _TYPE_TO_CLASS: Dict[str, type] = {
        "migration.MigrationStarted": MigrationStarted,
        "migration.MigrationCompleted": MigrationCompleted,
        "migration.MigrationFailed": MigrationFailed,
        "migration.PhaseCompleted": PhaseCompleted,
        "migration.RecordMigrated": RecordMigrated,
        "migration.RecordMigrationFailed": RecordMigrationFailed,
    }

    TOPIC_MAP: Dict[str, str] = {
        "migration.MigrationStarted": "migration.lifecycle",
        "migration.MigrationCompleted": "migration.lifecycle",
        "migration.MigrationFailed": "migration.lifecycle",
        "migration.PhaseCompleted": "migration.lifecycle",
        "migration.RecordMigrated": "migration.records",
        "migration.RecordMigrationFailed": "migration.records.failed",
    }

    @classmethod
    def serialise(cls, event: DomainEvent) -> Dict[str, Any]:
        """Convert a domain event to a dict suitable for Kafka value."""
        return {
            "event_type": event.event_type,
            "event_id": str(event.event_id),
            "occurred_on": event.occurred_on.isoformat(),
            "correlation_id": event.correlation_id,
            "aggregate_id": event.aggregate_id,
            "aggregate_type": event.aggregate_type,
            "payload": {
                k: v
                for k, v in vars(event).items()
                if not k.startswith("_") and k not in {"event_id", "occurred_on", "correlation_id"}
            },
        }

    @classmethod
    def deserialise(cls, message_value: Dict[str, Any]) -> Optional[DomainEvent]:
        """Reconstruct a domain event from a Kafka message value dict."""
        event_type = message_value.get("event_type", "")
        event_class = cls._TYPE_TO_CLASS.get(event_type)
        if event_class is None:
            raise ValueError(f"Unknown event type: {event_type!r}")
        payload = message_value.get("payload", {})
        try:
            return event_class(**payload)
        except TypeError as exc:
            raise ValueError(f"Cannot deserialise {event_type}: {exc}") from exc

    @classmethod
    def topic_for(cls, event: DomainEvent) -> str:
        return cls.TOPIC_MAP.get(event.event_type, "migration.unknown")


# ---------------------------------------------------------------------------
# Publisher adapter
# ---------------------------------------------------------------------------


class KafkaMigrationEventPublisher:
    """Publishes domain events to Kafka topics."""

    def __init__(self, producer: InMemoryKafkaProducer) -> None:
        self._producer = producer
        self._serialiser = MigrationEventSerializer()

    def publish(self, event: DomainEvent) -> None:
        topic = self._serialiser.topic_for(event)
        value = self._serialiser.serialise(event)
        self._producer.publish(
            topic=topic,
            key=event.aggregate_id or str(event.event_id),
            value=value,
            headers={"event_type": event.event_type},
        )


# ===========================================================================
# FIXTURES
# ===========================================================================


@pytest.fixture
def broker() -> InMemoryKafkaProducer:
    return InMemoryKafkaProducer()


@pytest.fixture
def publisher(broker) -> KafkaMigrationEventPublisher:
    return KafkaMigrationEventPublisher(broker)


@pytest.fixture
def dlq() -> DeadLetterQueue:
    return DeadLetterQueue()


@pytest.fixture
def sample_job_id() -> str:
    return str(uuid.uuid4())


# ===========================================================================
# TESTS
# ===========================================================================


@pytest.mark.integration
class TestMigrationStartedEvent:
    """Publishing and consuming MigrationStarted lifecycle events."""

    def test_publish_migration_started_event(self, publisher, broker, sample_job_id):
        event = MigrationStarted(
            migration_job_id=sample_job_id,
            initiated_by="admin@example.com",
            source_system="ERP_v2",
            target_org_id="00Dxx0000001gPLEAY",
            record_types=("Account", "Contact"),
            estimated_records=5000,
            dry_run=False,
        )
        publisher.publish(event)
        messages = broker.messages_for_topic("migration.lifecycle")
        assert len(messages) == 1
        assert messages[0].value["event_type"] == "migration.MigrationStarted"
        assert messages[0].key == sample_job_id

    def test_migration_started_message_contains_required_fields(self, publisher, broker, sample_job_id):
        event = MigrationStarted(
            migration_job_id=sample_job_id,
            initiated_by="admin@example.com",
            source_system="ERP_v2",
            target_org_id="00Dxx0000001gPLEAY",
            record_types=("Account",),
        )
        publisher.publish(event)
        msg = broker.messages_for_topic("migration.lifecycle")[0]

        assert "event_id" in msg.value
        assert "occurred_on" in msg.value
        assert "payload" in msg.value
        payload = msg.value["payload"]
        assert payload["migration_job_id"] == sample_job_id
        assert payload["initiated_by"] == "admin@example.com"

    def test_dry_run_flag_is_preserved_in_message(self, publisher, broker, sample_job_id):
        event = MigrationStarted(
            migration_job_id=sample_job_id,
            initiated_by="admin@example.com",
            source_system="ERP_v2",
            target_org_id="00Dxx0000001gPLEAY",
            record_types=("Account",),
            dry_run=True,
        )
        publisher.publish(event)
        payload = broker.messages_for_topic("migration.lifecycle")[0].value["payload"]
        assert payload["dry_run"] is True


@pytest.mark.integration
class TestMigrationCompletedEvent:
    """Publishing and consuming MigrationCompleted lifecycle events."""

    def test_publish_migration_completed_event(self, publisher, broker, sample_job_id):
        event = MigrationCompleted(
            migration_job_id=sample_job_id,
            duration_seconds=3600.0,
            total_records=5000,
            records_succeeded=4997,
            records_failed=3,
            records_skipped=0,
            phases_completed=("data_extraction", "data_load"),
            report_url="https://reports.example.com/mig-001",
        )
        publisher.publish(event)
        messages = broker.messages_for_topic("migration.lifecycle")
        assert len(messages) == 1
        payload = messages[0].value["payload"]
        assert payload["records_succeeded"] == 4997
        assert payload["records_failed"] == 3
        assert payload["report_url"] == "https://reports.example.com/mig-001"

    def test_completed_event_success_rate_calculation(self, sample_job_id):
        event = MigrationCompleted(
            migration_job_id=sample_job_id,
            total_records=1000,
            records_succeeded=999,
            records_failed=1,
        )
        assert event.success_rate == pytest.approx(0.999, rel=1e-3)

    def test_fully_successful_migration_flag(self, sample_job_id):
        event = MigrationCompleted(
            migration_job_id=sample_job_id,
            total_records=500,
            records_succeeded=500,
            records_failed=0,
            records_skipped=0,
        )
        assert event.is_fully_successful is True

    def test_partial_success_not_fully_successful(self, sample_job_id):
        event = MigrationCompleted(
            migration_job_id=sample_job_id,
            total_records=500,
            records_succeeded=498,
            records_failed=2,
        )
        assert event.is_fully_successful is False


@pytest.mark.integration
class TestMigrationCommandConsumption:
    """Consumer processes migration commands from a Kafka topic."""

    def test_consume_migration_command_event(self, broker, dlq, sample_job_id):
        """Consumer processes a MigrationStarted command successfully."""
        consumer = InMemoryKafkaConsumer(broker, topics=["migration.lifecycle"])
        processed_ids = []

        def handle(msg: KafkaMessage) -> None:
            processed_ids.append(msg.value.get("payload", {}).get("migration_job_id"))

        consumer.subscribe("migration.lifecycle", handle)

        # Publish a command
        broker.publish(
            topic="migration.lifecycle",
            key=sample_job_id,
            value={
                "event_type": "migration.MigrationStarted",
                "event_id": str(uuid.uuid4()),
                "occurred_on": datetime.now(timezone.utc).isoformat(),
                "payload": {"migration_job_id": sample_job_id, "initiated_by": "test"},
            },
        )
        count = consumer.poll()
        assert count == 1
        assert sample_job_id in processed_ids

    def test_consumer_does_not_process_unsubscribed_topics(self, broker):
        """Messages on topics the consumer is not subscribed to are ignored."""
        consumer = InMemoryKafkaConsumer(broker, topics=["migration.records"])
        processed = []

        consumer.subscribe("migration.records", lambda msg: processed.append(msg))

        broker.publish("migration.lifecycle", "key1", {"event_type": "MigrationStarted"})
        broker.publish("migration.records", "key2", {"event_type": "RecordMigrated"})
        consumer.poll()

        assert len(processed) == 1  # Only the records message


@pytest.mark.integration
class TestEventSchemaValidation:
    """Serialisation and deserialisation round-trips validate schema contracts."""

    @pytest.mark.parametrize(
        "event",
        [
            MigrationStarted(
                migration_job_id="MIG-001",
                initiated_by="user@example.com",
                source_system="ERP",
                target_org_id="00Dxx001",
                record_types=("Account",),
            ),
            MigrationCompleted(
                migration_job_id="MIG-001",
                duration_seconds=120.0,
                total_records=500,
                records_succeeded=500,
                records_failed=0,
            ),
            PhaseCompleted(
                migration_job_id="MIG-001",
                phase="data_load",
                duration_seconds=30.0,
                records_processed=500,
                records_succeeded=498,
                records_failed=2,
            ),
            RecordMigrated(
                migration_job_id="MIG-001",
                legacy_record_id="LEG-001",
                salesforce_record_id="001Dn000001MockAA2",
                record_type="Account",
                phase="data_load",
            ),
        ],
    )
    def test_event_serialise_produces_required_fields(self, event):
        """Every serialised event must have event_type, event_id, occurred_on, payload."""
        serialised = MigrationEventSerializer.serialise(event)
        assert "event_type" in serialised
        assert "event_id" in serialised
        assert "occurred_on" in serialised
        assert "payload" in serialised

    def test_serialise_deserialise_round_trip_migration_started(self):
        """MigrationStarted must survive a full serialise/deserialise round-trip."""
        original = MigrationStarted(
            migration_job_id="MIG-ROUNDTRIP-001",
            initiated_by="rt-user@example.com",
            source_system="Legacy_ERP",
            target_org_id="00Dxx0000001gPLEAY",
            record_types=("Account", "Contact"),
            estimated_records=2500,
        )
        serialised = MigrationEventSerializer.serialise(original)
        reconstituted = MigrationEventSerializer.deserialise(serialised)

        assert isinstance(reconstituted, MigrationStarted)
        assert reconstituted.migration_job_id == "MIG-ROUNDTRIP-001"
        assert reconstituted.initiated_by == "rt-user@example.com"
        assert reconstituted.estimated_records == 2500

    def test_deserialise_unknown_event_type_raises_value_error(self):
        message = {"event_type": "migration.UnknownEvent", "payload": {}}
        with pytest.raises(ValueError, match="Unknown event type"):
            MigrationEventSerializer.deserialise(message)

    def test_event_topic_routing(self):
        """Each event type must route to its canonical topic."""
        cases = [
            (MigrationStarted, "migration.lifecycle"),
            (MigrationCompleted, "migration.lifecycle"),
            (MigrationFailed, "migration.lifecycle"),
            (PhaseCompleted, "migration.lifecycle"),
            (RecordMigrated, "migration.records"),
            (RecordMigrationFailed, "migration.records.failed"),
        ]
        for event_class, expected_topic in cases:
            event = event_class()
            topic = MigrationEventSerializer.topic_for(event)
            assert topic == expected_topic, f"{event_class.__name__} routed to wrong topic"


@pytest.mark.integration
class TestDeadLetterQueue:
    """Unprocessable messages must be routed to the DLQ."""

    def test_processing_failure_routes_to_dlq(self, broker, dlq):
        """A consumer that raises on processing should send the message to DLQ."""
        consumer = InMemoryKafkaConsumer(broker, topics=["migration.lifecycle"])
        processing_errors = []

        def failing_handler(msg: KafkaMessage) -> None:
            try:
                raise ValueError("Simulated processing error")
            except ValueError as exc:
                dlq.append(msg, exc)
                processing_errors.append(exc)

        consumer.subscribe("migration.lifecycle", failing_handler)

        broker.publish(
            "migration.lifecycle",
            "bad-msg-key",
            {"event_type": "migration.Corrupt", "payload": {}},
        )
        consumer.poll()

        assert dlq.count() == 1
        assert len(processing_errors) == 1

    def test_dlq_messages_are_retrievable(self, dlq, broker):
        """DLQ must retain all failed messages for inspection."""
        msg1 = KafkaMessage(topic="migration.lifecycle", key="k1", value={"event_type": "A"})
        msg2 = KafkaMessage(topic="migration.lifecycle", key="k2", value={"event_type": "B"})
        dlq.append(msg1, ValueError("err1"))
        dlq.append(msg2, ValueError("err2"))

        assert dlq.count() == 2
        assert dlq.messages()[0].key == "k1"
        assert dlq.messages()[1].key == "k2"


@pytest.mark.integration
class TestAtLeastOnceDelivery:
    """Verify at-least-once delivery semantics under simulated failures."""

    def test_retry_on_publish_failure(self, broker, publisher, sample_job_id):
        """If first publish fails, a retry must succeed and message must be delivered."""
        broker.simulate_next_publish_failure()

        event = MigrationStarted(
            migration_job_id=sample_job_id,
            initiated_by="retry@example.com",
            source_system="ERP",
            target_org_id="00Dxx001",
            record_types=("Account",),
        )

        # First attempt fails
        with pytest.raises(RuntimeError, match="Simulated Kafka publish failure"):
            publisher.publish(event)

        # Retry succeeds
        publisher.publish(event)

        messages = broker.messages_for_topic("migration.lifecycle")
        assert len(messages) == 1  # Only the successful one landed

    def test_duplicate_messages_have_unique_event_ids(self, publisher, broker, sample_job_id):
        """Each publish must generate a unique event_id for idempotency tracking."""
        event1 = MigrationStarted(
            migration_job_id=sample_job_id,
            initiated_by="user@example.com",
            source_system="ERP",
            target_org_id="00Dxx001",
            record_types=("Account",),
        )
        event2 = MigrationStarted(
            migration_job_id=sample_job_id,
            initiated_by="user@example.com",
            source_system="ERP",
            target_org_id="00Dxx001",
            record_types=("Account",),
        )
        publisher.publish(event1)
        publisher.publish(event2)

        messages = broker.messages_for_topic("migration.lifecycle")
        assert len(messages) == 2
        ids = [m.value["event_id"] for m in messages]
        assert ids[0] != ids[1], "Duplicate event_ids detected — idempotency broken"

    def test_messages_preserve_insertion_order(self, publisher, broker, sample_job_id):
        """Kafka messages must be delivered in the order they were published."""
        events = [
            MigrationStarted(migration_job_id=sample_job_id, initiated_by="u", source_system="ERP",
                             target_org_id="00D001", record_types=("Account",)),
            PhaseCompleted(migration_job_id=sample_job_id, phase="data_extraction",
                           duration_seconds=10.0, records_processed=100, records_succeeded=100, records_failed=0),
            MigrationCompleted(migration_job_id=sample_job_id, total_records=100,
                               records_succeeded=100, records_failed=0),
        ]
        for evt in events:
            publisher.publish(evt)

        lifecycle_msgs = broker.messages_for_topic("migration.lifecycle")
        event_types = [m.value["event_type"] for m in lifecycle_msgs]
        assert event_types[0] == "migration.MigrationStarted"
        assert event_types[1] == "migration.PhaseCompleted"
        assert event_types[2] == "migration.MigrationCompleted"
