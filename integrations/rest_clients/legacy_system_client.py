"""
REST client for legacy system APIs.

Handles:
  - API-key based authentication (header or query-parameter strategies)
  - Automatic cursor / page-number / offset pagination
  - Response normalisation into a canonical dict schema
  - Field remapping via a configurable mapping table
  - Gzip decompression, date normalisation, null coercion
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import httpx

from .base_client import (
    AuthenticationError,
    BaseHTTPClient,
    ClientConfig,
    CircuitBreaker,
    RetryConfig,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config & enumerations
# ---------------------------------------------------------------------------


class AuthStrategy(str, Enum):
    HEADER = "header"   # e.g. X-API-Key: <key>
    QUERY = "query"     # e.g. ?api_key=<key>
    BEARER = "bearer"   # Authorization: Bearer <key>


class PaginationStyle(str, Enum):
    PAGE_NUMBER = "page_number"   # ?page=N&pageSize=M
    OFFSET = "offset"             # ?offset=N&limit=M
    CURSOR = "cursor"             # ?cursor=<token>
    LINK_HEADER = "link_header"   # RFC 5988 Link: <url>; rel="next"
    NONE = "none"                 # single-page endpoint


@dataclass
class FieldMapping:
    """
    Describes how a single legacy field maps to the canonical schema.

    Attributes:
        legacy_field:     Dot-separated path in the legacy response JSON.
        canonical_field:  Key name in the normalised output.
        transform:        Optional callable applied to the raw value.
        required:         If True and the field is absent, a warning is logged.
        default:          Fallback value when the field is absent.
    """

    legacy_field: str
    canonical_field: str
    transform: Optional[Callable[[Any], Any]] = None
    required: bool = False
    default: Any = None


@dataclass
class LegacySystemConfig:
    """Configuration for a legacy system REST integration."""

    base_url: str
    api_key: str
    auth_strategy: AuthStrategy = AuthStrategy.HEADER
    api_key_header_name: str = "X-API-Key"
    api_key_query_param: str = "api_key"
    pagination_style: PaginationStyle = PaginationStyle.PAGE_NUMBER
    page_param: str = "page"
    page_size_param: str = "pageSize"
    offset_param: str = "offset"
    limit_param: str = "limit"
    cursor_param: str = "cursor"
    default_page_size: int = 100
    records_key: str = "data"
    total_key: str = "total"
    next_cursor_key: str = "nextCursor"
    field_mappings: List[FieldMapping] = field(default_factory=list)
    timeout_seconds: float = 30.0
    verify_ssl: bool = True
    system_name: str = "legacy"


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _get_nested(data: Dict[str, Any], dot_path: str) -> Any:
    """Traverse a dot-separated path into a nested dict."""
    parts = dot_path.split(".")
    current: Any = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _coerce_datetime(value: Any) -> Optional[str]:
    """Normalise various date formats to ISO-8601 UTC string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        # Unix timestamp (seconds or milliseconds)
        ts = value / 1000 if value > 1e10 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%m/%d/%Y",
        ):
            try:
                parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                return parsed.isoformat()
            except ValueError:
                continue
    return str(value)


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    return bool(value)


def _coerce_decimal_string(value: Any) -> Optional[str]:
    """Return a normalised decimal string, stripping currency symbols."""
    if value is None:
        return None
    cleaned = str(value).replace(",", "").replace("$", "").replace("€", "").strip()
    try:
        float(cleaned)
        return cleaned
    except ValueError:
        return None


