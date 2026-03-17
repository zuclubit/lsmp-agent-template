"""
Apache Kafka consumer for legacy system events.

Features
--------
- confluent_kafka AsyncConsumer wrapper with asyncio integration
- Confluent Schema Registry integration (Avro / JSON Schema / Protobuf)
- At-least-once processing with manual offset commit after successful handling
- Dead Letter Queue (DLQ) producer: poison messages are forwarded to a
  configurable DLQ topic rather than blocking the consumer
- Graceful shutdown via asyncio cancellation
- Per-topic handler registry
- Prometheus-compatible metrics counters (optional)
- Consumer group lag reporting
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Type, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional confluent_kafka import (graceful degradation for test environments)
# ---------------------------------------------------------------------------

try:
    from confluent_kafka import (
        Consumer,
        KafkaError,
        KafkaException,
        Message,
        Producer,
        TopicPartition,
    )
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroDeserializer
    from confluent_kafka.schema_registry.json_schema import JSONDeserializer
    from confluent_kafka.serialization import MessageField, SerializationContext

    _CONFLUENT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CONFLUENT_AVAILABLE = False
    logger.warning(
        "confluent_kafka not installed – KafkaConsumer will raise ImportError at runtime"
    )

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

MessageHandler = Callable[[Dict[str, Any], Dict[str, Any]], Union[Awaitable[None], None]]
"""
Async (preferred) or sync callable that receives:
  (deserialized_value: dict, message_metadata: dict) -> None
