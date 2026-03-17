#!/usr/bin/env python3
"""
live.py — LSMP Live Mode Launcher
===================================
Starts all platform services in parallel and opens the browser:

  Port 8080 → Dashboard (KPIs, progress, agent feed, controls)
  Port 9001 → Mock Salesforce API (Bulk API 2.0 simulator)
  Port 8000 → Migration Control API (run status, health, metrics)

Usage:
    python3 live.py
    make live
"""
from __future__ import annotations

import os
import sys
import time
import signal
import threading
import subprocess
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Load .env ──────────────────────────────────────────────────────────────────
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

os.environ.setdefault("SF_MOCK_MODE", "true")
os.environ.setdefault("MIGRATION_DRY_RUN", "true")
os.environ.setdefault("ENVIRONMENT", "development")

# ── ANSI colours ───────────────────────────────────────────────────────────────
RESET = "\033[0m"; BOLD = "\033[1m"
GREEN = "\033[32m"; CYAN = "\033[36m"; YELLOW = "\033[33m"; RED = "\033[31m"; MAGENTA = "\033[35m"

def banner():
    print(f"""
{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════╗
║          LSMP — Migration Platform  LIVE MODE               ║
║   Legacy-to-Salesforce Migration Platform (Development)     ║
╚══════════════════════════════════════════════════════════════╝{RESET}
  {GREEN}✓{RESET}  Dashboard          →  {BOLD}http://localhost:8080{RESET}
  {GREEN}✓{RESET}  Mock Salesforce API →  {BOLD}http://localhost:9001{RESET}
  {GREEN}✓{RESET}  Migration API       →  {BOLD}http://localhost:8000{RESET}

  {YELLOW}SF_MOCK_MODE=true   MIGRATION_DRY_RUN=true{RESET}
  Press {BOLD}Ctrl+C{RESET} to stop all services.
""")

# ── Migration Control API (port 8000) ─────────────────────────────────────────

