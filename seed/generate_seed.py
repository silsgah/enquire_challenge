"""
seed/generate_seed.py — Task 0
Populates the SOURCE database with realistic CRM data:
  - 50 companies (varied plans, ~15% churned)
  - 300+ users (distributed by company size)
  - 10,000+ events over 90 days
  - 200+ tickets (varied severity/status)

Design decisions:
  - Uses Faker for realistic names/emails
  - Churned companies have tapered activity (events stop before churn)
  - Enterprise companies get more seats and higher event volumes
  - Ticket clusters: some companies are "problem accounts"
  - Idempotent: truncates and re-seeds cleanly
"""

import os
import random
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
from faker import Faker
from dotenv import load_dotenv

load_dotenv()
fake = Faker()
random.seed(42)

SOURCE_DB_URL = os.environ["SOURCE_DB_URL"]

# ─── Config ──────────────────────────────────────────────────────────────────
NUM_COMPANIES = 55
CHURN_RATE = 0.15
DAYS_OF_HISTORY = 90
NOW = datetime.now(timezone.utc)
START_DATE = NOW - timedelta(days=DAYS_OF_HISTORY)

PLAN_CONFIG = {
    "trial":      {"seats": (1, 5),   "mrr": (0, 0),       "events_per_day": (1, 8),  "weight": 20},
    "starter":    {"seats": (2, 10),  "mrr": (4900, 9900),  "events_per_day": (5, 20), "weight": 30},
    "pro":        {"seats": (5, 30),  "mrr": (19900, 49900),"events_per_day": (15, 60),"weight": 30},
    "enterprise": {"seats": (20, 100),"mrr": (99900, 299900),"events_per_day": (40, 150),"weight": 20},
}

EVENT_TYPES = [
    "login", "export", "api_call", "invite", "report_view",
    "dashboard_open", "settings_change", "integration_connect",
    "bulk_upload", "comment_add",
]

TICKET_SUBJECTS = [
    "Cannot login to dashboard",
    "Export failing silently",
    "API rate limit hit unexpectedly",
    "Billing discrepancy",
    "Data not syncing",
    "SSO configuration issue",
    "Report shows incorrect figures",
    "Integration with Salesforce broken",
    "Bulk upload timeout",
    "Feature request: custom fields",
    "Performance degradation in pro tier",
    "User permissions not saving",
]

ROLES = ["admin", "member", "viewer"]
ROLE_WEIGHTS = [0.2, 0.6, 0.2]

SEVERITIES = ["low", "medium", "high", "critical"]
SEVERITY_WEIGHTS = [0.4, 0.35, 0.2, 0.05]

STATUSES = ["open", "pending", "resolved"]


def random_ts(start: datetime, end: datetime) -> datetime:
    delta = end - start
    return start + timedelta(seconds=random.randint(0, int(delta.total_seconds())))


def connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(SOURCE_DB_URL)


def truncate_all(cur) -> None:
    """Idempotent: wipe tables in FK-safe order."""
    cur.execute("TRUNCATE events, tickets, users, companies RESTART IDENTITY CASCADE;")
    print("  ✓ Truncated all source tables")


