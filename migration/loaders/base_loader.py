"""
base_loader.py
─────────────────────────────────────────────────────────────────────────────
Abstract base class for all Salesforce data loaders.

Defines the contract for:
  - Authentication and connection management
  - Batch chunking
  - Rate-limit handling with exponential backoff
  - Success/failure CSV parsing (Bulk API 2.0 format)
  - Metrics collection and reporting
  - Retry policies

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import abc
import csv
import io
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ─── Load Metrics ─────────────────────────────────────────────────────────────

@dataclass
class LoadMetrics:
    """Tracks statistics for a single loader run."""
    loader_name:       str
    object_name:       str
    operation:         str  # insert | update | upsert | delete
    start_time:        datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time:          Optional[datetime] = None
    total_input_rows:  int = 0
    total_success:     int = 0
    total_failed:      int = 0
    total_batches:     int = 0
    batches_failed:    int = 0
    api_calls_made:    int = 0
    retries_made:      int = 0
    errors:            List[Dict] = field(default_factory=list)
    success_ids:       List[str] = field(default_factory=list)

    def finish(self) -> None:
        self.end_time = datetime.now(timezone.utc)

    @property
    def duration_seconds(self) -> float:
        end = self.end_time or datetime.now(timezone.utc)
        return (end - self.start_time).total_seconds()

    @property
    def success_rate(self) -> float:
        total = self.total_success + self.total_failed
        return (self.total_success / total * 100) if total > 0 else 0.0

    @property
    def rows_per_second(self) -> float:
        d = self.duration_seconds
        return self.total_success / d if d > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "loader_name":      self.loader_name,
            "object_name":      self.object_name,
            "operation":        self.operation,
            "start_time":       self.start_time.isoformat(),
            "end_time":         self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "total_input_rows": self.total_input_rows,
            "total_success":    self.total_success,
            "total_failed":     self.total_failed,
            "total_batches":    self.total_batches,
            "batches_failed":   self.batches_failed,
            "api_calls_made":   self.api_calls_made,
            "retries_made":     self.retries_made,
            "success_rate_pct": round(self.success_rate, 2),
            "rows_per_second":  round(self.rows_per_second, 1),
            "error_count":      len(self.errors),
            "sample_errors":    self.errors[:10],
        }


# ─── Retry Configuration ──────────────────────────────────────────────────────

@dataclass
class RetryPolicy:
    """Configuration for retry behaviour on transient failures."""
    max_retries:      int   = 5
    base_delay_sec:   float = 2.0
    max_delay_sec:    float = 120.0
    backoff_factor:   float = 2.0
    retryable_codes:  Tuple[int, ...] = (429, 500, 502, 503, 504)

    def delay_for_attempt(self, attempt: int) -> float:
        """Return delay (seconds) for a given retry attempt (1-indexed)."""
        delay = self.base_delay_sec * (self.backoff_factor ** (attempt - 1))
        return min(delay, self.max_delay_sec)


# ─── Abstract Base Loader ─────────────────────────────────────────────────────

class BaseLoader(abc.ABC):
    """
    Abstract base loader for Salesforce DML operations.

    Subclasses must implement:
        _authenticate()
        _load_batch(records, operation, external_id_field) -> (success_list, failure_list)
        _get_object_api_name() -> str
    """

    LOADER_NAME: str = "BaseLoader"

    def __init__(
        self,
        sf_instance_url:    str,
        sf_username:        str,
        sf_password:        str,
        sf_security_token:  str = "",
        batch_size:         int = 200,
        operation:          str = "upsert",
        external_id_field:  str = "Legacy_ID__c",
        retry_policy:       Optional[RetryPolicy] = None,
        output_dir:         Optional[Path] = None,
        max_failures_pct:   float = 10.0,
    ) -> None:
        self.sf_instance_url   = sf_instance_url
        self.sf_username       = sf_username
        self.sf_password       = sf_password
        self.sf_security_token = sf_security_token
        self.batch_size        = batch_size
        self.operation         = operation
        self.external_id_field = external_id_field
        self.retry_policy      = retry_policy or RetryPolicy()
        self.output_dir        = Path(output_dir) if output_dir else Path(".")
        self.max_failures_pct  = max_failures_pct
        self._sf_client        = None
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    @abc.abstractmethod
    def object_name(self) -> str:
        """Salesforce object API name (e.g., 'Account', 'Contact')."""
        ...

    @abc.abstractmethod
    def _authenticate(self) -> None:
        """Establish Salesforce connection/session."""
        ...

    @abc.abstractmethod
    def _load_batch(
        self,
        records:           List[Dict],
        operation:         str,
        external_id_field: str,
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Execute one DML batch.

        Returns:
            (successes, failures) — each a list of dicts with 'id' and 'error' keys
        """
        ...

    # ─── Public API ───────────────────────────────────────────────────────────

    def load(self, df: pd.DataFrame) -> LoadMetrics:
        """
        Load a DataFrame into Salesforce using the configured operation.

        Args:
            df: DataFrame with columns matching the target SF object

        Returns:
            LoadMetrics with final statistics
        """
        if self._sf_client is None:
            self._authenticate()

        metrics = LoadMetrics(
            loader_name=self.LOADER_NAME,
            object_name=self.object_name,
            operation=self.operation,
        )
        metrics.total_input_rows = len(df)
        logger.info("[%s] Starting %s of %d rows into %s.",
                    self.LOADER_NAME, self.operation, len(df), self.object_name)

        chunks = self._chunk_dataframe(df, self.batch_size)
        metrics.total_batches = len(chunks)

        for i, chunk in enumerate(chunks, start=1):
            records = chunk.to_dict(orient="records")
            logger.debug("[%s] Batch %d/%d: %d records",
                         self.LOADER_NAME, i, metrics.total_batches, len(records))

            successes, failures = self._execute_batch_with_retry(
                records, metrics, batch_num=i)

            metrics.total_success += len(successes)
            metrics.total_failed  += len(failures)
            metrics.success_ids.extend([s.get("id", "") for s in successes])
            for f in failures:
                metrics.errors.append({
                    "batch":        i,
                    "legacy_id":    f.get("legacy_id"),
                    "error":        f.get("error"),
                    "error_fields": f.get("error_fields"),
                })

            # Abort if failure rate too high
            if self._should_abort(metrics):
                logger.error(
                    "[%s] Failure rate %.1f%% exceeds threshold %.1f%%. Aborting.",
                    self.LOADER_NAME,
                    100 - metrics.success_rate,
                    self.max_failures_pct,
                )
                break

        metrics.finish()
        self._write_metrics(metrics)
        self._write_error_csv(metrics)
        logger.info("[%s] Load complete. success=%d failed=%d rate=%.1f%%",
                    self.LOADER_NAME, metrics.total_success,
                    metrics.total_failed, metrics.success_rate)
        return metrics

    # ─── Retry wrapper ────────────────────────────────────────────────────────

    def _execute_batch_with_retry(
        self,
        records:   List[Dict],
        metrics:   LoadMetrics,
        batch_num: int,
    ) -> Tuple[List[Dict], List[Dict]]:
        """Wraps _load_batch with exponential backoff on transient errors."""
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.retry_policy.max_retries + 1):
            try:
                metrics.api_calls_made += 1
                successes, failures = self._load_batch(
                    records, self.operation, self.external_id_field)
                return successes, failures

            except Exception as exc:
                last_exc = exc
                if attempt >= self.retry_policy.max_retries:
                    break
                delay = self.retry_policy.delay_for_attempt(attempt)
                logger.warning(
                    "[%s] Batch %d attempt %d/%d failed: %s. Retrying in %.1fs.",
                    self.LOADER_NAME, batch_num, attempt,
                    self.retry_policy.max_retries, exc, delay,
                )
                metrics.retries_made += 1
                time.sleep(delay)

        # All retries exhausted — mark whole batch as failed
        logger.error("[%s] Batch %d permanently failed: %s",
                     self.LOADER_NAME, batch_num, last_exc)
        metrics.batches_failed += 1
        failures = [{"legacy_id": r.get(self.external_id_field), "error": str(last_exc)}
                    for r in records]
        return [], failures

    # ─── Output helpers ───────────────────────────────────────────────────────

    def _write_metrics(self, metrics: LoadMetrics) -> None:
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"{self.object_name}_load_metrics_{ts}.json"
        with path.open("w") as fh:
            json.dump(metrics.to_dict(), fh, indent=2)
        logger.info("[%s] Metrics written: %s", self.LOADER_NAME, path)

    def _write_error_csv(self, metrics: LoadMetrics) -> None:
        if not metrics.errors:
            return
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"{self.object_name}_load_errors_{ts}.csv"
        err_df = pd.DataFrame(metrics.errors)
        err_df.to_csv(path, index=False)
        logger.info("[%s] Error CSV written: %s (%d rows)",
                    self.LOADER_NAME, path, len(err_df))

    def _write_success_ids(self, metrics: LoadMetrics) -> None:
        if not metrics.success_ids:
            return
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"{self.object_name}_success_ids_{ts}.txt"
        with path.open("w") as fh:
            fh.write("\n".join(metrics.success_ids))
        logger.info("[%s] Success IDs written: %s", self.LOADER_NAME, path)

    # ─── Utility ──────────────────────────────────────────────────────────────

    @staticmethod
    def _chunk_dataframe(df: pd.DataFrame, chunk_size: int) -> List[pd.DataFrame]:
        return [df.iloc[i:i + chunk_size] for i in range(0, len(df), chunk_size)]

    def _should_abort(self, metrics: LoadMetrics) -> bool:
        """Return True if the failure rate has exceeded the configured threshold."""
        total = metrics.total_success + metrics.total_failed
        if total == 0:
            return False
        failure_rate = metrics.total_failed / total * 100
        return failure_rate > self.max_failures_pct

    # ─── Context Manager ──────────────────────────────────────────────────────

    def __enter__(self) -> "BaseLoader":
        self._authenticate()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Salesforce sessions don't need explicit teardown
        pass