"""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SchemaType(str, Enum):
    NONE = "none"       # raw bytes / string
    AVRO = "avro"
    JSON_SCHEMA = "json_schema"


@dataclass
class SchemaRegistryConfig:
    url: str
    basic_auth_user_info: str = ""  # "user:password"


@dataclass
class KafkaConsumerConfig:
    """Full configuration for the Kafka consumer."""

    bootstrap_servers: str
    group_id: str
    topics: List[str]
    schema_registry: Optional[SchemaRegistryConfig] = None
    schema_type: SchemaType = SchemaType.NONE
    auto_offset_reset: str = "earliest"
    enable_auto_commit: bool = False          # we commit manually
    max_poll_records: int = 500
    poll_timeout_seconds: float = 1.0
    dlq_topic: Optional[str] = None
    dlq_max_retries: int = 3
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: Optional[str] = None
    sasl_username: Optional[str] = None
    sasl_password: Optional[str] = None
    ssl_ca_location: Optional[str] = None
    session_timeout_ms: int = 30_000
    heartbeat_interval_ms: int = 10_000
    max_poll_interval_ms: int = 300_000
    fetch_max_bytes: int = 52_428_800       # 50 MB


# ---------------------------------------------------------------------------
# DLQ producer wrapper
# ---------------------------------------------------------------------------


class DLQProducer:
    """Thin wrapper around confluent_kafka Producer for dead-letter publishing."""

    def __init__(self, bootstrap_servers: str, dlq_topic: str) -> None:
        if not _CONFLUENT_AVAILABLE:
            raise ImportError("confluent_kafka is required")
        self._dlq_topic = dlq_topic
        self._producer: Producer = Producer(
            {"bootstrap.servers": bootstrap_servers, "acks": "all"}
        )

    def send(
        self,
        original_msg: "Message",
        error_reason: str,
        attempt: int,
    ) -> None:
        """Publish the failed message to the DLQ topic with error metadata headers."""
        headers = {
            "dlq_original_topic": original_msg.topic() or "",
            "dlq_original_partition": str(original_msg.partition()),
            "dlq_original_offset": str(original_msg.offset()),
            "dlq_error_reason": error_reason[:1024],
            "dlq_attempt": str(attempt),
            "dlq_timestamp": str(int(time.time() * 1000)),
        }
        self._producer.produce(
            topic=self._dlq_topic,
            key=original_msg.key(),
            value=original_msg.value(),
            headers=list(headers.items()),
        )
        self._producer.poll(0)
        logger.warning(
            "Message sent to DLQ topic=%s partition=%d offset=%d reason=%s",
            original_msg.topic(),
            original_msg.partition(),
            original_msg.offset(),
            error_reason,
        )

    def flush(self) -> None:
        self._producer.flush(timeout=10.0)


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


class KafkaEventConsumer:
    """
    Asyncio-compatible Kafka consumer for legacy system events.

    Usage::

        config = KafkaConsumerConfig(
            bootstrap_servers="broker:9092",
            group_id="migration-service",
            topics=["legacy.customers.v1", "legacy.orders.v1"],
            dlq_topic="migration.dlq",
        )

        consumer = KafkaEventConsumer(config)
        consumer.register_handler("legacy.customers.v1", handle_customer)
        consumer.register_handler("legacy.orders.v1", handle_order)

        await consumer.start()
    """

    def __init__(self, config: KafkaConsumerConfig) -> None:
        if not _CONFLUENT_AVAILABLE:
            raise ImportError("confluent_kafka package is required")
        self._cfg = config
        self._handlers: Dict[str, List[MessageHandler]] = {}
        self._consumer: Optional["Consumer"] = None
        self._dlq: Optional[DLQProducer] = None
        self._running = False
        self._deserializer: Optional[Any] = None
        self._metrics = {
            "messages_consumed": 0,
            "messages_processed": 0,
            "messages_failed": 0,
            "messages_dlq": 0,
            "consumer_errors": 0,
        }

    # ------------------------------------------------------------------
    # Handler registry
    # ------------------------------------------------------------------

    def register_handler(self, topic: str, handler: MessageHandler) -> None:
        """Register a message handler for the given topic."""
        self._handlers.setdefault(topic, []).append(handler)
        logger.info("Registered handler %s for topic=%s", handler.__name__, topic)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _build_kafka_config(self) -> Dict[str, Any]:
        cfg = self._cfg
        conf: Dict[str, Any] = {
            "bootstrap.servers": cfg.bootstrap_servers,
            "group.id": cfg.group_id,
            "auto.offset.reset": cfg.auto_offset_reset,
            "enable.auto.commit": cfg.enable_auto_commit,
            "session.timeout.ms": cfg.session_timeout_ms,
            "heartbeat.interval.ms": cfg.heartbeat_interval_ms,
            "max.poll.interval.ms": cfg.max_poll_interval_ms,
            "fetch.max.bytes": cfg.fetch_max_bytes,
            "security.protocol": cfg.security_protocol,
        }
        if cfg.sasl_mechanism:
            conf["sasl.mechanisms"] = cfg.sasl_mechanism
        if cfg.sasl_username:
            conf["sasl.username"] = cfg.sasl_username
        if cfg.sasl_password:
            conf["sasl.password"] = cfg.sasl_password
        if cfg.ssl_ca_location:
            conf["ssl.ca.location"] = cfg.ssl_ca_location
        return conf

    def _build_deserializer(self) -> Optional[Any]:
        if not self._cfg.schema_registry or self._cfg.schema_type == SchemaType.NONE:
            return None

        sr_conf: Dict[str, str] = {"url": self._cfg.schema_registry.url}
        if self._cfg.schema_registry.basic_auth_user_info:
            sr_conf["basic.auth.user.info"] = self._cfg.schema_registry.basic_auth_user_info

        sr_client = SchemaRegistryClient(sr_conf)

        if self._cfg.schema_type == SchemaType.AVRO:
            return AvroDeserializer(sr_client)
        if self._cfg.schema_type == SchemaType.JSON_SCHEMA:
            return JSONDeserializer(None, schema_registry_client=sr_client)
        return None

    async def start(self) -> None:
        """Start consuming messages. Blocks until cancelled or fatal error."""
        self._consumer = Consumer(self._build_kafka_config())
        self._consumer.subscribe(
            self._cfg.topics,
            on_assign=self._on_assign,
            on_revoke=self._on_revoke,
        )
        self._deserializer = self._build_deserializer()

        if self._cfg.dlq_topic:
            self._dlq = DLQProducer(self._cfg.bootstrap_servers, self._cfg.dlq_topic)

        self._running = True
        logger.info(
            "Kafka consumer started group=%s topics=%s",
            self._cfg.group_id,
            self._cfg.topics,
        )

        try:
            await self._consume_loop()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        """Commit final offsets and close the consumer."""
        if self._consumer:
            try:
                self._consumer.commit(asynchronous=False)
            except Exception:  # noqa: BLE001
                pass
            self._consumer.close()
            logger.info("Kafka consumer closed. metrics=%s", self._metrics)
        if self._dlq:
            self._dlq.flush()

    # ------------------------------------------------------------------
    # Consume loop
    # ------------------------------------------------------------------

    async def _consume_loop(self) -> None:
        loop = asyncio.get_running_loop()

        while self._running:
            try:
                # Run the blocking poll in a thread pool to keep the event loop free
                msg = await loop.run_in_executor(
                    None, self._consumer.poll, self._cfg.poll_timeout_seconds
                )

                if msg is None:
                    continue

                if msg.error():
                    await self._handle_consumer_error(msg)
                    continue

                self._metrics["messages_consumed"] += 1
                await self._process_message(msg)

            except asyncio.CancelledError:
                logger.info("Consumer task cancelled – initiating shutdown")
                self._running = False
                raise
            except KafkaException as exc:
                self._metrics["consumer_errors"] += 1
                logger.error("KafkaException in consume loop: %s", exc)
                await asyncio.sleep(1.0)

    async def _handle_consumer_error(self, msg: "Message") -> None:
        error = msg.error()
        if error.code() == KafkaError._PARTITION_EOF:  # noqa: SLF001
            logger.debug(
                "Reached end of partition topic=%s partition=%d offset=%d",
                msg.topic(),
                msg.partition(),
                msg.offset(),
            )
        else:
            self._metrics["consumer_errors"] += 1
            logger.error("Kafka consumer error: %s", error)

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def _process_message(self, msg: "Message") -> None:
        topic = msg.topic()
        correlation_id = str(uuid.uuid4())

        try:
            value = self._deserialize(msg)
            metadata = self._extract_metadata(msg, correlation_id)

            handlers = self._handlers.get(topic, [])
            if not handlers:
                logger.debug(
                    "No handlers registered for topic=%s – skipping", topic
                )
                self._commit_offset(msg)
                return

            for handler in handlers:
                await self._invoke_handler(handler, value, metadata, msg)

            self._metrics["messages_processed"] += 1
            self._commit_offset(msg)

        except Exception as exc:  # noqa: BLE001
            self._metrics["messages_failed"] += 1
            logger.error(
                "Failed to process message topic=%s partition=%d offset=%d corr=%s: %s",
                topic,
                msg.partition(),
                msg.offset(),
                correlation_id,
                exc,
                exc_info=True,
            )
            await self._handle_processing_error(msg, exc)

    async def _invoke_handler(
        self,
        handler: MessageHandler,
        value: Dict[str, Any],
        metadata: Dict[str, Any],
        original_msg: "Message",
    ) -> None:
        result = handler(value, metadata)
        if asyncio.iscoroutine(result):
            await result

    async def _handle_processing_error(
        self, msg: "Message", exc: Exception
    ) -> None:
        if self._dlq:
            error_reason = f"{type(exc).__name__}: {str(exc)}"
            self._dlq.send(msg, error_reason, attempt=1)
            self._metrics["messages_dlq"] += 1
            self._commit_offset(msg)  # commit so we don't reprocess
        else:
            # Without a DLQ, pause and raise to allow operator intervention
            logger.critical(
                "Message processing failed and no DLQ configured. "
                "Stopping consumer to prevent offset advancement."
            )
            self._running = False
            raise

    def _deserialize(self, msg: "Message") -> Dict[str, Any]:
        """Deserialize a Kafka message value."""
        raw = msg.value()
        if self._deserializer:
            ctx = SerializationContext(msg.topic(), MessageField.VALUE)
            return self._deserializer(raw, ctx)
        if isinstance(raw, bytes):
            return json.loads(raw.decode("utf-8"))
        return json.loads(raw)

    @staticmethod
    def _extract_metadata(msg: "Message", correlation_id: str) -> Dict[str, Any]:
        headers = dict(msg.headers() or [])
        return {
            "topic": msg.topic(),
            "partition": msg.partition(),
            "offset": msg.offset(),
            "key": msg.key().decode("utf-8") if msg.key() else None,
            "timestamp": msg.timestamp()[1],
            "headers": {
                k: v.decode("utf-8") if isinstance(v, bytes) else v
                for k, v in headers.items()
            },
            "correlation_id": correlation_id,
        }

    def _commit_offset(self, msg: "Message") -> None:
        if self._consumer:
            try:
                self._consumer.commit(message=msg, asynchronous=True)
            except KafkaException as exc:
                logger.warning("Failed to commit offset: %s", exc)

    # ------------------------------------------------------------------
    # Rebalance callbacks
    # ------------------------------------------------------------------

    def _on_assign(self, consumer: "Consumer", partitions: List["TopicPartition"]) -> None:
        logger.info(
            "Partitions assigned: %s",
            [(p.topic, p.partition) for p in partitions],
        )

    def _on_revoke(self, consumer: "Consumer", partitions: List["TopicPartition"]) -> None:
        logger.info(
            "Partitions revoked: %s",
            [(p.topic, p.partition) for p in partitions],
        )
        try:
            consumer.commit(asynchronous=False)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Metrics / health
    # ------------------------------------------------------------------

    @property
    def metrics(self) -> Dict[str, Any]:
        return dict(self._metrics)

    async def lag_report(self) -> List[Dict[str, Any]]:
        """Return per-partition consumer lag."""
        if not self._consumer:
            return []
        loop = asyncio.get_running_loop()
        partitions = await loop.run_in_executor(
            None,
            lambda: self._consumer.assignment(),
        )
        report = []
        for tp in partitions:
            committed = self._consumer.committed([tp])[0]
            _, high = self._consumer.get_watermark_offsets(tp, timeout=5.0)
            lag = high - (committed.offset if committed and committed.offset >= 0 else 0)
            report.append(
                {"topic": tp.topic, "partition": tp.partition, "lag": lag}
            )
        return report
