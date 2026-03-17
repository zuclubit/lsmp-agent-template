"""
base_extractor.py
─────────────────────────────────────────────────────────────────────────────
Abstract base class for all legacy data extractors.

Defines the contract every extractor must implement:
  - connect()          — establish / validate DB connection
  - extract()          — iterator/generator yielding DataFrames per page
  - get_total_count()  — total record count for progress tracking
  - close()            — clean up connections and resources

Also provides common infrastructure:
  - Checkpoint management (resume after failure)
  - Parquet output with schema validation
  - Structured logging
  - Metrics collection (rows extracted, pages, errors, duration)

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import abc
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


# ─── Metric Collection ───────────────────────────────────────────────────────

@dataclass
class ExtractionMetrics:
    """Aggregated statistics collected during a single extraction run."""
    extractor_name:   str
    start_time:       datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time:         Optional[datetime] = None
    total_rows:       int = 0
    extracted_rows:   int = 0
    skipped_rows:     int = 0
    error_rows:       int = 0
    pages_processed:  int = 0
    pages_failed:     int = 0
    output_files:     List[str] = field(default_factory=list)
    errors:           List[str] = field(default_factory=list)

    def finish(self) -> None:
        self.end_time = datetime.now(timezone.utc)

    @property
    def duration_seconds(self) -> float:
        if self.end_time is None:
            return (datetime.now(timezone.utc) - self.start_time).total_seconds()
        return (self.end_time - self.start_time).total_seconds()

    @property
    def rows_per_second(self) -> float:
        dur = self.duration_seconds
        return self.extracted_rows / dur if dur > 0 else 0.0

    @property
    def success_rate(self) -> float:
        total = self.extracted_rows + self.error_rows
        return (self.extracted_rows / total * 100) if total > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "extractor_name":  self.extractor_name,
            "start_time":      self.start_time.isoformat(),
            "end_time":        self.end_time.isoformat() if self.end_time else None,
            "duration_seconds":round(self.duration_seconds, 2),
            "total_rows":      self.total_rows,
            "extracted_rows":  self.extracted_rows,
            "skipped_rows":    self.skipped_rows,
            "error_rows":      self.error_rows,
            "pages_processed": self.pages_processed,
            "pages_failed":    self.pages_failed,
            "rows_per_second": round(self.rows_per_second, 1),
            "success_rate_pct":round(self.success_rate, 2),
            "output_files":    self.output_files,
            "errors":          self.errors[-50:],  # last 50 errors
        }


# ─── Checkpoint ──────────────────────────────────────────────────────────────

class CheckpointManager:
    """
    Persists extraction progress to a JSON file so a failed run
    can be resumed from the last committed page.
    """

    def __init__(self, checkpoint_dir: Path, extractor_name: str) -> None:
        self._path = checkpoint_dir / f"{extractor_name}.checkpoint.json"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._state: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self._path.exists():
            try:
                with self._path.open("r") as fh:
                    data = json.load(fh)
                    logger.info("Checkpoint loaded from %s (page=%s)",
                                self._path, data.get("last_page", 0))
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load checkpoint %s: %s. Starting fresh.", self._path, exc)
        return {}

    def save(self, page: int, extracted_rows: int, **kwargs: Any) -> None:
        self._state.update({
            "last_page":     page,
            "extracted_rows":extracted_rows,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
            **kwargs,
        })
        try:
            with self._path.open("w") as fh:
                json.dump(self._state, fh, indent=2)
        except OSError as exc:
            logger.error("Failed to write checkpoint: %s", exc)

    def clear(self) -> None:
        self._state = {}
        if self._path.exists():
            self._path.unlink()
        logger.info("Checkpoint cleared: %s", self._path)

    @property
    def last_page(self) -> int:
        return int(self._state.get("last_page", 0))

    @property
    def extracted_rows(self) -> int:
        return int(self._state.get("extracted_rows", 0))


# ─── Abstract Base Extractor ─────────────────────────────────────────────────

class BaseExtractor(abc.ABC):
    """
    Abstract base for all legacy system extractors.

    Subclasses must implement:
        _build_count_query()    -> str
        _build_page_query()     -> str
        _get_expected_schema()  -> pa.Schema
        _post_process_df()      -> pd.DataFrame   (optional override)
    """

    # Subclasses set these class-level attributes
    EXTRACTOR_NAME: str = "BaseExtractor"
    OBJECT_NAME:    str = "unknown"

    def __init__(
        self,
        db_url:          str,
        output_dir:      Path,
        page_size:       int = 5000,
        checkpoint_dir:  Optional[Path] = None,
        max_retries:     int = 3,
        retry_delay_sec: float = 5.0,
        **engine_kwargs: Any,
    ) -> None:
        self.db_url          = db_url
        self.output_dir      = Path(output_dir)
        self.page_size       = page_size
        self.max_retries     = max_retries
        self.retry_delay_sec = retry_delay_sec
        self._engine: Optional[Engine] = None
        self.metrics = ExtractionMetrics(extractor_name=self.EXTRACTOR_NAME)
        self._engine_kwargs = engine_kwargs

        # Checkpoint
        ckpt_dir = checkpoint_dir or (self.output_dir / "checkpoints")
        self.checkpoint = CheckpointManager(ckpt_dir, self.EXTRACTOR_NAME)

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        logger.info("[%s] Initialized. output_dir=%s page_size=%d",
                    self.EXTRACTOR_NAME, self.output_dir, self.page_size)

    # ─── Connection ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Establish and validate the database connection."""
        logger.info("[%s] Connecting to source database...", self.EXTRACTOR_NAME)
        try:
            self._engine = create_engine(
                self.db_url,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=2,
                pool_timeout=30,
                **self._engine_kwargs,
            )
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("[%s] Database connection established.", self.EXTRACTOR_NAME)
        except SQLAlchemyError as exc:
            raise ConnectionError(
                f"[{self.EXTRACTOR_NAME}] Failed to connect: {exc}"
            ) from exc

    def close(self) -> None:
        """Release database resources."""
        if self._engine:
            self._engine.dispose()
            logger.info("[%s] Database connection closed.", self.EXTRACTOR_NAME)

    # ─── Abstract Interface ───────────────────────────────────────────────────

    @abc.abstractmethod
    def _build_count_query(self) -> str:
        """Return SQL to count total extractable records."""
        ...

    @abc.abstractmethod
    def _build_page_query(self, offset: int, limit: int) -> str:
        """Return SQL for a single page of records."""
        ...

    @abc.abstractmethod
    def _get_expected_schema(self) -> pa.Schema:
        """Return the PyArrow schema the output Parquet must conform to."""
        ...

    def _post_process_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Optional hook for subclass-specific DataFrame transformations
        before writing to Parquet (e.g., rename columns, cast types).
        """
        return df

    # ─── Public Extraction Entry Point ───────────────────────────────────────

    def extract(self, resume: bool = True) -> ExtractionMetrics:
        """
        Full extraction pipeline:
            1. Count total records
            2. Iterate pages (with optional resume from checkpoint)
            3. Write each page to Parquet
            4. Collect metrics and persist checkpoint

        Args:
            resume: If True, skip pages already processed per checkpoint.

        Returns:
            ExtractionMetrics with final statistics.
        """
        if self._engine is None:
            self.connect()

        start_page = self.checkpoint.last_page if resume else 0
        self.metrics.extracted_rows = self.checkpoint.extracted_rows if resume else 0

        logger.info("[%s] Starting extraction from page %d.", self.EXTRACTOR_NAME, start_page)

        try:
            # Step 1: Count
            self.metrics.total_rows = self._count_records()
            logger.info("[%s] Total records to extract: %d",
                        self.EXTRACTOR_NAME, self.metrics.total_rows)

            # Step 2: Paginate
            for page_df, page_num in self._paginate(start_page=start_page):
                try:
                    page_df = self._post_process_df(page_df)
                    self._validate_schema(page_df)
                    output_path = self._write_parquet(page_df, page_num)
                    self.metrics.extracted_rows += len(page_df)
                    self.metrics.pages_processed += 1
                    self.metrics.output_files.append(str(output_path))
                    self.checkpoint.save(
                        page=page_num + 1,
                        extracted_rows=self.metrics.extracted_rows,
                    )
                    self._log_progress()
                except Exception as page_exc:
                    self.metrics.pages_failed += 1
                    err_msg = f"Page {page_num} failed: {page_exc}"
                    self.metrics.errors.append(err_msg)
                    logger.error("[%s] %s", self.EXTRACTOR_NAME, err_msg)
                    # Continue to next page (non-fatal per page)

        except Exception as exc:
            logger.critical("[%s] Extraction aborted: %s", self.EXTRACTOR_NAME, exc)
            raise
        finally:
            self.metrics.finish()
            self._write_metrics()

        logger.info("[%s] Extraction complete. %s",
                    self.EXTRACTOR_NAME, self._summary_line())
        return self.metrics

    # ─── Pagination ───────────────────────────────────────────────────────────

    def _paginate(
        self, start_page: int = 0
    ) -> Generator[tuple[pd.DataFrame, int], None, None]:
        """Yields (DataFrame, page_number) for each page."""
        page = start_page
        while True:
            offset = page * self.page_size
            df = self._fetch_page_with_retry(offset)
            if df is None or df.empty:
                logger.info("[%s] No more records at offset %d.", self.EXTRACTOR_NAME, offset)
                break
            yield df, page
            if len(df) < self.page_size:
                break  # Last partial page
            page += 1

    def _fetch_page_with_retry(self, offset: int) -> Optional[pd.DataFrame]:
        """Fetch one page, retrying on transient failures."""
        sql = self._build_page_query(offset, self.page_size)
        for attempt in range(1, self.max_retries + 1):
            try:
                with self._engine.connect() as conn:
                    df = pd.read_sql(text(sql), conn)
                logger.debug("[%s] Page fetched: offset=%d rows=%d",
                             self.EXTRACTOR_NAME, offset, len(df))
                return df
            except SQLAlchemyError as exc:
                if attempt == self.max_retries:
                    self.metrics.errors.append(
                        f"offset={offset} failed after {self.max_retries} retries: {exc}")
                    logger.error("[%s] Giving up on offset %d: %s",
                                 self.EXTRACTOR_NAME, offset, exc)
                    return None
                delay = self.retry_delay_sec * (2 ** (attempt - 1))
                logger.warning("[%s] Retry %d/%d for offset %d (wait %.1fs): %s",
                               self.EXTRACTOR_NAME, attempt, self.max_retries,
                               offset, delay, exc)
                time.sleep(delay)
        return None

    def _count_records(self) -> int:
        sql = self._build_count_query()
        for attempt in range(1, self.max_retries + 1):
            try:
                with self._engine.connect() as conn:
                    result = conn.execute(text(sql))
                    return result.scalar() or 0
            except SQLAlchemyError as exc:
                if attempt == self.max_retries:
                    raise
                time.sleep(self.retry_delay_sec)
        return 0

    # ─── Parquet I/O ──────────────────────────────────────────────────────────

    def _write_parquet(self, df: pd.DataFrame, page_num: int) -> Path:
        """Write a DataFrame to a Parquet file, returning the file path."""
        ts    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        fname = f"{self.OBJECT_NAME}_page{page_num:05d}_{ts}.parquet"
        path  = self.output_dir / fname

        schema = self._get_expected_schema()
        table  = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
        pq.write_table(
            table, path,
            compression="snappy",
            row_group_size=100_000,
        )
        logger.debug("[%s] Wrote %d rows to %s", self.EXTRACTOR_NAME, len(df), path)
        return path

    def _validate_schema(self, df: pd.DataFrame) -> None:
        """Warn if required columns are missing from the extracted DataFrame."""
        expected = {field.name for field in self._get_expected_schema()}
        missing  = expected - set(df.columns)
        if missing:
            logger.warning(
                "[%s] Missing expected columns: %s. Available: %s",
                self.EXTRACTOR_NAME, missing, list(df.columns),
            )

    # ─── Metrics ──────────────────────────────────────────────────────────────

    def _write_metrics(self) -> None:
        path = self.output_dir / f"{self.EXTRACTOR_NAME}_metrics.json"
        try:
            with path.open("w") as fh:
                json.dump(self.metrics.to_dict(), fh, indent=2)
            logger.info("[%s] Metrics written to %s", self.EXTRACTOR_NAME, path)
        except OSError as exc:
            logger.error("[%s] Could not write metrics: %s", self.EXTRACTOR_NAME, exc)

    def _log_progress(self) -> None:
        pct = (
            (self.metrics.extracted_rows / self.metrics.total_rows * 100)
            if self.metrics.total_rows > 0 else 0
        )
        logger.info(
            "[%s] Progress: %d/%d rows (%.1f%%) | pages=%d | speed=%.0f rows/s",
            self.EXTRACTOR_NAME,
            self.metrics.extracted_rows,
            self.metrics.total_rows,
            pct,
            self.metrics.pages_processed,
            self.metrics.rows_per_second,
        )

    def _summary_line(self) -> str:
        m = self.metrics
        return (
            f"extracted={m.extracted_rows} total={m.total_rows} "
            f"pages={m.pages_processed} failed_pages={m.pages_failed} "
            f"duration={m.duration_seconds:.1f}s rate={m.rows_per_second:.0f}rows/s "
            f"success={m.success_rate:.1f}%"
        )

    # ─── Context Manager ──────────────────────────────────────────────────────

    def __enter__(self) -> "BaseExtractor":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
        if exc_type is None:
            self.checkpoint.clear()
