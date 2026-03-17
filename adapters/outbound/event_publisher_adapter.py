"""
EventPublisherAdapter – outbound adapter for domain event publishing.

Supports two transport backends, selected by configuration:
  1. Apache Kafka  (via confluent-kafka)
  2. Azure Service Bus (via azure-servicebus)

Domain events are serialised to JSON and routed by event type.
Each backend is wrapped in its own class; the EventPublisherAdapter
selects between them at construction time.

Design:
  - Retries with exponential back-off (3 attempts by default).
  - Failed publications are written to a dead-letter file for manual replay.
  - All methods are async; Kafka/Service Bus I/O happens in an executor.
  - No domain types are imported by the concrete publisher classes; they
    receive pre-serialised dicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from domain.events.migration_events import DomainEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _event_to_dict(event: DomainEvent) -> dict[str, Any]:
    """
    Serialise a DomainEvent frozen dataclass to a JSON-safe dictionary.

    The top-level envelope adds schema versioning and routing metadata.
    """
    body = {}
    for field_name in event.__dataclass_fields__:  # type: ignore[attr-defined]
        val = getattr(event, field_name)
        if isinstance(val, datetime):
            body[field_name] = val.isoformat()
        elif isinstance(val, uuid.UUID):
            body[field_name] = str(val)
        elif isinstance(val, tuple):
            body[field_name] = list(val)
        else:
            body[field_name] = val

    return {
        "specversion": "1.0",
        "id": str(event.event_id),
        "type": event.event_type,
        "source": "migration-service",
        "time": event.occurred_on.isoformat(),
        "datacontenttype": "application/json",
        "correlationid": event.correlation_id or "",
        "aggregateid": event.aggregate_id,
        "aggregatetype": event.aggregate_type,
        "data": body,
    }


# ---------------------------------------------------------------------------
# Abstract transport
# ---------------------------------------------------------------------------


class EventTransport(ABC):
    """Abstract base for event transport backends."""

    @abstractmethod
    async def publish(self, topic: str, key: str, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Kafka transport
# ---------------------------------------------------------------------------


class KafkaEventTransport(EventTransport):
    """
    Publishes domain events to Apache Kafka topics.

    Topic naming convention: migration.<EventType> (lowercase, dot-separated).
    E.g.: migration.migrationstarted, migration.migrationcompleted
    """

    def __init__(
        self,
        bootstrap_servers: str,
        topic_prefix: str = "migration",
        producer_config: Optional[dict[str, Any]] = None,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topic_prefix = topic_prefix
        self._extra_config = producer_config or {}
        self._producer: Any = None  # confluent_kafka.Producer
        self._loop = asyncio.get_event_loop()

    def _get_producer(self) -> Any:
        if self._producer is None:
            try:
                from confluent_kafka import Producer  # type: ignore
                config = {
                    "bootstrap.servers": self._bootstrap_servers,
                    "client.id": "migration-service-event-publisher",
                    "acks": "all",
                    "retries": 3,
                    "compression.type": "snappy",
                    **self._extra_config,
                }
                self._producer = Producer(config)
                logger.info("Kafka producer initialised: %s", self._bootstrap_servers)
            except ImportError:
                raise RuntimeError(
                    "confluent-kafka is not installed. "
                    "Run: pip install confluent-kafka"
                )
        return self._producer

    async def publish(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        producer = self._get_producer()
        value = json.dumps(payload, default=str).encode("utf-8")

        def _produce() -> None:
            producer.produce(
                topic,
                key=key.encode("utf-8"),
                value=value,
                on_delivery=self._on_delivery,
            )
            producer.poll(0)

        await self._loop.run_in_executor(None, _produce)

    async def close(self) -> None:
        if self._producer:
            await self._loop.run_in_executor(None, lambda: self._producer.flush(30))
            self._producer = None

    @staticmethod
    def _on_delivery(err: Any, msg: Any) -> None:
        if err:
            logger.error("Kafka delivery failed: %s", err)
        else:
            logger.debug(
                "Kafka message delivered: topic=%s partition=%s offset=%s",
                msg.topic(), msg.partition(), msg.offset(),
            )


# ---------------------------------------------------------------------------
# Azure Service Bus transport
# ---------------------------------------------------------------------------


class ServiceBusEventTransport(EventTransport):
    """
    Publishes domain events to Azure Service Bus topics.

    Each event type maps to a topic; the aggregate ID is used as the session
    key to ensure ordered delivery per aggregate.
    """

    def __init__(
        self,
        connection_string: str,
        topic_prefix: str = "migration",
    ) -> None:
        self._connection_string = connection_string
        self._topic_prefix = topic_prefix
        self._sender_cache: dict[str, Any] = {}

    async def publish(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        try:
            from azure.servicebus.aio import ServiceBusClient  # type: ignore
            from azure.servicebus import ServiceBusMessage  # type: ignore
        except ImportError:
            raise RuntimeError(
                "azure-servicebus is not installed. "
                "Run: pip install azure-servicebus"
            )

        body = json.dumps(payload, default=str)
        async with ServiceBusClient.from_connection_string(self._connection_string) as client:
            async with client.get_topic_sender(topic_name=topic) as sender:
                message = ServiceBusMessage(
                    body=body,
                    message_id=payload.get("id", str(uuid.uuid4())),
                    session_id=key,
                    content_type="application/json",
                    subject=payload.get("type", ""),
                )
                await sender.send_messages(message)
                logger.debug("Service Bus message sent: topic=%s key=%s", topic, key)

    async def close(self) -> None:
        self._sender_cache.clear()


# ---------------------------------------------------------------------------
# Dead-letter / fallback transport
# ---------------------------------------------------------------------------


class DeadLetterTransport(EventTransport):
    """
    Writes failed events to a JSONL file for manual replay.
    Used as fallback when the primary transport is unavailable.
    """

    def __init__(self, dead_letter_path: str = "/tmp/migration_dead_letter.jsonl") -> None:
        self._path = Path(dead_letter_path)

    async def publish(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        entry = {
            "dead_lettered_at": datetime.now(tz=timezone.utc).isoformat(),
            "topic": topic,
            "key": key,
            "payload": payload,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        logger.warning("Event dead-lettered: topic=%s id=%s", topic, payload.get("id"))

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------


class EventPublisherAdapter:
    """
    Outbound adapter that publishes domain events to the configured transport.

    Usage::

        adapter = EventPublisherAdapter(
            transport=KafkaEventTransport("kafka:9092"),
            dead_letter_transport=DeadLetterTransport(),
        )
        await adapter.publish_all(job.collect_events())
    """

    def __init__(
        self,
        transport: EventTransport,
        dead_letter_transport: Optional[EventTransport] = None,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        topic_prefix: str = "migration",
    ) -> None:
        self._transport = transport
        self._dead_letter = dead_letter_transport or DeadLetterTransport()
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._topic_prefix = topic_prefix

    async def publish_all(self, events: list[DomainEvent]) -> None:
        """Publish all events; log errors but don't raise."""
        for event in events:
            await self._publish_with_retry(event)

    async def publish(self, event: DomainEvent) -> None:
        """Publish a single domain event."""
        await self._publish_with_retry(event)

    async def close(self) -> None:
        """Flush and close the underlying transport."""
        await self._transport.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _publish_with_retry(self, event: DomainEvent) -> None:
        topic = self._topic_for(event)
        key = event.aggregate_id or str(event.event_id)
        payload = _event_to_dict(event)

        for attempt in range(1, self._max_retries + 1):
            try:
                await self._transport.publish(topic, key, payload)
                logger.debug(
                    "Event published: type=%s id=%s attempt=%d",
                    event.event_type, event.event_id, attempt,
                )
                return
            except Exception as exc:
                if attempt == self._max_retries:
                    logger.error(
                        "Event publication failed after %d attempts: type=%s id=%s error=%s",
                        self._max_retries, event.event_type, event.event_id, exc,
                    )
                    await self._dead_letter.publish(topic, key, payload)
                    return
                delay = self._retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Event publish attempt %d/%d failed, retrying in %.1fs: %s",
                    attempt, self._max_retries, delay, exc,
                )
                await asyncio.sleep(delay)

    def _topic_for(self, event: DomainEvent) -> str:
        """Derive topic name from event type."""
        # migration.MigrationStarted → migration.migrationstarted
        class_name = type(event).__name__.lower()
        return f"{self._topic_prefix}.{class_name}"
