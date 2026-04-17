"""
ai/summarizer.py — Task 3: AI Account Summaries

Generates 3–5 sentence Claude summaries for each company when their
health score changes by more than 5 points.

Cost-awareness design:
  1. Prompt hash (SHA-256): skip API call if inputs haven't changed
  2. Token usage logged to ai_summaries table
  3. Uses claude-3-haiku-20240307 (cheapest; switch to sonnet for quality)
  4. Structured output: also extracts recommended_action via prompt engineering

Trigger: called by the scorer or the API's manual trigger endpoint.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import anthropic
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SIDECAR_DB_URL = os.environ["SIDECAR_DB_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AI_MODEL = os.environ.get("AI_MODEL", "claude-3-haiku-20240307")

SCORE_CHANGE_THRESHOLD = 5  # Only regenerate if score moved more than this


def connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(SIDECAR_DB_URL)


def get_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ─── Data gathering ──────────────────────────────────────────────────────────

def fetch_account_context(cur, company_id: int) -> dict | None:
    """Pull everything Claude needs to write a meaningful summary."""
    # Account basics
    cur.execute(
        """
        SELECT company_id, company_name, plan, mrr_cents, churned_at,
               total_users, open_tickets, critical_tickets,
               last_login_at, distinct_event_types
        FROM dim_accounts WHERE company_id = %s
        """,
        (company_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    account = dict(zip(cols, row))

    # Last 2 health scores for trend
    cur.execute(
        """
        SELECT score, components, at_risk, scored_at
        FROM fact_health_score
        WHERE company_id = %s
        ORDER BY scored_at DESC
        LIMIT 2
        """,
        (company_id,),
    )
    scores = cur.fetchall()
    account["latest_score"] = scores[0][0] if scores else None
    account["latest_components"] = scores[0][1] if scores else {}
    account["at_risk"] = scores[0][2] if scores else False
    account["prev_score"] = scores[1][0] if len(scores) > 1 else None

    # Recent event types (last 30 days)
    cur.execute(
        """
        SELECT event_type, SUM(event_count) AS cnt
        FROM fact_events_daily
        WHERE company_id = %s AND event_date >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY event_type
        ORDER BY cnt DESC
        LIMIT 5
        """,
        (company_id,),
    )
    account["recent_events"] = [{"type": r[0], "count": r[1]} for r in cur.fetchall()]

    # Open tickets (most recent 5)
    cur.execute(
        """
        SELECT alert_type, message, created_at
        FROM alert_log
        WHERE company_id = %s AND acknowledged = FALSE
        ORDER BY created_at DESC
        LIMIT 3
        """,
        (company_id,),
    )
    account["open_alerts"] = [{"type": r[0], "msg": r[1]} for r in cur.fetchall()]

    # Last CS note
    cur.execute(
        """
        SELECT author, note, created_at
        FROM crm_notes
        WHERE company_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (company_id,),
    )
    note_row = cur.fetchone()
    account["last_cs_note"] = (
        {"author": note_row[0], "note": note_row[1], "date": note_row[2].isoformat()}
        if note_row
        else None
    )

    return account


def build_prompt(ctx: dict) -> str:
    score = ctx.get("latest_score", "N/A")
    prev_score = ctx.get("prev_score")
    trend = (
        f"improved from {prev_score} to {score}"
        if prev_score and score > prev_score
        else f"declined from {prev_score} to {score}"
        if prev_score and score < prev_score
        else f"stable at {score}"
    )
    at_risk_note = " This account is flagged AT-RISK." if ctx.get("at_risk") else ""

    recent_events_str = ", ".join(
        f"{e['type']} ({e['count']} times)" for e in ctx.get("recent_events", [])
    ) or "no recent activity"

    last_note = ctx.get("last_cs_note")
    note_str = (
        f'CS note from {last_note["author"]}: "{last_note["note"]}"'
        if last_note
        else "No CS notes recorded yet."
    )

    return f"""You are a Customer Success AI assistant. Write a concise account summary and recommend a next action.

ACCOUNT: {ctx['company_name']}
PLAN: {ctx['plan'].upper()} | MRR: ${ctx['mrr_cents'] / 100:.0f}/mo | SEATS: {ctx['total_users']}
HEALTH SCORE: {trend}.{at_risk_note}
SCORE BREAKDOWN: {json.dumps(ctx.get('latest_components', {}), indent=None)}
RECENT ACTIVITY (30d): {recent_events_str}
OPEN TICKETS: {ctx['open_tickets']} total, {ctx['critical_tickets']} critical
LAST CS NOTE: {note_str}

Respond ONLY with a valid JSON object (no markdown, no extra text):
{{
  "summary": "3-5 sentence account health summary incorporating the health trend, activity patterns, ticket status, and CS note",
  "recommended_action": "One specific, actionable next step for the CS team (1-2 sentences)"
}}"""


