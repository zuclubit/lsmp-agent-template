#!/usr/bin/env python3
"""
setup_salesforce_sandbox.py — Interactive Salesforce sandbox connection setup.

Guides the user through:
  1. Installing Salesforce CLI
  2. Authenticating to sandbox org
  3. Verifying the connection
  4. Updating .env with sandbox credentials
  5. Checking API limits

Usage:
    python3 scripts/setup_salesforce_sandbox.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; CYAN="\033[36m"; RESET="\033[0m"

def h(t):    print(f"\n{BOLD}{CYAN}{'─'*55}{RESET}\n{BOLD}{CYAN}  {t}{RESET}\n{BOLD}{CYAN}{'─'*55}{RESET}")
def ok(t):   print(f"  {GREEN}✓{RESET} {t}")
def warn(t): print(f"  {YELLOW}⚠{RESET} {t}")
def info(t): print(f"  · {t}")
def fail(t): print(f"  {RED}✗{RESET} {t}")
def ask(prompt, default=""):
    val = input(f"\n  {BOLD}{prompt}{RESET} [{default}]: ").strip()
    return val if val else default

def run(cmd, capture=True):
    r = subprocess.run(cmd, shell=True, capture_output=capture, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def main():
    print(f"\n{BOLD}{'━'*55}{RESET}")
    print(f"{BOLD}  LSMP — Salesforce Sandbox Setup{RESET}")
    print(f"{BOLD}{'━'*55}{RESET}")

    # ── 1. Check SFDX CLI ──────────────────────────────────────────────────
    h("Step 1 — Salesforce CLI")
    sf_cmd = None
    # Try common install locations in addition to PATH
    candidates = ["sf", "sfdx",
                  "/opt/homebrew/bin/sf", "/opt/homebrew/bin/sfdx",
                  "/usr/local/bin/sf",    "/usr/local/bin/sfdx"]
    for cmd in candidates:
        rc, out, err = run(f"{cmd} version 2>/dev/null")
        if rc == 0 and out:
            sf_cmd = cmd
            ver = out.split("\n")[0].strip()
            ok(f"Salesforce CLI found: {cmd} — {ver}")
            break
        # also try --json variant
        rc2, out2, _ = run(f"{cmd} version --json 2>/dev/null")
        if rc2 == 0 and out2:
            sf_cmd = cmd
            try:
                data = json.loads(out2)
                ver = data.get("sfdxCLIVersion") or data.get("version", "installed")
            except Exception:
                ver = "installed"
            ok(f"Salesforce CLI found: {cmd} ({ver})")
            break

    if not sf_cmd:
        warn("Salesforce CLI not installed.")
        print(f"\n  Install it with:")
        print(f"    {BOLD}npm install -g @salesforce/cli{RESET}")
        print(f"  Or via Homebrew:")
        print(f"    {BOLD}brew install sf{RESET}")
        print(f"\n  Then re-run this script.")
        sys.exit(1)

    # ── 2. Check existing auth ─────────────────────────────────────────────
    h("Step 2 — Org Authentication")
    rc, out, _ = run(f"{sf_cmd} org list --json 2>/dev/null")
    existing_orgs = []
    if rc == 0:
        try:
            data = json.loads(out)
            orgs = data.get("result", {})
            sandboxes = orgs.get("sandboxes", []) + orgs.get("scratchOrgs", [])
            non_scratch = orgs.get("nonScratchOrgs", [])
            existing_orgs = sandboxes + non_scratch
            if existing_orgs:
                print(f"\n  Authenticated orgs:")
                for org in existing_orgs[:5]:
                    alias = org.get("alias", "")
                    user  = org.get("username", "")
                    url   = org.get("instanceUrl", "")
                    print(f"    {GREEN}·{RESET} {alias or user} ({url})")
        except Exception:
            pass

    use_existing = False
    if existing_orgs:
        ans = ask("Use an existing authenticated org? (y/n)", "y")
        use_existing = ans.lower().startswith("y")

    if use_existing:
        alias = ask("Enter org alias", existing_orgs[0].get("alias","") if existing_orgs else "")
    else:
        print(f"\n  Opening browser to authenticate to Salesforce sandbox...")
        print(f"  (Use https://test.salesforce.com for sandbox environments)")
        alias = ask("Enter alias for this org", "lsmp-sandbox")
        instance_url = ask("Instance URL", "https://test.salesforce.com")
        rc, out, err = run(
            f"{sf_cmd} org login web --alias {alias} --instance-url {instance_url}",
            capture=False
        )
        if rc != 0:
            fail(f"Authentication failed: {err}")
            sys.exit(1)
        ok(f"Authenticated as {alias}")

    # ── 3. Verify connection ───────────────────────────────────────────────
    h("Step 3 — Verify Connection")
    rc, out, _ = run(f"{sf_cmd} org display --target-org {alias} --json")
    if rc != 0:
        fail(f"Cannot connect to org '{alias}'")
        sys.exit(1)

    try:
        data   = json.loads(out)
        result = data.get("result", {})
        username     = result.get("username", "")
        instance_url = result.get("instanceUrl", "")
        org_id       = result.get("id", "")[:18] if result.get("id") else ""
        ok(f"Username: {username}")
        ok(f"Instance: {instance_url}")
        ok(f"Org ID:   {org_id}")
    except Exception:
        fail("Could not parse org info")
        sys.exit(1)

    # ── 4. Update .env ─────────────────────────────────────────────────────
    h("Step 4 — Update .env")
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path) as fh:
            content = fh.read()

        # Update SF values
        updates = {
            "SF_INSTANCE_URL": instance_url,
            "SF_USERNAME":     username,
            "SF_ORG_ID":       org_id,
            "SF_MOCK_MODE":    "false",
        }
        for key, val in updates.items():
            import re
            if re.search(rf"^{key}=", content, re.MULTILINE):
                content = re.sub(rf"^{key}=.*$", f"{key}={val}", content, flags=re.MULTILINE)
            else:
                content += f"\n{key}={val}"

        with open(env_path, "w") as fh:
            fh.write(content)
        ok(f".env updated with sandbox credentials")
        warn("Still need to set SF_PASSWORD and SF_SECURITY_TOKEN manually in .env")
    else:
        warn(".env not found — run 'make setup' first")

    # ── 5. API limits ──────────────────────────────────────────────────────
    h("Step 5 — API Limits Check")
    rc, out, _ = run(f"{sf_cmd} limits api display --target-org {alias} --json 2>/dev/null")
    if rc == 0:
        try:
            data = json.loads(out)
            limits = data.get("result", [])
            key_limits = ["DailyApiRequests", "DailyBulkApiRequests", "DataStorageMB"]
            for lim in limits:
                if lim.get("name") in key_limits:
                    name = lim["name"]
                    used = lim.get("used", 0)
                    max_ = lim.get("max", 0)
                    pct  = int(used/max_*100) if max_ else 0
                    color = RED if pct > 80 else YELLOW if pct > 50 else GREEN
                    print(f"  {color}■{RESET} {name}: {used}/{max_} ({pct}% used)")
        except Exception:
            warn("Could not parse API limits")
    else:
        warn("Could not fetch API limits (may require additional permissions)")

    # ── Done ────────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{GREEN}{'━'*55}{RESET}")
    print(f"{BOLD}{GREEN}  ✓ Sandbox setup complete!{RESET}")
    print(f"{BOLD}{GREEN}{'━'*55}{RESET}")
    print(f"\n  Next steps:")
    print(f"    1. Set SF_PASSWORD and SF_SECURITY_TOKEN in .env")
    print(f"    2. Run: {BOLD}bash scripts/deploy_to_salesforce.sh {alias}{RESET}")
    print(f"    3. After deploy: {BOLD}make migrate{RESET}")
    print()

if __name__ == "__main__":
    main()
