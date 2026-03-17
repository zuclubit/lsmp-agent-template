"""
EventListenerAdapter – inbound adapter for consuming domain events.

Listens to migration domain events published to Kafka (or Azure Service Bus)
and dispatches them to registered application-layer handlers.

Event handlers perform side effects such as:
  - Updating dashboards / websocket clients
  - Triggering downstream workflows
  - Writing audit logs
  - Sending notifications

This adapter runs as a long-lived background task (e.g. started with
asyncio.create_task() in the FastAPI lifespan or in a dedicated worker process).

Supports:
  1. Kafka consumer (confluent-kafka)
  2. Azure Service Bus subscription consumer
  3. In-memory event bus (for testing / single-process mode)
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Type alias for event handlers
EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


# ---------------------------------------------------------------------------
# In-memory event bus (single-process / test mode)
# ---------------------------------------------------------------------------


class InMemoryEventBus:
    """
    Simple in-memory pub/sub bus.

    publish() puts events into a queue; listeners consume them via run().
    Used for testing and single-process deployments.
    """

    def __init__(self, max_queue_size: int = 10_000) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for the given event type (e.g. 'migration.MigrationStarted')."""
        self._handlers.setdefault(event_type, []).append(handler)
        logger.debug("Subscribed handler %s for event type %s", handler.__name__, event_type)

    async def publish(self, event: dict[str, Any]) -> None:
        await self._queue.put(event)

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """Consume events from the queue until stop_event is set."""
        logger.info("InMemoryEventBus: starting event loop")
        while not (stop_event and stop_event.is_set()):
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._dispatch(event)
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.exception("Event dispatch failed: %s", exc)

    async def _dispatch(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")
        handlers = self._handlers.get(event_type, []) + self._handlers.get("*", [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:
                logger.error("Handler %s failed for event %s: %s", handler.__name__, event_type, exc)


# ---------------------------------------------------------------------------
# Kafka event listener
# ---------------------------------------------------------------------------


class KafkaEventListener:
    """
    Consumes domain events from Apache Kafka topics.

    Runs in a background thread (confluent-kafka is not async-native) and
    bridges events to the async event handlers via asyncio.Queue.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        group_id: str,
        topics: list[str],
        event_bus: InMemoryEventBus,
        consumer_config: Optional[dict[str, Any]] = None,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._group_id = group_id
        self._topics = topics
        self._bus = event_bus
        self._extra_config = consumer_config or {}
        self._running = False

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        try:
            from confluent_kafka import Consumer, KafkaError  # type: ignore
        except ImportError:
            raise RuntimeError("confluent-kafka is not installed. Run: pip install confluent-kafka")

        config = {
            "bootstrap.servers": self._bootstrap_servers,
            "group.id": self._group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "session.timeout.ms": 30_000,
            **self._extra_config,
        }

        consumer = Consumer(config)
        consumer.subscribe(self._topics)
        self._running = True
        loop = asyncio.get_event_loop()

        logger.info("Kafka consumer started: topics=%s group=%s", self._topics, self._group_id)

        try:
            while self._running and not (stop_event and stop_event.is_set()):
                msg = await loop.run_in_executor(None, lambda: consumer.poll(timeout=1.0))

                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("Kafka error: %s", msg.error())
                    continue

                try:
                    payload = json.loads(msg.value().decode("utf-8"))
                    await self._bus.publish(payload)
                    consumer.commit(msg)
                    logger.debug(
                        "Kafka message consumed: topic=%s partition=%s offset=%s",
                        msg.topic(), msg.partition(), msg.offset(),
                    )
                except json.JSONDecodeError as exc:
                    logger.error("Malformed event payload: %s", exc)
        finally:
            consumer.close()
            logger.info("Kafka consumer stopped")

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Azure Service Bus event listener
# ---------------------------------------------------------------------------


class ServiceBusEventListener:
    """
    Consumes domain events from Azure Service Bus topic subscriptions.
    """

    def __init__(
        self,
        connection_string: str,
        topic_name: str,
        subscription_name: str,
        event_bus: InMemoryEventBus,
    ) -> None:
        self._connection_string = connection_string
        self._topic_name = topic_name
        self._subscription_name = subscription_name
        self._bus = event_bus
        self._running = False

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        try:
            from azure.servicebus.aio import ServiceBusClient  # type: ignore
        except ImportError:
            raise RuntimeError("azure-servicebus is not installed. Run: pip install azure-servicebus")

        self._running = True
        logger.info(
            "Service Bus listener started: topic=%s subscription=%s",
            self._topic_name, self._subscription_name,
        )

        async with ServiceBusClient.from_connection_string(self._connection_string) as client:
            async with client.get_subscription_receiver(
                topic_name=self._topic_name,
                subscription_name=self._subscription_name,
                max_wait_time=5,
            ) as receiver:
                while self._running and not (stop_event and stop_event.is_set()):
                    async for message in receiver:
                        try:
                            payload = json.loads(str(message))
                            await self._bus.publish(payload)
                            await receiver.complete_message(message)
                        except Exception as exc:
                            logger.error("Failed to process Service Bus message: %s", exc)
                            await receiver.abandon_message(message)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Built-in event handlers
# ---------------------------------------------------------------------------


class MigrationEventHandlers:
    """
    Collection of application-level handlers for migration domain events.

    Each handler receives the CloudEvents envelope dict and performs side
    effects (logging, audit, downstream triggers).
    """

    def __init__(self, audit_log_path: str = "/var/log/migration/audit.jsonl") -> None:
        import pathlib
        self._audit_path = pathlib.Path(audit_log_path)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)

    def register_all(self, bus: InMemoryEventBus) -> None:
        """Register all handlers on the given bus."""
        bus.subscribe("migration.migrationstarted", self.on_migration_started)
        bus.subscribe("migration.migrationcompleted", self.on_migration_completed)
        bus.subscribe("migration.migrationfailed", self.on_migration_failed)
        bus.subscribe("migration.migrationpaused", self.on_migration_paused)
        bus.subscribe("migration.phasecompleted", self.on_phase_completed)
        bus.subscribe("migration.recordmigrated", self.on_record_migrated)
        bus.subscribe("migration.recordmigrationfailed", self.on_record_migration_failed)
        bus.subscribe("*", self.on_any_event)  # wildcard: write all to audit log

    async def on_migration_started(self, event: dict[str, Any]) -> None:
        data = event.get("data", {})
        logger.info(
            "EVENT: MigrationStarted | job=%s source=%s dry_run=%s estimated=%s",
            data.get("migration_job_id"),
            data.get("source_system"),
            data.get("dry_run"),
            data.get("estimated_records"),
        )

    async def on_migration_completed(self, event: dict[str, Any]) -> None:
        data = event.get("data", {})
        logger.info(
            "EVENT: MigrationCompleted | job=%s succeeded=%s failed=%s duration=%.1fs",
            data.get("migration_job_id"),
            data.get("records_succeeded"),
            data.get("records_failed"),
            data.get("duration_seconds", 0.0),
        )

    async def on_migration_failed(self, event: dict[str, Any]) -> None:
        data = event.get("data", {})
        logger.error(
            "EVENT: MigrationFailed | job=%s phase=%s error=%s",
            data.get("migration_job_id"),
            data.get("failed_phase"),
            data.get("error_message"),
        )

    async def on_migration_paused(self, event: dict[str, Any]) -> None:
        data = event.get("data", {})
        logger.info(
            "EVENT: MigrationPaused | job=%s by=%s reason=%s",
            data.get("migration_job_id"),
            data.get("paused_by"),
            data.get("reason"),
        )

    async def on_phase_completed(self, event: dict[str, Any]) -> None:
        data = event.get("data", {})
        logger.info(
            "EVENT: PhaseCompleted | job=%s phase=%s succeeded=%s failed=%s duration=%.1fs",
            data.get("migration_job_id"),
            data.get("phase"),
            data.get("records_succeeded"),
            data.get("records_failed"),
            data.get("duration_seconds", 0.0),
        )

    async def on_record_migrated(self, event: dict[str, Any]) -> None:
        # High-volume: only log at DEBUG to avoid spam
        data = event.get("data", {})
        logger.debug(
            "EVENT: RecordMigrated | legacy=%s → sf=%s type=%s",
            data.get("legacy_record_id"),
            data.get("salesforce_record_id"),
            data.get("record_type"),
        )

    async def on_record_migration_failed(self, event: dict[str, Any]) -> None:
        data = event.get("data", {})
        logger.warning(
            "EVENT: RecordMigrationFailed | legacy=%s type=%s error=%s retryable=%s",
            data.get("legacy_record_id"),
            data.get("record_type"),
            data.get("error_code"),
            data.get("retryable"),
        )

    async def on_any_event(self, event: dict[str, Any]) -> None:
        """Write every event to the audit log."""
        try:
            entry = {
                "audit_ts": datetime.now(tz=timezone.utc).isoformat(),
                "event_id": event.get("id"),
                "event_type": event.get("type"),
                "aggregate_id": event.get("aggregateid"),
                "correlation_id": event.get("correlationid"),
            }
            with self._audit_path.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.debug("Audit log write failed: %s", exc)


# ---------------------------------------------------------------------------
# Composite listener (factory / wiring helper)
# ---------------------------------------------------------------------------


class EventListenerAdapter:
    """
    Top-level adapter that:
    1. Creates an InMemoryEventBus.
    2. Registers all domain event handlers.
    3. Starts the chosen transport listener (Kafka, Service Bus, or in-memory).
    4. Runs the bus dispatcher.

    Usage::

        adapter = EventListenerAdapter.kafka(
            bootstrap_servers="kafka:9092",
            topics=["migration.migrationstarted", "migration.migrationcompleted"],
        )
        stop = asyncio.Event()
        asyncio.gather(adapter.run(stop), ...)
    """

    def __init__(
        self,
        bus: InMemoryEventBus,
        transport_listeners: list[Any],  # KafkaEventListener | ServiceBusEventListener | etc.
    ) -> None:
        self._bus = bus
        self._transport_listeners = transport_listeners

    @classmethod
    def kafka(
        cls,
        bootstrap_servers: str,
        topics: list[str],
        group_id: str = "migration-service",
        audit_log_path: str = "/var/log/migration/audit.jsonl",
    ) -> "EventListenerAdapter":
        bus = InMemoryEventBus()
        handlers = MigrationEventHandlers(audit_log_path=audit_log_path)
        handlers.register_all(bus)
        listener = KafkaEventListener(bootstrap_servers, group_id, topics, bus)
        return cls(bus=bus, transport_listeners=[listener])

    @classmethod
    def service_bus(
        cls,
        connection_string: str,
        topic_name: str,
        subscription_name: str,
        audit_log_path: str = "/var/log/migration/audit.jsonl",
    ) -> "EventListenerAdapter":
        bus = InMemoryEventBus()
        handlers = MigrationEventHandlers(audit_log_path=audit_log_path)
        handlers.register_all(bus)
        listener = ServiceBusEventListener(connection_string, topic_name, subscription_name, bus)
        return cls(bus=bus, transport_listeners=[listener])

    @classmethod
    def in_memory(
        cls,
        audit_log_path: str = "/tmp/migration_audit.jsonl",
    ) -> "EventListenerAdapter":
        """Create an in-process adapter suitable for testing."""
        bus = InMemoryEventBus()
        handlers = MigrationEventHandlers(audit_log_path=audit_log_path)
        handlers.register_all(bus)
        return cls(bus=bus, transport_listeners=[])

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """Start all transport listeners and the bus dispatcher concurrently."""
        tasks = [self._bus.run(stop_event)]
        for listener in self._transport_listeners:
            tasks.append(listener.run(stop_event))
        await asyncio.gather(*tasks)

    async def publish(self, event: dict[str, Any]) -> None:
        """Directly publish an event dict (for testing or local triggers)."""
        await self._bus.publish(event)

    def stop_all(self) -> None:
        for listener in self._transport_listeners:
            if hasattr(listener, "stop"):
                listener.stop()
