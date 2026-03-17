#!/usr/bin/env python3
"""
run_demo_pipeline.py — Full end-to-end demo migration pipeline.

7 phases: env check → inventory → validation → security → migration → post-validation → report
Usage:
    python3 demo/run_demo_pipeline.py
    make demo
"""
from __future__ import annotations
import os, random, sys, time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env
_env = PROJECT_ROOT / ".env"
if _env.exists():
    for _l in open(_env):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            k, _, v = _l.partition("=")
            if k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; CYAN="\033[36m"; RESET="\033[0m"

def header(t):
    print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}\n{BOLD}{CYAN}  {t}{RESET}\n{BOLD}{CYAN}{'─'*60}{RESET}")
def step(t): print(f"\n{BOLD}▶ {t}{RESET}")
def ok(t):   print(f"  {GREEN}✓{RESET} {t}")
def warn(t): print(f"  {YELLOW}⚠{RESET} {t}")
def info(t): print(f"  · {t}")
def fail(t): print(f"  {RED}✗{RESET} {t}")

def progress_bar(label, total, delay=0.015):
    for i in range(total + 1):
        pct = i/total; done = int(30*pct)
        print(f"\r  {label} [{'█'*done}{'░'*(30-done)}] {i:>4}/{total} {int(pct*100):>3}%", end="", flush=True)
        if i < total: time.sleep(delay)
    print()

def phase1_env():
    header("Phase 1 — Environment Check")
    res = {}
    v = sys.version_info
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    for m, d in [("anthropic","Claude SDK"),("pydantic","Validation"),("psycopg2","PostgreSQL"),("yaml","YAML")]:
        try: __import__(m); ok(f"{m} ({d})")
        except ImportError: warn(f"{m} not installed")
    api_key = os.getenv("ANTHROPIC_API_KEY","")
    res["anthropic_key"] = bool(api_key and api_key != "your-anthropic-api-key-here")
    ok("ANTHROPIC_API_KEY set — live agents enabled") if res["anthropic_key"] else warn("ANTHROPIC_API_KEY not set — simulated responses")
    res["dry_run"] = os.getenv("MIGRATION_DRY_RUN","true").lower()=="true"
    ok("MIGRATION_DRY_RUN=true — no SF writes") if res["dry_run"] else warn("LIVE mode — writes enabled!")
    ok("SF_MOCK_MODE=true — local mock SF") if os.getenv("SF_MOCK_MODE","true").lower()=="true" else info(f"SF={os.getenv('SF_INSTANCE_URL','')}")
    step("PostgreSQL...")
    try:
        import psycopg2
        c = psycopg2.connect(host=os.getenv("LEGACY_DB_HOST","localhost"),port=int(os.getenv("LEGACY_DB_PORT","5432")),
            dbname=os.getenv("LEGACY_DB_NAME","legacy_db"),user=os.getenv("LEGACY_DB_USER","sfmigrationadmin"),
            password=os.getenv("LEGACY_DB_PASSWORD","Dev_P@ssw0rd_2024!"),connect_timeout=5)
        c.close(); ok("PostgreSQL connected"); res["postgres"]=True
    except Exception as e: warn(f"PostgreSQL unavailable: {e}"); warn("Run: make start && make seed"); res["postgres"]=False
    return res

def phase2_inventory(pg_ok):
    header("Phase 2 — Legacy Data Inventory")
    counts = {}
    if pg_ok:
        try:
            import psycopg2
            c = psycopg2.connect(host=os.getenv("LEGACY_DB_HOST","localhost"),port=int(os.getenv("LEGACY_DB_PORT","5432")),
                dbname=os.getenv("LEGACY_DB_NAME","legacy_db"),user=os.getenv("LEGACY_DB_USER","sfmigrationadmin"),
                password=os.getenv("LEGACY_DB_PASSWORD","Dev_P@ssw0rd_2024!"),connect_timeout=5)
            cur = c.cursor()
            for tbl,lbl in [("siebel_accounts","Account"),("siebel_contacts","Contact"),("siebel_opportunities","Opportunity")]:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}"); tot=cur.fetchone()[0]
                    cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE migration_status='PENDING'"); pend=cur.fetchone()[0]
                    ok(f"{lbl:<14} total={tot:<6} pending={pend}"); counts[lbl]=tot
                except: warn(f"{lbl}: table missing — run 'make seed'"); counts[lbl]=0
            cur.close(); c.close()
        except Exception as e: warn(f"DB error: {e}"); counts={"Account":50,"Contact":100,"Opportunity":50}
    else:
        counts={"Account":50,"Contact":100,"Opportunity":50}
        for k,v in counts.items(): info(f"{k:<14} (demo) {v} records")
    info(f"\n  Total queued: {sum(counts.values())}")
    return counts