def build_migration_api():
    """Minimal FastAPI app simulating the migration control-plane API."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    import random, uuid
    from datetime import datetime, timezone

    app = FastAPI(title="LSMP Migration Control API", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    _runs: dict = {}

    def _make_run(run_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "run_id": run_id,
            "status": "RUNNING",
            "created_at": now,
            "object_types": ["Account", "Contact", "Opportunity"],
            "total_records": 200,
            "processed_records": random.randint(40, 180),
            "successful_records": random.randint(30, 170),
            "failed_records": random.randint(0, 5),
            "error_rate": round(random.uniform(0.0, 0.04), 4),
            "batch_size": 200,
            "current_object": random.choice(["Account", "Contact", "Opportunity"]),
            "salesforce_job_id": f"750{uuid.uuid4().hex[:15].upper()}",
            "dry_run": True,
            "environment": "development",
        }

    @app.get("/api/v1/health")
    def health():
        return {"status": "healthy", "version": "1.0.0", "mode": "demo",
                "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.get("/api/v1/migrations/runs/{run_id}")
    def get_run(run_id: str):
        if run_id not in _runs:
            _runs[run_id] = _make_run(run_id)
        r = _runs[run_id]
        # simulate progress
        if r["status"] == "RUNNING":
            r["processed_records"] = min(r["total_records"],
                                         r["processed_records"] + random.randint(1, 6))
            r["successful_records"] = r["processed_records"] - r["failed_records"]
            if r["processed_records"] >= r["total_records"]:
                r["status"] = "COMPLETED"
        return JSONResponse(r)

    @app.post("/api/v1/migrations/runs/{run_id}/pause")
    def pause_run(run_id: str):
        if run_id not in _runs:
            _runs[run_id] = _make_run(run_id)
        _runs[run_id]["status"] = "PAUSED"
        return {"run_id": run_id, "status": "PAUSED"}

    @app.post("/api/v1/migrations/runs/{run_id}/resume")
    def resume_run(run_id: str):
        if run_id not in _runs:
            _runs[run_id] = _make_run(run_id)
        _runs[run_id]["status"] = "RUNNING"
        return {"run_id": run_id, "status": "RUNNING"}

    @app.get("/api/v1/migrations/errors")
    def get_errors(run_id: str = "demo-001", page: int = 1, limit: int = 20):
        errors = [
            {"legacy_id": f"SIEBL-{1000+i}", "object_type": random.choice(["Account","Contact"]),
             "error_code": random.choice(["DUPLICATE_VALUE","REQUIRED_FIELD_MISSING","FIELD_TOO_LONG"]),
             "error_message": "Field validation failed", "retry_count": random.randint(0,3)}
            for i in range(3)
        ]
        return {"run_id": run_id, "errors": errors, "total": 3, "page": page}

    @app.get("/api/v1/integrations/salesforce/limits")
    def sf_limits():
        return {
            "DailyApiRequests": {"Max": 15000, "Remaining": 14823},
            "DailyBulkApiRequests": {"Max": 5000, "Remaining": 4991},
            "DataStorageMB": {"Max": 1024, "Remaining": 987},
        }

    @app.post("/api/v1/migrations/runs")
    def create_run(body: dict = None):
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        _runs[run_id] = _make_run(run_id)
        return _runs[run_id]

    return app


# ── Enhanced Dashboard (port 8080) ────────────────────────────────────────────

def build_dashboard():
    """Full dashboard with live data wiring to migration API."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    import random
    from datetime import datetime

    app = FastAPI(title="LSMP Dashboard")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    # Demo state that evolves over time
    _state = {
        "Account":     {"total": 50,  "migrated": 0, "failed": 0, "pending": 50},
        "Contact":     {"total": 100, "migrated": 0, "failed": 0, "pending": 100},
        "Opportunity": {"total": 50,  "migrated": 0, "failed": 0, "pending": 50},
        "running": False,
        "paused": False,
        "run_id": None,
        "started_at": None,
    }

    _events = [
        {"ts": datetime.now().strftime("%H:%M:%S"), "agent": "orchestrator-agent",
         "message": "System initialised. All gates GREEN.", "level": "success"},
        {"ts": datetime.now().strftime("%H:%M:%S"), "agent": "planning-agent",
         "message": "Migration plan loaded: Account → Contact → Opportunity", "level": "info"},
        {"ts": datetime.now().strftime("%H:%M:%S"), "agent": "security-agent",
         "message": "Security preflight complete. 0 critical, 0 high.", "level": "success"},
        {"ts": datetime.now().strftime("%H:%M:%S"), "agent": "validation-agent",
         "message": "Pre-migration validation: Grade A (score 0.964)", "level": "success"},
    ]

    def _add_event(agent, msg, level="info"):
        _events.insert(0, {"ts": datetime.now().strftime("%H:%M:%S"),
                           "agent": agent, "message": msg, "level": level})
        if len(_events) > 60:
            _events.pop()

    def _tick():
        """Advance simulation state if running."""
        if not _state["running"] or _state["paused"]:
            return
        for obj in ["Account", "Contact", "Opportunity"]:
            s = _state[obj]
            if s["pending"] > 0:
                batch = random.randint(1, 4)
                fail = 1 if random.random() < 0.03 else 0
                moved = min(batch, s["pending"])
                s["pending"]  -= moved
                s["migrated"] += (moved - fail)
                s["failed"]   += fail

    @app.get("/api/stats")
    def stats():
        _tick()
        s = _state
        total    = sum(s[o]["total"]    for o in ["Account","Contact","Opportunity"])
        migrated = sum(s[o]["migrated"] for o in ["Account","Contact","Opportunity"])
        failed   = sum(s[o]["failed"]   for o in ["Account","Contact","Opportunity"])
        pending  = sum(s[o]["pending"]  for o in ["Account","Contact","Opportunity"])
        return JSONResponse({
            "total": total, "migrated": migrated, "failed": failed, "pending": pending,
            "progress_pct": round(migrated / total * 100, 1) if total else 0,
            "error_rate": round(failed / max(migrated+failed,1) * 100, 2),
            "objects": {o: s[o] for o in ["Account","Contact","Opportunity"]},
            "running": s["running"], "paused": s["paused"],
            "run_id": s["run_id"], "db_connected": False,
            "agents": {
                "orchestrator-agent": "active" if s["running"] else "idle",
                "planning-agent": "active" if s["running"] else "idle",
                "validation-agent": "active" if s["running"] else "idle",
                "execution-agent": "active" if s["running"] else "idle",
                "security-agent": "idle",
                "debugging-agent": "idle",
            }
        })

    @app.get("/api/events")
    def events():
        return JSONResponse(_events)

    @app.post("/api/run/start")
    def run_start():
        import uuid
        _state["running"] = True
        _state["paused"]  = False
        _state["run_id"]  = f"run-{uuid.uuid4().hex[:8]}"
        _state["started_at"] = datetime.now().isoformat()
        for obj in ["Account","Contact","Opportunity"]:
            n = _state[obj]["total"]
            _state[obj].update({"migrated": 0, "failed": 0, "pending": n})
        _add_event("orchestrator-agent", f"Migration run {_state['run_id']} started (dry-run=true)", "success")
        _add_event("execution-agent", "Bulk API 2.0 jobs initialised for Account batch 1", "info")
        return {"status": "started", "run_id": _state["run_id"]}

    @app.post("/api/run/pause")
    def run_pause():
        _state["paused"] = True
        _add_event("orchestrator-agent", f"Run {_state['run_id']} PAUSED by operator", "warning")
        return {"status": "paused"}

    @app.post("/api/run/resume")
    def run_resume():
        _state["paused"] = False
        _add_event("orchestrator-agent", f"Run {_state['run_id']} RESUMED", "info")
        return {"status": "running"}

    # ── HTML ──────────────────────────────────────────────────────────────────
    HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LSMP — Migration Control Center</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}
