"""
Runtime Context Server — MCP Server for Live Migration Platform State

Provides real-time operational metrics and status to agents via MCP protocol.
Data is fetched from internal APIs using async HTTP — never stale cache.

MCP Resources:
  - runtime://jobs/{migration_id}/status          — current migration job state
  - runtime://kafka/{consumer_group}/lag          — Kafka consumer lag metrics
  - runtime://salesforce/{tenant_id}/limits       — SF API limits remaining
  - runtime://spire/{service_id}/svid             — SPIRE SVID TTL (not the cert itself)
  - runtime://dlq/{topic}/depth                   — DLQ depth

MCP Tools:
  - get_job_status(migration_id)                  -> JobStatus
  - get_kafka_lag(consumer_group, topic)          -> KafkaLagMetrics
  - get_salesforce_limits(tenant_id)              -> SalesforceLimits
  - get_spire_svid_ttl(service_id)               -> SpireSvidInfo
  - get_dlq_depth(topic)                          -> DLQMetrics
  - get_runtime_snapshot(migration_id, tenant_id) -> RuntimeSnapshot (all metrics combined)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment / Config
# ---------------------------------------------------------------------------

MIGRATION_API_BASE = os.environ.get("MIGRATION_API_BASE", "http://api.migration.internal")
KAFKA_METRICS_API = os.environ.get("KAFKA_METRICS_API", "http://kafka-exporter.internal:9308")
SALESFORCE_API_VERSION = os.environ.get("SALESFORCE_API_VERSION", "v59.0")
SPIRE_AGENT_SOCKET = os.environ.get("SPIRE_AGENT_SOCKET", "/run/spire/sockets/agent.sock")
SPIRE_API_BASE = os.environ.get("SPIRE_API_BASE", "http://spire-agent.internal:8081")
DLQ_METRICS_API = os.environ.get("DLQ_METRICS_API", "http://kafka-exporter.internal:9308")

SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")  # Injected via SPIRE SVID — never hard-coded

CONFIG_PATH = os.environ.get(
    "RUNTIME_CONTEXT_CONFIG",
    os.path.join(os.path.dirname(__file__), "config.json"),
)

def _load_config() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}

CONFIG: dict[str, Any] = _load_config()

# ---------------------------------------------------------------------------
# Domain Types
# ---------------------------------------------------------------------------

@dataclass
class JobStatus:
    migration_id: str
    state: str  # IDLE | INITIALIZING | PRE_VALIDATING | EXECUTING | POST_VALIDATING | COMPLETED | FAILED | ROLLED_BACK
    phase: str
    records_migrated: int
    records_failed: int
    dlq_count: int
    progress_pct: float
    estimated_completion_utc: Optional[str]
    started_at: Optional[str]
    updated_at: str
    current_chunk: int
    total_chunks: int
    error_code: Optional[str]
    error_message: Optional[str]


@dataclass
class KafkaLagMetrics:
    consumer_group: str
    topic: str
    partition_count: int
    total_lag: int
    max_partition_lag: int
    min_partition_lag: int
    lag_per_partition: dict[str, int]
    consumer_count: int
    is_healthy: bool
    lag_threshold_exceeded: bool
    measured_at: str


@dataclass
class SalesforceLimits:
    tenant_id: str
    org_id: str
    daily_api_requests_used: int
    daily_api_requests_limit: int
    daily_api_requests_remaining: int
    daily_bulk_api_requests_used: int
    daily_bulk_api_requests_remaining: int
    data_storage_mb_used: int
    data_storage_mb_limit: int
    data_storage_mb_remaining: int
    concurrent_apex_executions_used: int
    concurrent_apex_executions_limit: int
    api_usage_pct: float
    measured_at: str


@dataclass
class SpireSvidInfo:
    service_id: str
    spiffe_id: str
    ttl_seconds: int
    expires_at: str
    is_valid: bool
    needs_rotation: bool  # True if TTL < rotation_threshold_seconds
    rotation_threshold_seconds: int


@dataclass
class DLQMetrics:
    topic: str
    dlq_topic: str
    depth: int
    oldest_message_age_seconds: Optional[int]
    newest_message_age_seconds: Optional[int]
    error_breakdown: dict[str, int]  # error_code -> count
    is_healthy: bool
    threshold_depth: int
    threshold_exceeded: bool
    measured_at: str


@dataclass
class RuntimeSnapshot:
    migration_id: str
    tenant_id: str
    job_status: JobStatus
    kafka_lag: Optional[KafkaLagMetrics]
    salesforce_limits: Optional[SalesforceLimits]
    spire_svid: Optional[SpireSvidInfo]
    dlq_metrics: Optional[DLQMetrics]
    snapshot_at: str
    warnings: list[str]
    alerts: list[str]


# ---------------------------------------------------------------------------
# Async HTTP fetchers
# ---------------------------------------------------------------------------

async def _async_get(client: httpx.AsyncClient, url: str, params: Optional[dict] = None) -> Any:
    try:
        resp = await client.get(url, params=params, timeout=8.0)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("HTTP %s from %s: %s", exc.response.status_code, url, exc.response.text[:200])
        raise
    except httpx.RequestError as exc:
        logger.error("Request error fetching %s: %s", url, exc)
        raise


async def fetch_job_status(client: httpx.AsyncClient, migration_id: str) -> JobStatus:
    data = await _async_get(client, f"{MIGRATION_API_BASE}/api/v1/migrations/{migration_id}/status")
    return JobStatus(
        migration_id=data["migration_id"],
        state=data["state"],
        phase=data.get("phase", "UNKNOWN"),
        records_migrated=data.get("records_migrated", 0),
        records_failed=data.get("records_failed", 0),
        dlq_count=data.get("dlq_count", 0),
        progress_pct=data.get("progress_pct", 0.0),
        estimated_completion_utc=data.get("estimated_completion_utc"),
        started_at=data.get("started_at"),
        updated_at=data["updated_at"],
        current_chunk=data.get("current_chunk", 0),
        total_chunks=data.get("total_chunks", 0),
        error_code=data.get("error_code"),
        error_message=data.get("error_message"),
    )


async def fetch_kafka_lag(
    client: httpx.AsyncClient, consumer_group: str, topic: str
) -> KafkaLagMetrics:
    # Prometheus metrics API from kafka-exporter
    params = {
        "query": f'kafka_consumer_group_lag{{consumergroup="{consumer_group}",topic="{topic}"}}'
    }
    data = await _async_get(client, f"{KAFKA_METRICS_API}/api/v1/query", params=params)

    results = data.get("data", {}).get("result", [])
    lag_per_partition: dict[str, int] = {}
    total_lag = 0
    for r in results:
        partition = r["metric"].get("partition", "0")
        lag_value = int(float(r["value"][1]))
        lag_per_partition[partition] = lag_value
        total_lag += lag_value

    max_lag = max(lag_per_partition.values(), default=0)
    min_lag = min(lag_per_partition.values(), default=0)
    lag_threshold = CONFIG.get("thresholds", {}).get("kafka_lag_critical", 10000)

    return KafkaLagMetrics(
        consumer_group=consumer_group,
        topic=topic,
        partition_count=len(lag_per_partition),
        total_lag=total_lag,
        max_partition_lag=max_lag,
        min_partition_lag=min_lag,
        lag_per_partition=lag_per_partition,
        consumer_count=len(results),
        is_healthy=total_lag < lag_threshold,
        lag_threshold_exceeded=total_lag >= lag_threshold,
        measured_at=_utcnow(),
    )


async def fetch_salesforce_limits(
    client: httpx.AsyncClient, tenant_id: str, sf_instance_url: str, sf_access_token: str
) -> SalesforceLimits:
    # Fetch from Salesforce Limits API directly
    headers = {"Authorization": f"Bearer {sf_access_token}"}
    url = f"{sf_instance_url}/services/data/{SALESFORCE_API_VERSION}/limits"
    try:
        resp = await client.get(url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch SF limits for tenant %s: %s", tenant_id, exc)
        raise

    daily = data.get("DailyApiRequests", {})
    bulk = data.get("DailyBulkApiRequests", {})
    storage = data.get("DataStorageMB", {})
    apex = data.get("ConcurrentApexExecutions", {})

    daily_limit = daily.get("Max", 0)
    daily_used = daily.get("Remaining", daily_limit)  # SF returns Remaining not Used
    daily_remaining = daily.get("Remaining", 0)
    daily_actual_used = daily_limit - daily_remaining

    return SalesforceLimits(
        tenant_id=tenant_id,
        org_id=data.get("OrgId", ""),
        daily_api_requests_used=daily_actual_used,
        daily_api_requests_limit=daily_limit,
        daily_api_requests_remaining=daily_remaining,
        daily_bulk_api_requests_used=bulk.get("Max", 0) - bulk.get("Remaining", 0),
        daily_bulk_api_requests_remaining=bulk.get("Remaining", 0),
        data_storage_mb_used=storage.get("Max", 0) - storage.get("Remaining", 0),
        data_storage_mb_limit=storage.get("Max", 0),
        data_storage_mb_remaining=storage.get("Remaining", 0),
        concurrent_apex_executions_used=apex.get("Max", 0) - apex.get("Remaining", 0),
        concurrent_apex_executions_limit=apex.get("Max", 0),
        api_usage_pct=round((daily_actual_used / daily_limit * 100) if daily_limit > 0 else 0.0, 2),
        measured_at=_utcnow(),
    )


async def fetch_spire_svid_ttl(client: httpx.AsyncClient, service_id: str) -> SpireSvidInfo:
    # Query SPIRE agent workload API for SVID info
    data = await _async_get(client, f"{SPIRE_API_BASE}/v1/agent/svidinfo", params={"service_id": service_id})

    rotation_threshold = CONFIG.get("thresholds", {}).get("spire_rotation_threshold_seconds", 3600)
    ttl = data.get("ttl_seconds", 0)

    return SpireSvidInfo(
        service_id=service_id,
        spiffe_id=data.get("spiffe_id", ""),
        ttl_seconds=ttl,
        expires_at=data.get("expires_at", ""),
        is_valid=data.get("is_valid", False),
        needs_rotation=ttl < rotation_threshold,
        rotation_threshold_seconds=rotation_threshold,
    )


async def fetch_dlq_depth(
    client: httpx.AsyncClient, topic: str
) -> DLQMetrics:
    dlq_topic = f"{topic}.dlq"
    params = {
        "query": f'kafka_topic_partition_current_offset{{topic="{dlq_topic}"}}'
         + f' - kafka_topic_partition_oldest_offset{{topic="{dlq_topic}"}}'
    }
    # Fetch depth via kafka-exporter Prometheus metrics
    data = await _async_get(client, f"{DLQ_METRICS_API}/api/v1/query", params=params)
    results = data.get("data", {}).get("result", [])

    depth = sum(int(float(r["value"][1])) for r in results)

    # Fetch error breakdown from migration API
    breakdown_data = await _async_get(
        client,
        f"{MIGRATION_API_BASE}/api/v1/dlq/{topic}/error-breakdown"
    )
    error_breakdown: dict[str, int] = breakdown_data.get("by_error_code", {})

    threshold = CONFIG.get("thresholds", {}).get("dlq_depth_critical", 100)

    return DLQMetrics(
        topic=topic,
        dlq_topic=dlq_topic,
        depth=depth,
        oldest_message_age_seconds=breakdown_data.get("oldest_message_age_seconds"),
        newest_message_age_seconds=breakdown_data.get("newest_message_age_seconds"),
        error_breakdown=error_breakdown,
        is_healthy=depth < threshold,
        threshold_depth=threshold,
        threshold_exceeded=depth >= threshold,
        measured_at=_utcnow(),
    )


# ---------------------------------------------------------------------------
# Runtime Context Server
# ---------------------------------------------------------------------------

class RuntimeContextServer:
    """
    MCP-compatible context server providing real-time operational metrics.
    All data is fetched from internal APIs on each request — no stale cache.
    """

    SERVER_ID = "runtime-context"
    SERVER_VERSION = "2.0.0"

    def __init__(self) -> None:
        self._http_client: Optional[httpx.AsyncClient] = None

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {SERVICE_TOKEN}",
                    "X-Service-ID": self.SERVER_ID,
                },
                timeout=10.0,
            )
        return self._http_client

    async def _handle_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        client = self._get_http_client()

        if tool_name == "get_job_status":
            result = await fetch_job_status(client, arguments["migration_id"])
            return asdict(result)

        elif tool_name == "get_kafka_lag":
            result = await fetch_kafka_lag(
                client,
                arguments["consumer_group"],
                arguments["topic"],
            )
            return asdict(result)

        elif tool_name == "get_salesforce_limits":
            # Fetch SF credentials from migration API (never stored here)
            creds_data = await _async_get(
                client,
                f"{MIGRATION_API_BASE}/api/v1/tenants/{arguments['tenant_id']}/sf-session",
            )
            result = await fetch_salesforce_limits(
                client,
                arguments["tenant_id"],
                creds_data["instance_url"],
                creds_data["access_token"],
            )
            return asdict(result)

        elif tool_name == "get_spire_svid_ttl":
            result = await fetch_spire_svid_ttl(client, arguments["service_id"])
            return asdict(result)

        elif tool_name == "get_dlq_depth":
            result = await fetch_dlq_depth(client, arguments["topic"])
            return asdict(result)

        elif tool_name == "get_runtime_snapshot":
            return await self._get_runtime_snapshot(client, arguments)

        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    async def _get_runtime_snapshot(
        self, client: httpx.AsyncClient, args: dict[str, Any]
    ) -> dict[str, Any]:
        migration_id = args["migration_id"]
        tenant_id = args["tenant_id"]

        warnings: list[str] = []
        alerts: list[str] = []

        # Fetch all metrics concurrently
        job_coro = fetch_job_status(client, migration_id)
        kafka_coro = fetch_kafka_lag(client, args.get("consumer_group", f"migration-{migration_id}"), args.get("kafka_topic", "migration-records"))
        dlq_coro = fetch_dlq_depth(client, args.get("kafka_topic", "migration-records"))
        spire_coro = fetch_spire_svid_ttl(client, self.SERVER_ID)

        job_result, kafka_result, dlq_result, spire_result = await asyncio.gather(
            job_coro, kafka_coro, dlq_coro, spire_coro,
            return_exceptions=True,
        )

        # Resolve SF limits — requires a separate session fetch
        sf_limits = None
        try:
            creds_data = await _async_get(client, f"{MIGRATION_API_BASE}/api/v1/tenants/{tenant_id}/sf-session")
            sf_limits = await fetch_salesforce_limits(client, tenant_id, creds_data["instance_url"], creds_data["access_token"])
        except Exception as exc:
            warnings.append(f"Could not fetch Salesforce limits: {exc}")

        # Check for alerts
        if isinstance(kafka_result, KafkaLagMetrics) and kafka_result.lag_threshold_exceeded:
            alerts.append(f"Kafka lag threshold exceeded: {kafka_result.total_lag} messages behind")
        if isinstance(dlq_result, DLQMetrics) and dlq_result.threshold_exceeded:
            alerts.append(f"DLQ threshold exceeded: {dlq_result.depth} messages in DLQ")
        if isinstance(spire_result, SpireSvidInfo) and spire_result.needs_rotation:
            alerts.append(f"SPIRE SVID needs rotation: {spire_result.ttl_seconds}s remaining")
        if sf_limits and sf_limits.api_usage_pct > 80:
            alerts.append(f"Salesforce API usage at {sf_limits.api_usage_pct}%")

        snapshot = RuntimeSnapshot(
            migration_id=migration_id,
            tenant_id=tenant_id,
            job_status=job_result if isinstance(job_result, JobStatus) else None,
            kafka_lag=kafka_result if isinstance(kafka_result, KafkaLagMetrics) else None,
            salesforce_limits=sf_limits,
            spire_svid=spire_result if isinstance(spire_result, SpireSvidInfo) else None,
            dlq_metrics=dlq_result if isinstance(dlq_result, DLQMetrics) else None,
            snapshot_at=_utcnow(),
            warnings=warnings,
            alerts=alerts,
        )

        # Include partial errors in warnings
        for label, result in [("job_status", job_result), ("kafka_lag", kafka_result), ("dlq", dlq_result), ("spire", spire_result)]:
            if isinstance(result, Exception):
                warnings.append(f"Could not fetch {label}: {result}")

        return asdict(snapshot)

    def handle_mcp_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Synchronous wrapper — runs async coroutine in event loop."""
        return asyncio.run(self._handle_mcp_request_async(request))

    async def _handle_mcp_request_async(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        try:
            if method == "tools/call":
                tool_name = params["name"]
                arguments = params.get("arguments", {})
                result = await self._handle_tool(tool_name, arguments)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}

            elif method == "tools/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOL_DEFINITIONS}}

            elif method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": self.SERVER_ID, "version": self.SERVER_VERSION},
                        "capabilities": {"tools": {}},
                    },
                }

            else:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": str(exc)}}
        except Exception as exc:
            logger.exception("Unhandled error in runtime context server: id=%s method=%s", req_id, method)
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "Internal error"}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    import datetime
    return datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# MCP Tool Definitions
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS = [
    {
        "name": "get_job_status",
        "description": "Get current state of a migration job",
        "inputSchema": {
            "type": "object",
            "properties": {"migration_id": {"type": "string"}},
            "required": ["migration_id"],
        },
    },
    {
        "name": "get_kafka_lag",
        "description": "Get Kafka consumer group lag metrics for a topic",
        "inputSchema": {
            "type": "object",
            "properties": {
                "consumer_group": {"type": "string"},
                "topic": {"type": "string"},
            },
            "required": ["consumer_group", "topic"],
        },
    },
    {
        "name": "get_salesforce_limits",
        "description": "Get remaining Salesforce API call budget for a tenant",
        "inputSchema": {
            "type": "object",
            "properties": {"tenant_id": {"type": "string"}},
            "required": ["tenant_id"],
        },
    },
    {
        "name": "get_spire_svid_ttl",
        "description": "Get SPIRE SVID time-to-live for a service identity (not the cert value)",
        "inputSchema": {
            "type": "object",
            "properties": {"service_id": {"type": "string"}},
            "required": ["service_id"],
        },
    },
    {
        "name": "get_dlq_depth",
        "description": "Get current dead-letter queue depth for a Kafka topic",
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
    {
        "name": "get_runtime_snapshot",
        "description": "Get all runtime metrics for a migration in a single call",
        "inputSchema": {
            "type": "object",
            "properties": {
                "migration_id": {"type": "string"},
                "tenant_id": {"type": "string"},
                "consumer_group": {"type": "string"},
                "kafka_topic": {"type": "string"},
            },
            "required": ["migration_id", "tenant_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    server = RuntimeContextServer()
    logger.info("RuntimeContextServer starting — reading from stdin")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}
            print(json.dumps(response), flush=True)
            continue

        response = server.handle_mcp_request(request)
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
