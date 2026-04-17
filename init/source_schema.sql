-- ====================================================
-- SOURCE DB SCHEMA
-- Simulates a read-only production application database
-- We READ from these tables; we never write to them.
-- ====================================================

CREATE TABLE IF NOT EXISTS companies (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    plan        TEXT NOT NULL CHECK (plan IN ('trial', 'starter', 'pro', 'enterprise')),
    mrr_cents   INT DEFAULT 0,
    churned_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    company_id  INT REFERENCES companies(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    created_at  TIMESTAMPTZ DEFAULT now(),
    last_login  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    user_id     INT REFERENCES users(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    occurred_at TIMESTAMPTZ DEFAULT now(),
    metadata    JSONB
);

CREATE TABLE IF NOT EXISTS tickets (
    id          SERIAL PRIMARY KEY,
    company_id  INT REFERENCES companies(id) ON DELETE CASCADE,
    subject     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('open', 'pending', 'resolved')),
    severity    TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    created_at  TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

-- Indexes on source tables for efficient incremental sync
CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id);
CREATE INDEX IF NOT EXISTS idx_tickets_company ON tickets(company_id, status);
