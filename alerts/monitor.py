"""
alerts/monitor.py — Task 4: Alert System

Scheduled job that scans for at-risk accounts and logs alerts.
Also supports:
  - Slack webhook (if SLACK_WEBHOOK_URL is set)
  - Claude-generated daily digest summarizing the at-risk list

Runs via APScheduler (every hour by default).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import anthropic
import httpx
import psycopg2
import psycopg2.extras
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SIDECAR_DB_URL = os.environ["SIDECAR_DB_URL"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-3-haiku-20240307")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
ALERT_SCAN_INTERVAL_MINUTES = int(os.environ.get("ALERT_SCAN_INTERVAL_MINUTES", "60"))


def connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(SIDECAR_DB_URL)


# ─── Core alert scanner ───────────────────────────────────────────────────────

def scan_for_at_risk_accounts(cur) -> list[dict]:
    """Find companies flagged at_risk in their most recent score snapshot."""
    cur.execute(
        """
        SELECT DISTINCT ON (fhs.company_id)
            fhs.company_id,
            da.company_name,
            da.plan,
            fhs.score,
            fhs.scored_at
        FROM fact_health_score fhs
        JOIN dim_accounts da ON da.company_id = fhs.company_id
        WHERE fhs.at_risk = TRUE
        ORDER BY fhs.company_id, fhs.scored_at DESC
        """
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def already_alerted_today(cur, company_id: int) -> bool:
    """Avoid duplicate alerts within the same calendar day."""
    cur.execute(
        """
        SELECT 1 FROM alert_log
        WHERE company_id = %s
          AND alert_type = 'score_drop'
          AND created_at >= CURRENT_DATE
        LIMIT 1
        """,
        (company_id,),
    )
    return cur.fetchone() is not None


def log_alert(cur, company_id: int, alert_type: str, message: str) -> int:
    cur.execute(
        """
        INSERT INTO alert_log (company_id, alert_type, message, acknowledged, created_at)
        VALUES (%s, %s, %s, FALSE, now())
        RETURNING id
        """,
        (company_id, alert_type, message),
    )
    return cur.fetchone()[0]


# ─── Slack integration ────────────────────────────────────────────────────────

def send_slack_alert(company_name: str, score: int, plan: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    payload = {
        "text": f"🔴 *At-Risk Account Alert*\n*{company_name}* ({plan.upper()}) — Health Score: {score}/100\nImmediate CS follow-up recommended."
    }
    try:
        resp = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("  Slack alert sent for %s", company_name)
    except Exception as e:
        log.warning("  Slack alert failed: %s", e)


def generate_claude_digest(at_risk_accounts: list[dict]) -> str:
    """Use Claude to write a CS-team-friendly daily digest."""
    if not ANTHROPIC_API_KEY or not at_risk_accounts:
        return ""

    account_list = "\n".join(
        f"- {a['company_name']} ({a['plan'].upper()}, score={a['score']})"
        for a in at_risk_accounts
    )
    prompt = f"""You are a Customer Success AI assistant. Write a brief, plain-English daily digest 
for the CS team about at-risk accounts. Be concise, actionable, and professional.

AT-RISK ACCOUNTS TODAY ({len(at_risk_accounts)} total):
{account_list}

Write 3-5 sentences summarizing the situation and recommending prioritization."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=AI_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        digest = msg.content[0].text.strip()
        log.info("Claude digest generated (%d tokens)", msg.usage.output_tokens)
        return digest
    except Exception as e:
        log.error("Claude digest failed: %s", e)
        return ""


def send_slack_digest(digest: str, at_risk_count: int) -> None:
    if not SLACK_WEBHOOK_URL or not digest:
        return
    payload = {
        "text": f"📋 *Daily At-Risk Account Digest* ({at_risk_count} accounts)\n\n{digest}"
    }
    try:
        resp = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Slack digest sent.")
    except Exception as e:
        log.warning("Slack digest send failed: %s", e)


# ─── Main scan job ────────────────────────────────────────────────────────────

def run_alert_scan() -> dict:
    """
    Main scheduled job.
    1. Scans for at-risk companies
    2. Deduplicates (one alert per company per day)
    3. Logs to alert_log
    4. Optionally sends Slack webhook
    5. Optionally generates Claude digest (daily)
    """
    started_at = datetime.now(timezone.utc)
    log.info("Running alert scan at %s", started_at.isoformat())

    conn = connect()
    new_alerts = 0
    skipped = 0

    try:
        with conn:
            with conn.cursor() as cur:
                at_risk = scan_for_at_risk_accounts(cur)

                for account in at_risk:
                    cid = account["company_id"]
                    if already_alerted_today(cur, cid):
                        skipped += 1
                        continue

                    message = (
                        f"{account['company_name']} ({account['plan'].upper()}) "
                        f"health score is {account['score']}/100 — flagged at-risk."
                    )
                    log_alert(cur, cid, "score_drop", message)
                    send_slack_alert(account["company_name"], account["score"], account["plan"])
                    new_alerts += 1
                    log.warning("  🔴 Alert logged: %s", message)

                # Daily digest (only if it's early in the day — first run after midnight)
                if at_risk and started_at.hour < 2:
                    digest = generate_claude_digest(at_risk)
                    send_slack_digest(digest, len(at_risk))

    finally:
        conn.close()

    result = {
        "scanned_at": started_at.isoformat(),
        "at_risk_found": len(at_risk) if "at_risk" in dir() else 0,
        "new_alerts_logged": new_alerts,
        "skipped_duplicate": skipped,
    }
    log.info("Alert scan done: %d new alerts, %d skipped (already alerted today).", new_alerts, skipped)
    return result


# ─── Scheduler setup ─────────────────────────────────────────────────────────

def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_alert_scan,
        "interval",
        minutes=ALERT_SCAN_INTERVAL_MINUTES,
        next_run_time=datetime.now(),  # Run immediately on start
        id="alert_scan",
    )
    scheduler.start()
    log.info("Alert scheduler started (interval=%d min)", ALERT_SCAN_INTERVAL_MINUTES)
    return scheduler


if __name__ == "__main__":
    run_alert_scan()
