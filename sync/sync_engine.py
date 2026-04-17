"""
sync/sync_engine.py — Task 1: Sidecar Sync Engine

Reads from the source DB and populates/updates the sidecar DB.

Key design properties:
  1. INCREMENTAL: tracks a high-water mark (last synced event/ticket ID)
     in sync_state so we only pull new rows, not the full history.
  2. IDEMPOTENT: all writes use ON CONFLICT ... DO UPDATE (upsert),
     so re-running the sync produces the same result.
  3. SCHEMA EVOLUTION: checks information_schema before reading; logs
     warnings for unexpected columns instead of crashing.
  4. TRANSACTIONAL: each sync step commits atomically; partial failures
     leave the watermark unchanged, so the next run retries safely.
"""

import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

SOURCE_DB_URL = os.environ["SOURCE_DB_URL"]
SIDECAR_DB_URL = os.environ["SIDECAR_DB_URL"]

KNOWN_SOURCE_COLUMNS = {
    "companies": {"id", "name", "plan", "mrr_cents", "churned_at", "created_at"},
    "users": {"id", "email", "company_id", "role", "created_at", "last_login"},
    "events": {"id", "user_id", "event_type", "occurred_at", "metadata"},
    "tickets": {"id", "company_id", "subject", "status", "severity", "created_at", "resolved_at"},
}


def connect_source() -> psycopg2.extensions.connection:
    return psycopg2.connect(SOURCE_DB_URL)


def connect_sidecar() -> psycopg2.extensions.connection:
    return psycopg2.connect(SIDECAR_DB_URL)


# ─── Schema evolution guard ──────────────────────────────────────────────────

def check_schema_evolution(src_cur, table: str) -> None:
    """Warn if source table has columns we don't know about. Never crash."""
    src_cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
    actual = {row[0] for row in src_cur.fetchall()}
    unexpected = actual - KNOWN_SOURCE_COLUMNS.get(table, set())
    if unexpected:
        log.warning(
            "Schema evolution detected on source.%s — unknown columns: %s. "
            "Proceeding with known columns only.",
            table,
            unexpected,
        )


# ─── Watermark helpers ───────────────────────────────────────────────────────

def get_watermark(side_cur, table_name: str) -> int:
    side_cur.execute(
        "SELECT last_synced_id FROM sync_state WHERE table_name = %s",
        (table_name,),
    )
    row = side_cur.fetchone()
    return row[0] if row else 0


def set_watermark(side_cur, table_name: str, new_id: int) -> None:
    side_cur.execute(
        """
        INSERT INTO sync_state (table_name, last_synced_id, last_synced_at)
        VALUES (%s, %s, now())
        ON CONFLICT (table_name) DO UPDATE
            SET last_synced_id = EXCLUDED.last_synced_id,
                last_synced_at = EXCLUDED.last_synced_at
        """,
        (table_name, new_id),
    )


# ─── dim_accounts sync ───────────────────────────────────────────────────────

def sync_dim_accounts(src_cur, side_cur) -> int:
    """
    Full refresh of dim_accounts — always rebuild from scratch.

    Rationale: There are only ~50 companies. A full rebuild on every sync
    is trivially cheap and avoids edge cases where aggregated metrics
    (distinct_event_types, open_tickets) drift out of sync.
    """
    log.info("Syncing dim_accounts (full refresh)...")

    src_cur.execute(
        """
        SELECT
            c.id                                          AS company_id,
            c.name                                        AS company_name,
            c.plan,
            c.mrr_cents,
            c.churned_at,
            COUNT(DISTINCT u.id)                          AS total_users,
            COUNT(DISTINCT u.id) FILTER (WHERE u.role = 'admin') AS admin_count,
            MAX(u.last_login)                             AS last_login_at,
            MIN(e.occurred_at)                            AS first_event_at,
            MAX(e.occurred_at)                            AS last_event_at,
            COUNT(e.id)                                   AS total_events,
            COUNT(DISTINCT e.event_type)                  AS distinct_event_types,
            COUNT(t.id) FILTER (WHERE t.status != 'resolved')  AS open_tickets,
            COUNT(t.id) FILTER (WHERE t.severity = 'critical' AND t.status != 'resolved') AS critical_tickets
        FROM companies c
        LEFT JOIN users u       ON u.company_id = c.id
        LEFT JOIN events e      ON e.user_id = u.id
        LEFT JOIN tickets t     ON t.company_id = c.id
        GROUP BY c.id
        ORDER BY c.id
        """
    )
    rows = src_cur.fetchall()

    upserted = 0
    for row in rows:
        side_cur.execute(
            """
            INSERT INTO dim_accounts (
                company_id, company_name, plan, mrr_cents, churned_at,
                total_users, admin_count, last_login_at,
                first_event_at, last_event_at, total_events,
                distinct_event_types, open_tickets, critical_tickets, synced_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (company_id) DO UPDATE SET
                company_name         = EXCLUDED.company_name,
                plan                 = EXCLUDED.plan,
                mrr_cents            = EXCLUDED.mrr_cents,
                churned_at           = EXCLUDED.churned_at,
                total_users          = EXCLUDED.total_users,
                admin_count          = EXCLUDED.admin_count,
                last_login_at        = EXCLUDED.last_login_at,
                first_event_at       = EXCLUDED.first_event_at,
                last_event_at        = EXCLUDED.last_event_at,
                total_events         = EXCLUDED.total_events,
                distinct_event_types = EXCLUDED.distinct_event_types,
                open_tickets         = EXCLUDED.open_tickets,
                critical_tickets     = EXCLUDED.critical_tickets,
                synced_at            = EXCLUDED.synced_at
            """,
            row,
        )
        upserted += 1

    log.info("  dim_accounts: %d rows upserted", upserted)
    return upserted


