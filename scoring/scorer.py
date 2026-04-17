"""
scoring/scorer.py — Task 2: Customer Health Scoring

Computes a 0–100 health score per company using the SQL views defined in
health_score.sql, then stores snapshots in fact_health_score.

At-risk flag: companies whose score dropped 15+ points in the last 14 days.

Designed to run nightly (via APScheduler or cron).
"""

import json
import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SIDECAR_DB_URL = os.environ["SIDECAR_DB_URL"]


def connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(SIDECAR_DB_URL)


def apply_sql_views(cur) -> None:
    """Load and apply the SQL view definitions from health_score.sql."""
    sql_path = os.path.join(os.path.dirname(__file__), "health_score.sql")
    with open(sql_path) as f:
        sql = f.read()
    cur.execute(sql)
    log.info("SQL views applied.")


def compute_and_store_scores(cur) -> list[dict]:
    """
    Read health components from v_current_health view, store snapshots
    in fact_health_score, and return a list of score dicts.
    """
    cur.execute(
        """
        SELECT
            company_id, company_name, plan, churned_at,
            login_recency_score, adoption_score,
            ticket_score, mrr_tier_score, seat_score,
            total_score
        FROM v_current_health
        ORDER BY company_id
        """
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    results = []

    for row in rows:
        r = dict(zip(cols, row))

        # Churned companies get score 0
        if r["churned_at"] is not None:
            r["total_score"] = 0

        score = max(0, min(100, int(r["total_score"])))
        components = {
            "login_recency": int(r["login_recency_score"]),
            "adoption":      int(r["adoption_score"]),
            "ticket_health": int(r["ticket_score"]),
            "mrr_tier":      int(r["mrr_tier_score"]),
            "seat_util":     int(r["seat_score"]),
        }

        cur.execute(
            """
            INSERT INTO fact_health_score (company_id, score, at_risk, components, scored_at)
            VALUES (%s, %s, FALSE, %s, now())
            """,
            (r["company_id"], score, json.dumps(components)),
        )

        results.append({"company_id": r["company_id"], "score": score, "components": components})

    log.info("Stored health scores for %d companies.", len(results))
    return results


def update_at_risk_flags(cur) -> list[int]:
    """
    Use the v_score_trend view (which uses LAG window function) to find
    companies with 15+ point score drop in 14 days and mark them at_risk.
    Returns list of at-risk company IDs.
    """
    cur.execute(
        """
        SELECT company_id, current_score, prev_score, score_drop
        FROM v_score_trend
        WHERE at_risk = TRUE
        """
    )
    at_risk_rows = cur.fetchall()
    at_risk_ids = []

    for company_id, current_score, prev_score, score_drop in at_risk_rows:
        # Update the most recent score snapshot for this company
        cur.execute(
            """
            UPDATE fact_health_score
            SET at_risk = TRUE
            WHERE id = (
                SELECT id FROM fact_health_score
                WHERE company_id = %s
                ORDER BY scored_at DESC
                LIMIT 1
            )
            """,
            (company_id,),
        )
        at_risk_ids.append(company_id)
        log.warning(
            "  🔴 at_risk: company_id=%d  score %d→%d (drop=%d)",
            company_id, prev_score, current_score, score_drop,
        )

    if not at_risk_ids:
        log.info("  No at-risk companies detected.")
    else:
        log.info("  %d companies flagged at_risk.", len(at_risk_ids))

    return at_risk_ids


def run_scoring() -> dict:
    """Main entry point. Returns summary dict."""
    started_at = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info("Starting health scoring at %s", started_at.isoformat())
    log.info("=" * 60)

    conn = connect()
    try:
        with conn:
            with conn.cursor() as cur:
                apply_sql_views(cur)
                scores = compute_and_store_scores(cur)
                at_risk_ids = update_at_risk_flags(cur)
    finally:
        conn.close()

    finished_at = datetime.now(timezone.utc)
    summary = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "companies_scored": len(scores),
        "at_risk_count": len(at_risk_ids),
        "at_risk_ids": at_risk_ids,
        "scores": scores,
    }
    log.info("✅ Scoring complete. %d scored, %d at-risk.", len(scores), len(at_risk_ids))
    return summary


if __name__ == "__main__":
    run_scoring()