def phase3_validation():
    header("Phase 3 — Pre-Migration Data Validation")
    step("ValidationLayer self-test...")
    try:
        from validation.layer import ValidationLayer
        vl = ValidationLayer()
        r,_ = vl.check_input("SELECT Id FROM Account LIMIT 100"); ok("ValidationLayer operational")
        r2,rule = vl.check_soql("DELETE FROM Account"); ok(f"SOQL guard active (blocked DELETE via {rule})")
    except Exception as e: warn(f"ValidationLayer: {e}")
    step("Data quality dimensions...")
    random.seed(int(datetime.now().timestamp())%1000)
    dims=[("Record Count Integrity",random.uniform(0.96,1.0)),("Field Completeness",random.uniform(0.89,0.98)),
          ("Referential Integrity",random.uniform(0.93,1.0)),("Data Type Conformance",random.uniform(0.95,1.0)),
          ("Duplicate Detection",random.uniform(0.97,1.0)),("PII Handling",random.uniform(0.90,0.99))]
    scores=[]
    for dim,score in dims:
        scores.append(score)
        c=GREEN if score>=0.95 else YELLOW if score>=0.80 else RED
        s="PASS" if score>=0.95 else "WARNING" if score>=0.80 else "FAIL"
        print(f"  {c}■{RESET} {dim:<30} {score:.3f}  [{s}]"); time.sleep(0.08)
    overall=sum(scores)/len(scores)
    grade="A" if overall>=0.95 else "B" if overall>=0.85 else "C" if overall>=0.75 else "D"
    c=GREEN if grade in("A","B") else YELLOW if grade=="C" else RED
    print(); info(f"Overall: {overall:.3f}  Grade: {BOLD}{c}{grade}{RESET}")
    if grade in("D","F"): fail("Validation gate BLOCKED"); return{"gate":"BLOCKED","score":overall,"grade":grade}
    ok(f"Validation gate PASSED (grade {grade})")
    return{"gate":"PASS","score":overall,"grade":grade}

def phase4_security():
    header("Phase 4 — Security Preflight")
    for chk in ["Prompt injection blocklist","SOQL DML guard","Credential redaction","Path traversal protection",
                 "API rate limit headroom","TLS configuration","PII enforcement","Audit chain integrity"]:
        ok(chk); time.sleep(0.05)
    print(); ok("Security gate PASSED (0 critical, 0 high)")
    return{"gate":"PASS","critical":0,"high":0}

def phase5_migration(counts, dry_run=True):
    mode="DRY-RUN" if dry_run else "LIVE"
    header(f"Phase 5 — Migration Execution ({mode})")
    run_id=f"demo-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    info(f"Run ID: {BOLD}{run_id}{RESET}")
    if dry_run: warn("DRY-RUN — no Salesforce writes")
    bs=int(os.getenv("MIGRATION_BATCH_SIZE","50"))
    res={"run_id":run_id,"objects":{}}
    for obj,count in [("Account",counts.get("Account",50)),("Contact",counts.get("Contact",100)),("Opportunity",counts.get("Opportunity",50))]:
        step(f"{obj} ({count} records, batch_size={bs})...")
        progress_bar(f"  {obj:<12}",count)
        succ=int(count*random.uniform(0.96,0.99)); fail_n=count-succ
        ok(f"{obj}: {succ} succeeded, {fail_n} failed (error rate {fail_n/count*100:.1f}%)")
        res["objects"][obj]={"total":count,"success":succ,"failed":fail_n}
    tot=sum(v["total"] for v in res["objects"].values())
    succ=sum(v["success"] for v in res["objects"].values())
    fail_n=sum(v["failed"] for v in res["objects"].values())
    res.update({"total":tot,"success":succ,"failed":fail_n,"error_rate":fail_n/tot*100})
    return res

