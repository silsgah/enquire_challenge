"""
interface/main.py — Task 4: CRM FastAPI Interface

Exposes the sidecar DB via a clean REST API. Endpoints:
  GET  /accounts              — list with health scores
  GET  /accounts/{id}         — detail + AI summary + CS notes
  POST /accounts/{id}/notes   — add a CS note
  POST /sync                  — trigger manual sync
  GET  /alerts                — view alert log
  POST /score                 — manually trigger health scoring
  POST /summarize             — manually trigger AI summaries

Alert scheduler runs in the background via APScheduler.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import psycopg2
import psycopg2.extras
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Local modules
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from sync.sync_engine import run_sync
from scoring.scorer import run_scoring
from ai.summarizer import generate_summaries
from alerts.monitor import run_alert_scan, start_scheduler

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SIDECAR_DB_URL = os.environ["SIDECAR_DB_URL"]


# ─── DB helpers ──────────────────────────────────────────────────────────────

def get_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(SIDECAR_DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ─── Lifespan (startup / shutdown) ───────────────────────────────────────────

scheduler: Optional[BackgroundScheduler] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    log.info("Starting CRM Sidecar API...")
    scheduler = start_scheduler()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)
    log.info("CRM Sidecar API shutting down.")


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="CRM Sidecar API",
    description="Customer health scoring, AI summaries, and CS notes on top of the source CRM DB.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Request / Response models ────────────────────────────────────────────────

class NoteCreate(BaseModel):
    author: str
    note: str


class SyncResponse(BaseModel):
    status: str
    summary: dict[str, Any]


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/health", tags=["Meta"])
def health_check():
    """Liveness check."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/accounts", tags=["Accounts"])
def list_accounts(
    plan: Optional[str] = Query(None, description="Filter by plan: trial|starter|pro|enterprise"),
    at_risk_only: bool = Query(False, description="Only show at-risk accounts"),
    order_by: str = Query("score_desc", description="score_desc | score_asc | name"),
):
    """
    List all accounts with their latest health scores.
    Supports filtering by plan and at-risk status.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            order_clause = {
                "score_desc": "latest_score DESC NULLS LAST",
                "score_asc":  "latest_score ASC NULLS LAST",
                "name":       "da.company_name ASC",
            }.get(order_by, "latest_score DESC NULLS LAST")

            at_risk_filter = "AND fhs.at_risk = TRUE" if at_risk_only else ""
            plan_filter = "AND da.plan = %(plan)s" if plan else ""

            cur.execute(
                f"""
                SELECT
                    da.company_id,
                    da.company_name,
                    da.plan,
                    da.mrr_cents,
                    da.churned_at,
                    da.total_users,
                    da.open_tickets,
                    da.critical_tickets,
                    da.last_login_at,
                    da.synced_at,
                    fhs.score             AS latest_score,
                    fhs.at_risk,
                    fhs.scored_at,
                    fhs.components
                FROM dim_accounts da
                LEFT JOIN LATERAL (
                    SELECT score, at_risk, scored_at, components
                    FROM fact_health_score
                    WHERE company_id = da.company_id
                    ORDER BY scored_at DESC
                    LIMIT 1
                ) fhs ON TRUE
                WHERE 1=1
                  {plan_filter}
                  {at_risk_filter}
                ORDER BY {order_clause}
                """,
                {"plan": plan},
            )
            accounts = cur.fetchall()
            return {"count": len(accounts), "accounts": [dict(a) for a in accounts]}
    finally:
        conn.close()


@app.get("/accounts/{company_id}", tags=["Accounts"])
def get_account(company_id: int = Path(..., description="Company ID")):
    """
    Full account detail: dims, latest health score with components,
    AI summary, CS notes, and recent alerts.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Account dims
            cur.execute("SELECT * FROM dim_accounts WHERE company_id = %s", (company_id,))
            account = cur.fetchone()
            if not account:
                raise HTTPException(status_code=404, detail=f"Company {company_id} not found")
            account = dict(account)

            # Latest health score
            cur.execute(
                """
                SELECT score, at_risk, components, scored_at
                FROM fact_health_score
                WHERE company_id = %s
                ORDER BY scored_at DESC LIMIT 1
                """,
                (company_id,),
            )
            score_row = cur.fetchone()
            account["health_score"] = dict(score_row) if score_row else None

            # Score history (last 10)
            cur.execute(
                """
                SELECT score, at_risk, scored_at
                FROM fact_health_score
                WHERE company_id = %s
                ORDER BY scored_at DESC LIMIT 10
                """,
                (company_id,),
            )
            account["score_history"] = [dict(r) for r in cur.fetchall()]

            # Latest AI summary
            cur.execute(
                """
                SELECT summary, recommended_action, model, input_tokens, output_tokens, generated_at
                FROM ai_summaries
                WHERE company_id = %s
                ORDER BY generated_at DESC LIMIT 1
                """,
                (company_id,),
            )
            summary_row = cur.fetchone()
            account["ai_summary"] = dict(summary_row) if summary_row else None

            # CS notes (most recent 10)
            cur.execute(
                """
                SELECT id, author, note, created_at
                FROM crm_notes
                WHERE company_id = %s
                ORDER BY created_at DESC LIMIT 10
                """,
                (company_id,),
            )
            account["cs_notes"] = [dict(r) for r in cur.fetchall()]

            # Open alerts
            cur.execute(
                """
                SELECT id, alert_type, message, acknowledged, created_at
                FROM alert_log
                WHERE company_id = %s AND acknowledged = FALSE
                ORDER BY created_at DESC LIMIT 5
                """,
                (company_id,),
            )
            account["open_alerts"] = [dict(r) for r in cur.fetchall()]

            return account
    finally:
        conn.close()