header{background:linear-gradient(135deg,#1a237e,#283593);padding:1rem 2rem;display:flex;
  align-items:center;justify-content:space-between;border-bottom:2px solid #3949ab;position:sticky;top:0;z-index:100}
header h1{font-size:1.4rem;font-weight:700;letter-spacing:.05em}
header h1 span{color:#82b1ff}
.badge{font-size:.7rem;padding:.2rem .6rem;border-radius:999px;font-weight:600;letter-spacing:.05em}
.badge-live{background:#1b5e20;color:#69f0ae;border:1px solid #2e7d32}
.badge-mock{background:#e65100;color:#ffccbc;border:1px solid #bf360c}
.badge-dry{background:#1a237e;color:#82b1ff;border:1px solid #283593}
.header-badges{display:flex;gap:.5rem;align-items:center}
.ts{font-size:.75rem;color:#78909c}

main{padding:1.5rem 2rem;max-width:1400px;margin:0 auto}

/* KPI */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:1.5rem}
.kpi{background:#1e2130;border-radius:12px;padding:1.2rem;border-top:3px solid var(--c);
  transition:transform .2s}
.kpi:hover{transform:translateY(-2px)}
.kpi .val{font-size:2.2rem;font-weight:800;color:var(--c)}
.kpi .lbl{font-size:.75rem;color:#90a4ae;text-transform:uppercase;letter-spacing:.1em;margin-top:.3rem}
.kpi .sub{font-size:.7rem;color:#546e7a;margin-top:.2rem}

/* Progress */
.section{background:#1e2130;border-radius:12px;padding:1.2rem;margin-bottom:1rem}
.section h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.12em;color:#78909c;margin-bottom:1rem}
.obj-row{display:flex;align-items:center;gap:1rem;margin-bottom:.8rem}
.obj-label{width:100px;font-size:.8rem;font-weight:600;color:#b0bec5}
.bar-wrap{flex:1;background:#263238;border-radius:999px;height:10px;overflow:hidden}
.bar-fill{height:100%;border-radius:999px;transition:width .6s ease;min-width:2px}
.bar-green{background:linear-gradient(90deg,#1b5e20,#43a047)}
.obj-pct{width:45px;text-align:right;font-size:.8rem;font-weight:700;color:#81c784}
.obj-counts{font-size:.7rem;color:#546e7a;width:160px;text-align:right}

/* Two-column layout */
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem}
@media(max-width:900px){.cols{grid-template-columns:1fr}}

/* Controls */
.controls{display:flex;gap:.8rem;flex-wrap:wrap;margin-top:.5rem}
.btn{padding:.55rem 1.4rem;border-radius:8px;border:none;cursor:pointer;font-size:.85rem;
  font-weight:600;letter-spacing:.04em;transition:all .15s}
.btn-start{background:#1b5e20;color:#e8f5e9}
.btn-start:hover{background:#2e7d32}
.btn-pause{background:#e65100;color:#fff3e0}
.btn-pause:hover{background:#bf360c}
.btn-resume{background:#1565c0;color:#e3f2fd}
.btn-resume:hover{background:#1976d2}
.btn:disabled{opacity:.4;cursor:not-allowed}

/* Agent grid */
.agent-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.6rem}
.agent-card{background:#161b27;border-radius:8px;padding:.7rem 1rem;border-left:3px solid var(--ac);
  display:flex;align-items:center;gap:.6rem}
.agent-dot{width:8px;height:8px;border-radius:50%;background:var(--ac);flex-shrink:0}
.agent-name{font-size:.75rem;color:#b0bec5}
.agent-status{font-size:.65rem;color:var(--ac);font-weight:600;text-transform:uppercase;margin-left:auto}

/* Events */
.events{max-height:260px;overflow-y:auto}
.event{display:flex;gap:.6rem;padding:.45rem 0;border-bottom:1px solid #1c2333;font-size:.78rem}
.event:last-child{border-bottom:none}
.evt-ts{color:#546e7a;flex-shrink:0;width:56px}
.evt-agent{color:#82b1ff;flex-shrink:0;width:140px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.evt-msg{color:#cfd8dc}
.evt-success .evt-msg{color:#81c784}
.evt-warning .evt-msg{color:#ffb74d}
.evt-error   .evt-msg{color:#ef9a9a}

/* Status bar */
footer{position:fixed;bottom:0;left:0;right:0;background:#0d1117;border-top:1px solid #1c2333;
  padding:.4rem 2rem;display:flex;gap:2rem;font-size:.72rem;color:#546e7a}
footer span b{color:#82b1ff}
</style>
</head>
<body>
<header>
  <div>
    <h1>LSMP — <span>Migration Control Center</span></h1>
    <div class="ts" id="clock"></div>
  </div>
  <div class="header-badges">
    <span class="badge badge-live">● LIVE</span>
    <span class="badge badge-mock">SF MOCK</span>
    <span class="badge badge-dry">DRY-RUN</span>
  </div>
</header>

<main>
  <!-- KPI row -->
  <div class="kpis">
    <div class="kpi" style="--c:#42a5f5"><div class="val" id="k-total">—</div>
      <div class="lbl">Total Records</div><div class="sub">Account+Contact+Opp</div></div>
    <div class="kpi" style="--c:#66bb6a"><div class="val" id="k-migrated">—</div>
      <div class="lbl">Migrated</div><div class="sub" id="k-pct">0%</div></div>
    <div class="kpi" style="--c:#ef5350"><div class="val" id="k-failed">—</div>
      <div class="lbl">Failed</div><div class="sub" id="k-err">0% error rate</div></div>
    <div class="kpi" style="--c:#ffa726"><div class="val" id="k-pending">—</div>
      <div class="lbl">Pending</div><div class="sub" id="k-status">Idle</div></div>
  </div>

  <!-- Progress + Controls -->
  <div class="cols">
    <div class="section">
      <h2>Object Progress</h2>
      <div id="obj-rows">
        <div class="obj-row"><span class="obj-label">Account</span>
          <div class="bar-wrap"><div class="bar-fill bar-green" id="bar-Account" style="width:0%"></div></div>
          <span class="obj-pct" id="pct-Account">0%</span>
          <span class="obj-counts" id="cnt-Account">0/0</span></div>
        <div class="obj-row"><span class="obj-label">Contact</span>
          <div class="bar-wrap"><div class="bar-fill bar-green" id="bar-Contact" style="width:0%"></div></div>
          <span class="obj-pct" id="pct-Contact">0%</span>
          <span class="obj-counts" id="cnt-Contact">0/0</span></div>
        <div class="obj-row"><span class="obj-label">Opportunity</span>
          <div class="bar-wrap"><div class="bar-fill bar-green" id="bar-Opportunity" style="width:0%"></div></div>
          <span class="obj-pct" id="pct-Opportunity">0%</span>
          <span class="obj-counts" id="cnt-Opportunity">0/0</span></div>
      </div>
    </div>

    <div class="section">
      <h2>Migration Controls</h2>
      <div style="font-size:.82rem;color:#90a4ae;margin-bottom:.8rem">
        Current run: <b id="run-id" style="color:#82b1ff">—</b><br>
        Status: <b id="run-status" style="color:#ffa726">IDLE</b>
      </div>
      <div class="controls">
        <button class="btn btn-start"  id="btn-start"  onclick="ctrlRun('start')">▶ Start Migration</button>
        <button class="btn btn-pause"  id="btn-pause"  onclick="ctrlRun('pause')"  disabled>⏸ Pause</button>
        <button class="btn btn-resume" id="btn-resume" onclick="ctrlRun('resume')" disabled>▶ Resume</button>
      </div>
      <div style="margin-top:.8rem;font-size:.72rem;color:#546e7a">
        ⚠ Dry-run mode active — no real Salesforce writes
      </div>
    </div>
  </div>

  <!-- Agents + Events -->
  <div class="cols">
    <div class="section">
      <h2>Agent Status</h2>
      <div class="agent-grid" id="agent-grid">
        <!-- populated by JS -->
      </div>
    </div>

    <div class="section">
      <h2>Activity Feed</h2>
      <div class="events" id="events-feed"></div>
    </div>
  </div>
</main>

<footer>
  <span>Dashboard: <b>http://localhost:8080</b></span>
  <span>Mock SF: <b>http://localhost:9001</b></span>
  <span>Migration API: <b>http://localhost:8000</b></span>
  <span id="db-status">DB: <b>demo-mode</b></span>
  <span style="margin-left:auto" id="last-refresh">Refreshing…</span>
</footer>

<script>
const AGENT_COLORS = {
  'orchestrator-agent': '#42a5f5',
  'planning-agent':     '#ab47bc',
  'validation-agent':   '#26c6da',
  'execution-agent':    '#66bb6a',
  'security-agent':     '#ffa726',
  'debugging-agent':    '#ef5350',
};

function clock() {
  document.getElementById('clock').textContent =
    new Date().toLocaleTimeString('en-US',{hour12:false}) + ' local';
}
setInterval(clock, 1000); clock();

async function fetchStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('k-total').textContent    = d.total.toLocaleString();
    document.getElementById('k-migrated').textContent = d.migrated.toLocaleString();
    document.getElementById('k-failed').textContent   = d.failed.toLocaleString();
    document.getElementById('k-pending').textContent  = d.pending.toLocaleString();
    document.getElementById('k-pct').textContent      = d.progress_pct + '% complete';
    document.getElementById('k-err').textContent      = d.error_rate + '% error rate';
    document.getElementById('k-status').textContent   =
      d.paused ? 'PAUSED' : d.running ? 'RUNNING' : 'IDLE';
    document.getElementById('run-id').textContent     = d.run_id || '—';
    document.getElementById('run-status').textContent =
      d.paused ? 'PAUSED' : d.running ? 'RUNNING' : 'IDLE';
    document.getElementById('run-status').style.color =
      d.paused ? '#ffa726' : d.running ? '#66bb6a' : '#78909c';

    document.getElementById('btn-start').disabled  = d.running && !d.paused;
    document.getElementById('btn-pause').disabled  = !d.running || d.paused;
    document.getElementById('btn-resume').disabled = !d.paused;

    for (const obj of ['Account','Contact','Opportunity']) {
      const s = d.objects[obj];
      const pct = s.total ? Math.round(s.migrated/s.total*100) : 0;
      document.getElementById('bar-'+obj).style.width = pct+'%';
      document.getElementById('pct-'+obj).textContent = pct+'%';
      document.getElementById('cnt-'+obj).textContent =
        s.migrated.toLocaleString()+' / '+s.total.toLocaleString()
        + (s.failed ? ` (${s.failed} err)` : '');
    }

    // agents
    const grid = document.getElementById('agent-grid');
    grid.innerHTML = '';
    for (const [name, status] of Object.entries(d.agents)) {
      const c = AGENT_COLORS[name] || '#78909c';
      const active = status === 'active';
      grid.innerHTML += `<div class="agent-card" style="--ac:${c}">
        <div class="agent-dot" style="${active?'animation:pulse 1.5s infinite':''}"></div>
        <span class="agent-name">${name}</span>
        <span class="agent-status">${status}</span>
      </div>`;
    }

    document.getElementById('db-status').innerHTML =
      'DB: <b>' + (d.db_connected ? 'PostgreSQL' : 'demo-mode') + '</b>';
    document.getElementById('last-refresh').textContent =
      'Last refresh: ' + new Date().toLocaleTimeString();
  } catch(e) { console.error('stats error', e); }
}

async function fetchEvents() {
  try {
    const r = await fetch('/api/events');
    const events = await r.json();
    const feed = document.getElementById('events-feed');
    feed.innerHTML = events.slice(0,30).map(e =>
      `<div class="event evt-${e.level}">
        <span class="evt-ts">${e.ts}</span>
        <span class="evt-agent">${e.agent}</span>
        <span class="evt-msg">${e.message}</span>
      </div>`
    ).join('');
  } catch(e) {}
}

async function ctrlRun(action) {
  try {
    await fetch('/api/run/'+action, {method:'POST'});
    await fetchStats();
    await fetchEvents();
  } catch(e) { alert('Error: '+e); }
}

// Initial load + auto-refresh every 3s
fetchStats(); fetchEvents();
setInterval(() => { fetchStats(); fetchEvents(); }, 3000);

// Pulse animation
const style = document.createElement('style');
style.textContent = '@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}';
document.head.appendChild(style);
</script>
</body>
</html>"""

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML

    return app


# ── Service runner ─────────────────────────────────────────────────────────────

def run_uvicorn(app, port: int, name: str):
    import uvicorn
    print(f"  {GREEN}▶{RESET}  {name} starting on port {port}…")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


def run_mock_sf(port: int = 9001):
    """Import and run the existing mock SF server."""
    print(f"  {GREEN}▶{RESET}  mock-salesforce starting on port {port}…")
    # Patch port in env so mock_sf_server picks it up
    os.environ["MOCK_SF_PORT"] = str(port)
    try:
        import uvicorn
        sys.path.insert(0, str(PROJECT_ROOT / "demo"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mock_sf_server", PROJECT_ROOT / "demo" / "mock_sf_server.py")
        mod = importlib.util.load_from_spec = spec.loader
        # Run directly
        subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "demo" / "mock_sf_server.py")],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "MOCK_SF_PORT": str(port)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"  {YELLOW}⚠{RESET}  mock-sf startup warning: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

processes = []

def shutdown(sig, frame):
    print(f"\n{YELLOW}Stopping all services…{RESET}")
    for p in processes:
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

if __name__ == "__main__":
    banner()

    migration_api_app = build_migration_api()
    dashboard_app     = build_dashboard()

    # Start Migration API (port 8000) in background thread
    t1 = threading.Thread(
        target=run_uvicorn,
        args=(migration_api_app, 8000, "migration-api"),
        daemon=True,
    )
    t1.start()

    # Start Mock SF server (port 9001) as subprocess
    mock_sf_proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "demo" / "mock_sf_server.py")],
        cwd=str(PROJECT_ROOT),
        env={**os.environ},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    processes.append(mock_sf_proc)
    print(f"  {GREEN}▶{RESET}  mock-salesforce started (pid {mock_sf_proc.pid})")

    # Wait briefly then open browser
    def _open_browser():
        time.sleep(2.5)
        url = "http://localhost:8080"
        print(f"\n  {CYAN}Opening browser → {url}{RESET}\n")
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    # Dashboard runs in main thread (blocks)
    print(f"  {GREEN}▶{RESET}  dashboard starting on port 8080…\n")
    import uvicorn
    uvicorn.run(dashboard_app, host="0.0.0.0", port=8080, log_level="warning")
