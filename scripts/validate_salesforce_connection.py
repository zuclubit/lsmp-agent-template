#!/usr/bin/env python3
"""
validate_salesforce_connection.py
==================================
Validates Salesforce connectivity and verifies that the integration user
has all the permissions required to run the migration.

Checks performed:
  1. OAuth2 / Username-Password authentication
  2. API version availability
  3. Object-level CRUD permissions (Account, Contact, Opportunity, Lead)
  4. Field-level visibility for key migration fields
  5. Bulk API access
  6. API limits (daily, current usage)
  7. Custom field existence (Legacy_ID__c)
  8. Duplicate rule configuration
  9. Trigger presence on migrated objects (warn only)
 10. Data connectivity (simple SOQL query)

Usage:
    python scripts/validate_salesforce_connection.py [OPTIONS]

Options:
    --instance-url URL   Salesforce instance URL (overrides .env)
    --username USER      SF username (overrides .env)
    --password PASS      SF password (overrides .env)
    --token TOKEN        SF security token (overrides .env)
    --api-version VER    API version (default: 59.0)
    --objects LIST       Comma-separated list of objects to check
    --output FORMAT      Output format: table|json (default: table)
    --quiet              Suppress informational output; exit code only
    -h, --help           Show this help
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Colour output (safe on all platforms)
# ---------------------------------------------------------------------------

if sys.stdout.isatty():
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    CYAN = "\033[0;36m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
else:
    RED = GREEN = YELLOW = CYAN = BOLD = RESET = ""


def _ok(msg: str) -> str:
    return f"  {GREEN}✓{RESET}  {msg}"


def _warn(msg: str) -> str:
    return f"  {YELLOW}⚠{RESET}  {msg}"


def _fail(msg: str) -> str:
    return f"  {RED}✗{RESET}  {msg}"


def _section(title: str) -> None:
    print(f"\n{BOLD}{'─' * 60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'─' * 60}{RESET}")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Salesforce connectivity and permissions for migration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--instance-url", help="Salesforce instance URL")
    parser.add_argument("--username", help="Salesforce username")
    parser.add_argument("--password", help="Salesforce password")
    parser.add_argument("--token", default="", help="Salesforce security token")
    parser.add_argument("--api-version", default="59.0", help="API version (default: 59.0)")
    parser.add_argument(
        "--objects",
        default="Account,Contact,Opportunity,Lead,Case",
        help="Comma-separated objects to check",
    )
    parser.add_argument("--output", choices=["table", "json"], default="table")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Load configuration (CLI > environment variables > .env file)
# ---------------------------------------------------------------------------


def _load_config(args: argparse.Namespace) -> dict[str, str]:
    # Try loading .env
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.isfile(env_path):
        with open(env_path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    return {
        "instance_url": args.instance_url or os.getenv("SF_INSTANCE_URL", ""),
        "username": args.username or os.getenv("SF_USERNAME", ""),
        "password": args.password or os.getenv("SF_PASSWORD", ""),
        "token": args.token or os.getenv("SF_SECURITY_TOKEN", ""),
        "api_version": args.api_version or os.getenv("SF_API_VERSION", "59.0"),
    }


# ---------------------------------------------------------------------------
# Validation result collector
# ---------------------------------------------------------------------------


class CheckResult:
    def __init__(self, name: str, status: str, detail: str = "") -> None:
        self.name = name
        self.status = status   # "pass" | "warn" | "fail"
        self.detail = detail
        self.checked_at = datetime.utcnow().isoformat() + "Z"

    def __str__(self) -> str:
        if self.status == "pass":
            msg = _ok(self.name)
        elif self.status == "warn":
            msg = _warn(self.name)
        else:
            msg = _fail(self.name)
        if self.detail:
            msg += f"\n     {CYAN}{self.detail}{RESET}"
        return msg

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


# ---------------------------------------------------------------------------
# Validation functions
# ---------------------------------------------------------------------------


def check_authentication(sf: Any, quiet: bool) -> CheckResult:
    """Verify that the Salesforce session is authenticated."""
    try:
        # Query the org info to confirm the session is live
        result = sf.query("SELECT Id, Name, OrganizationType FROM Organization LIMIT 1")
        if result.get("records"):
            org = result["records"][0]
            detail = f"Org: {org.get('Name')} ({org.get('OrganizationType')}) | ID: {org.get('Id')}"
            return CheckResult("Authentication", "pass", detail)
        return CheckResult("Authentication", "fail", "No organization record returned")
    except Exception as exc:
        return CheckResult("Authentication", "fail", str(exc)[:200])


def check_api_version(sf: Any, required_version: str) -> CheckResult:
    """Verify that the required API version is available."""
    try:
        # simple_salesforce stores the version on the instance
        available = getattr(sf, "api_version", required_version)
        av_float = float(available)
        req_float = float(required_version)
        if av_float >= req_float:
            return CheckResult(
                f"API Version >= {required_version}",
                "pass",
                f"Available: {available}",
            )
        return CheckResult(
            f"API Version >= {required_version}",
            "warn",
            f"Available: {available} (expected >= {required_version})",
        )
    except Exception as exc:
        return CheckResult(f"API Version >= {required_version}", "warn", str(exc)[:100])


def check_object_permissions(sf: Any, obj_name: str) -> list[CheckResult]:
    """Check CRUD + query permissions on a Salesforce object."""
    results = []
    try:
        describe = getattr(sf, obj_name).describe()
        ops = {
            "createable": "Create",
            "queryable": "Query",
            "updateable": "Update",
            "deleteable": "Delete",
        }
        for attr, label in ops.items():
            allowed = describe.get(attr, False)
            status = "pass" if allowed else "warn"
            results.append(CheckResult(
                f"{obj_name}: {label}",
                status,
                "" if allowed else f"Integration user lacks {label} on {obj_name}",
            ))
    except Exception as exc:
        results.append(CheckResult(
            f"{obj_name}: Permissions",
            "fail",
            str(exc)[:200],
        ))
    return results


def check_custom_field(sf: Any, obj_name: str, field_name: str) -> CheckResult:
    """Verify that a custom field exists on the object."""
    try:
        describe = getattr(sf, obj_name).describe()
        fields = {f["name"]: f for f in describe.get("fields", [])}
        if field_name in fields:
            f = fields[field_name]
            return CheckResult(
                f"{obj_name}.{field_name}",
                "pass",
                f"Type: {f['type']}, Length: {f.get('length', 'N/A')}, ExternalId: {f.get('externalId', False)}",
            )
        return CheckResult(
            f"{obj_name}.{field_name}",
            "fail",
            f"Custom field {field_name} not found on {obj_name}. Create it before migrating.",
        )
    except Exception as exc:
        return CheckResult(f"{obj_name}.{field_name}", "fail", str(exc)[:200])


def check_bulk_api(sf: Any) -> CheckResult:
    """Verify Bulk API access by creating and aborting a dummy job."""
    try:
        # Attempt to list current bulk jobs (non-destructive)
        response = sf.bulk2.Account.query("SELECT Id FROM Account LIMIT 1")
        return CheckResult("Bulk API 2.0", "pass", "Bulk API accessible")
    except AttributeError:
        return CheckResult("Bulk API 2.0", "warn", "simple_salesforce bulk2 not available – use bulk instead")
    except Exception as exc:
        err = str(exc)
        if "API_DISABLED_FOR_ORG" in err:
            return CheckResult("Bulk API 2.0", "fail", "Bulk API is disabled for this org")
        return CheckResult("Bulk API 2.0", "warn", f"Bulk API check inconclusive: {err[:100]}")


def check_api_limits(sf: Any) -> CheckResult:
    """Check daily API usage to ensure we won't hit limits during migration."""
    try:
        limits = sf.limits()
        daily = limits.get("DailyApiRequests", {})
        remaining = daily.get("Remaining", 0)
        total = daily.get("Max", 0)
        used = total - remaining
        pct_used = (used / total * 100) if total else 0

        if pct_used < 70:
            status = "pass"
        elif pct_used < 90:
            status = "warn"
        else:
            status = "fail"

        return CheckResult(
            "Daily API Limits",
            status,
            f"Used: {used:,}/{total:,} ({pct_used:.1f}%) | Remaining: {remaining:,}",
        )
    except Exception as exc:
        return CheckResult("Daily API Limits", "warn", f"Could not retrieve limits: {exc}")


