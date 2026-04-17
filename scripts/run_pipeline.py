#!/usr/bin/env python3
"""
scripts/run_pipeline.py

One-shot convenience script that runs the full pipeline in order:
  1. Sync source → sidecar
  2. Score all accounts
  3. Generate AI summaries
  4. Scan for at-risk alerts

Useful for nightly cron or initial bootstrap.
Run: python scripts/run_pipeline.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import logging
from datetime import datetime, timezone

from sync.sync_engine import run_sync
from scoring.scorer import run_scoring
from ai.summarizer import generate_summaries
from alerts.monitor import run_alert_scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def main():
    started = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info("🚀 CRM Sidecar Full Pipeline")
    log.info("=" * 60)

    results = {}

    log.info("\n📦 Step 1/4: Syncing source → sidecar...")
    results["sync"] = run_sync()

    log.info("\n📊 Step 2/4: Computing health scores...")
    results["scoring"] = run_scoring()

    log.info("\n🤖 Step 3/4: Generating AI summaries...")
    results["summaries"] = generate_summaries()

    log.info("\n🔔 Step 4/4: Scanning for at-risk alerts...")
    results["alerts"] = run_alert_scan()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info("\n" + "=" * 60)
    log.info("✅ Pipeline complete in %.1fs", elapsed)
    log.info("   Companies scored:  %d", results["scoring"].get("companies_scored", 0))
    log.info("   At-risk accounts:  %d", results["scoring"].get("at_risk_count", 0))
    log.info("   Summaries generated: %d", results["summaries"].get("generated", 0))
    log.info("   Summaries cached:    %d", results["summaries"].get("skipped_cached", 0))
    log.info("   New alerts logged:   %d", results["alerts"].get("new_alerts_logged", 0))
    log.info("   Est. AI cost: $%.4f", results["summaries"].get("estimated_cost_usd", 0))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
