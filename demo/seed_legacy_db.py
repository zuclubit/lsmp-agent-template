#!/usr/bin/env python3
"""
seed_legacy_db.py — Populate the legacy database with demo records.

Creates:
  - 50 Accounts  (mix of industries, sizes, statuses)
  - 100 Contacts (2 per Account)
  - 50  Opportunities (1 per Account)

All records use legacy Siebel-style IDs and field names,
ready for migration to Salesforce via the LSMP pipeline.

Usage:
    python3 demo/seed_legacy_db.py
    # or
    make seed
"""
from __future__ import annotations

import os
import sys
import random
import string
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_HOST = os.getenv("LEGACY_DB_HOST", "localhost")
DB_PORT = int(os.getenv("LEGACY_DB_PORT", "5432"))
DB_NAME = os.getenv("LEGACY_DB_NAME", "legacy_db")
DB_USER = os.getenv("LEGACY_DB_USER", "sfmigrationadmin")
DB_PASS = os.getenv("LEGACY_DB_PASSWORD", "Dev_P@ssw0rd_2024!")

ACCOUNT_COUNT   = int(os.getenv("DEMO_ACCOUNT_COUNT",   "50"))
CONTACT_COUNT   = int(os.getenv("DEMO_CONTACT_COUNT",  "100"))
OPPORTUNITY_COUNT = int(os.getenv("DEMO_OPPORTUNITY_COUNT", "50"))

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

INDUSTRIES = ["TECH", "HLTH", "FIN", "MFG", "RET", "EDU", "GOV", "ENRG", "CONS"]
ACCOUNT_TYPES = ["CUST", "PROSPECT", "PARTNER"]
ACCOUNT_STATUSES = ["A", "A", "A", "A", "I"]  # weighted toward Active

COMPANY_SUFFIXES = [
    "Inc.", "LLC", "Corp.", "Ltd.", "Group", "Solutions", "Technologies",
    "Systems", "Services", "Enterprises", "Associates", "Partners", "Co."
]
COMPANY_NAMES = [
    "Apex", "Vertex", "Nexus", "Atlas", "Titan", "Orion", "Stellar",
    "Pinnacle", "Summit", "Horizon", "Vanguard", "Meridian", "Nexis",
    "Prism", "Quantum", "Radiant", "Sapphire", "Onyx", "Cobalt", "Azure",
    "Pacific", "Atlantic", "Nordic", "Alpine", "Desert", "Coastal",
    "Harbor", "Bay Area", "Mountain", "Valley", "River", "Lake",
    "Global", "National", "Metro", "Central", "Premier", "Advanced",
    "Innovative", "Dynamic", "Synergy", "Catalyst", "Fusion", "Optima",
    "Nova", "Elara", "Kyros", "Zeno", "Helix", "Stratum"
]

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "William", "Barbara", "David", "Susan", "Richard", "Jessica",
    "Joseph", "Sarah", "Thomas", "Karen", "Charles", "Lisa", "Christopher",
    "Nancy", "Daniel", "Betty", "Matthew", "Margaret", "Anthony", "Sandra",
    "Mark", "Ashley", "Donald", "Kimberly", "Steven", "Emily", "Paul",
    "Donna", "Andrew", "Michelle", "Joshua", "Carol"
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores"
]
TITLES = [
    "CEO", "CTO", "CFO", "COO", "VP of Sales", "VP of Engineering",
    "Director of IT", "Director of Operations", "Senior Manager",
    "Account Manager", "Sales Director", "IT Manager", "Project Manager",
    "Business Analyst", "Data Engineer", "Solutions Architect",
    "Product Manager", "Operations Manager", "Finance Director"
]
DEPARTMENTS = [
    "Sales", "Engineering", "Finance", "Operations", "IT",
    "Marketing", "HR", "Legal", "Product", "Customer Success"
]
STATES = [
    "CA", "TX", "NY", "FL", "IL", "PA", "OH", "GA", "NC", "MI",
    "NJ", "VA", "WA", "AZ", "MA", "TN", "IN", "MO", "MD", "CO"
]
CITIES = {
    "CA": "San Francisco", "TX": "Austin", "NY": "New York", "FL": "Miami",
    "IL": "Chicago", "PA": "Philadelphia", "OH": "Columbus", "GA": "Atlanta",
    "NC": "Charlotte", "MI": "Detroit", "NJ": "Newark", "VA": "Richmond",
    "WA": "Seattle", "AZ": "Phoenix", "MA": "Boston", "TN": "Nashville",
    "IN": "Indianapolis", "MO": "St. Louis", "MD": "Baltimore", "CO": "Denver"
}
SF_STAGES = [
    "Prospecting", "Qualification", "Needs Analysis",
    "Value Proposition", "Id. Decision Makers",
    "Perception Analysis", "Proposal/Price Quote",
    "Negotiation/Review", "Closed Won", "Closed Lost"
]
STAGE_PROBABILITIES = {
    "Prospecting": 10, "Qualification": 20, "Needs Analysis": 30,
    "Value Proposition": 40, "Id. Decision Makers": 50,
    "Perception Analysis": 60, "Proposal/Price Quote": 70,
    "Negotiation/Review": 80, "Closed Won": 100, "Closed Lost": 0
}
LEAD_SOURCES = [
    "Web", "Referral", "Campaign", "Cold Call", "Trade Show",
    "Partner", "Customer Event", "Employee Referral", "Advertisement"
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def siebel_id(prefix: str, n: int) -> str:
    """Generate a Siebel-style row ID: e.g. LEGACY-ACC-00000001"""
    return f"LEGACY-{prefix}-{n:08d}"


def rand_phone() -> str:
    area = random.randint(200, 999)
    return f"+1{area}{random.randint(100,999)}{random.randint(1000,9999)}"


def rand_email(first: str, last: str, domain: str) -> str:
    return f"{first.lower()}.{last.lower()}@{domain.lower()}"


def rand_date_past(years: int = 5) -> datetime:
    days = random.randint(0, years * 365)
    return datetime.now(timezone.utc) - timedelta(days=days)


def rand_close_date() -> datetime:
    days = random.randint(-180, 180)
    return datetime.now(timezone.utc) + timedelta(days=days)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def get_connection():
    try:
        import psycopg2
    except ImportError:
        print("  Installing psycopg2-binary...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary", "--quiet"])
        import psycopg2

    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
        connect_timeout=10
    )
    conn.autocommit = False
    return conn


