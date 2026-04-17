# CRM Sidecar

> A production-grade customer health analytics sidecar for a CRM source database.
> Built as part of a 4-6 hour interview challenge.

---

## Quick Start

### The Easy Way: Using the `Makefile` (Recommended)
This project includes a `Makefile` to streamline execution. After adding your API key to `.env`, run:
```bash
make up          # 1. Start both Postgres databases via Docker
make seed        # 2. Reset and populate source DB with realistic records
make pipeline    # 3. Fast-forward through Sync -> Score -> AI Summarize
make api         # 4. Start the FastAPI Interface (available at localhost:8000/docs)
make test        # 5. Run the pure-Python unit test suite
make down        # 6. Stop all Docker containers
```

---

### The Manual Way (Step-by-Step)

If you prefer to run scripts individually without `make`:

**1. Start the databases**
```bash
docker compose up -d
# Wait for both DBs to be healthy:
docker compose ps
```

**2. Install Python deps**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**3. Seed the source DB (Task 0)**
```bash
python seed/generate_seed.py
```

**4. Run Pipeline Steps individually (Tasks 1-4)**
```bash
python sync/sync_engine.py       # Incremental Sync
python scoring/scorer.py         # Compute Health Scores
python ai/summarizer.py          # Generate AI Summaries
python alerts/monitor.py         # Detect At-Risk targets
```

**5. Start the API**
```bash
uvicorn interface.main:app --reload --port 8000
```

**6. Run Tests**
```bash
pytest tests/ -v
```

---

## Architecture

### Why a Sidecar?

The source DB is owned by the product team — we treat it as read-only.
A sidecar database lets the Customer Success (CS) team:

1. **Add their own data** (CS notes, alerts) without touching prod
2. **Run analytics** without impacting production query performance
3. **Layer AI** on top without polluting the source schema

This is the same pattern used by Salesforce (next to the billing DB),
Zendesk (next to the product DB), and internal CS tools at most SaaS companies.

### Two-Database Docker Setup

```
Source DB  (port 5432) — crm_source      ← read-only simulation of "prod"
Sidecar DB (port 5433) — crm_sidecar     ← our controlled analytics layer
```

**Trade-off considered:** Using two schemas within one Postgres instance would be simpler.
I chose two separate databases because:
- It accurately models the real-world constraint (different teams, different ownership)
- It makes the sync direction explicit and testable
- It prevents accidental cross-schema JOINs that would bypass the sync layer

---

## Sidecar Schema Design

### Tables

| Table | Purpose | Design Decision |
|---|---|---|
| `dim_accounts` | Denormalized company summary | Full refresh on every sync (50 rows = cheap); avoids expensive cross-DB JOINs at query time |
| `fact_events_daily` | Daily event aggregations | Reduces 10K+ raw events to ~hundreds of rows; enables window functions for trends |
| `crm_notes` | CS team notes | Never synced FROM source; only written via the API |
| `fact_health_score` | Score snapshots per company | Append-only for trend tracking; `components JSONB` for transparency |
| `ai_summaries` | Claude-generated summaries | `prompt_hash` enables cache-hit detection; token usage logged for cost control |
| `alert_log` | At-risk account notifications | Deduplication prevents spam; `acknowledged` flag for CS workflow |
| `sync_state` | Per-table sync watermarks | Enables incremental sync without full table scans |

### Key Index Decisions

```sql
-- Health scores: always queried per-company, most-recent-first
CREATE INDEX idx_health_score_company ON fact_health_score(company_id, scored_at DESC);

-- Events: always queried per-company over a date range
CREATE INDEX idx_events_daily_company ON fact_events_daily(company_id, event_date);
```

---

## Task Implementation Notes

### Task 1 — Sync Engine

**Algorithm:**
1. Read `sync_state` to get the last synced event ID (high-water mark)
2. Query source DB for events with `id > watermark`
3. Aggregate into `fact_events_daily` using `ON CONFLICT DO UPDATE`
4. Full-refresh `dim_accounts` (always safe; 50 rows)
5. Update `sync_state` watermark atomically

**Idempotency guarantee:** All writes use `ON CONFLICT ... DO UPDATE` (upsert).
Re-running the sync on the same data produces identical results.

**Schema evolution:** Before each sync, the engine queries `information_schema.columns`
to detect unknown columns on source tables. It logs a warning and proceeds with
known columns rather than crashing. This prevents a new product column from
taking down the nightly sync job.

### Task 2 — Health Scoring

**Score = sum of 5 weighted components (0–100 total):**

| Component | Max | Signal |
|---|---|---|
| Login Recency | 25 | Days since last user login across the company |
| Feature Adoption | 20 | `distinct_event_types / 5 * 20` (capped at 20) |
| Ticket Health | 20 | `20 - (critical×8 + other_open×3)` floored at 0 |
| MRR Tier | 15 | enterprise=15, pro=12, starter=8, trial=4 |
| Seat Utilization | 20 | `(active_users_30d / total_users) * 20` |

