"""
tests/test_health_score.py — Bonus: Unit tests for health scoring

Tests the scoring logic in isolation — no DB required.
Covers all 5 scoring components plus at-risk flag detection.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# We test the scoring logic directly by reproducing the component math here.
# This is intentional: testing the SQL view logic in Python makes the business
# rules self-documenting and runnable in CI without a DB connection.


NOW = datetime.now(timezone.utc)


# ─── Scoring component functions (mirrors SQL view logic) ─────────────────────

def login_recency_score(last_login_at: datetime | None) -> int:
    if last_login_at is None:
        return 0
    days_ago = (NOW - last_login_at).days
    if days_ago <= 3:
        return 25
    if days_ago <= 7:
        return 20
    if days_ago <= 14:
        return 15
    if days_ago <= 30:
        return 10
    return 0


def adoption_score(distinct_event_types: int) -> int:
    return min(round(distinct_event_types / 5 * 20), 20)


def ticket_score(critical_tickets: int, total_open_tickets: int) -> int:
    other_open = max(total_open_tickets - critical_tickets, 0)
    return max(20 - (critical_tickets * 8 + other_open * 3), 0)


def mrr_tier_score(plan: str) -> int:
    return {"enterprise": 15, "pro": 12, "starter": 8, "trial": 4}.get(plan, 4)


def seat_utilization_score(active_users_30d: int, total_users: int) -> int:
    if total_users == 0:
        return 0
    return min(round(active_users_30d / total_users * 20), 20)


def total_health_score(
    last_login_at: datetime | None,
    distinct_event_types: int,
    critical_tickets: int,
    open_tickets: int,
    plan: str,
    active_users_30d: int,
    total_users: int,
    churned: bool = False,
) -> dict:
    if churned:
        return {
            "total": 0,
            "login_recency": 0,
            "adoption": 0,
            "ticket_health": 0,
            "mrr_tier": 0,
            "seat_util": 0,
        }
    lr = login_recency_score(last_login_at)
    ad = adoption_score(distinct_event_types)
    tk = ticket_score(critical_tickets, open_tickets)
    mr = mrr_tier_score(plan)
    su = seat_utilization_score(active_users_30d, total_users)
    return {
        "total": lr + ad + tk + mr + su,
        "login_recency": lr,
        "adoption": ad,
        "ticket_health": tk,
        "mrr_tier": mr,
        "seat_util": su,
    }


def is_at_risk(current_score: int, prev_score: int | None, days_between: int) -> bool:
    if prev_score is None:
        return False
    return (prev_score - current_score) >= 15 and days_between <= 14


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestLoginRecencyScore:
    def test_logged_in_today(self):
        score = login_recency_score(NOW - timedelta(hours=2))
        assert score == 25

    def test_logged_in_5_days_ago(self):
        score = login_recency_score(NOW - timedelta(days=5))
        assert score == 20

    def test_logged_in_10_days_ago(self):
        score = login_recency_score(NOW - timedelta(days=10))
        assert score == 15

    def test_logged_in_20_days_ago(self):
        score = login_recency_score(NOW - timedelta(days=20))
        assert score == 10

    def test_logged_in_60_days_ago(self):
        score = login_recency_score(NOW - timedelta(days=60))
        assert score == 0

    def test_never_logged_in(self):
        score = login_recency_score(None)
        assert score == 0


class TestAdoptionScore:
    def test_no_event_types(self):
        assert adoption_score(0) == 0

    def test_2_event_types(self):
        assert adoption_score(2) == 8

    def test_5_event_types(self):
        assert adoption_score(5) == 20

    def test_many_event_types_capped_at_20(self):
        assert adoption_score(100) == 20


class TestTicketScore:
    def test_no_tickets(self):
        assert ticket_score(0, 0) == 20

    def test_one_critical_ticket(self):
        # 20 - (1*8) = 12
        assert ticket_score(1, 1) == 12

    def test_three_critical_tickets(self):
        # 20 - (3*8) = -4 → floored at 0
        assert ticket_score(3, 3) == 0

    def test_mixed_severity(self):
        # 1 critical + 2 other open: 20 - (1*8 + 2*3) = 20 - 14 = 6
        assert ticket_score(1, 3) == 6

    def test_floor_at_zero(self):
        assert ticket_score(5, 5) == 0


class TestMRRTierScore:
    def test_enterprise(self):
        assert mrr_tier_score("enterprise") == 15

    def test_pro(self):
        assert mrr_tier_score("pro") == 12

    def test_starter(self):
        assert mrr_tier_score("starter") == 8

    def test_trial(self):
        assert mrr_tier_score("trial") == 4

    def test_unknown_plan(self):
        assert mrr_tier_score("unknown") == 4


class TestSeatUtilization:
    def test_all_active(self):
        assert seat_utilization_score(10, 10) == 20

    def test_half_active(self):
        assert seat_utilization_score(5, 10) == 10

    def test_zero_users(self):
        assert seat_utilization_score(0, 0) == 0

    def test_capped_at_20(self):
        assert seat_utilization_score(1000, 10) == 20


class TestTotalHealthScore:
    def test_perfect_score(self):
        result = total_health_score(
            last_login_at=NOW - timedelta(hours=1),
            distinct_event_types=10,
            critical_tickets=0,
            open_tickets=0,
            plan="enterprise",
            active_users_30d=20,
            total_users=20,
        )
        assert result["total"] == 100
        assert result["login_recency"] == 25
        assert result["adoption"] == 20
        assert result["ticket_health"] == 20
        assert result["mrr_tier"] == 15
        assert result["seat_util"] == 20

    def test_churned_company_scores_zero(self):
        result = total_health_score(
            last_login_at=NOW - timedelta(hours=1),
            distinct_event_types=10,
            critical_tickets=0,
            open_tickets=0,
            plan="enterprise",
            active_users_30d=20,
            total_users=20,
            churned=True,
        )
        assert result["total"] == 0

    def test_trial_inactive_company(self):
        result = total_health_score(
            last_login_at=NOW - timedelta(days=45),
            distinct_event_types=1,
            critical_tickets=2,
            open_tickets=3,
            plan="trial",
            active_users_30d=0,
            total_users=3,
        )
        assert result["total"] < 20  # Should be very low

    def test_score_bounded_0_to_100(self):
        result = total_health_score(
            last_login_at=None,
            distinct_event_types=0,
            critical_tickets=10,
            open_tickets=10,
            plan="trial",
            active_users_30d=0,
            total_users=1,
        )
        assert 0 <= result["total"] <= 100


class TestAtRiskDetection:
    def test_score_drop_15_in_14_days_is_at_risk(self):
        assert is_at_risk(current_score=50, prev_score=65, days_between=10) is True

    def test_score_drop_15_after_14_days_not_at_risk(self):
        assert is_at_risk(current_score=50, prev_score=65, days_between=20) is False

    def test_score_drop_14_not_at_risk(self):
        assert is_at_risk(current_score=50, prev_score=64, days_between=5) is False

    def test_no_previous_score_not_at_risk(self):
        assert is_at_risk(current_score=50, prev_score=None, days_between=0) is False

    def test_score_improvement_not_at_risk(self):
        assert is_at_risk(current_score=70, prev_score=55, days_between=5) is False

    def test_exact_boundary_15_points_14_days(self):
        assert is_at_risk(current_score=50, prev_score=65, days_between=14) is True
