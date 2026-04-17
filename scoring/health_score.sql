-- ====================================================
-- scoring/health_score.sql — Task 2
-- SQL views for customer health scoring
--
-- Uses window functions (LAG) to detect score drops.
-- All views read from the sidecar DB only.
-- ====================================================

-- ─────────────────────────────────────────────────────
-- View 1: seat utilization per company (last 30 days)
-- Counts distinct users with events in the last 30 days
-- ─────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_seat_utilization AS
SELECT
    da.company_id,
    da.total_users,
    COALESCE(SUM(fed.unique_users), 0) AS active_users_30d,
    CASE
        WHEN da.total_users = 0 THEN 0
        ELSE LEAST(
            ROUND(COALESCE(SUM(fed.unique_users), 0)::NUMERIC / da.total_users * 20),
            20
        )
    END AS seat_score
FROM dim_accounts da
LEFT JOIN fact_events_daily fed
    ON fed.company_id = da.company_id
    AND fed.event_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY da.company_id, da.total_users;

-- ─────────────────────────────────────────────────────
-- View 2: raw health score components per company
-- Each sub-score reflects one pillar of health
-- ─────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_health_components AS
SELECT
    da.company_id,
    da.company_name,
    da.plan,
    da.mrr_cents,
    da.churned_at,

    -- Component 1: Login Recency (max 25 pts)
    CASE
        WHEN da.last_login_at >= NOW() - INTERVAL '3 days'   THEN 25
        WHEN da.last_login_at >= NOW() - INTERVAL '7 days'   THEN 20
        WHEN da.last_login_at >= NOW() - INTERVAL '14 days'  THEN 15
        WHEN da.last_login_at >= NOW() - INTERVAL '30 days'  THEN 10
        ELSE 0
    END AS login_recency_score,

    -- Component 2: Feature Adoption Breadth (max 20 pts)
    -- distinct_event_types / 5 * 20, capped at 20
    LEAST(ROUND(da.distinct_event_types::NUMERIC / 5 * 20), 20) AS adoption_score,

    -- Component 3: Ticket Health (max 20 pts)
    -- Penalise for open critical/high/medium tickets
    GREATEST(
        20 - (da.critical_tickets * 8 + GREATEST(da.open_tickets - da.critical_tickets, 0) * 3),
        0
    ) AS ticket_score,

    -- Component 4: MRR Tier (max 15 pts)
    CASE da.plan
        WHEN 'enterprise' THEN 15
        WHEN 'pro'        THEN 12
        WHEN 'starter'    THEN 8
        WHEN 'trial'      THEN 4
        ELSE 4
    END AS mrr_tier_score,

    -- Component 5: Seat Utilization (max 20 pts) — from view above
    COALESCE(su.seat_score, 0) AS seat_score

FROM dim_accounts da
LEFT JOIN v_seat_utilization su ON su.company_id = da.company_id;

-- ─────────────────────────────────────────────────────
-- View 3: current health score (sum of components)
-- ─────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_current_health AS
SELECT
    company_id,
    company_name,
    plan,
    churned_at,
    login_recency_score,
    adoption_score,
    ticket_score,
    mrr_tier_score,
    seat_score,
    -- Total score — naturally bounded 0..100 by component maximums
    (login_recency_score + adoption_score + ticket_score + mrr_tier_score + seat_score) AS total_score
FROM v_health_components;

-- ─────────────────────────────────────────────────────
-- View 4: score trend with window function (LAG)
-- Detects 15+ point drops within a 14-day window
-- Uses ROW_NUMBER to get only the latest score per company
-- ─────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_score_trend AS
WITH scored AS (
    SELECT
        company_id,
        score,
        scored_at,
        LAG(score)     OVER (PARTITION BY company_id ORDER BY scored_at) AS prev_score,
        LAG(scored_at) OVER (PARTITION BY company_id ORDER BY scored_at) AS prev_scored_at,
        ROW_NUMBER()   OVER (PARTITION BY company_id ORDER BY scored_at DESC) AS rn
    FROM fact_health_score
)
SELECT
    company_id,
    score                        AS current_score,
    prev_score,
    scored_at,
    prev_scored_at,
    (prev_score - score)         AS score_drop,
    -- at_risk: dropped ≥15 points within last 14 days
    CASE
        WHEN prev_score IS NOT NULL
             AND (prev_score - score) >= 15
             AND (scored_at - prev_scored_at) <= INTERVAL '14 days'
        THEN TRUE
        ELSE FALSE
    END AS at_risk
FROM scored
WHERE rn = 1;  -- only the most recent snapshot per company
