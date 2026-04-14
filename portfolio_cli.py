#!/usr/bin/env python3
"""AD Capital v0 — Portfolio management CLI.

Designed to be called by Claude Code for paper trading operations.
All research and trading decisions are made by Claude Code using web search;
this CLI handles portfolio state, trade logging, and reporting.

Usage:
    # Portfolio status
    python portfolio_cli.py status

    # Execute a paper trade
    python portfolio_cli.py trade BUY AVGO 50 234.50 "Strong AI chip demand catalyst"

    # Daily snapshot (update all prices + P&L)
    python portfolio_cli.py snapshot

    # Sector breakdown
    python portfolio_cli.py sectors

    # Trade history
    python portfolio_cli.py history [--last N]

    # S&P 500 watchlist: sector movers and opportunities
    python portfolio_cli.py watchlist [--sector Technology]

    # Generate daily report
    python portfolio_cli.py report

    # Initialize portfolio (first time only)
    python portfolio_cli.py init
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from src.portfolio import PortfolioManager
from src.sectors import SectorTracker, normalize_sector
from src.data_sources import get_batch_prices, get_stock_info
from src.report import generate_daily_report
from src.watchlist import SP500Watchlist


def load_config() -> dict:
    with open(Path(__file__).parent / "config.yaml") as f:
        return yaml.safe_load(f)


def cmd_init(args, config):
    """Initialize a fresh portfolio."""
    pm = PortfolioManager(config)
    state = pm.load()
    if state["positions"] and not args.force:
        print(f"Portfolio already exists with {len(state['positions'])} positions.")
        print(f"Total value: ${state['total_value']:,.2f}")
        print("Use --force to reinitialize (WARNING: destroys existing portfolio)")
        return
    state = {
        "inception_date": date.today().isoformat(),
        "cash": config["portfolio"]["initial_capital"],
        "positions": {},
        "total_value": config["portfolio"]["initial_capital"],
        "benchmark_start_price": None,
    }
    # Get benchmark start price
    prices = get_batch_prices([config["portfolio"]["benchmark"]])
    if config["portfolio"]["benchmark"] in prices:
        state["benchmark_start_price"] = prices[config["portfolio"]["benchmark"]]
    pm.save(state)
    pm.save_trades([])
    print(f"Portfolio initialized: ${config['portfolio']['initial_capital']:,.2f} cash")
    print(f"Benchmark: {config['portfolio']['benchmark']} @ ${state['benchmark_start_price']}")
    print(f"Inception: {state['inception_date']}")


def cmd_status(args, config):
    """Show current portfolio status."""
    pm = PortfolioManager(config)
    state = pm.load()
    if not state.get("inception_date"):
        print("No portfolio yet. Run: python portfolio_cli.py init")
        return

    # Update prices
    tickers = list(state["positions"].keys())
    prices = get_batch_prices(tickers + [config["portfolio"]["benchmark"]])

    total = state["cash"]
    print(f"\n{'='*70}")
    print(f" AD Capital v0 — Portfolio Status ({date.today().isoformat()})")
    print(f"{'='*70}")
    print(f" Inception: {state.get('inception_date', '?')}")
    print()

    if state["positions"]:
        print(f" {'Ticker':<8} {'Shares':>8} {'Price':>10} {'AvgCost':>10} "
              f"{'Value':>12} {'P&L':>10} {'Weight':>8} {'Sector'}")
        print(f" {'-'*8} {'-'*8} {'-'*10} {'-'*10} {'-'*12} {'-'*10} {'-'*8} {'-'*15}")

        holdings = []
        for ticker, pos in state["positions"].items():
            price = prices.get(ticker, pos.get("last_price", 0))
            value = pos["shares"] * price
            pnl = (price - pos["avg_cost"]) * pos["shares"]
            total += value
            holdings.append((ticker, pos, price, value, pnl))

        holdings.sort(key=lambda x: x[3], reverse=True)
        for ticker, pos, price, value, pnl in holdings:
            weight = (value / total * 100) if total > 0 else 0
            pnl_str = f"${pnl:+,.2f}"
            avg_cost = pos['avg_cost']
            price_str = f"${price:,.2f}"
            cost_str = f"${avg_cost:,.2f}"
            val_str = f"${value:,.2f}"
            wt_str = f"{weight:.1f}%"
            sector = pos.get('sector', '?')
            print(f" {ticker:<8} {pos['shares']:>8.2f} {price_str:>10} "
                  f"{cost_str:>10} {val_str:>12} "
                  f"{pnl_str:>10} {wt_str:>8} {sector}")

    print()
    bench_price = prices.get(config["portfolio"]["benchmark"], 0)
    bench_start = state.get("benchmark_start_price", bench_price)
    bench_ret = ((bench_price / bench_start) - 1) * 100 if bench_start and bench_start > 0 else 0
    port_ret = ((total / config["portfolio"]["initial_capital"]) - 1) * 100

    print(f" Cash:              ${state['cash']:>12,.2f}")
    print(f" Total Value:       ${total:>12,.2f}")
    print(f" Portfolio Return:  {port_ret:>+11.2f}%")
    print(f" SPY Return:        {bench_ret:>+11.2f}%")
    print(f" Alpha:             {port_ret - bench_ret:>+11.2f}%")
    print(f" Positions:         {len(state['positions']):>12}")
    print(f"{'='*70}\n")


def cmd_trade(args, config):
    """Execute a paper trade."""
    pm = PortfolioManager(config)
    state = pm.load()
    trades = pm.load_trades()

    ticker = args.ticker.upper()
    action = args.action.upper()
    shares = float(args.shares)
    rationale = args.rationale

    # Get price: use provided price or fetch current
    if args.price:
        price = float(args.price)
    else:
        prices = get_batch_prices([ticker])
        price = prices.get(ticker)
        if not price:
            print(f"ERROR: Could not fetch price for {ticker}. Provide --price manually.")
            return

    # Get sector info
    sector = "Unknown"
    if ticker in state["positions"]:
        sector = state["positions"][ticker].get("sector", "Unknown")
    else:
        info = get_stock_info(ticker)
        if info:
            sector = normalize_sector(info.get("sector", "Unknown"))

    value = shares * price
    now = datetime.now()

    if action == "BUY":
        if value > state["cash"]:
            max_shares = state["cash"] / price
            print(f"WARNING: Insufficient cash (${state['cash']:,.2f}). Max {max_shares:.2f} shares.")
            if shares > max_shares:
                shares = max_shares
                value = shares * price

        state["cash"] -= value
        old = state["positions"].get(ticker, {"shares": 0, "avg_cost": price, "sector": sector})
        new_shares = old["shares"] + shares
        avg_cost = ((old["avg_cost"] * old["shares"]) + value) / new_shares if new_shares > 0 else price
        state["positions"][ticker] = {
            "shares": round(new_shares, 6),
            "avg_cost": round(avg_cost, 4),
            "last_price": price,
            "sector": sector,
            "target_weight": 0,
        }

    elif action == "SELL":
        pos = state["positions"].get(ticker)
        if not pos:
            print(f"ERROR: No position in {ticker}")
            return
        if shares > pos["shares"]:
            print(f"WARNING: Only have {pos['shares']:.2f} shares. Selling all.")
            shares = pos["shares"]
            value = shares * price

        state["cash"] += value
        remaining = pos["shares"] - shares
        pnl = (price - pos["avg_cost"]) * shares
        if remaining < 0.001:
            del state["positions"][ticker]
        else:
            pos["shares"] = round(remaining, 6)
            pos["last_price"] = price

    else:
        print(f"ERROR: Unknown action '{action}'. Use BUY or SELL.")
        return

    # Log trade with full rationale
    trade_record = {
        "id": len(trades) + 1,
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "ticker": ticker,
        "action": action,
        "shares": round(shares, 6),
        "price": price,
        "value": round(value, 2),
        "sector": sector,
        "rationale": rationale,
        "conviction": args.conviction or "MEDIUM",
        "catalyst": args.catalyst or "research",
        "portfolio_cash_after": round(state["cash"], 2),
        "portfolio_value_after": None,  # filled below
    }

    # Update total value
    all_tickers = list(state["positions"].keys())
    prices_map = get_batch_prices(all_tickers) if all_tickers else {}
    total = state["cash"]
    for t, p in state["positions"].items():
        px = prices_map.get(t, p.get("last_price", 0))
        p["last_price"] = px
        total += p["shares"] * px
    state["total_value"] = round(total, 2)
    trade_record["portfolio_value_after"] = state["total_value"]

    trades.append(trade_record)
    pm.save(state)
    pm.save_trades(trades)

    pnl_str = ""
    if action == "SELL":
        pnl_str = f" | P&L: ${pnl:+,.2f}"

    print(f"\n  TRADE #{trade_record['id']} EXECUTED")
    print(f"  {action} {shares:.2f} {ticker} @ ${price:,.2f} = ${value:,.2f}{pnl_str}")
    print(f"  Sector: {sector} | Conviction: {trade_record['conviction']}")
    print(f"  Rationale: {rationale}")
    print(f"  Cash remaining: ${state['cash']:,.2f} | Portfolio: ${state['total_value']:,.2f}\n")


def cmd_snapshot(args, config):
    """Take daily snapshot — update all prices and P&L."""
    pm = PortfolioManager(config)
    state = pm.load()
    if not state["positions"]:
        print("No positions to snapshot.")
        return

    tracker = SectorTracker()
    snap = pm.snapshot(state)
    sector_perf = tracker.record(snap)

    print(f"\nSnapshot taken: ${snap['total_value']:,.2f}")
    print(f"Return: {snap['portfolio_return_pct']:+.2f}% vs SPY {snap['benchmark_return_pct']:+.2f}%")
    print(f"Alpha: {snap['alpha']:+.2f}%")


def cmd_sectors(args, config):
    """Show sector allocation and performance."""
    pm = PortfolioManager(config)
    state = pm.load()
    if not state["positions"]:
        print("No positions.")
        return

    tickers = list(state["positions"].keys())
    prices = get_batch_prices(tickers) if tickers else {}

    total = state["cash"]
    sectors: dict[str, dict] = {}
    for ticker, pos in state["positions"].items():
        price = prices.get(ticker, pos.get("last_price", 0))
        value = pos["shares"] * price
        pnl = (price - pos["avg_cost"]) * pos["shares"]
        total += value
        sector = normalize_sector(pos.get("sector", "Unknown"))
        if sector not in sectors:
            sectors[sector] = {"value": 0, "pnl": 0, "tickers": [], "count": 0}
        sectors[sector]["value"] += value
        sectors[sector]["pnl"] += pnl
        sectors[sector]["tickers"].append(ticker)
        sectors[sector]["count"] += 1

    print(f"\n{'='*65}")
    print(f" Sector Allocation ({date.today().isoformat()})")
    print(f"{'='*65}")
    print(f" {'Sector':<25} {'Weight':>8} {'P&L':>12} {'#':>3}  Tickers")
    print(f" {'-'*25} {'-'*8} {'-'*12} {'-'*3}  {'-'*20}")
    for sector in sorted(sectors, key=lambda s: sectors[s]["value"], reverse=True):
        s = sectors[sector]
        weight = (s["value"] / total * 100) if total > 0 else 0
        tks = ", ".join(s["tickers"])
        pnl_str = f"${s['pnl']:+,.2f}"
        print(f" {sector:<25} {weight:>7.1f}% {pnl_str:>12} {s['count']:>3}  {tks}")
    print(f"{'='*65}\n")


def cmd_history(args, config):
    """Show trade history."""
    pm = PortfolioManager(config)
    trades = pm.load_trades()
    if not trades:
        print("No trades yet.")
        return

    last_n = args.last or len(trades)
    recent = trades[-last_n:]

    print(f"\n{'='*90}")
    print(f" Trade History (showing last {len(recent)} of {len(trades)})")
    print(f"{'='*90}")
    for t in recent:
        pnl_note = ""
        print(f"\n  #{t.get('id', '?')} | {t['date']} {t.get('time', '')} | "
              f"{t['action']} {t['shares']:.2f} {t['ticker']} @ ${t['price']:,.2f} = ${t['value']:,.2f}")
        print(f"     Sector: {t.get('sector', '?')} | Conviction: {t.get('conviction', '?')} | "
              f"Catalyst: {t.get('catalyst', '?')}")
        print(f"     Rationale: {t.get('rationale', 'N/A')}")
    print(f"\n{'='*90}\n")


def cmd_watchlist(args, config):
    """Show S&P 500 watchlist with sector movers."""
    wl = SP500Watchlist()
    if args.sector:
        wl.show_sector(args.sector)
    elif args.scan:
        wl.scan_movers()
    else:
        wl.show_summary()


def cmd_report(args, config):
    """Generate daily markdown report."""
    pm = PortfolioManager(config)
    state = pm.load()
    if not state["positions"]:
        print("No positions to report on.")
        return

    snap = pm.snapshot(state)
    SectorTracker().record(snap)

    # Get today's trades for the report
    trades = pm.load_trades()
    today_str = date.today().isoformat()
    today_trades = [t for t in trades if t.get("date") == today_str]

    report = generate_daily_report(
        snap,
        day_trades=today_trades,
        reports_dir=config["data"]["reports_dir"],
    )
    print(report)


def main():
    parser = argparse.ArgumentParser(description="AD Capital v0 — Portfolio CLI")
    sub = parser.add_subparsers(dest="command", help="Command")

    # init
    p_init = sub.add_parser("init", help="Initialize portfolio")
    p_init.add_argument("--force", action="store_true")

    # status
    sub.add_parser("status", help="Show portfolio status")

    # trade
    p_trade = sub.add_parser("trade", help="Execute a paper trade")
    p_trade.add_argument("action", choices=["BUY", "SELL", "buy", "sell"])
    p_trade.add_argument("ticker")
    p_trade.add_argument("shares", type=float)
    p_trade.add_argument("--price", type=float, help="Price (fetched if omitted)")
    p_trade.add_argument("rationale", help="Trading rationale")
    p_trade.add_argument("--conviction", choices=["LOW", "MEDIUM", "HIGH"],
                         default="MEDIUM")
    p_trade.add_argument("--catalyst", default="research",
                         help="Catalyst type: research, news, earnings, macro, technical, sector_rotation")

    # snapshot
    sub.add_parser("snapshot", help="Update prices and take daily snapshot")

    # sectors
    sub.add_parser("sectors", help="Show sector allocation")

    # history
    p_hist = sub.add_parser("history", help="Show trade history")
    p_hist.add_argument("--last", type=int, help="Show last N trades")

    # watchlist
    p_watch = sub.add_parser("watchlist", help="S&P 500 watchlist")
    p_watch.add_argument("--sector", help="Filter by sector")
    p_watch.add_argument("--scan", action="store_true", help="Scan for movers")

    # report
    sub.add_parser("report", help="Generate daily report")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    config = load_config()

    commands = {
        "init": cmd_init,
        "status": cmd_status,
        "trade": cmd_trade,
        "snapshot": cmd_snapshot,
        "sectors": cmd_sectors,
        "history": cmd_history,
        "watchlist": cmd_watchlist,
        "report": cmd_report,
    }
    commands[args.command](args, config)


if __name__ == "__main__":
    main()