**Window function for at-risk detection:**
```sql
LAG(score) OVER (PARTITION BY company_id ORDER BY scored_at)
```
A company is `at_risk = TRUE` when: `prev_score - current_score >= 15`
within a `<= 14 day` window.

**Why JSONB for components?** During the debrief, a CS manager can ask "why did
Acme Corp's score drop?" and you can show the breakdown, not just the number.

### Task 3 — AI Summaries

**Two-gate caching strategy** (prevents needless API spend):
1. **Score change gate:** Only regenerate if score moved by > 5 points
2. **Prompt hash gate:** SHA-256 of key context fields; if hash matches the last
   stored summary, skip the API call entirely

**Structured output:** Claude is prompted to return a JSON object:
```json
{
  "summary": "3-5 sentence account overview...",
  "recommended_action": "Specific next step for CS team..."
}
```

**Model choice:** `claude-3-haiku-20240307` — the cheapest capable Claude model.
For a production deployment with higher quality requirements, switch to
`claude-3-5-sonnet-20241022` by updating `AI_MODEL` in `.env`.

**Cost transparency:** Every API call logs `input_tokens` and `output_tokens`
to `ai_summaries`. The summarizer also estimates `cost_usd` in its run summary.

### Task 4 — Interface & Alerts

**FastAPI over Typer CLI** because:
- Enables curl/Postman testing during the debrief call
- Swagger UI (`/docs`) is a better demo than CLI help text
- The scheduler (APScheduler) runs cleanly as a background thread within uvicorn

**Alert deduplication:** `already_alerted_today()` prevents the same company
from flooding `alert_log` with duplicate entries on every scan cycle.

**Bonus — Claude daily digest:** At the first scan after midnight, if `SLACK_WEBHOOK_URL`
is set, the monitor calls Claude to write a CS-team-friendly plain-English summary
of all at-risk accounts and posts it to Slack.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SOURCE_DB_URL` | ✅ | PostgreSQL URL for the source (read-only) DB |
| `SIDECAR_DB_URL` | ✅ | PostgreSQL URL for the sidecar DB |
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API key for Claude |
| `AI_MODEL` | ❌ | Claude model (default: `claude-3-haiku-20240307`) |
| `ALERT_SCAN_INTERVAL_MINUTES` | ❌ | How often to scan for at-risk accounts (default: 60) |
| `SLACK_WEBHOOK_URL` | ❌ | Slack incoming webhook URL for digest |

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/accounts` | List accounts with health scores |
| `GET` | `/accounts/{id}` | Full account detail |
| `POST` | `/accounts/{id}/notes` | Add a CS note |
| `POST` | `/sync` | Trigger manual sync |
| `POST` | `/score` | Trigger health scoring |
| `POST` | `/summarize` | Trigger AI summary generation |
| `GET` | `/alerts` | View alert log |
| `PATCH` | `/alerts/{id}/acknowledge` | Acknowledge an alert |

Full interactive docs: **http://localhost:8000/docs**

---

## Project Structure

```
crm-sidecar/
├── README.md
├── ai_usage_log.md              # AI tool diary (see evaluation criteria)
├── docker-compose.yml           # Two Postgres instances
├── requirements.txt
├── .env.example
├── init/
│   ├── source_schema.sql        # Source DB DDL
│   └── sidecar_schema.sql       # Sidecar DB DDL
├── seed/
│   └── generate_seed.py         # Task 0: realistic test data
├── sync/
│   └── sync_engine.py           # Task 1: incremental sync
├── scoring/
│   ├── health_score.sql         # Task 2: SQL views + window functions
│   └── scorer.py                # Task 2: Python orchestration
├── ai/
│   └── summarizer.py            # Task 3: Claude API + caching
├── interface/
│   └── main.py                  # Task 4: FastAPI
├── alerts/
│   └── monitor.py               # Task 4: APScheduler + Slack
└── tests/
    └── test_health_score.py     # Bonus: unit tests
```

---

## Trade-offs & What I'd Do Differently in Production

1. **Async DB access:** For a real production API, I'd swap `psycopg2` for `asyncpg`
   to avoid blocking the uvicorn event loop on DB queries.

2. **Celery instead of APScheduler:** APScheduler is fine for a single-process demo.
   In production, I'd use Celery with Redis for distributed, retryable jobs.

3. **Migration tooling:** Schema managed manually here. In production: Alembic.

4. **Secrets management:** `.env` file is fine for a challenge. Production: AWS Secrets Manager or Vault.

5. **Observability:** Would add structured logging (structlog) and OpenTelemetry traces to the sync and scoring pipelines.
