#!/usr/bin/env python3
"""Adversarial Alpha — main pipeline runner.

Usage:
    # Full pipeline (screening + research + modeling + portfolio construction)
    python run_pipeline.py

    # Daily snapshot only (just update prices and P&L, no rebalance)
    python run_pipeline.py --snapshot-only

    # Intraday monitoring (check for day trades during market hours)
    python run_pipeline.py --intraday

    # Force rebalance even if portfolio already exists
    python run_pipeline.py --force-rebalance
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import yaml

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent))

from src.universe import get_universe
from src.screener import screen_universe
from src.research import run_adversarial_research
from src.modeler import build_scenarios
from src.optimizer import construct_portfolio
from src.portfolio import PortfolioManager
from src.sectors import SectorTracker
from src.report import generate_daily_report
from src.day_trader import evaluate_day_trades, execute_day_trades, _is_market_hours

log = logging.getLogger("adversarial_alpha")


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


async def run_full_pipeline(config: dict, force: bool = False):
    """Run the complete 4-stage pipeline and rebalance portfolio."""
    pm = PortfolioManager(config)
    state = pm.load()
    tracker = SectorTracker()

    # Check if we already have a portfolio and aren't forcing
    if state["positions"] and not force:
        log.info("Portfolio exists with %d positions. Use --force-rebalance to rebuild.",
                 len(state["positions"]))
        log.info("Running daily snapshot instead...")
        snap = pm.snapshot(state)
        tracker.record(snap)
        report = generate_daily_report(snap, reports_dir=config["data"]["reports_dir"])
        print(report)
        return

    pipeline_cfg = config["pipeline"]
    model_cfg = config["model"]

    # Stage 1: Screen universe
    log.info("=" * 60)
    log.info("STAGE 1: Screening universe")
    log.info("=" * 60)
    universe = get_universe()
    candidates = await screen_universe(
        universe,
        num_candidates=pipeline_cfg["num_screen_candidates"],
        model=model_cfg["screener"],
    )

    # Stage 2: Adversarial research
    log.info("=" * 60)
    log.info("STAGE 2: Adversarial research (%d candidates)", len(candidates))
    log.info("=" * 60)
    research = await run_adversarial_research(
        candidates,
        model=model_cfg["research"],
        max_concurrent=pipeline_cfg["max_concurrent_agents"],
        news_days=pipeline_cfg["news_lookback_days"],
    )

    # Stage 3: Scenario modeling
    log.info("=" * 60)
    log.info("STAGE 3: Scenario modeling")
    log.info("=" * 60)
    scenarios = await build_scenarios(
        candidates,
        research,
        model=model_cfg["modeler"],
        max_concurrent=pipeline_cfg["max_concurrent_agents"],
    )

    # Stage 4: Portfolio construction
    log.info("=" * 60)
    log.info("STAGE 4: Portfolio optimization")
    log.info("=" * 60)
    portfolio_alloc = await construct_portfolio(
        scenarios,
        research,
        model=model_cfg["optimizer"],
    )

    # Save pipeline artifacts for debugging
    artifacts_dir = f"data/artifacts/{date.today().isoformat()}"
    os.makedirs(artifacts_dir, exist_ok=True)
    with open(f"{artifacts_dir}/candidates.json", "w") as f:
        json.dump(candidates, f, indent=2, default=str)
    with open(f"{artifacts_dir}/research.json", "w") as f:
        json.dump(research, f, indent=2, default=str)
    with open(f"{artifacts_dir}/scenarios.json", "w") as f:
        json.dump(scenarios, f, indent=2, default=str)
    with open(f"{artifacts_dir}/portfolio_allocation.json", "w") as f:
        json.dump(portfolio_alloc, f, indent=2, default=str)

    # Execute rebalance
    log.info("=" * 60)
    log.info("EXECUTING REBALANCE")
    log.info("=" * 60)
    state = pm.rebalance(portfolio_alloc, state)

    # Take snapshot and generate report
    snap = pm.snapshot(state)
    tracker.record(snap)
    report = generate_daily_report(
        snap,
        portfolio_allocation=portfolio_alloc,
        reports_dir=config["data"]["reports_dir"],
    )
    print(report)

    log.info("Pipeline complete. Portfolio: $%.2f across %d positions.",
             state["total_value"], len(state["positions"]))


async def run_snapshot(config: dict):
    """Just update prices and take a daily snapshot."""
    pm = PortfolioManager(config)
    state = pm.load()
    tracker = SectorTracker()

    if not state["positions"]:
        log.warning("No portfolio exists. Run full pipeline first.")
        return

    snap = pm.snapshot(state)
    tracker.record(snap)
    report = generate_daily_report(snap, reports_dir=config["data"]["reports_dir"])
    print(report)


async def run_intraday(config: dict):
    """Evaluate and execute intraday trades during market hours."""
    if not _is_market_hours():
        log.info("Market is closed. Skipping intraday evaluation.")
        return

    pm = PortfolioManager(config)
    state = pm.load()

    if not state["positions"]:
        log.warning("No portfolio exists. Run full pipeline first.")
        return

    model = config["model"].get("research", "claude-sonnet-4-6")
    evaluation = await evaluate_day_trades(state, model=model)

    print(f"\nMarket Assessment: {evaluation.get('market_assessment', 'N/A')}")
    print(f"Recommended trades: {len(evaluation.get('trades', []))}")
    print(f"Watch list: {', '.join(evaluation.get('watch_list', []))}")

    trades = evaluation.get("trades", [])
    if trades:
        print("\nRecommended trades:")
        for t in trades:
            print(f"  [{t['urgency']}] {t['action']} {t['ticker']} "
                  f"({t['pct_of_position']}%) — {t['reason']}")

        # Execute medium+ urgency trades
        executed = await execute_day_trades(evaluation, pm, state, min_urgency="MEDIUM")
        if executed:
            print(f"\nExecuted {len(executed)} day trades.")
            # Update snapshot
            tracker = SectorTracker()
            snap = pm.snapshot()
            tracker.record(snap)
    else:
        print("No action needed.")


def main():
    parser = argparse.ArgumentParser(description="Adversarial Alpha — AI-powered stock portfolio")
    parser.add_argument("--snapshot-only", action="store_true",
                        help="Only take a daily snapshot (no rebalance)")
    parser.add_argument("--intraday", action="store_true",
                        help="Run intraday monitoring and day trading")
    parser.add_argument("--force-rebalance", action="store_true",
                        help="Force full pipeline rebalance even if portfolio exists")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy libraries
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config()

    if args.intraday:
        asyncio.run(run_intraday(config))
    elif args.snapshot_only:
        asyncio.run(run_snapshot(config))
    else:
        asyncio.run(run_full_pipeline(config, force=args.force_rebalance))


if __name__ == "__main__":
    main()
