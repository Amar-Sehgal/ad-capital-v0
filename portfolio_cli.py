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
from src.quant import (
    QuantBook, compute_signals, scan_signals, STRATEGIES,
    multifactor_v3_score, trend_pullback_signal, scan_multifactor_v3,
)
from src.data_sources import get_batch_prices, get_stock_info
from src.report import generate_daily_report
from src.watchlist import SP500Watchlist
from src.session import (
    load_session, save_session, reset_session, record_loop,
    get_phase, get_loop_interval_seconds,
    save_research, load_research, load_all_research,
    save_idea, load_ideas, get_unexecuted_ideas, mark_idea_executed,
    now_pt,
)


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
    sect_capital = config["portfolio"].get("sector_fund_capital", config["portfolio"]["initial_capital"] * 0.7)
    quant_capital = config["portfolio"].get("quant_book_capital", config["portfolio"]["initial_capital"] * 0.3)

    state = {
        "inception_date": date.today().isoformat(),
        "cash": sect_capital,
        "positions": {},
        "total_value": sect_capital,
        "benchmark_start_price": None,
    }
    # Get benchmark start price
    prices = get_batch_prices([config["portfolio"]["benchmark"]])
    if config["portfolio"]["benchmark"] in prices:
        state["benchmark_start_price"] = prices[config["portfolio"]["benchmark"]]
    pm.save(state)
    pm.save_trades([])

    # Initialize quant book
    qb = QuantBook(capital_allocation=quant_capital)
    qb_state = {
        "inception_date": date.today().isoformat(),
        "cash": quant_capital,
        "positions": {},
        "total_value": quant_capital,
        "realized_pnl": 0,
        "total_trades": 0,
        "winning_trades": 0,
    }
    qb.save(qb_state)
    qb.save_trades([])

    print(f"Portfolio initialized: ${config['portfolio']['initial_capital']:,.2f} total")
    print(f"  Sector Fund: ${sect_capital:,.2f}")
    print(f"  Quant Book:  ${quant_capital:,.2f}")
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


def cmd_session(args, config):
    """Show or manage trading session state."""
    if args.reset:
        state = reset_session()
        print(f"Session reset. Phase: {state['phase']}")
        return

    state = load_session()
    phase = get_phase()
    interval = get_loop_interval_seconds(phase)
    t = now_pt().strftime("%I:%M %p PT")

    print(f"\n{'='*60}")
    print(f" Trading Session — {t}")
    print(f"{'='*60}")
    print(f" Phase:              {phase}")
    print(f" Loop interval:      {interval}s ({interval//60}m)")
    print(f" Session started:    {state.get('started_at', 'not started')}")
    print(f" Loop count:         {state['loop_count']}")
    print(f" Sectors researched: {len(state['sectors_researched'])}/11")
    print(f"   Done: {', '.join(state['sectors_researched']) or 'none'}")
    remaining = [s for s in state['research_queue'] if s not in state['sectors_researched']]
    print(f"   Queue: {', '.join(remaining) or 'all done'}")
    print(f" Ideas generated:    {state['ideas_generated']}")
    print(f" Trades today:       {state['trades_executed_today']}")

    if state["notes"]:
        print(f"\n Last 5 actions:")
        for n in state["notes"][-5:]:
            print(f"   [{n['time']}] {n['phase']}: {n['action']}")
    print(f"{'='*60}\n")


def cmd_research(args, config):
    """View accumulated research notes."""
    if args.sector:
        content = load_research(args.sector)
        if content:
            print(content)
        else:
            print(f"No research for '{args.sector}' today.")
        return

    all_research = load_all_research()
    if not all_research:
        print("No research notes today.")
        return

    for sector, content in sorted(all_research.items()):
        lines = content.strip().split('\n')
        # Show header + first few lines
        print(f"\n  {sector} ({len(lines)} lines)")
        for line in lines[:3]:
            if line.strip():
                print(f"    {line.strip()}")
    print(f"\n  Total: {len(all_research)} sectors researched")
    print(f"  Use --sector <name> for full notes\n")


