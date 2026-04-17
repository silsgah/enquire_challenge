# AI Usage Log

> Required by the challenge: a diary of how AI tools were used, what changed, and where AI was pushed back on.

---

## Tool Used
**Antigravity (powered by Claude / Gemini)** — used throughout as a pair programmer.

---

## Session Log

### Phase 0 — Challenge Analysis
**Prompt:** "Read the CRM interview challenge PDF and create a detailed implementation plan."

**AI output:** Full plan with sidecar schema, 4-task breakdown, scoring formula, file structure.

**What I verified:** Cross-checked the schema against the source tables given in the PDF.
The AI initially included a `company_events` materialized view that wasn't needed —
I removed it to keep the schema minimal and YAGNI-compliant.

**Change made:** Dropped the materialized view suggestion. `fact_events_daily` with a UNIQUE
constraint achieves the same goal more simply.

---

### Phase 1 — Sidecar Schema
**Prompt:** "Design the sidecar schema with rationale for each table."

**AI output:** 7 tables with proper constraints and indexes.

**What I verified:** Checked that `UNIQUE (company_id, event_date, event_type)` on
`fact_events_daily` correctly enables idempotent upserts. Confirmed `ON CONFLICT DO UPDATE`
semantics — the AI's version was correct.

**Where AI was right:** Using `JSONB` for `components` on `fact_health_score` was the
AI's suggestion. I kept it — being able to explain *why* a score changed is more valuable
than a normalized breakdown table for a 50-company demo.

---

### Phase 2 — Sync Engine
**Prompt:** "Write an incremental sync engine with watermarks and schema evolution handling."

**AI output:** `sync_engine.py` with watermark tracking and `information_schema` check.

**What I verified and changed:**
- The AI used `conn.cursor()` inside a `with conn:` block, which auto-commits on exit.
  I verified this is correct `psycopg2` behavior (the context manager calls `commit()` on
  success, `rollback()` on exception).
- The AI's initial version rebuilt `dim_accounts` AND ran the incremental events sync in
  a single transaction. I split them into two separate transactions — if event sync fails,
  the dim refresh should still commit rather than rolling back.
- The schema evolution warning logs correctly but the AI initially used `log.error`
  level, which would trigger alerts. I changed to `log.warning` — it's a signal, not a failure.

---

### Phase 3 — Health Scoring
**Prompt:** "Implement the 5-component health score with window function for at-risk detection."

**AI output:** SQL views + Python scorer.

**Where I pushed back:** The AI suggested computing the seat utilization score by
querying `users` directly each time scoring runs. I rejected this — `dim_accounts`
already has `total_users` pre-joined. Using `fact_events_daily` for the active user
count (which it also has) keeps all scoring reads confined to the sidecar.

**One AI bug caught:** In `v_score_trend`, the AI used `WHERE at_risk = TRUE` after
the CTE — but `at_risk` is computed *in* the CTE, not stored. It was referencing the
wrong value. I corrected it to compute `at_risk` as a CASE expression in the SELECT
and filter on the computed column.

Actually: SQL doesn't allow filtering on aliases in the same SELECT. Fixed by wrapping
in an outer query or using the CASE expression in the WHERE clause directly.

---

### Phase 4 — AI Summarizer
**Prompt:** "Build a Claude API integration with caching and structured output."

**AI output:** `summarizer.py` with dual-gate caching (score threshold + prompt hash).

**What I added that the AI missed:**
- The `estimated_cost_usd` calculation in the run summary — useful for the debrief to
  show cost awareness. The AI logged tokens but didn't convert to dollars.
- I added explicit `json.JSONDecodeError` handling for non-JSON Claude responses.
  The AI assumed Claude would always return valid JSON from the prompt — that's optimistic
  in practice; Claude occasionally adds explanation text before the JSON.

**Model choice decision:** The AI initially defaulted to `claude-3-5-sonnet-20241022`.
I switched to `claude-3-haiku-20240307` for the default. Haiku is ~15x cheaper and
sufficient for account summaries. I documented the switch path in README.

---

### Phase 5 — FastAPI Interface
**Prompt:** "Build FastAPI endpoints with background scheduler for alerts."

**AI output:** `interface/main.py` with 7 endpoints.

**What I verified:**
- The `lifespan` context manager pattern (replacing deprecated `@app.on_event("startup")`)
  was correctly used — the AI knew the FastAPI 0.95+ API.
- The `LATERAL` join for fetching the latest health score per account was correct but
  I double-checked the semantics: `LEFT JOIN LATERAL ... ON TRUE` is the right way to
  express "for each account, get the most recent score row."

---

### Phase 6 — Unit Tests
**Prompt:** "Write unit tests for health scoring logic without requiring a DB connection."

**AI output:** Full test suite mirroring the SQL view logic in Python.

**Design decision I made:** The AI wanted to mock the DB and test `scorer.py` directly.
I chose instead to extract the scoring math into pure Python functions and test those.
This is better because:
1. Tests run fast with no DB
2. The business rules are explicitly documented in Python
3. Each component is tested in isolation

**Trade-off acknowledged:** This means the SQL views and Python functions can drift.
In production I'd add an integration test that runs both and compares results.

---

## Summary

| Area | AI Quality | Changes Made |
|---|---|---|
| Schema design | ✅ Excellent | Removed unnecessary materialized view |
| Sync engine | ✅ Good | Split into two transactions; fixed log level |
| SQL views | ⚠️ One bug | Fixed `at_risk` alias issue in WHERE clause |
| AI summarizer | ✅ Good | Added cost calculation; added JSON error handling |
| FastAPI | ✅ Excellent | Minimal changes; verified LATERAL join semantics |
| Tests | ✅ Good | Changed testing strategy from mocking to pure functions |

**Verdict:** AI was an excellent accelerator for boilerplate and structure.
The places it required oversight were subtle (transaction boundaries, SQL alias
scoping, model cost trade-offs) — exactly the things a senior engineer would catch.
