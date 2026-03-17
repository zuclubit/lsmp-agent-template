"""
orchestrator.py
─────────────────────────────────────────────────────────────────────────────
Main migration orchestrator — implements a full ETL pipeline as an
Apache Airflow DAG (with a Prefect fallback shim).

Pipeline stages:
  1. extract_accounts       — Extract from legacy SQL
  2. extract_contacts       — Extract from legacy SQL (parallel)
  3. transform_accounts     — Map, clean, validate
  4. transform_contacts     — Map, clean, validate (after accounts)
  5. load_accounts          — Bulk upsert to Salesforce
  6. generate_account_map   — Write legacy_id → SF_id mapping CSV
  7. load_contacts          — Bulk upsert to Salesforce
  8. validate_accounts      — Post-migration checks
  9. validate_contacts      — Post-migration checks
  10. reconciliation_report — HTML + CSV report

Configuration is loaded from migration_config.yaml.

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("MigrationOrchestrator")

# ─── Config helper ────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config" / "migration_config.yaml"


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    with path.open("r") as fh:
        return yaml.safe_load(fh)


# ─── Airflow DAG (primary) ────────────────────────────────────────────────────

try:
    from airflow import DAG
    from airflow.operators.python import PythonOperator
    from airflow.utils.dates import days_ago
    AIRFLOW_AVAILABLE = True
except ImportError:
    AIRFLOW_AVAILABLE = False
    logger.warning("Apache Airflow not available — using standalone runner.")


def _resolve_paths(cfg: Dict) -> Dict[str, Path]:
    """Build absolute output directories from config."""
    base = Path(cfg["pipeline"]["working_dir"])
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return {
        "raw_accounts":    base / "raw"       / "accounts",
        "raw_contacts":    base / "raw"       / "contacts",
        "tfm_accounts":    base / "transform" / "accounts",
        "tfm_contacts":    base / "transform" / "contacts",
        "load_output":     base / "load",
        "validation":      base / "validation",
        "reports":         base / "reports"   / run_ts,
        "checkpoints":     base / "checkpoints",
    }


# ─── Task functions (called by Airflow operators or standalone runner) ─────────

def task_extract_accounts(**context: Any) -> str:
    from legacy_extractors.account_extractor import AccountExtractor

    cfg   = load_config()
    paths = _resolve_paths(cfg)
    db    = cfg["source_database"]

    extractor = AccountExtractor(
        db_url=db["url"],
        output_dir=paths["raw_accounts"],
        page_size=cfg["pipeline"]["extract"]["page_size"],
        active_only=cfg["pipeline"]["extract"]["active_only"],
        checkpoint_dir=paths["checkpoints"],
    )
    with extractor:
        metrics = extractor.extract(
            resume=cfg["pipeline"]["extract"]["resume_on_failure"])

    logger.info("[Orchestrator] Account extraction complete: %s", metrics.to_dict())
    return str(paths["raw_accounts"])


def task_extract_contacts(**context: Any) -> str:
    from legacy_extractors.contact_extractor import ContactExtractor

    cfg   = load_config()
    paths = _resolve_paths(cfg)
    db    = cfg["source_database"]

    extractor = ContactExtractor(
        db_url=db["url"],
        output_dir=paths["raw_contacts"],
        page_size=cfg["pipeline"]["extract"]["page_size"],
        active_only=cfg["pipeline"]["extract"]["active_only"],
        checkpoint_dir=paths["checkpoints"],
        mask_pii=cfg["pipeline"]["extract"].get("mask_pii", False),
    )
    with extractor:
        metrics = extractor.extract(
            resume=cfg["pipeline"]["extract"]["resume_on_failure"])

    logger.info("[Orchestrator] Contact extraction complete: %s", metrics.to_dict())
    return str(paths["raw_contacts"])


def task_transform_accounts(**context: Any) -> str:
    from data_transformations.account_transformer import AccountTransformer

    cfg   = load_config()
    paths = _resolve_paths(cfg)

    transformer = AccountTransformer(
        input_dir=paths["raw_accounts"],
        output_dir=paths["tfm_accounts"],
        dry_run=cfg["pipeline"]["transform"].get("dry_run", False),
    )
    metrics = transformer.transform()
    logger.info("[Orchestrator] Account transform complete: %s", metrics)
    return str(paths["tfm_accounts"])


def task_transform_contacts(**context: Any) -> str:
    from data_transformations.contact_transformer import ContactTransformer

    cfg   = load_config()
    paths = _resolve_paths(cfg)

    # Find account mapping CSV from load stage
    acct_map_glob = sorted(paths["load_output"].glob("account_id_mapping_*.csv"))
    acct_map_csv  = acct_map_glob[-1] if acct_map_glob else None

    transformer = ContactTransformer(
        input_dir=paths["raw_contacts"],
        output_dir=paths["tfm_contacts"],
        account_mapping_csv=acct_map_csv,
        dry_run=cfg["pipeline"]["transform"].get("dry_run", False),
    )
    metrics = transformer.transform()
    logger.info("[Orchestrator] Contact transform complete: %s", metrics)
    return str(paths["tfm_contacts"])


def task_load_accounts(**context: Any) -> str:
    from loaders.salesforce_bulk_loader import SalesforceBulkLoader
    import pyarrow.parquet as pq
    import pandas as pd

    cfg   = load_config()
    paths = _resolve_paths(cfg)
    sf    = cfg["salesforce"]
    lc    = cfg["pipeline"]["load"]

    files  = sorted(paths["tfm_accounts"].glob("sf_accounts_*.parquet"))
    frames = [pq.read_table(f).to_pandas() for f in files]
    df     = pd.concat(frames, ignore_index=True)

    loader = SalesforceBulkLoader(
        sf_instance_url=sf["instance_url"],
        sf_username=sf["username"],
        sf_password=sf["password"],
        sf_security_token=sf.get("security_token", ""),
        sf_object_name="Account",
        batch_size=lc["batch_size"],
        job_size=lc["job_size"],
        operation=lc.get("operation", "upsert"),
        external_id_field="Legacy_ID__c",
        use_sandbox=sf.get("is_sandbox", True),
        output_dir=paths["load_output"],
        write_account_map=True,
    )
    metrics = loader.load(df)
    loader.write_account_mapping_csv(metrics)
    logger.info("[Orchestrator] Account load complete: success=%d failed=%d",
                metrics.total_success, metrics.total_failed)
    return str(paths["load_output"])


def task_load_contacts(**context: Any) -> str:
    from loaders.salesforce_bulk_loader import SalesforceBulkLoader
    import pyarrow.parquet as pq
    import pandas as pd

    cfg   = load_config()
    paths = _resolve_paths(cfg)
    sf    = cfg["salesforce"]
    lc    = cfg["pipeline"]["load"]

    files  = sorted(paths["tfm_contacts"].glob("sf_contacts_*.parquet"))
    frames = [pq.read_table(f).to_pandas() for f in files]
    df     = pd.concat(frames, ignore_index=True)

    loader = SalesforceBulkLoader(
        sf_instance_url=sf["instance_url"],
        sf_username=sf["username"],
        sf_password=sf["password"],
        sf_security_token=sf.get("security_token", ""),
        sf_object_name="Contact",
        batch_size=lc["batch_size"],
        job_size=lc["job_size"],
        operation=lc.get("operation", "upsert"),
        external_id_field="Legacy_ID__c",
        use_sandbox=sf.get("is_sandbox", True),
        output_dir=paths["load_output"],
    )
    metrics = loader.load(df)
    logger.info("[Orchestrator] Contact load complete: success=%d failed=%d",
                metrics.total_success, metrics.total_failed)
    return str(paths["load_output"])


def task_validate_accounts(**context: Any) -> str:
    from validation.data_validator import DataValidator

    cfg   = load_config()
    paths = _resolve_paths(cfg)
    sf    = cfg["salesforce"]
    run_id = datetime.now(timezone.utc).strftime("VAL-%Y%m%d-%H%M%S")

    validator = DataValidator(
        sf_username=sf["username"],
        sf_password=sf["password"],
        sf_security_token=sf.get("security_token", ""),
        use_sandbox=sf.get("is_sandbox", True),
        output_dir=paths["validation"],
    )
    report = validator.validate_accounts(paths["tfm_accounts"], run_id)
    logger.info("[Orchestrator] Account validation: %s (%d/%d checks passed)",
                "PASS" if report.is_passing else "FAIL",
                report.passed_count, len(report.checks))

    if not report.is_passing:
        critical = [c.message for c in report.critical_failures]
        raise RuntimeError(
            f"Account validation FAILED. Critical issues: {critical}")
    return run_id


def task_validate_contacts(**context: Any) -> str:
    from validation.data_validator import DataValidator

    cfg   = load_config()
    paths = _resolve_paths(cfg)
    sf    = cfg["salesforce"]
    run_id = datetime.now(timezone.utc).strftime("VAL-%Y%m%d-%H%M%S")

    validator = DataValidator(
        sf_username=sf["username"],
        sf_password=sf["password"],
        sf_security_token=sf.get("security_token", ""),
        use_sandbox=sf.get("is_sandbox", True),
        output_dir=paths["validation"],
    )
    report = validator.validate_contacts(paths["tfm_contacts"], run_id)
    logger.info("[Orchestrator] Contact validation: %s", "PASS" if report.is_passing else "FAIL")
    return run_id


def task_reconciliation_report(**context: Any) -> str:
    from validation.data_validator import DataValidator, ValidationReport
    from validation.reconciliation_report import ReconciliationReport

    cfg   = load_config()
    paths = _resolve_paths(cfg)
    paths["reports"].mkdir(parents=True, exist_ok=True)
    sf    = cfg["salesforce"]

    validator = DataValidator(
        sf_username=sf["username"],
        sf_password=sf["password"],
        sf_security_token=sf.get("security_token", ""),
        use_sandbox=sf.get("is_sandbox", True),
        output_dir=paths["validation"],
    )

    run_id = datetime.now(timezone.utc).strftime("RPT-%Y%m%d-%H%M%S")
    acct_report = validator.validate_accounts(paths["tfm_accounts"], run_id)
    cont_report = validator.validate_contacts(paths["tfm_contacts"], run_id)

    report_gen = ReconciliationReport(
        reports=[acct_report, cont_report],
        run_id=run_id,
        output_dir=paths["reports"],
        title="Legacy → Salesforce Migration Reconciliation Report",
    )
    output_paths = report_gen.generate()
    logger.info("[Orchestrator] Reconciliation report generated: %s", output_paths)
    return output_paths.get("html", "")


# ─── Airflow DAG definition ────────────────────────────────────────────────────

if AIRFLOW_AVAILABLE:
    _cfg = load_config()
    _sf  = _cfg.get("salesforce", {})

    DEFAULT_ARGS = {
        "owner":            "migration-team",
        "depends_on_past":  False,
        "email":            [_sf.get("notification_email", "migration-ops@company.com")],
        "email_on_failure": True,
        "email_on_retry":   False,
        "retries":          2,
        "retry_delay":      timedelta(minutes=5),
    }

    dag = DAG(
        dag_id="legacy_to_salesforce_migration",
        default_args=DEFAULT_ARGS,
        description="Full Legacy → Salesforce data migration pipeline",
        schedule_interval=None,   # Triggered manually
        start_date=days_ago(1),
        catchup=False,
        max_active_runs=1,
        tags=["migration", "salesforce", "etl"],
    )

    with dag:
        t_extract_accts = PythonOperator(
            task_id="extract_accounts",
            python_callable=task_extract_accounts,
            dag=dag,
        )
        t_extract_conts = PythonOperator(
            task_id="extract_contacts",
            python_callable=task_extract_contacts,
            dag=dag,
        )
        t_transform_accts = PythonOperator(
            task_id="transform_accounts",
            python_callable=task_transform_accounts,
            dag=dag,
        )
        t_transform_conts = PythonOperator(
            task_id="transform_contacts",
            python_callable=task_transform_contacts,
            dag=dag,
        )
        t_load_accts = PythonOperator(
            task_id="load_accounts",
            python_callable=task_load_accounts,
            dag=dag,
        )
        t_load_conts = PythonOperator(
            task_id="load_contacts",
            python_callable=task_load_contacts,
            dag=dag,
        )
        t_validate_accts = PythonOperator(
            task_id="validate_accounts",
            python_callable=task_validate_accounts,
            dag=dag,
        )
        t_validate_conts = PythonOperator(
            task_id="validate_contacts",
            python_callable=task_validate_contacts,
            dag=dag,
        )
        t_report = PythonOperator(
            task_id="reconciliation_report",
            python_callable=task_reconciliation_report,
            dag=dag,
        )

        # Pipeline graph
        # Extract in parallel
        [t_extract_accts, t_extract_conts]

        # Transform accounts after extraction
        t_extract_accts >> t_transform_accts

        # Load accounts before transforming contacts (needs mapping)
        t_transform_accts >> t_load_accts

        # Transform contacts requires account mapping
        t_load_accts >> t_transform_conts
        t_extract_conts >> t_transform_conts

        # Load contacts after transformation
        t_transform_conts >> t_load_conts

        # Validate in parallel after loading
        t_load_accts >> t_validate_accts
        t_load_conts >> t_validate_conts

        # Final report after both validations
        [t_validate_accts, t_validate_conts] >> t_report


# ─── Standalone runner (no Airflow) ───────────────────────────────────────────

class StandalonePipelineRunner:
    """
    Runs the full migration pipeline sequentially without Airflow.
    Useful for development, testing, and small migrations.
    """

    TASKS = [
        ("extract_accounts",       task_extract_accounts),
        ("extract_contacts",       task_extract_contacts),
        ("transform_accounts",     task_transform_accounts),
        ("load_accounts",          task_load_accounts),
        ("transform_contacts",     task_transform_contacts),
        ("load_contacts",          task_load_contacts),
        ("validate_accounts",      task_validate_accounts),
        ("validate_contacts",      task_validate_contacts),
        ("reconciliation_report",  task_reconciliation_report),
    ]

    def run(
        self,
        start_from: Optional[str] = None,
        stop_after: Optional[str] = None,
        dry_run:    bool = False,
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        started = start_from is None

        for name, fn in self.TASKS:
            if not started:
                if name == start_from:
                    started = True
                else:
                    logger.info("[StandaloneRunner] Skipping task: %s", name)
                    continue

            logger.info("[StandaloneRunner] ── Starting task: %s ──", name)
            try:
                if dry_run:
                    logger.info("[StandaloneRunner] DRY RUN — skipping execution of %s", name)
                    results[name] = "DRY_RUN"
                else:
                    result       = fn()
                    results[name] = result
                    logger.info("[StandaloneRunner] Task %s completed. Result: %s", name, result)
            except Exception as exc:
                logger.error("[StandaloneRunner] Task %s FAILED: %s", name, exc)
                results[name] = f"FAILED: {exc}"
                raise

            if stop_after and name == stop_after:
                logger.info("[StandaloneRunner] Stopping after task: %s", name)
                break

        return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Legacy → Salesforce migration pipeline.")
    parser.add_argument("--start-from", default=None,
                        help="Task name to start from (skips earlier tasks)")
    parser.add_argument("--stop-after", default=None,
                        help="Task name to stop after")
    parser.add_argument("--dry-run",    action="store_true",
                        help="Print tasks without executing")
    args = parser.parse_args()

    runner  = StandalonePipelineRunner()
    results = runner.run(
        start_from=args.start_from,
        stop_after=args.stop_after,
        dry_run=args.dry_run,
    )

    logger.info("Pipeline Results:")
    for task, result in results.items():
        status = "FAILED" if str(result).startswith("FAILED") else "OK"
        logger.info("  %-35s %s", task, status)