# Default field transforms
BUILTIN_TRANSFORMS: Dict[str, Callable[[Any], Any]] = {
    "datetime": _coerce_datetime,
    "bool": _coerce_bool,
    "decimal_str": _coerce_decimal_string,
    "upper": lambda v: str(v).upper().strip() if v is not None else None,
    "lower": lambda v: str(v).lower().strip() if v is not None else None,
    "strip": lambda v: str(v).strip() if v is not None else None,
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LegacySystemClient(BaseHTTPClient):
    """
    Async REST client for legacy systems.

    Normalises raw API responses into a canonical dict schema using a
    configurable ``field_mappings`` list.

    Usage::

        config = LegacySystemConfig(
            base_url="https://erp.internal.example.com/api/v2",
            api_key=os.environ["ERP_API_KEY"],
            field_mappings=[
                FieldMapping("customer.customerId", "external_id", required=True),
                FieldMapping("customer.fullName", "name", transform=str.strip),
                FieldMapping("audit.createdOn", "created_at", transform=_coerce_datetime),
            ],
        )
        async with LegacySystemClient(config) as client:
            async for page in client.paginate_records("/customers"):
                for record in page:
                    print(record)
    """

    def __init__(self, config: LegacySystemConfig) -> None:
        self._legacy_config = config
        client_config = ClientConfig(
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
            verify_ssl=config.verify_ssl,
            retry=RetryConfig(max_attempts=3, wait_min_seconds=1.0),
            circuit_breaker=CircuitBreaker(failure_threshold=5),
            default_headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        super().__init__(client_config)

    # ------------------------------------------------------------------
    # BaseHTTPClient – Auth
    # ------------------------------------------------------------------

    async def _build_auth_headers(self) -> Dict[str, str]:
        cfg = self._legacy_config
        if cfg.auth_strategy == AuthStrategy.HEADER:
            return {cfg.api_key_header_name: cfg.api_key}
        if cfg.auth_strategy == AuthStrategy.BEARER:
            return {"Authorization": f"Bearer {cfg.api_key}"}
        # QUERY strategy – injected into params, not headers
        return {}

    async def _on_auth_error(self, response: httpx.Response) -> bool:
        """Legacy systems typically require a new API key; no auto-refresh."""
        logger.error(
            "[%s] Authentication error %d – check API key configuration",
            self._legacy_config.system_name,
            response.status_code,
        )
        return False

    def _auth_params(self) -> Dict[str, str]:
        cfg = self._legacy_config
        if cfg.auth_strategy == AuthStrategy.QUERY:
            return {cfg.api_key_query_param: cfg.api_key}
        return {}

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    def normalise(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Map a raw legacy record to the canonical schema.

        Fields not covered by ``field_mappings`` are passed through as-is
        (with their original key names) so no data is silently lost.
        """
        result: Dict[str, Any] = {}

        for mapping in self._legacy_config.field_mappings:
            value = _get_nested(raw, mapping.legacy_field)
            if value is None:
                if mapping.required:
                    logger.warning(
                        "[%s] Required field '%s' missing in record",
                        self._legacy_config.system_name,
                        mapping.legacy_field,
                    )
                value = mapping.default
            if value is not None and mapping.transform:
                try:
                    value = mapping.transform(value)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[%s] Transform failed for field '%s': %s",
                        self._legacy_config.system_name,
                        mapping.legacy_field,
                        exc,
                    )
            result[mapping.canonical_field] = value

        # Pass-through fields that are not covered by explicit mappings
        mapped_sources = {m.legacy_field.split(".")[0] for m in self._legacy_config.field_mappings}
        for key, val in raw.items():
            if key not in mapped_sources and key not in result:
                result[f"_raw_{key}"] = val

        result["_source_system"] = self._legacy_config.system_name
        return result

    def normalise_batch(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.normalise(r) for r in records]

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def paginate_records(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        page_size: Optional[int] = None,
        normalise: bool = True,
    ) -> AsyncIterator[List[Dict[str, Any]]]:
        """
        Async generator that yields normalised pages of records.

        Handles all :class:`PaginationStyle` variants transparently.

        Usage::

            async for page in client.paginate_records("/customers"):
                for record in page:
                    process(record)
        """
        cfg = self._legacy_config
        size = page_size or cfg.default_page_size
        base_params = dict(params or {})
        base_params.update(self._auth_params())

        if cfg.pagination_style == PaginationStyle.NONE:
            resp = await self.get(path, params=base_params)
            resp.raise_for_status()
            body = resp.json()
            raw_records = body if isinstance(body, list) else body.get(cfg.records_key, [])
            yield self.normalise_batch(raw_records) if normalise else raw_records
            return

        if cfg.pagination_style == PaginationStyle.PAGE_NUMBER:
            page = 1
            while True:
                p = {**base_params, cfg.page_param: page, cfg.page_size_param: size}
                resp = await self.get(path, params=p)
                resp.raise_for_status()
                body = resp.json()
                records = body.get(cfg.records_key, [])
                if not records:
                    break
                yield self.normalise_batch(records) if normalise else records
                page += 1
                total = body.get(cfg.total_key)
                if total is not None and (page - 1) * size >= total:
                    break

        elif cfg.pagination_style == PaginationStyle.OFFSET:
            offset = 0
            while True:
                p = {**base_params, cfg.offset_param: offset, cfg.limit_param: size}
                resp = await self.get(path, params=p)
                resp.raise_for_status()
                body = resp.json()
                records = body.get(cfg.records_key, [])
                if not records:
                    break
                yield self.normalise_batch(records) if normalise else records
                offset += len(records)
                total = body.get(cfg.total_key)
                if total is not None and offset >= total:
                    break

        elif cfg.pagination_style == PaginationStyle.CURSOR:
            cursor: Optional[str] = None
            while True:
                p = {**base_params, cfg.page_size_param: size}
                if cursor:
                    p[cfg.cursor_param] = cursor
                resp = await self.get(path, params=p)
                resp.raise_for_status()
                body = resp.json()
                records = body.get(cfg.records_key, [])
                if not records:
                    break
                yield self.normalise_batch(records) if normalise else records
                cursor = body.get(cfg.next_cursor_key)
                if not cursor:
                    break

        elif cfg.pagination_style == PaginationStyle.LINK_HEADER:
            url: Optional[str] = path
            while url:
                resp = await self.get(url, params=base_params)
                resp.raise_for_status()
                body = resp.json()
                records = body.get(cfg.records_key, []) if isinstance(body, dict) else body
                if not records:
                    break
                yield self.normalise_batch(records) if normalise else records
                url = self._parse_link_header_next(resp.headers.get("Link", ""))
                base_params = {}  # next URL is self-contained

    @staticmethod
    def _parse_link_header_next(header: str) -> Optional[str]:
        """Extract the URL for rel="next" from an RFC 5988 Link header."""
        for part in header.split(","):
            segments = [s.strip() for s in part.split(";")]
            if len(segments) >= 2 and 'rel="next"' in segments[1]:
                return segments[0].strip("<>")
        return None

    # ------------------------------------------------------------------
    # Single-record helpers
    # ------------------------------------------------------------------

    async def get_record(
        self, path: str, normalise: bool = True
    ) -> Dict[str, Any]:
        """Fetch a single record by path."""
        p = self._auth_params()
        resp = await self.get(path, params=p or None)
        resp.raise_for_status()
        raw = resp.json()
        return self.normalise(raw) if normalise else raw

    async def create_record(
        self, path: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST a new record to the legacy system."""
        p = self._auth_params()
        resp = await self.post(path, json=data, params=p or None)
        resp.raise_for_status()
        return resp.json()

    async def update_record(
        self, path: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT/PATCH an existing record."""
        p = self._auth_params()
        resp = await self.patch(path, json=data, params=p or None)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------
    # Health / connectivity check
    # ------------------------------------------------------------------

    async def health_check(self) -> Dict[str, Any]:
        """
        Ping the legacy system's health or ping endpoint.

        Returns a dict with ``status``, ``latency_ms``, and ``system``.
        """
        import time as _time

        start = _time.perf_counter()
        try:
            resp = await self.get("/health", params=self._auth_params() or None)
            latency = (_time.perf_counter() - start) * 1000
            return {
                "status": "healthy" if resp.status_code < 400 else "degraded",
                "http_status": resp.status_code,
                "latency_ms": round(latency, 2),
                "system": self._legacy_config.system_name,
            }
        except Exception as exc:  # noqa: BLE001
            latency = (_time.perf_counter() - start) * 1000
            return {
                "status": "unhealthy",
                "error": str(exc),
                "latency_ms": round(latency, 2),
                "system": self._legacy_config.system_name,
            }