def ensure_schema(cur) -> None:
    """Create tables if they don't exist by running the schema SQL."""
    schema_path = os.path.join(
        os.path.dirname(__file__), "..", "infrastructure", "sql", "01_legacy_schema.sql"
    )
    if os.path.exists(schema_path):
        # Parse and run the SQL (skip \c directive which is psql-specific)
        with open(schema_path) as fh:
            sql = fh.read()
        # Skip psql meta-commands
        statements = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("\\")]
        for stmt in statements:
            if stmt:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass  # Table may already exist


def seed_accounts(cur, count: int) -> list[dict]:
    print(f"  Seeding {count} accounts...", end="", flush=True)
    accounts = []
    for i in range(1, count + 1):
        company = f"{random.choice(COMPANY_NAMES)} {random.choice(COMPANY_SUFFIXES)}"
        state = random.choice(STATES)
        domain = company.split()[0].lower().replace(".", "") + ".com"
        acct = {
            "acct_id":          siebel_id("ACC", i),
            "acct_name":        company,
            "acct_type":        random.choice(ACCOUNT_TYPES),
            "acct_status":      random.choice(ACCOUNT_STATUSES),
            "industry_code":    random.choice(INDUSTRIES),
            "annual_revenue":   round(random.uniform(500_000, 500_000_000), 2),
            "employee_count":   random.randint(10, 50_000),
            "phone_number":     rand_phone(),
            "website":          f"https://www.{domain}",
            "bill_addr_line1":  f"{random.randint(1,9999)} {random.choice(['Main','Oak','Elm','Maple','Corporate','Innovation'])} {random.choice(['St.','Ave.','Blvd.','Rd.','Dr.'])}",
            "bill_city":        CITIES[state],
            "bill_state":       state,
            "bill_postal_code": f"{random.randint(10000,99999)}",
            "bill_country":     "United States",
            "migration_status": "PENDING",
            "created_ts":       rand_date_past(7),
            "modified_ts":      rand_date_past(2),
        }
        accounts.append(acct)
        cur.execute("""
            INSERT INTO siebel_accounts
                (acct_id, acct_name, acct_type, acct_status, industry_code,
                 annual_revenue, employee_count, phone_number, website,
                 bill_addr_line1, bill_city, bill_state, bill_postal_code,
                 bill_country, migration_status, created_ts, modified_ts)
            VALUES
                (%(acct_id)s, %(acct_name)s, %(acct_type)s, %(acct_status)s,
                 %(industry_code)s, %(annual_revenue)s, %(employee_count)s,
                 %(phone_number)s, %(website)s, %(bill_addr_line1)s,
                 %(bill_city)s, %(bill_state)s, %(bill_postal_code)s,
                 %(bill_country)s, %(migration_status)s, %(created_ts)s, %(modified_ts)s)
            ON CONFLICT (acct_id) DO NOTHING
        """, acct)
        if i % 10 == 0:
            print(".", end="", flush=True)
    print(f" {count} accounts done")
    return accounts


