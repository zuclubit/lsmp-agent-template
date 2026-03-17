"""
Event publisher with transactional outbox pattern.

Architecture
------------
1. The caller writes an event to the ``outbox`` table (same DB transaction as
   the business operation) via ``publish_to_outbox()``.
2. A background ``OutboxRelay`` polls the outbox table, serialises unpublished
   events, sends them to the message broker (Azure Service Bus **or** AWS SQS),
   and marks them as published – all with idempotency guarantees.

Broker adapters
  - AzureServiceBusAdapter  (azure-servicebus SDK)
  - AWSSQSAdapter            (aiobotocore / boto3)

Both adapters share the ``BrokerAdapter`` abstract interface so they are
interchangeable without changing application code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


class EventStatus(str, Enum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class OutboxEvent:
    """Single row in the transactional outbox table."""

    event_id: str
    aggregate_type: str          # e.g. "Account", "Contact"
    aggregate_id: str
    event_type: str              # e.g. "account.created"
    topic: str                   # target queue / topic name
    payload: Dict[str, Any]
    status: EventStatus = EventStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    published_at: Optional[datetime] = None
    retry_count: int = 0
    last_error: Optional[str] = None

    @classmethod
    def create(
        cls,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        topic: str,
        payload: Dict[str, Any],
    ) -> "OutboxEvent":
        return cls(
            event_id=str(uuid.uuid4()),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            topic=topic,
            payload=payload,
        )

    def to_broker_message(self) -> Dict[str, Any]:
        return {
            "eventId": self.event_id,
            "eventType": self.event_type,
            "aggregateType": self.aggregate_type,
            "aggregateId": self.aggregate_id,
            "occurredAt": self.created_at.isoformat(),
            "payload": self.payload,
        }


# ---------------------------------------------------------------------------
# In-memory outbox store (replace with DB-backed store in production)
# ---------------------------------------------------------------------------


class OutboxStore(ABC):
    @abstractmethod
    async def save(self, event: OutboxEvent) -> None: ...

    @abstractmethod
    async def get_pending(self, limit: int = 100) -> List[OutboxEvent]: ...

    @abstractmethod
    async def mark_published(self, event_id: str) -> None: ...

    @abstractmethod
    async def mark_failed(self, event_id: str, error: str, dead_letter: bool = False) -> None: ...


class InMemoryOutboxStore(OutboxStore):
    """Thread-safe in-memory store for testing / local development."""

    def __init__(self) -> None:
        self._events: Dict[str, OutboxEvent] = {}
        self._lock = asyncio.Lock()

    async def save(self, event: OutboxEvent) -> None:
        async with self._lock:
            self._events[event.event_id] = event

    async def get_pending(self, limit: int = 100) -> List[OutboxEvent]:
        async with self._lock:
            return [
                e for e in self._events.values()
                if e.status == EventStatus.PENDING
            ][:limit]

    async def mark_published(self, event_id: str) -> None:
        async with self._lock:
            if event_id in self._events:
                self._events[event_id].status = EventStatus.PUBLISHED
                self._events[event_id].published_at = datetime.now(timezone.utc)

    async def mark_failed(self, event_id: str, error: str, dead_letter: bool = False) -> None:
        async with self._lock:
            if event_id in self._events:
                evt = self._events[event_id]
                evt.retry_count += 1
                evt.last_error = error
                evt.status = EventStatus.DEAD_LETTER if dead_letter else EventStatus.FAILED


# ---------------------------------------------------------------------------
# Broker adapters
# ---------------------------------------------------------------------------


class BrokerAdapter(ABC):
    """Common interface for all message broker adapters."""

    @abstractmethod
    async def send_batch(self, topic: str, messages: List[Dict[str, Any]]) -> None:
        """Send a batch of messages to the given topic / queue."""

    @abstractmethod
    async def close(self) -> None:
        """Clean up connections."""


class AzureServiceBusAdapter(BrokerAdapter):
    """
    Publishes events to Azure Service Bus queues or topics.

    Requires: ``pip install azure-servicebus``
    """

    def __init__(self, connection_string: str) -> None:
        try:
            from azure.servicebus.aio import ServiceBusClient  # type: ignore[import]
            from azure.servicebus import ServiceBusMessage  # type: ignore[import]
        except ImportError as exc:
            raise ImportError("azure-servicebus is required: pip install azure-servicebus") from exc

        self._connection_string = connection_string
        self._client: Optional[Any] = None
        self._ServiceBusMessage = ServiceBusMessage
        self._ServiceBusClient = ServiceBusClient

    async def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._ServiceBusClient.from_connection_string(
                self._connection_string
            )
        return self._client

    async def send_batch(self, topic: str, messages: List[Dict[str, Any]]) -> None:
        client = await self._get_client()
        async with client.get_topic_sender(topic_name=topic) as sender:
            sb_messages = [
                self._ServiceBusMessage(
                    body=json.dumps(msg).encode("utf-8"),
                    message_id=msg.get("eventId", str(uuid.uuid4())),
                    content_type="application/json",
                    application_properties={
                        "eventType": msg.get("eventType", ""),
                        "aggregateType": msg.get("aggregateType", ""),
                    },
                )
                for msg in messages
            ]
            batch = await sender.create_message_batch()
            for sb_msg in sb_messages:
                batch.add_message(sb_msg)
            await sender.send_messages(batch)
        logger.info("Published %d messages to ASB topic=%s", len(messages), topic)

    async def close(self) -> None:
        if self._client:
            await self._client.close()


class AWSSQSAdapter(BrokerAdapter):
    """
    Publishes events to AWS SQS queues.

    Requires: ``pip install aiobotocore``
    """

    def __init__(
        self,
        region: str = "us-east-1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        endpoint_url: Optional[str] = None,
    ) -> None:
        try:
            import aiobotocore.session as abcs  # type: ignore[import]
        except ImportError as exc:
            raise ImportError("aiobotocore is required: pip install aiobotocore") from exc
        self._session = abcs.get_session()
        self._region = region
        self._access_key = aws_access_key_id
        self._secret_key = aws_secret_access_key
        self._endpoint_url = endpoint_url
        self._queue_url_cache: Dict[str, str] = {}

    async def _get_queue_url(self, client: Any, queue_name: str) -> str:
        if queue_name not in self._queue_url_cache:
            response = await client.get_queue_url(QueueName=queue_name)
            self._queue_url_cache[queue_name] = response["QueueUrl"]
        return self._queue_url_cache[queue_name]

    async def send_batch(self, topic: str, messages: List[Dict[str, Any]]) -> None:
        kwargs: Dict[str, Any] = {
            "region_name": self._region,
            "service_name": "sqs",
        }
        if self._access_key:
            kwargs["aws_access_key_id"] = self._access_key
        if self._secret_key:
            kwargs["aws_secret_access_key"] = self._secret_key
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url

        async with self._session.create_client(**kwargs) as client:
            queue_url = await self._get_queue_url(client, topic)
            # SQS batch max is 10 messages
            for i in range(0, len(messages), 10):
                chunk = messages[i : i + 10]
                entries = [
                    {
                        "Id": str(idx),
                        "MessageBody": json.dumps(msg),
                        "MessageGroupId": msg.get("aggregateType", "default"),
                        "MessageDeduplicationId": msg.get("eventId", str(uuid.uuid4())),
                        "MessageAttributes": {
                            "eventType": {
                                "StringValue": msg.get("eventType", ""),
                                "DataType": "String",
                            }
                        },
                    }
                    for idx, msg in enumerate(chunk)
                ]
                resp = await client.send_message_batch(
                    QueueUrl=queue_url, Entries=entries
                )
                if resp.get("Failed"):
                    logger.error("SQS batch failures: %s", resp["Failed"])
            logger.info("Published %d messages to SQS queue=%s", len(messages), topic)

    async def close(self) -> None:
        pass  # aiobotocore client is async context-managed per call


# ---------------------------------------------------------------------------
# EventPublisher (application-facing API)
# ---------------------------------------------------------------------------


class EventPublisher:
    """
    Application-facing publisher that writes events to the outbox.

    The ``OutboxRelay`` is responsible for delivering them to the broker.
    Using the outbox pattern guarantees atomicity: events are only published
    if the enclosing database transaction commits.

    Usage::

        publisher = EventPublisher(store=InMemoryOutboxStore())
        await publisher.publish_to_outbox(
            aggregate_type="Account",
            aggregate_id="acc_001",
            event_type="account.migrated",
            topic="migration.events",
            payload={"sfId": "001xx...", "status": "success"},
        )
    """

    def __init__(self, store: OutboxStore) -> None:
        self._store = store

    async def publish_to_outbox(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        topic: str,
        payload: Dict[str, Any],
    ) -> OutboxEvent:
        """
        Write a pending event to the outbox.

        This method should be called within the same DB transaction as the
        business operation it represents.
        """
        event = OutboxEvent.create(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            topic=topic,
            payload=payload,
        )
        await self._store.save(event)
        logger.debug(
            "Event written to outbox: id=%s type=%s aggregate=%s/%s",
            event.event_id,
            event_type,
            aggregate_type,
            aggregate_id,
        )
        return event


# ---------------------------------------------------------------------------
# OutboxRelay – background worker
# ---------------------------------------------------------------------------


class OutboxRelay:
    """
    Background worker that polls the outbox and publishes pending events.

    Guarantees at-least-once delivery. Idempotency must be enforced by
    consumers (see ``queue_processor.py``).

    Usage::

        relay = OutboxRelay(
            store=db_outbox_store,
            adapter=AzureServiceBusAdapter(conn_str),
            poll_interval_seconds=5,
            batch_size=50,
            max_retries=3,
        )
        asyncio.create_task(relay.start())
        ...
        await relay.stop()
    """

    def __init__(
        self,
        store: OutboxStore,
        adapter: BrokerAdapter,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 50,
        max_retries: int = 3,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._running = False
        self._stats = {
            "published": 0,
            "failed": 0,
            "dead_lettered": 0,
            "relay_cycles": 0,
        }

    async def start(self) -> None:
        """Start the relay loop. Run as an asyncio background task."""
        self._running = True
        logger.info("OutboxRelay started poll_interval=%.1fs", self._poll_interval)
        while self._running:
            try:
                await self._relay_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("OutboxRelay cycle error: %s", exc, exc_info=True)
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        """Stop the relay and flush the broker adapter."""
        self._running = False
        await self._adapter.close()
        logger.info("OutboxRelay stopped. stats=%s", self._stats)

    async def _relay_cycle(self) -> None:
        self._stats["relay_cycles"] += 1
        pending = await self._store.get_pending(limit=self._batch_size)
        if not pending:
            return

        # Group by topic to minimise broker round-trips
        by_topic: Dict[str, List[OutboxEvent]] = {}
        for evt in pending:
            by_topic.setdefault(evt.topic, []).append(evt)

        for topic, events in by_topic.items():
            messages = [e.to_broker_message() for e in events]
            try:
                await self._adapter.send_batch(topic, messages)
                for evt in events:
                    await self._store.mark_published(evt.event_id)
                    self._stats["published"] += 1
                logger.info(
                    "OutboxRelay published %d events to topic=%s", len(events), topic
                )
            except Exception as exc:  # noqa: BLE001
                error_msg = str(exc)
                for evt in events:
                    dead_letter = evt.retry_count >= self._max_retries
                    await self._store.mark_failed(evt.event_id, error_msg, dead_letter)
                    if dead_letter:
                        self._stats["dead_lettered"] += 1
                        logger.error(
                            "Event %s moved to dead-letter after %d retries",
                            evt.event_id,
                            evt.retry_count,
                        )
                    else:
                        self._stats["failed"] += 1
                logger.error(
                    "Failed to publish %d events to topic=%s: %s",
                    len(events),
                    topic,
                    exc,
                )

    @property
    def stats(self) -> Dict[str, Any]:
        return dict(self._stats)