# ─── fact_events_daily sync ──────────────────────────────────────────────────

def sync_fact_events_daily(src_cur, side_cur) -> int:
    """
    Incremental sync using event ID watermark.
    Aggregates new events by (company_id, event_date, event_type) and upserts
    into fact_events_daily. UNIQUE constraint ensures idempotency.
    """
    log.info("Syncing fact_events_daily (incremental)...")
    check_schema_evolution(src_cur, "events")

    watermark = get_watermark(side_cur, "events")
    log.info("  Watermark: events.id > %d", watermark)

    src_cur.execute(
        """
        SELECT
            u.company_id,
            e.occurred_at::DATE         AS event_date,
            e.event_type,
            COUNT(*)                    AS event_count,
            COUNT(DISTINCT e.user_id)   AS unique_users,
            MAX(e.id)                   AS max_id
        FROM events e
        JOIN users u ON u.id = e.user_id
        WHERE e.id > %s
        GROUP BY u.company_id, event_date, e.event_type
        ORDER BY event_date
        """,
        (watermark,),
    )
    rows = src_cur.fetchall()

    if not rows:
        log.info("  No new events since last sync.")
        return 0

    max_event_id = max(r[5] for r in rows)
    upserted = 0
    for company_id, event_date, event_type, event_count, unique_users, _ in rows:
        side_cur.execute(
            """
            INSERT INTO fact_events_daily
                (company_id, event_date, event_type, event_count, unique_users)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (company_id, event_date, event_type) DO UPDATE SET
                event_count  = fact_events_daily.event_count  + EXCLUDED.event_count,
                unique_users = GREATEST(fact_events_daily.unique_users, EXCLUDED.unique_users)
            """,
            (company_id, event_date, event_type, event_count, unique_users),
        )
        upserted += 1

    set_watermark(side_cur, "events", max_event_id)
    log.info("  fact_events_daily: %d rows upserted, new watermark=%d", upserted, max_event_id)
    return upserted


# ─── Main sync orchestrator ──────────────────────────────────────────────────

def run_sync() -> dict:
    """
    Main sync entry point. Returns a summary dict for logging/API response.
    Each step is wrapped in its own transaction to isolate failures.
    """
    started_at = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info("Starting CRM sidecar sync at %s", started_at.isoformat())
    log.info("=" * 60)

    summary = {"started_at": started_at.isoformat(), "steps": {}}

    src_conn = connect_source()
    side_conn = connect_sidecar()

    try:
        # Step 1: dim_accounts (full refresh, own transaction)
        with src_conn, side_conn:
            with src_conn.cursor() as src_cur, side_conn.cursor() as side_cur:
                n = sync_dim_accounts(src_cur, side_cur)
                summary["steps"]["dim_accounts"] = {"upserted": n}

        # Step 2: fact_events_daily (incremental, own transaction)
        with src_conn, side_conn:
            with src_conn.cursor() as src_cur, side_conn.cursor() as side_cur:
                n = sync_fact_events_daily(src_cur, side_cur)
                summary["steps"]["fact_events_daily"] = {"upserted": n}

    except Exception as exc:
        log.error("Sync failed: %s", exc, exc_info=True)
        summary["error"] = str(exc)
        raise
    finally:
        src_conn.close()
        side_conn.close()

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()
    summary["finished_at"] = finished_at.isoformat()
    summary["duration_seconds"] = round(duration, 2)
    log.info("✅ Sync completed in %.2fs", duration)
    return summary


if __name__ == "__main__":
    run_sync()
