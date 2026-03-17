"""
reconciliation_report.py
─────────────────────────────────────────────────────────────────────────────
Generates HTML and CSV reconciliation reports comparing source legacy data
against Salesforce post-migration records.

Report sections:
  - Executive summary (counts, rates, pass/fail)
  - Per-object reconciliation table
  - Field-level mismatch details
  - Orphaned / missing records
  - Error log summary
  - Trend charts (if matplotlib is available)

Output formats: HTML (styled) and CSV (raw data).

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from data_validator import ValidationReport

logger = logging.getLogger(__name__)

# ─── Optional chart support ───────────────────────────────────────────────────
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# ─── HTML template constants ──────────────────────────────────────────────────
HTML_STYLE = """
<style>
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f6fa;
         color: #1a1a2e; margin: 0; padding: 0; }
  .container { max-width: 1200px; margin: 0 auto; padding: 32px 24px; }
  h1 { color: #0176d3; border-bottom: 3px solid #0176d3; padding-bottom: 8px; }
  h2 { color: #032d60; margin-top: 40px; }
  .summary-grid { display: grid; grid-template-columns: repeat(4,1fr);
                  gap: 16px; margin: 24px 0; }
  .kpi { background: #fff; border-radius: 8px; padding: 20px;
         box-shadow: 0 2px 8px rgba(0,0,0,.08);
         border-top: 4px solid #0176d3; text-align: center; }
  .kpi.pass { border-top-color: #2e844a; }
  .kpi.fail { border-top-color: #ba0517; }
  .kpi.warn { border-top-color: #dd7a01; }
  .kpi-value { font-size: 2.2rem; font-weight: 700; margin: 8px 0 4px; }
  .kpi-label { font-size: 0.75rem; color: #706e6b; text-transform: uppercase;
               letter-spacing: .08em; }
  table { width: 100%; border-collapse: collapse; background: #fff;
          border-radius: 8px; overflow: hidden;
          box-shadow: 0 2px 8px rgba(0,0,0,.08); margin: 16px 0 32px; }
  th { background: #032d60; color: #fff; padding: 12px 16px;
       text-align: left; font-size: 0.85rem; }
  td { padding: 10px 16px; border-bottom: 1px solid #e5e5e5;
       font-size: 0.85rem; }
  tr:last-child td { border-bottom: none; }
  tr:nth-child(even) { background: #f8f9fb; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 12px;
           font-size: 0.75rem; font-weight: 700; }
  .badge-pass { background: #eaf4ee; color: #2e844a; }
  .badge-fail { background: #fef1ee; color: #ba0517; }
  .badge-warn { background: #fef9ee; color: #dd7a01; }
  .badge-info { background: #e8f4fc; color: #0176d3; }
  footer { text-align: center; color: #706e6b; font-size: 0.75rem;
           margin-top: 40px; padding-top: 16px;
           border-top: 1px solid #e5e5e5; }
</style>
"""


@dataclass
class ReconciliationRow:
    """One row in the per-object reconciliation table."""
    object_name:    str
    source_count:   int
    sf_count:       int
    diff:           int
    diff_pct:       float
    success_rate:   float
    failed_count:   int
    status:         str  # "PASS" | "FAIL" | "WARN"
    notes:          str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_name":   self.object_name,
            "source_count":  self.source_count,
            "sf_count":      self.sf_count,
            "diff":          self.diff,
            "diff_pct":      self.diff_pct,
            "success_rate":  self.success_rate,
            "failed_count":  self.failed_count,
            "status":        self.status,
            "notes":         self.notes,
        }


class ReconciliationReport:
    """
    Generates a complete reconciliation report from one or more
    ValidationReport objects.
    """

    def __init__(
        self,
        reports:    List[ValidationReport],
        run_id:     str,
        output_dir: Path,
        title:      str = "Migration Reconciliation Report",
    ) -> None:
        self.reports    = reports
        self.run_id     = run_id
        self.output_dir = Path(output_dir)
        self.title      = title
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._generated_at = datetime.now(timezone.utc)

    def generate(self) -> Dict[str, str]:
        """
        Generate HTML and CSV reports.

        Returns:
            Dict with keys 'html', 'csv', 'json' pointing to output file paths
        """
        rows     = self._build_reconciliation_rows()
        summary  = self._build_summary(rows)
        all_checks = self._flatten_checks()

        ts       = self._generated_at.strftime("%Y%m%d_%H%M%S")
        html_path = self.output_dir / f"reconciliation_report_{ts}.html"
        csv_path  = self.output_dir / f"reconciliation_report_{ts}.csv"
        json_path = self.output_dir / f"reconciliation_report_{ts}.json"

        self._write_html(html_path, rows, summary, all_checks)
        self._write_csv(csv_path, rows, all_checks)
        self._write_json(json_path, rows, summary, all_checks)

        if MATPLOTLIB_AVAILABLE:
            chart_path = self.output_dir / f"reconciliation_chart_{ts}.png"
            self._write_chart(chart_path, rows)

        logger.info("[ReconciliationReport] Reports written to %s", self.output_dir)
        return {"html": str(html_path), "csv": str(csv_path), "json": str(json_path)}

    # ─── Data builders ────────────────────────────────────────────────────────

    def _build_reconciliation_rows(self) -> List[ReconciliationRow]:
        rows = []
        for rpt in self.reports:
            source_count = self._extract_source_count(rpt)
            sf_count     = self._extract_sf_count(rpt)
            diff         = sf_count - source_count
            diff_pct     = abs(diff) / max(source_count, 1) * 100
            status_dist  = self._extract_status_distribution(rpt)
            failed       = status_dist.get("Failed", 0)
            total_sf     = sum(status_dist.values()) or 1
            success_rate = (total_sf - failed) / total_sf * 100

            status = "PASS"
            if diff_pct > 1.0 or success_rate < 95:
                status = "FAIL"
            elif diff_pct > 0.5 or success_rate < 99:
                status = "WARN"

            rows.append(ReconciliationRow(
                object_name=rpt.object_name,
                source_count=source_count,
                sf_count=sf_count,
                diff=diff,
                diff_pct=round(diff_pct, 2),
                success_rate=round(success_rate, 2),
                failed_count=failed,
                status=status,
                notes=self._extract_notes(rpt),
            ))
        return rows

    def _build_summary(self, rows: List[ReconciliationRow]) -> Dict[str, Any]:
        total_source = sum(r.source_count for r in rows)
        total_sf     = sum(r.sf_count     for r in rows)
        total_failed = sum(r.failed_count for r in rows)
        pass_count   = sum(1 for r in rows if r.status == "PASS")
        fail_count   = sum(1 for r in rows if r.status == "FAIL")
        warn_count   = sum(1 for r in rows if r.status == "WARN")

        all_checks     = self._flatten_checks()
        critical_fails = [c for c in all_checks
                          if not c["passed"] and c["severity"] == "critical"]

        return {
            "run_id":              self.run_id,
            "generated_at":        self._generated_at.isoformat(),
            "total_source_records":total_source,
            "total_sf_records":    total_sf,
            "total_failed":        total_failed,
            "objects_pass":        pass_count,
            "objects_fail":        fail_count,
            "objects_warn":        warn_count,
            "critical_failures":   len(critical_fails),
            "overall_status":      "PASS" if fail_count == 0 and len(critical_fails) == 0
                                   else "FAIL",
        }

    def _flatten_checks(self) -> List[Dict]:
        checks = []
        for rpt in self.reports:
            for c in rpt.checks:
                d = c.to_dict()
                d["object_name"] = rpt.object_name
                checks.append(d)
        return checks

    # ─── Extraction helpers ───────────────────────────────────────────────────

    def _extract_source_count(self, rpt: ValidationReport) -> int:
        for c in rpt.checks:
            if c.check_name == "record_count" and c.details:
                return c.details.get("source_count", 0)
        return 0

    def _extract_sf_count(self, rpt: ValidationReport) -> int:
        for c in rpt.checks:
            if c.check_name == "record_count" and c.details:
                return c.details.get("sf_count", 0)
        return 0

    def _extract_status_distribution(self, rpt: ValidationReport) -> Dict[str, int]:
        for c in rpt.checks:
            if c.check_name == "migration_status_distribution" and c.details:
                return c.details.get("distribution", {})
        return {}

    def _extract_notes(self, rpt: ValidationReport) -> str:
        fails = [c.message for c in rpt.checks if not c.passed]
        return "; ".join(fails[:3]) if fails else "All checks passed."

    # ─── Output writers ───────────────────────────────────────────────────────

    def _write_html(
        self,
        path:       Path,
        rows:       List[ReconciliationRow],
        summary:    Dict[str, Any],
        all_checks: List[Dict],
    ) -> None:
        def badge(status: str) -> str:
            cls = {"PASS":"pass","FAIL":"fail","WARN":"warn",
                   "critical":"fail","warning":"warn","info":"info"}.get(status,"info")
            return f'<span class="badge badge-{cls}">{status.upper()}</span>'

        kpis_html = f"""
        <div class="summary-grid">
          <div class="kpi">
            <div class="kpi-label">Total Source Records</div>
            <div class="kpi-value">{summary["total_source_records"]:,}</div>
          </div>
          <div class="kpi {'pass' if summary['overall_status']=='PASS' else 'fail'}">
            <div class="kpi-label">Total SF Records</div>
            <div class="kpi-value">{summary["total_sf_records"]:,}</div>
          </div>
          <div class="kpi {'fail' if summary['total_failed']>0 else 'pass'}">
            <div class="kpi-label">Failed Records</div>
            <div class="kpi-value">{summary["total_failed"]:,}</div>
          </div>
          <div class="kpi {'pass' if summary['overall_status']=='PASS' else 'fail'}">
            <div class="kpi-label">Overall Status</div>
            <div class="kpi-value">{badge(summary["overall_status"])}</div>
          </div>
        </div>
        """

        recon_rows_html = "\n".join(
            f"""<tr>
              <td><strong>{r.object_name}</strong></td>
              <td>{r.source_count:,}</td>
              <td>{r.sf_count:,}</td>
              <td>{r.diff:+,}</td>
              <td>{r.diff_pct:.2f}%</td>
              <td>{r.success_rate:.1f}%</td>
              <td>{r.failed_count:,}</td>
              <td>{badge(r.status)}</td>
              <td style="font-size:0.75rem">{r.notes[:120]}</td>
            </tr>"""
            for r in rows
        )

        checks_rows_html = "\n".join(
            f"""<tr>
              <td>{c["object_name"]}</td>
              <td>{c["check_name"]}</td>
              <td>{badge(c["severity"])}</td>
              <td>{badge("PASS" if c["passed"] else "FAIL")}</td>
              <td style="font-size:0.75rem">{c["message"][:200]}</td>
            </tr>"""
            for c in all_checks
        )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{self.title}</title>
  {HTML_STYLE}
</head>
<body>
  <div class="container">
    <h1>{self.title}</h1>
    <p>Run ID: <strong>{self.run_id}</strong>
       &nbsp;|&nbsp; Generated: {self._generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")}
    </p>

    <h2>Executive Summary</h2>
    {kpis_html}

    <h2>Object Reconciliation</h2>
    <table>
      <thead>
        <tr>
          <th>Object</th><th>Source Count</th><th>SF Count</th><th>Diff</th>
          <th>Diff %</th><th>Success Rate</th><th>Failed</th>
          <th>Status</th><th>Notes</th>
        </tr>
      </thead>
      <tbody>{recon_rows_html}</tbody>
    </table>

    <h2>Detailed Check Results</h2>
    <table>
      <thead>
        <tr>
          <th>Object</th><th>Check</th><th>Severity</th>
          <th>Result</th><th>Message</th>
        </tr>
      </thead>
      <tbody>{checks_rows_html}</tbody>
    </table>

    <footer>
      Generated by Migration Platform &nbsp;|&nbsp;
      {self._generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")}
    </footer>
  </div>
</body>
</html>"""

        path.write_text(html, encoding="utf-8")
        logger.info("[ReconciliationReport] HTML written: %s", path)

    def _write_csv(
        self, path: Path, rows: List[ReconciliationRow], all_checks: List[Dict]
    ) -> None:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            # Summary section
            writer.writerow(["=== OBJECT RECONCILIATION ==="])
            writer.writerow(["object_name","source_count","sf_count","diff",
                              "diff_pct","success_rate","failed_count","status","notes"])
            for r in rows:
                writer.writerow([r.object_name, r.source_count, r.sf_count,
                                  r.diff, r.diff_pct, r.success_rate,
                                  r.failed_count, r.status, r.notes])
            writer.writerow([])
            # Checks section
            writer.writerow(["=== DETAILED CHECKS ==="])
            writer.writerow(["object_name","check_name","severity","passed","message"])
            for c in all_checks:
                writer.writerow([c["object_name"], c["check_name"], c["severity"],
                                  c["passed"], c["message"]])
        logger.info("[ReconciliationReport] CSV written: %s", path)

    def _write_json(
        self, path: Path, rows: List[ReconciliationRow],
        summary: Dict, all_checks: List[Dict]
    ) -> None:
        payload = {
            "summary":          summary,
            "reconciliation":   [r.to_dict() for r in rows],
            "checks":           all_checks,
        }
        with path.open("w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        logger.info("[ReconciliationReport] JSON written: %s", path)

    def _write_chart(self, path: Path, rows: List[ReconciliationRow]) -> None:
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        fig.suptitle("Migration Reconciliation Summary", fontsize=14, fontweight="bold")

        # Bar chart: source vs SF counts
        objs   = [r.object_name for r in rows]
        source = [r.source_count for r in rows]
        sf     = [r.sf_count     for r in rows]
        x      = range(len(objs))
        axes[0].bar([i - 0.2 for i in x], source, width=0.4, label="Source", color="#0176d3")
        axes[0].bar([i + 0.2 for i in x], sf,     width=0.4, label="Salesforce", color="#2e844a")
        axes[0].set_xticks(list(x))
        axes[0].set_xticklabels(objs, rotation=15)
        axes[0].set_title("Record Counts: Source vs Salesforce")
        axes[0].legend()
        axes[0].yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))

        # Donut: overall status
        pass_c = sum(1 for r in rows if r.status == "PASS")
        warn_c = sum(1 for r in rows if r.status == "WARN")
        fail_c = sum(1 for r in rows if r.status == "FAIL")
        sizes  = [s for s in [pass_c, warn_c, fail_c] if s > 0]
        labels = [l for l, s in [("PASS", pass_c), ("WARN", warn_c), ("FAIL", fail_c)] if s > 0]
        colors = ["#2e844a", "#dd7a01", "#ba0517"][:len(sizes)]
        axes[1].pie(sizes, labels=labels, colors=colors, autopct="%1.0f%%",
                    wedgeprops={"width": 0.5})
        axes[1].set_title("Object Status Distribution")

        plt.tight_layout()
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        logger.info("[ReconciliationReport] Chart written: %s", path)