def cmd_ideas(args, config):
    """View trade ideas."""
    if args.unexecuted:
        ideas = get_unexecuted_ideas()
        label = "Unexecuted"
    else:
        ideas = load_ideas()
        label = "All"

    if not ideas:
        print("No trade ideas today.")
        return

    print(f"\n{'='*70}")
    print(f" {label} Trade Ideas — {date.today().isoformat()}")
    print(f"{'='*70}")
    for i in ideas:
        exec_mark = " [DONE]" if i.get("executed") else ""
        print(f"\n  #{i['id']}{exec_mark} | {i.get('action', '?')} {i.get('ticker', '?')} | "
              f"{i.get('sector', '?')} | Conv: {i.get('conviction', '?')}")
        print(f"  Thesis: {i.get('thesis', 'N/A')}")
        if i.get("catalysts"):
            print(f"  Catalysts: {', '.join(i['catalysts'])}")
        if i.get("target_weight"):
            print(f"  Target: {i['target_weight']}% of portfolio")
    print(f"\n{'='*70}\n")


def cmd_quant(args, config):
    """Quant book operations."""
    qb = QuantBook(capital_allocation=config["portfolio"]["initial_capital"] * 0.3)

    if args.quant_action == "status":
        print(qb.status())

    elif args.quant_action == "trade":
        state = qb.trade(
            ticker=args.ticker.upper(),
            action=args.action.upper(),
            shares=float(args.shares),
            price=float(args.price) if args.price else None,
            strategy=args.strategy,
            signal=args.signal or "",
            entry_rule=args.entry_rule or "",
            exit_rule=args.exit_rule or "",
        )
        print(f"  Quant trade executed: {args.action.upper()} {args.shares} {args.ticker.upper()}")

    elif args.quant_action == "signals":
        ticker = args.ticker.upper()
        sigs = compute_signals(ticker)
        if sigs.get("error"):
            print(f"Error: {sigs['error']}")
            return
        print(f"\n  {ticker} @ ${sigs['price']}")
        print(f"  Daily: {sigs['daily_return_pct']:+.2f}% | RSI: {sigs['rsi']:.1f} | "
              f"MACD: {sigs['macd']:.3f}")
        print(f"  BB: {sigs['bb_pct']:.3f} (${sigs['bb_lower']}-${sigs['bb_upper']})")
        print(f"  SMA20: ${sigs['sma_20']} | SMA50: {sigs.get('sma_50') or 'N/A'}")
        print(f"  Volume ratio: {sigs['volume_ratio']:.2f}x | Gap: {sigs['gap_pct']:+.2f}%")
        print(f"  ATR: ${sigs['atr']} ({sigs['atr_pct']:.2f}%)")
        if sigs["signals"]:
            print(f"  SIGNALS: {', '.join(sigs['signals'])}")
        else:
            print(f"  No active signals")
        print()

    elif args.quant_action == "scan":
        tickers = args.tickers.split(",") if args.tickers else []
        if not tickers:
            # Default: scan current sector fund holdings + quant holdings
            pm = PortfolioManager(config)
            pstate = pm.load()
            qstate = qb.load()
            tickers = list(set(
                list(pstate.get("positions", {}).keys()) +
                list(qstate.get("positions", {}).keys())
            ))
        if not tickers:
            print("No tickers to scan. Provide --tickers or have open positions.")
            return
        print(f"Scanning {len(tickers)} tickers for signals...")
        results = scan_signals(tickers, signal_filter=args.filter)
        if not results:
            print("No signals found.")
            return
        for s in results:
            sigs_str = ", ".join(s["signals"])
            print(f"  {s['ticker']:<8} ${s['price']:>8.2f} ({s['daily_return_pct']:+.2f}%) "
                  f"RSI:{s['rsi']:.0f} Vol:{s['volume_ratio']:.1f}x -> {sigs_str}")

    elif args.quant_action == "strategies":
        print(f"\n{'='*60}")
        print(f" Registered Quant Strategies")
        print(f"{'='*60}")
        for key, strat in STRATEGIES.items():
            print(f"\n  {strat['name']} [{key}]")
            print(f"  {strat['description']}")
            print(f"  Holding: {strat['holding_period']}")
            print(f"  Signals: {', '.join(strat['signals'])}")
        print(f"\n{'='*60}\n")

    elif args.quant_action == "mfv3":
        # MultiFactorV3 scoring
        if args.ticker:
            r = multifactor_v3_score(args.ticker.upper())
            if not r:
                print(f"No data for {args.ticker.upper()}")
                return
            f = r["factors"]
            print(f"\n  MultiFactorV3: {r['ticker']} @ ${r['price']}")
            print(f"  Score: {r['score']}/{r['max_score']} -> {r['signal'].upper()}")
            print(f"  Factors:")
            print(f"    SMA(10/30):  {f['sma_10_30']}")
            print(f"    SMA(20/60):  {f['sma_20_60']}")
            print(f"    SMA(40/100): {f['sma_40_100']}")
            print(f"    RSI:         {f['rsi']} ({f['rsi_zone']})")
            print(f"    Volume:      {f['volume_ratio']}x avg ({'elevated' if f['volume_elevated'] else 'normal'})")
            print(f"    ADX:         {f['adx']} ({'trending' if f['trending'] else 'not trending'})")
            print()
        elif args.tickers:
            tickers = args.tickers.split(",")
            results = scan_multifactor_v3(tickers)
            for r in results:
                sig = r["signal"].replace("mfv3_", "").upper()
                print(f"  {r['ticker']:<8} ${r['price']:>8.2f}  Score: {r['score']}/6  -> {sig}")
        else:
            print("Provide --ticker AVGO or --tickers AVGO,NVDA,TSM")

    elif args.quant_action == "pullback":
        if not args.ticker:
            print("Provide --ticker")
            return
        r = trend_pullback_signal(args.ticker.upper())
        if not r:
            print(f"No data for {args.ticker.upper()}")
            return
        print(f"\n  TrendPullback: {r['ticker']} @ ${r['price']}")
        print(f"  Signal: {r['signal'].upper()}")
        print(f"  SMA(20): ${r['sma_20']} | SMA(50): ${r['sma_50']}")
        print(f"  ADX: {r['adx']} | ATR: ${r['atr']}")
        print(f"  Chandelier Stop: ${r['chandelier_stop']}")
        print(f"  Uptrend: {r['in_uptrend']} | At Pullback: {r['at_pullback']}")
        print()

    elif args.quant_action == "history":
        trades = qb.load_trades()
        if not trades:
            print("No quant trades yet.")
            return
        last_n = args.last or len(trades)
        recent = trades[-last_n:]
        print(f"\n{'='*80}")
        print(f" Quant Trade History (last {len(recent)} of {len(trades)})")
        print(f"{'='*80}")
        for t in recent:
            pnl_str = f" P&L: ${t['realized_pnl']:+,.2f}" if t.get("realized_pnl") is not None else ""
            print(f"\n  #{t['id']} | {t['date']} {t.get('time', '')} | "
                  f"{t['action']} {t['shares']:.2f} {t['ticker']} @ ${t['price']:,.2f}{pnl_str}")
            print(f"     Strategy: {t.get('strategy', '?')} | Signal: {t.get('signal', '?')}")
            print(f"     Entry: {t.get('entry_rule', '?')} | Exit: {t.get('exit_rule', '?')}")
        print(f"\n{'='*80}\n")