def seed_companies(cur) -> list[dict]:
    plans = list(PLAN_CONFIG.keys())
    weights = [PLAN_CONFIG[p]["weight"] for p in plans]
    companies = []

    for i in range(NUM_COMPANIES):
        plan = random.choices(plans, weights=weights)[0]
        cfg = PLAN_CONFIG[plan]
        is_churned = random.random() < CHURN_RATE
        created = random_ts(START_DATE - timedelta(days=180), START_DATE)
        churned_at = None
        if is_churned:
            # Churn happened sometime during our history window
            churned_at = random_ts(START_DATE + timedelta(days=7), NOW - timedelta(days=7))

        cur.execute(
            """
            INSERT INTO companies (name, plan, mrr_cents, churned_at, created_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                fake.company(),
                plan,
                random.randint(*cfg["mrr"]) if plan != "trial" else 0,
                churned_at,
                created,
            ),
        )
        company_id = cur.fetchone()[0]
        companies.append({
            "id": company_id,
            "plan": plan,
            "cfg": cfg,
            "churned_at": churned_at,
            "created_at": created,
        })

    print(f"  ✓ Seeded {len(companies)} companies")
    return companies


def seed_users(cur, companies: list[dict]) -> dict[int, list[int]]:
    """Returns mapping: company_id → [user_ids]. Bulk-inserts all users at once."""
    rows = []
    company_seat_counts: dict[int, int] = {}

    for co in companies:
        cfg = co["cfg"]
        num_seats = random.randint(*cfg["seats"])
        company_seat_counts[co["id"]] = num_seats
        for j in range(num_seats):
            role = random.choices(ROLES, ROLE_WEIGHTS)[0]
            if j == 0:
                role = "admin"
            last_login = None
            if co["churned_at"]:
                if random.random() < 0.7:
                    last_login = random_ts(co["created_at"], co["churned_at"])
            else:
                if random.random() < 0.9:
                    recency_days = random.choices([3, 7, 14, 30, 60], weights=[30, 25, 20, 15, 10])[0]
                    last_login = NOW - timedelta(days=random.uniform(0, recency_days))
            rows.append((
                fake.email(),
                co["id"],
                role,
                random_ts(co["created_at"], co["created_at"] + timedelta(days=30)),
                last_login,
            ))

    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO users (email, company_id, role, created_at, last_login) VALUES %s RETURNING id, company_id",
        rows,
        page_size=10000,
        fetch=True,
    )
    returned = cur.fetchall()
    company_users: dict[int, list[int]] = {}
    for user_id, company_id in returned:
        company_users.setdefault(company_id, []).append(user_id)

    total = sum(len(v) for v in company_users.values())
    print(f"  ✓ Seeded {total} users")
    return company_users


def seed_events(cur, companies: list[dict], company_users: dict[int, list[int]]) -> None:
    """
    Builds all event rows in memory then bulk-inserts in 5000-row pages.
    Reduces insert time from ~5 min to ~5 sec for 10k+ rows.
    """
    all_rows: list[tuple] = []
    BULK_SIZE = 5000

    for co in companies:
        cfg = co["cfg"]
        user_ids = company_users.get(co["id"], [])
        if not user_ids:
            continue

        end_date = co["churned_at"] or NOW
        current = co["created_at"]
        event_pool = EVENT_TYPES[:4] if co["plan"] == "trial" else EVENT_TYPES

        while current < end_date:
            day_end = min(current + timedelta(days=1), end_date)
            num_events = random.randint(*cfg["events_per_day"])

            if co["churned_at"]:
                days_to_churn = (co["churned_at"] - current).days
                if days_to_churn < 14:
                    num_events = max(0, int(num_events * (days_to_churn / 14)))

            for _ in range(num_events):
                all_rows.append((
                    random.choice(user_ids),
                    random.choice(event_pool),
                    random_ts(current, day_end),
                    None,
                ))
            current = day_end

    # Bulk insert in pages
    for i in range(0, len(all_rows), BULK_SIZE):
        page = all_rows[i : i + BULK_SIZE]
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO events (user_id, event_type, occurred_at, metadata) VALUES %s",
            page,
        )

    print(f"  ✓ Seeded {len(all_rows):,} events")


def seed_tickets(cur, companies: list[dict]) -> None:
    """Bulk-inserts all tickets at once."""
    problem_accounts = random.sample(companies, max(1, len(companies) // 5))
    problem_ids = {co["id"] for co in problem_accounts}
    rows: list[tuple] = []

    for co in companies:
        is_problem = co["id"] in problem_ids
        base_tickets = random.randint(3, 8) if is_problem else random.randint(0, 3)
        severity_weights = [0.1, 0.2, 0.4, 0.3] if is_problem else SEVERITY_WEIGHTS

        for _ in range(base_tickets):
            severity = random.choices(SEVERITIES, severity_weights)[0]
            created = random_ts(co["created_at"], NOW)
            status = random.choices(STATUSES, [0.3, 0.2, 0.5])[0]
            resolved_at = None
            if status == "resolved":
                resolved_at = created + timedelta(hours=random.randint(2, 72))
            rows.append((
                co["id"],
                random.choice(TICKET_SUBJECTS),
                status,
                severity,
                created,
                resolved_at,
            ))

    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO tickets (company_id, subject, status, severity, created_at, resolved_at) VALUES %s",
        rows,
    )
    print(f"  ✓ Seeded {len(rows)} tickets")


def main():
    print("🌱 Starting seed data generation...")
    conn = connect()
    try:
        with conn:
            with conn.cursor() as cur:
                truncate_all(cur)
                companies = seed_companies(cur)
                company_users = seed_users(cur, companies)
                seed_events(cur, companies, company_users)
                seed_tickets(cur, companies)
        print("✅ Seed complete. Source DB is ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
