"""
Salesforce Platform Events consumer using CometD / Bayeux protocol.

Features
--------
- Long-polling via the CometD Bayeux handshake → subscribe → connect loop
- Durable replay: tracks the last received replayId in a persistent store
  (Redis by default; falls back to in-memory) so the consumer can resume
  after a restart without missing events.
- Automatic reconnection with exponential backoff.
- Configurable event-processing callback (async or sync).
- Graceful shutdown via asyncio cancellation.

CometD Bayeux channels
  /meta/handshake  – negotiate client ID and transport
  /meta/subscribe  – subscribe to a topic channel
  /meta/connect    – long-poll for events
  /meta/disconnect – clean shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

EventCallback = Callable[[Dict[str, Any]], Union[Awaitable[None], None]]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COMETD_PATH = "/cometd/58.0"
REPLAY_SENTINEL = -1  # -1 = new events only, -2 = all retained events


@dataclass
class PlatformEventConfig:
    """Runtime configuration for the Platform Events consumer."""

    instance_url: str
    access_token: str                   # OAuth access token
    channel: str                        # e.g. /event/Migration_Status__e
    replay_id: int = REPLAY_SENTINEL    # -1 new, -2 all retained
    replay_store: Optional[Any] = None  # redis.asyncio.Redis or None
    replay_store_key: str = ""          # Redis key prefix
    max_reconnect_attempts: int = 10
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 120.0
    connect_timeout_seconds: float = 30.0
    long_poll_timeout_ms: int = 110_000  # must be < SF server timeout (120s)


# ---------------------------------------------------------------------------
# Replay store
# ---------------------------------------------------------------------------


class InMemoryReplayStore:
    """Simple dict-backed replay store (not durable across process restarts)."""

    def __init__(self) -> None:
        self._store: Dict[str, int] = {}

    async def get(self, key: str) -> Optional[int]:
        return self._store.get(key)

    async def set(self, key: str, replay_id: int) -> None:
        self._store[key] = replay_id


class RedisReplayStore:
    """Durable replay store backed by Redis."""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def get(self, key: str) -> Optional[int]:
        val = await self._redis.get(key)
        return int(val) if val is not None else None

    async def set(self, key: str, replay_id: int) -> None:
        await self._redis.set(key, replay_id)


# ---------------------------------------------------------------------------
# Bayeux message builder
# ---------------------------------------------------------------------------


def _handshake_msg(client_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "channel": "/meta/handshake",
        "version": "1.0",
        "minimumVersion": "1.0",
        "supportedConnectionTypes": ["long-polling"],
        "id": str(uuid.uuid4()),
    }


def _subscribe_msg(
    client_id: str, channel: str, replay_id: int
) -> Dict[str, Any]:
    return {
        "channel": "/meta/subscribe",
        "clientId": client_id,
        "subscription": channel,
        "id": str(uuid.uuid4()),
        "ext": {"replay": {channel: replay_id}},
    }


def _connect_msg(client_id: str, timeout_ms: int) -> Dict[str, Any]:
    return {
        "channel": "/meta/connect",
        "clientId": client_id,
        "connectionType": "long-polling",
        "id": str(uuid.uuid4()),
        "advice": {"timeout": timeout_ms, "interval": 0},
    }


def _disconnect_msg(client_id: str) -> Dict[str, Any]:
    return {
        "channel": "/meta/disconnect",
        "clientId": client_id,
        "id": str(uuid.uuid4()),
    }


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


class SalesforcePlatformEventConsumer:
    """
    Async long-poll consumer for Salesforce Platform Events.

    Usage::

        config = PlatformEventConfig(
            instance_url="https://myorg.my.salesforce.com",
            access_token=token,
            channel="/event/Migration_Status__e",
            replay_id=-2,
        )

        async def handle(event: dict) -> None:
            print(event)

        consumer = SalesforcePlatformEventConsumer(config, handle)
        await consumer.start()          # blocks until cancelled / error
    """

    def __init__(
        self,
        config: PlatformEventConfig,
        callback: EventCallback,
    ) -> None:
        self._cfg = config
        self._callback = callback
        self._client_id: Optional[str] = None
        self._running = False
        self._http: Optional[httpx.AsyncClient] = None
        self._replay_store: Any = None
        self._reconnect_attempts = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the consumer loop. Runs until cancelled or fatal error."""
        self._running = True
        self._replay_store = (
            RedisReplayStore(self._cfg.replay_store)
            if self._cfg.replay_store
            else InMemoryReplayStore()
        )
        replay_key = self._replay_store_key()
        stored = await self._replay_store.get(replay_key)
        effective_replay = stored if stored is not None else self._cfg.replay_id

        logger.info(
            "Starting Platform Events consumer channel=%s replay_id=%d",
            self._cfg.channel,
            effective_replay,
        )

        async with httpx.AsyncClient(
            base_url=self._cfg.instance_url,
            timeout=httpx.Timeout(
                connect=10.0,
                read=self._cfg.connect_timeout_seconds + 10,
                write=30.0,
                pool=5.0,
            ),
        ) as client:
            self._http = client
            await self._consume_loop(effective_replay)

    async def stop(self) -> None:
        """Signal a graceful shutdown."""
        self._running = False
        if self._http and self._client_id:
            try:
                await self._send([_disconnect_msg(self._client_id)])
            except Exception:  # noqa: BLE001
                pass
        logger.info("Platform Events consumer stopped for channel=%s", self._cfg.channel)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _consume_loop(self, replay_id: int) -> None:
        while self._running:
            try:
                self._client_id = await self._handshake()
                await self._subscribe(replay_id)
                self._reconnect_attempts = 0

                while self._running:
                    messages = await self._connect()
                    for msg in messages:
                        channel = msg.get("channel", "")
                        if channel == "/meta/connect":
                            advice = msg.get("advice", {})
                            if advice.get("reconnect") == "none":
                                logger.error("Server instructed no reconnect; stopping")
                                self._running = False
                                return
                        elif not channel.startswith("/meta/"):
                            await self._dispatch(msg)
                            replay_id = msg.get("data", {}).get("replayId", replay_id)
                            await self._replay_store.set(self._replay_store_key(), replay_id)

            except (httpx.TimeoutException, httpx.NetworkError, ConnectionError) as exc:
                self._reconnect_attempts += 1
                if self._reconnect_attempts > self._cfg.max_reconnect_attempts:
                    logger.error(
                        "Max reconnect attempts (%d) exceeded; giving up",
                        self._cfg.max_reconnect_attempts,
                    )
                    raise

                delay = min(
                    self._cfg.backoff_base_seconds * (2 ** (self._reconnect_attempts - 1)),
                    self._cfg.backoff_max_seconds,
                )
                logger.warning(
                    "CometD connection error (attempt %d/%d): %s – retrying in %.1fs",
                    self._reconnect_attempts,
                    self._cfg.max_reconnect_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                logger.info("Consumer task cancelled")
                await self.stop()
                raise

    # ------------------------------------------------------------------
    # CometD protocol
    # ------------------------------------------------------------------

    async def _send(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        assert self._http is not None
        headers = {
            "Authorization": f"Bearer {self._cfg.access_token}",
            "Content-Type": "application/json",
        }
        resp = await self._http.post(
            COMETD_PATH,
            content=json.dumps(messages),
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def _handshake(self) -> str:
        """Perform CometD handshake and return the assigned clientId."""
        responses = await self._send([_handshake_msg()])
        for msg in responses:
            if msg.get("channel") == "/meta/handshake":
                if not msg.get("successful"):
                    raise ConnectionError(
                        f"CometD handshake failed: {msg.get('error', 'unknown')}"
                    )
                client_id = msg["clientId"]
                logger.info("CometD handshake successful clientId=%s", client_id)
                return client_id
        raise ConnectionError("No handshake response received")

    async def _subscribe(self, replay_id: int) -> None:
        """Subscribe to the configured channel."""
        responses = await self._send(
            [_subscribe_msg(self._client_id, self._cfg.channel, replay_id)]  # type: ignore[arg-type]
        )
        for msg in responses:
            if msg.get("channel") == "/meta/subscribe":
                if not msg.get("successful"):
                    raise ConnectionError(
                        f"CometD subscribe failed: {msg.get('error', 'unknown')}"
                    )
                logger.info(
                    "Subscribed to %s with replayId=%d", self._cfg.channel, replay_id
                )
                return
        raise ConnectionError("No subscribe response received")

    async def _connect(self) -> List[Dict[str, Any]]:
        """Long-poll for events and return all received messages."""
        responses = await self._send(
            [_connect_msg(self._client_id, self._cfg.long_poll_timeout_ms)]  # type: ignore[arg-type]
        )
        return responses

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, message: Dict[str, Any]) -> None:
        """Invoke the user callback with error isolation."""
        try:
            result = self._callback(message)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Error in Platform Event callback for channel=%s: %s",
                self._cfg.channel,
                exc,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _replay_store_key(self) -> str:
        if self._cfg.replay_store_key:
            return f"{self._cfg.replay_store_key}:{self._cfg.channel}"
        return f"sf_replay:{self._cfg.channel}"