def cmd_overview(args, config):
    """Combined view of sector fund + quant book."""
    pm = PortfolioManager(config)
    sect_state = pm.load()
    qb = QuantBook(capital_allocation=config["portfolio"]["initial_capital"] * 0.3)
    qb_state = qb.load()

    sect_tickers = list(sect_state.get("positions", {}).keys())
    qb_tickers = list(qb_state.get("positions", {}).keys())
    all_tickers = list(set(sect_tickers + qb_tickers + [config["portfolio"]["benchmark"]]))
    prices = get_batch_prices(all_tickers) if all_tickers else {}

    # Sector fund value
    sect_total = sect_state.get("cash", 0)
    for t, p in sect_state.get("positions", {}).items():
        sect_total += p["shares"] * prices.get(t, p.get("last_price", 0))

    # Quant book value
    qb_total = qb_state.get("cash", 0)
    for t, p in qb_state.get("positions", {}).items():
        qb_total += p["shares"] * prices.get(t, p.get("last_price", 0))

    combined = sect_total + qb_total
    initial = config["portfolio"]["initial_capital"]
    sect_capital = config["portfolio"].get("sector_fund_capital", initial * 0.7)
    quant_capital = config["portfolio"].get("quant_book_capital", initial * 0.3)
    combined_ret = ((combined / initial) - 1) * 100
    sect_ret = ((sect_total / sect_capital) - 1) * 100 if sect_capital > 0 else 0
    qb_ret = ((qb_total / quant_capital) - 1) * 100 if quant_capital > 0 else 0

    bench = prices.get(config["portfolio"]["benchmark"], 0)
    bench_start = sect_state.get("benchmark_start_price", bench)
    bench_ret = ((bench / bench_start) - 1) * 100 if bench_start and bench_start > 0 else 0

    print(f"\n{'='*60}")
    print(f" AD Capital v0 — Combined Overview ({date.today().isoformat()})")
    print(f"{'='*60}")
    print(f"")
    print(f" {'Book':<20} {'Value':>12} {'Return':>10} {'Positions':>10}")
    print(f" {'-'*20} {'-'*12} {'-'*10} {'-'*10}")
    print(f" {'Sector Fund':<20} ${sect_total:>10,.2f} {sect_ret:>+9.2f}% "
          f"{len(sect_state.get('positions', {})):>10}")
    print(f" {'Quant Book':<20} ${qb_total:>10,.2f} {qb_ret:>+9.2f}% "
          f"{len(qb_state.get('positions', {})):>10}")
    print(f" {'-'*20} {'-'*12} {'-'*10} {'-'*10}")
    print(f" {'COMBINED':<20} ${combined:>10,.2f} {combined_ret:>+9.2f}% "
          f"{len(sect_state.get('positions', {})) + len(qb_state.get('positions', {})):>10}")
    print(f"")
    print(f" SPY Benchmark:     {bench_ret:>+10.2f}%")
    print(f" Alpha:             {combined_ret - bench_ret:>+10.2f}%")

    win_rate = 0
    total_trades = qb_state.get("total_trades", 0)
    if total_trades > 0:
        win_rate = qb_state.get("winning_trades", 0) / total_trades * 100
    print(f"")
    print(f" Quant Win Rate:    {win_rate:>9.1f}% ({qb_state.get('winning_trades', 0)}/{total_trades})")
    print(f" Quant Realized:    ${qb_state.get('realized_pnl', 0):>+10,.2f}")
    print(f"{'='*60}\n")


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

    # session
    p_sess = sub.add_parser("session", help="Show/manage trading session")
    p_sess.add_argument("--reset", action="store_true", help="Reset session for today")

    # research
    p_research = sub.add_parser("research", help="View research notes")
    p_research.add_argument("--sector", help="View specific sector research")

    # ideas
    p_ideas = sub.add_parser("ideas", help="View trade ideas")
    p_ideas.add_argument("--unexecuted", action="store_true", help="Show only unexecuted ideas")

    # quant
    p_quant = sub.add_parser("quant", help="Quant book operations")
    q_sub = p_quant.add_subparsers(dest="quant_action")
    q_sub.add_parser("status", help="Show quant book status")
    q_sub.add_parser("strategies", help="List registered strategies")
    q_trade = q_sub.add_parser("trade", help="Execute a quant trade")
    q_trade.add_argument("action", choices=["BUY", "SELL", "buy", "sell"])
    q_trade.add_argument("ticker")
    q_trade.add_argument("shares", type=float)
    q_trade.add_argument("--price", type=float, required=True)
    q_trade.add_argument("--strategy", required=True, help="Strategy name (e.g., mean_reversion)")
    q_trade.add_argument("--signal", help="Signal that triggered the trade")
    q_trade.add_argument("--entry-rule", help="Entry rule description")
    q_trade.add_argument("--exit-rule", help="Exit rule description")
    q_signals = q_sub.add_parser("signals", help="Compute signals for a ticker")
    q_signals.add_argument("ticker")
    q_scan = q_sub.add_parser("scan", help="Scan tickers for signals")
    q_scan.add_argument("--tickers", help="Comma-separated tickers (default: current holdings)")
    q_scan.add_argument("--filter", help="Filter by signal name")
    q_mfv3 = q_sub.add_parser("mfv3", help="MultiFactorV3 scoring (ported from stock_prediction)")
    q_mfv3.add_argument("--ticker", help="Score single ticker")
    q_mfv3.add_argument("--tickers", help="Comma-separated tickers to batch score")
    q_pb = q_sub.add_parser("pullback", help="TrendPullback analysis (ported from stock_prediction)")
    q_pb.add_argument("--ticker", required=True, help="Ticker to analyze")
    q_hist = q_sub.add_parser("history", help="Show quant trade history")
    q_hist.add_argument("--last", type=int, help="Show last N trades")

    # overview (combined view)
    sub.add_parser("overview", help="Combined sector fund + quant book view")

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
        "session": cmd_session,
        "research": cmd_research,
        "ideas": cmd_ideas,
        "quant": cmd_quant,
        "overview": cmd_overview,
        "report": cmd_report,
    }
    commands[args.command](args, config)


if __name__ == "__main__":
    main()