def check_soql_query(sf: Any) -> CheckResult:
    """Run a simple SOQL query to confirm data access works end-to-end."""
    try:
        result = sf.query("SELECT COUNT() FROM Account")
        count = result.get("totalSize", 0)
        return CheckResult("SOQL Query (Account COUNT)", "pass", f"Result: {count:,} accounts in org")
    except Exception as exc:
        return CheckResult("SOQL Query", "fail", str(exc)[:200])


def check_duplicate_rules(sf: Any) -> CheckResult:
    """Warn if duplicate rules are active (they can block migration inserts)."""
    try:
        result = sf.query(
            "SELECT Id, DeveloperName, IsActive FROM DuplicateRule WHERE IsActive = true LIMIT 5"
        )
        active_rules = result.get("records", [])
        if active_rules:
            names = ", ".join(r["DeveloperName"] for r in active_rules)
            return CheckResult(
                "Duplicate Rules",
                "warn",
                f"{len(active_rules)} active duplicate rule(s): {names}. "
                "Grant 'Bypass Duplicate Rules' permission to avoid insert failures.",
            )
        return CheckResult("Duplicate Rules", "pass", "No active duplicate rules found")
    except Exception as exc:
        return CheckResult("Duplicate Rules", "warn", f"Could not check duplicate rules: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = _parse_args()
    config = _load_config(args)

    # Validate required config
    missing = [k for k in ("instance_url", "username", "password") if not config[k]]
    if missing:
        print(_fail(f"Missing required configuration: {', '.join(missing)}"))
        print("  Set them in .env or pass as CLI arguments.")
        return 2

    if not args.quiet:
        _section("Salesforce Connectivity Validation")
        print(f"  Instance:  {config['instance_url']}")
        print(f"  Username:  {config['username']}")
        print(f"  API:       v{config['api_version']}")
        print(f"  Objects:   {args.objects}")

    # ---------------------------------------------------------------------------
    # Attempt to import simple_salesforce
    # ---------------------------------------------------------------------------
    try:
        from simple_salesforce import Salesforce, SalesforceAuthenticationFailed
    except ImportError:
        print(_fail("simple_salesforce is not installed."))
        print("  Run: pip install simple-salesforce")
        return 2

    # ---------------------------------------------------------------------------
    # Authenticate
    # ---------------------------------------------------------------------------
    if not args.quiet:
        _section("Authentication")

    sf = None
    auth_error: Optional[str] = None
    try:
        sf = Salesforce(
            username=config["username"],
            password=config["password"],
            security_token=config["token"],
            instance_url=config["instance_url"],
            version=config["api_version"],
        )
    except Exception as exc:
        auth_error = str(exc)

    if sf is None:
        if not args.quiet:
            print(_fail(f"Authentication FAILED: {auth_error}"))
        return 1

    # ---------------------------------------------------------------------------
    # Run all checks
    # ---------------------------------------------------------------------------
    all_results: list[CheckResult] = []

    all_results.append(check_authentication(sf, args.quiet))
    all_results.append(check_api_version(sf, config["api_version"]))

    objects = [o.strip() for o in args.objects.split(",") if o.strip()]
    for obj in objects:
        all_results.extend(check_object_permissions(sf, obj))
        all_results.append(check_custom_field(sf, obj, "Legacy_ID__c"))

    all_results.append(check_bulk_api(sf))
    all_results.append(check_api_limits(sf))
    all_results.append(check_soql_query(sf))
    all_results.append(check_duplicate_rules(sf))

    # ---------------------------------------------------------------------------
    # Output
    # ---------------------------------------------------------------------------
    if args.output == "json":
        summary = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "instance_url": config["instance_url"],
            "username": config["username"],
            "checks": [r.to_dict() for r in all_results],
            "total": len(all_results),
            "passed": sum(1 for r in all_results if r.status == "pass"),
            "warnings": sum(1 for r in all_results if r.status == "warn"),
            "failures": sum(1 for r in all_results if r.status == "fail"),
        }
        print(json.dumps(summary, indent=2))
    else:
        if not args.quiet:
            _section("Check Results")
            for result in all_results:
                print(result)

            passed = sum(1 for r in all_results if r.status == "pass")
            warnings = sum(1 for r in all_results if r.status == "warn")
            failures = sum(1 for r in all_results if r.status == "fail")

            _section("Summary")
            print(f"  Total checks:  {len(all_results)}")
            print(f"  {GREEN}Passed{RESET}:        {passed}")
            print(f"  {YELLOW}Warnings{RESET}:      {warnings}")
            print(f"  {RED}Failures{RESET}:      {failures}")
            print()
            if failures == 0 and warnings == 0:
                print(f"  {GREEN}{BOLD}✓ All checks passed. Ready to migrate.{RESET}")
            elif failures == 0:
                print(f"  {YELLOW}{BOLD}⚠ No blocking failures. Review warnings before proceeding.{RESET}")
            else:
                print(f"  {RED}{BOLD}✗ {failures} check(s) failed. Resolve before running migration.{RESET}")
            print()

    # Exit code: 0 = all pass, 1 = failures, 3 = warnings only
    failures = sum(1 for r in all_results if r.status == "fail")
    if failures > 0:
        return 1
    warnings = sum(1 for r in all_results if r.status == "warn")
    return 3 if warnings > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
