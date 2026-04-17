-- ====================================================
-- SIDECAR DB SCHEMA
-- Our controlled analytics + CS layer
-- ====================================================

-- Denormalized account dimension: one row per company
-- Pre-joined from source to avoid expensive cross-DB JOINs at query time
CREATE TABLE IF NOT EXISTS dim_accounts (
    company_id          INT PRIMARY KEY,
    company_name        TEXT NOT NULL,
    plan                TEXT NOT NULL,
    mrr_cents           INT DEFAULT 0,
    churned_at          TIMESTAMPTZ,
    total_users         INT DEFAULT 0,
    admin_count         INT DEFAULT 0,
    last_login_at       TIMESTAMPTZ,
    first_event_at      TIMESTAMPTZ,
    last_event_at       TIMESTAMPTZ,
    total_events        INT DEFAULT 0,
    distinct_event_types INT DEFAULT 0,
    open_tickets        INT DEFAULT 0,
    critical_tickets    INT DEFAULT 0,
    synced_at           TIMESTAMPTZ DEFAULT now()
);

-- Daily event aggregation: enables trend analysis without scanning raw events
-- UNIQUE constraint makes upserts safe (idempotent)
CREATE TABLE IF NOT EXISTS fact_events_daily (
    id           SERIAL PRIMARY KEY,
    company_id   INT NOT NULL,
    event_date   DATE NOT NULL,
    event_type   TEXT NOT NULL,
    event_count  INT DEFAULT 0,
    unique_users INT DEFAULT 0,
    UNIQUE (company_id, event_date, event_type)
);

-- CS team notes: human-writable, never synced FROM source
CREATE TABLE IF NOT EXISTS crm_notes (
    id         SERIAL PRIMARY KEY,
    company_id INT NOT NULL,
    author     TEXT NOT NULL,
    note       TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Health scores: snapshots per company for trend analysis
CREATE TABLE IF NOT EXISTS fact_health_score (
    id         SERIAL PRIMARY KEY,
    company_id INT NOT NULL,
    score      INT NOT NULL CHECK (score BETWEEN 0 AND 100),
    at_risk    BOOLEAN DEFAULT FALSE,
    components JSONB,           -- breakdown: {login_recency: 20, adoption: 15, ...}
    scored_at  TIMESTAMPTZ DEFAULT now()
);

-- AI-generated account summaries with caching
CREATE TABLE IF NOT EXISTS ai_summaries (
    id                 SERIAL PRIMARY KEY,
    company_id         INT NOT NULL,
    summary            TEXT NOT NULL,
    recommended_action TEXT,
    prompt_hash        TEXT,    -- SHA256 of prompt inputs; used to skip regeneration
    input_tokens       INT,
    output_tokens      INT,
    model              TEXT,
    generated_at       TIMESTAMPTZ DEFAULT now()
);

-- Alert log: at-risk account notifications
CREATE TABLE IF NOT EXISTS alert_log (
    id           SERIAL PRIMARY KEY,
    company_id   INT NOT NULL,
    alert_type   TEXT NOT NULL,    -- 'score_drop' | 'critical_ticket' | 'churned'
    message      TEXT,
    acknowledged BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT now()
);

-- Sync watermarks: per-table tracking for incremental sync
CREATE TABLE IF NOT EXISTS sync_state (
    table_name     TEXT PRIMARY KEY,
    last_synced_id BIGINT DEFAULT 0,
    last_synced_at TIMESTAMPTZ
);

-- ============================================================
-- INDEXES
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_events_daily_company ON fact_events_daily(company_id, event_date);
CREATE INDEX IF NOT EXISTS idx_health_score_company  ON fact_health_score(company_id, scored_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_summaries_company  ON ai_summaries(company_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_log_company     ON alert_log(company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_notes_company     ON crm_notes(company_id, created_at DESC);