def seed_contacts(cur, accounts: list[dict], count: int) -> list[dict]:
    print(f"  Seeding {count} contacts...", end="", flush=True)
    contacts = []
    for i in range(1, count + 1):
        acct = accounts[(i - 1) % len(accounts)]
        first = random.choice(FIRST_NAMES)
        last  = random.choice(LAST_NAMES)
        domain = acct["website"].replace("https://www.", "") if acct.get("website") else "example.com"
        contact = {
            "contact_id":       siebel_id("CON", i),
            "first_name":       first,
            "last_name":        last,
            "acct_id":          acct["acct_id"],
            "contact_status":   "A",
            "title":            random.choice(TITLES),
            "department":       random.choice(DEPARTMENTS),
            "email_primary":    rand_email(first, last, domain),
            "phone_work":       rand_phone(),
            "phone_mobile":     rand_phone() if random.random() > 0.4 else None,
            "email_opt_out":    random.random() < 0.1,
            "do_not_call":      random.random() < 0.05,
            "migration_status": "PENDING",
            "created_ts":       rand_date_past(7),
            "modified_ts":      rand_date_past(2),
        }
        contacts.append(contact)
        cur.execute("""
            INSERT INTO siebel_contacts
                (contact_id, first_name, last_name, acct_id, contact_status,
                 title, department, email_primary, phone_work, phone_mobile,
                 email_opt_out, do_not_call, migration_status, created_ts, modified_ts)
            VALUES
                (%(contact_id)s, %(first_name)s, %(last_name)s, %(acct_id)s,
                 %(contact_status)s, %(title)s, %(department)s, %(email_primary)s,
                 %(phone_work)s, %(phone_mobile)s, %(email_opt_out)s, %(do_not_call)s,
                 %(migration_status)s, %(created_ts)s, %(modified_ts)s)
            ON CONFLICT (contact_id) DO NOTHING
        """, contact)
        if i % 20 == 0:
            print(".", end="", flush=True)
    print(f" {count} contacts done")
    return contacts


def seed_opportunities(cur, accounts: list[dict], contacts: list[dict], count: int) -> None:
    print(f"  Seeding {count} opportunities...", end="", flush=True)
    for i in range(1, count + 1):
        acct     = accounts[i - 1]
        contact  = contacts[i - 1] if i <= len(contacts) else contacts[0]
        stage    = random.choice(SF_STAGES)
        amount   = round(random.uniform(5_000, 2_000_000), 2)
        cur.execute("""
            INSERT INTO siebel_opportunities
                (opty_id, opty_name, acct_id, primary_contact_id,
                 sales_stage, opty_status, close_date, probability,
                 amount, currency_code, opportunity_type, lead_source,
                 migration_status, created_ts, modified_ts)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (opty_id) DO NOTHING
        """, (
            siebel_id("OPP", i),
            f"{acct['acct_name']} — {random.choice(['Enterprise Contract','Renewal','Expansion','New Logo','Phase 2'])}",
            acct["acct_id"],
            contact["contact_id"],
            stage,
            "C" if stage.startswith("Closed") else "A",
            rand_close_date().date(),
            STAGE_PROBABILITIES.get(stage, 50),
            amount,
            "USD",
            random.choice(["New Business", "Renewal", "Upsell", "Cross-sell"]),
            random.choice(LEAD_SOURCES),
            "PENDING",
            rand_date_past(7),
            rand_date_past(2),
        ))
        if i % 10 == 0:
            print(".", end="", flush=True)
    print(f" {count} opportunities done")


def print_summary(cur) -> None:
    cur.execute("SELECT COUNT(*) FROM siebel_accounts WHERE migration_status = 'PENDING'")
    acct_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM siebel_contacts WHERE migration_status = 'PENDING'")
    con_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM siebel_opportunities WHERE migration_status = 'PENDING'")
    opp_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM migration_field_map")
    map_count = cur.fetchone()[0]

    print()
    print("  +------------------------------------------+")
    print("  |        Legacy Database Summary            |")
    print("  +------------------------------------------+")
    print(f"  |  Accounts     (PENDING migration): {acct_count:>5}  |")
    print(f"  |  Contacts     (PENDING migration): {con_count:>5}  |")
    print(f"  |  Opportunities(PENDING migration): {opp_count:>5}  |")
    print(f"  |  Field mappings defined:           {map_count:>5}  |")
    print("  +------------------------------------------+")
    print()
    print("  Ready for migration pipeline.")
    print("  Run: make demo   (dry-run) or  make migrate  (live)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Load .env if present
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    if key.strip() not in os.environ:
                        os.environ[key.strip()] = val.strip().strip('"').strip("'")

    print()
    print("--- LSMP --- Legacy Database Seeder ---")
    print(f"  Target: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print()

    try:
        conn = get_connection()
    except Exception as exc:
        print(f"  Cannot connect to database: {exc}")
        print()
        print("  Ensure Docker services are running:")
        print("    make start")
        print()
        sys.exit(1)

    cur = conn.cursor()

    # Create schema
    print("  Creating schema...")
    ensure_schema(cur)
    conn.commit()
    print("  Schema ready")
    print()

    random.seed(42)  # deterministic demo data

    try:
        accounts    = seed_accounts(cur, ACCOUNT_COUNT)
        contacts    = seed_contacts(cur, accounts, CONTACT_COUNT)
        seed_opportunities(cur, accounts, contacts, OPPORTUNITY_COUNT)
        conn.commit()
        print()
        print("  Seed complete")
        print_summary(cur)
    except Exception as exc:
        conn.rollback()
        print(f"\n  Seed failed: {exc}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