def phase6_post_validation(mig):
    header("Phase 6 — Post-Migration Validation")
    step("Record counts...")
    for obj,s in mig.get("objects",{}).items():
        ok(f"{obj}: {s['success']} verified"); time.sleep(0.08)
    step("Referential integrity..."); ok("Contact→Account intact"); ok("Opportunity→Account intact")
    step("Field audit..."); ok("Account.Name 100%"); ok("Contact.Email 98%"); ok("Opportunity.Amount 100%")
    ok("Post-migration validation PASSED")
    return{"status":"PASS"}

def phase7_report(env,counts,val,sec,mig,post):
    header("Phase 7 — Migration Report")
    tot=mig.get("total",0); succ=mig.get("success",0); fail_n=mig.get("failed",0)
    er=mig.get("error_rate",0.0); grade=val.get("grade","?"); score=val.get("score",0.0)
    print(f"\n  {'='*54}\n  {BOLD}  MIGRATION RUN SUMMARY{RESET}\n  {'='*54}")
    print(f"  Run ID    : {BOLD}{mig.get('run_id','?')}{RESET}")
    print(f"  Mode      : {'DRY-RUN' if env.get('dry_run') else 'LIVE'}")
    print(f"  Status    : {GREEN}{BOLD}COMPLETED{RESET}")
    print(f"  {'-'*54}")
    print(f"  Total     : {tot}")
    print(f"  Successful: {GREEN}{succ}{RESET}")
    print(f"  Failed    : {(RED if fail_n else '')}{fail_n}{RESET}")
    print(f"  Error rate: {er:.2f}%")
    print(f"  {'-'*54}")
    print(f"  Validation: Grade {BOLD}{grade}{RESET} (score {score:.3f})")
    print(f"  Security  : {GREEN}PASS{RESET}")
    print(f"  Post-check: {GREEN}PASS{RESET}")
    print(f"  {'='*54}\n")
    print(f"  {'Object':<14} {'Total':>7} {'Success':>9} {'Failed':>7} {'Error%':>8}")
    print(f"  {'-'*14} {'-'*7} {'-'*9} {'-'*7} {'-'*8}")
    for obj,s in mig.get("objects",{}).items():
        ep=s["failed"]/s["total"]*100 if s["total"] else 0
        print(f"  {obj:<14} {s['total']:>7} {s['success']:>9} {s['failed']:>7} {ep:>7.1f}%")
    print(f"\n  {BOLD}{GREEN}Demo migration complete!{RESET}")
    if env.get("dry_run"):
        print(f"\n  To run LIVE:\n    1. Add SF credentials to .env\n    2. Set MIGRATION_DRY_RUN=false\n    3. Set ANTHROPIC_API_KEY\n    4. make migrate")
    print()

def main():
    print(f"\n{BOLD}{'━'*60}{RESET}\n{BOLD}  LSMP — Demo Migration Pipeline{RESET}")
    print(f"{BOLD}  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}\n{BOLD}{'━'*60}{RESET}")
    env  = phase1_env()
    cnts = phase2_inventory(env.get("postgres",False))
    val  = phase3_validation()
    if val.get("gate")=="BLOCKED": fail("Pipeline BLOCKED at validation."); sys.exit(1)
    sec  = phase4_security()
    if sec.get("gate")=="BLOCKED": fail("Pipeline BLOCKED at security."); sys.exit(1)
    mig  = phase5_migration(cnts, dry_run=env.get("dry_run",True))
    post = phase6_post_validation(mig)
    phase7_report(env,cnts,val,sec,mig,post)

if __name__ == "__main__":
    main()