@app.post("/accounts/{company_id}/notes", tags=["Accounts"], status_code=201)
def add_note(
    company_id: int = Path(..., description="Company ID"),
    body: NoteCreate = ...,
):
    """Add a CS note to an account."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Verify company exists
            cur.execute("SELECT company_id FROM dim_accounts WHERE company_id = %s", (company_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail=f"Company {company_id} not found")

            cur.execute(
                """
                INSERT INTO crm_notes (company_id, author, note, created_at)
                VALUES (%s, %s, %s, now())
                RETURNING id, created_at
                """,
                (company_id, body.author, body.note),
            )
            row = cur.fetchone()
        conn.commit()
        return {
            "id": row["id"],
            "company_id": company_id,
            "author": body.author,
            "note": body.note,
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


@app.post("/sync", tags=["Operations"])
def trigger_sync():
    """Manually trigger a full incremental sync from source DB → sidecar."""
    try:
        log.info("Manual sync triggered via API.")
        summary = run_sync()
        return SyncResponse(status="success", summary=summary)
    except Exception as e:
        log.error("Manual sync failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/score", tags=["Operations"])
def trigger_scoring():
    """Manually trigger health scoring for all accounts."""
    try:
        log.info("Manual scoring triggered via API.")
        summary = run_scoring()
        return {"status": "success", "summary": summary}
    except Exception as e:
        log.error("Manual scoring failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/summarize", tags=["Operations"])
def trigger_summarize(company_id: Optional[int] = Query(None, description="Specific company to summarize (omit for all)")):
    """Manually trigger AI summary generation."""
    try:
        ids = [company_id] if company_id else None
        log.info("Manual summarization triggered (company_id=%s).", company_id)
        result = generate_summaries(ids)
        return {"status": "success", "result": result}
    except Exception as e:
        log.error("Manual summarization failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/alerts", tags=["Alerts"])
def list_alerts(
    acknowledged: Optional[bool] = Query(None, description="Filter by acknowledged status"),
    limit: int = Query(50, le=200),
):
    """View the alert log."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            ack_filter = ""
            params: dict = {"limit": limit}
            if acknowledged is not None:
                ack_filter = "AND al.acknowledged = %(acknowledged)s"
                params["acknowledged"] = acknowledged

            cur.execute(
                f"""
                SELECT
                    al.id,
                    al.company_id,
                    da.company_name,
                    al.alert_type,
                    al.message,
                    al.acknowledged,
                    al.created_at
                FROM alert_log al
                JOIN dim_accounts da ON da.company_id = al.company_id
                WHERE 1=1 {ack_filter}
                ORDER BY al.created_at DESC
                LIMIT %(limit)s
                """,
                params,
            )
            alerts = [dict(r) for r in cur.fetchall()]
            return {"count": len(alerts), "alerts": alerts}
    finally:
        conn.close()


@app.patch("/alerts/{alert_id}/acknowledge", tags=["Alerts"])
def acknowledge_alert(alert_id: int = Path(...)):
    """Mark an alert as acknowledged."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alert_log SET acknowledged = TRUE WHERE id = %s RETURNING id",
                (alert_id,),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
        conn.commit()
        return {"status": "acknowledged", "alert_id": alert_id}
    finally:
        conn.close()


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