def hash_prompt_inputs(ctx: dict) -> str:
    """SHA-256 of key context fields — used to skip regeneration if nothing changed."""
    key_data = json.dumps(
        {
            "score": ctx.get("latest_score"),
            "prev_score": ctx.get("prev_score"),
            "at_risk": ctx.get("at_risk"),
            "recent_events": ctx.get("recent_events"),
            "open_tickets": ctx.get("open_tickets"),
            "last_cs_note": ctx.get("last_cs_note"),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(key_data.encode()).hexdigest()


def get_existing_summary_hash(cur, company_id: int) -> str | None:
    cur.execute(
        """
        SELECT prompt_hash FROM ai_summaries
        WHERE company_id = %s
        ORDER BY generated_at DESC LIMIT 1
        """,
        (company_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


# ─── Score change check ───────────────────────────────────────────────────────

def score_changed_significantly(cur, company_id: int) -> bool:
    """Return True if the two most recent scores differ by > SCORE_CHANGE_THRESHOLD."""
    cur.execute(
        """
        SELECT score FROM fact_health_score
        WHERE company_id = %s
        ORDER BY scored_at DESC LIMIT 2
        """,
        (company_id,),
    )
    scores = [r[0] for r in cur.fetchall()]
    if len(scores) < 2:
        return True  # No prior score → always generate
    return abs(scores[0] - scores[1]) > SCORE_CHANGE_THRESHOLD


# ─── Claude call ─────────────────────────────────────────────────────────────

def call_claude(client: anthropic.Anthropic, prompt: str) -> tuple[str, str, int, int]:
    """
    Returns (summary, recommended_action, input_tokens, output_tokens).
    Parses structured JSON response from Claude.
    """
    message = client.messages.create(
        model=AI_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    content = message.content[0].text.strip()
    usage = message.usage

    try:
        parsed = json.loads(content)
        summary = parsed.get("summary", content)
        action = parsed.get("recommended_action", "")
    except json.JSONDecodeError:
        log.warning("Claude returned non-JSON response; storing raw text.")
        summary = content
        action = ""

    return summary, action, usage.input_tokens, usage.output_tokens


# ─── Main summarizer ─────────────────────────────────────────────────────────

def generate_summaries(company_ids: list[int] | None = None) -> dict:
    """
    Generate/update AI summaries.
    If company_ids is None, processes all companies.
    """
    started_at = datetime.now(timezone.utc)
    log.info("Starting AI summarization...")

    conn = connect()
    client = get_anthropic_client()

    generated = 0
    skipped_no_change = 0
    skipped_cached = 0
    total_input_tokens = 0
    total_output_tokens = 0

    try:
        with conn:
            with conn.cursor() as cur:
                if company_ids is None:
                    cur.execute("SELECT company_id FROM dim_accounts ORDER BY company_id")
                    company_ids = [r[0] for r in cur.fetchall()]

                for company_id in company_ids:
                    try:
                        # Gate 1: Did score change significantly?
                        if not score_changed_significantly(cur, company_id):
                            log.debug("  company_id=%d: score stable, skipping.", company_id)
                            skipped_no_change += 1
                            continue

                        # Gate 2: Is the prompt hash identical to the last summary?
                        ctx = fetch_account_context(cur, company_id)
                        if not ctx:
                            continue

                        current_hash = hash_prompt_inputs(ctx)
                        existing_hash = get_existing_summary_hash(cur, company_id)
                        if current_hash == existing_hash:
                            log.debug("  company_id=%d: context unchanged, skipping.", company_id)
                            skipped_cached += 1
                            continue

                        # Call Claude
                        prompt = build_prompt(ctx)
                        summary, action, in_tok, out_tok = call_claude(client, prompt)

                        # Store result
                        cur.execute(
                            """
                            INSERT INTO ai_summaries
                                (company_id, summary, recommended_action, prompt_hash,
                                 input_tokens, output_tokens, model, generated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                            """,
                            (company_id, summary, action, current_hash, in_tok, out_tok, AI_MODEL),
                        )
                        total_input_tokens += in_tok
                        total_output_tokens += out_tok
                        generated += 1
                        log.info(
                            "  ✓ company_id=%d summary generated (%d+%d tokens)",
                            company_id, in_tok, out_tok,
                        )

                    except Exception as e:
                        log.error("  ✗ company_id=%d failed: %s", company_id, e)

    finally:
        conn.close()

    finished_at = datetime.now(timezone.utc)
    summary_result = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "generated": generated,
        "skipped_no_score_change": skipped_no_change,
        "skipped_cached": skipped_cached,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "estimated_cost_usd": round((total_input_tokens * 0.00025 + total_output_tokens * 0.00125) / 1000, 4),
    }
    log.info(
        "✅ Summarization done: %d generated, %d skipped (no change), %d skipped (cached). "
        "Tokens: %d in / %d out. Est. cost: $%.4f",
        generated, skipped_no_change, skipped_cached,
        total_input_tokens, total_output_tokens,
        summary_result["estimated_cost_usd"],
    )
    return summary_result


if __name__ == "__main__":
    generate_summaries()
