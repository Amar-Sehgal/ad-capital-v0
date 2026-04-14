"""Paper portfolio manager — track positions, P&L, and daily snapshots."""

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path

from src.data_sources import get_batch_prices

log = logging.getLogger(__name__)


class PortfolioManager:
    """Manages a paper-trading portfolio backed by JSON files."""

    def __init__(self, config: dict):
        self.initial_capital = config["portfolio"]["initial_capital"]
        self.portfolio_file = config["data"]["portfolio_file"]
        self.trades_file = config["data"]["trades_file"]
        self.snapshots_dir = config["data"]["snapshots_dir"]
        self.benchmark = config["portfolio"]["benchmark"]
        os.makedirs(self.snapshots_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.portfolio_file) or ".", exist_ok=True)

    def load(self) -> dict:
        """Load current portfolio state from disk."""
        if os.path.exists(self.portfolio_file):
            with open(self.portfolio_file) as f:
                return json.load(f)
        return {
            "inception_date": date.today().isoformat(),
            "cash": self.initial_capital,
            "positions": {},  # ticker -> {"shares": N, "avg_cost": N, "sector": "..."}
            "total_value": self.initial_capital,
            "benchmark_start_price": None,
        }

    def save(self, state: dict):
        """Persist portfolio state."""
        with open(self.portfolio_file, "w") as f:
            json.dump(state, f, indent=2, default=str)

    def load_trades(self) -> list[dict]:
        """Load trade history."""
        if os.path.exists(self.trades_file):
            with open(self.trades_file) as f:
                return json.load(f)
        return []

    def save_trades(self, trades: list[dict]):
        """Persist trade history."""
        with open(self.trades_file, "w") as f:
            json.dump(trades, f, indent=2, default=str)

    def rebalance(self, target_portfolio: dict, state: dict | None = None) -> dict:
        """Rebalance to match target portfolio allocation.

        target_portfolio: output from optimizer (has 'positions' list with ticker/weight/sector)
        Returns updated state.
        """
        state = state or self.load()
        trades = self.load_trades()

        # Get current prices for all relevant tickers
        target_positions = target_portfolio.get("positions", [])
        all_tickers = list(set(
            [p["ticker"] for p in target_positions] +
            list(state["positions"].keys()) +
            [self.benchmark]
        ))
        prices = get_batch_prices(all_tickers)

        # Calculate current portfolio value
        portfolio_value = state["cash"]
        for ticker, pos in state["positions"].items():
            price = prices.get(ticker, pos.get("last_price", 0))
            portfolio_value += pos["shares"] * price

        if portfolio_value <= 0:
            portfolio_value = self.initial_capital

        # Set benchmark start price on first run
        if state.get("benchmark_start_price") is None and self.benchmark in prices:
            state["benchmark_start_price"] = prices[self.benchmark]

        now = datetime.now().isoformat()

        # Close positions not in target
        target_tickers = {p["ticker"] for p in target_positions}
        for ticker in list(state["positions"].keys()):
            if ticker not in target_tickers:
                pos = state["positions"][ticker]
                price = prices.get(ticker, pos.get("last_price", 0))
                proceeds = pos["shares"] * price
                state["cash"] += proceeds
                trades.append({
                    "timestamp": now,
                    "ticker": ticker,
                    "action": "SELL",
                    "shares": pos["shares"],
                    "price": price,
                    "value": round(proceeds, 2),
                    "reason": "not in target portfolio",
                })
                log.info("SELL %s: %.2f shares @ $%.2f = $%.2f",
                         ticker, pos["shares"], price, proceeds)
                del state["positions"][ticker]

        # Rebalance existing and open new positions
        for tp in target_positions:
            ticker = tp["ticker"]
            target_value = portfolio_value * (tp["weight"] / 100)
            price = prices.get(ticker)
            if not price or price <= 0:
                log.warning("No price for %s, skipping", ticker)
                continue

            current_shares = state["positions"].get(ticker, {}).get("shares", 0)
            current_value = current_shares * price
            delta_value = target_value - current_value
            delta_shares = delta_value / price

            if abs(delta_value) < portfolio_value * 0.005:
                # Skip tiny rebalances (<0.5% of portfolio)
                if ticker in state["positions"]:
                    state["positions"][ticker]["last_price"] = price
                continue

            if delta_shares > 0:
                # BUY
                cost = delta_shares * price
                if cost > state["cash"]:
                    delta_shares = state["cash"] / price
                    cost = delta_shares * price
                state["cash"] -= cost
                old_shares = current_shares
                new_shares = old_shares + delta_shares
                avg_cost = (
                    (state["positions"].get(ticker, {}).get("avg_cost", price) * old_shares + cost)
                    / new_shares
                ) if new_shares > 0 else price
                state["positions"][ticker] = {
                    "shares": round(new_shares, 6),
                    "avg_cost": round(avg_cost, 2),
                    "last_price": price,
                    "sector": tp.get("sector", "Unknown"),
                    "target_weight": tp["weight"],
                }
                trades.append({
                    "timestamp": now,
                    "ticker": ticker,
                    "action": "BUY",
                    "shares": round(delta_shares, 6),
                    "price": price,
                    "value": round(cost, 2),
                    "reason": tp.get("rationale", "rebalance"),
                })
                log.info("BUY %s: %.2f shares @ $%.2f = $%.2f", ticker, delta_shares, price, cost)
            else:
                # SELL (trim)
                sell_shares = abs(delta_shares)
                proceeds = sell_shares * price
                state["cash"] += proceeds
                remaining = current_shares - sell_shares
                if remaining < 0.001:
                    if ticker in state["positions"]:
                        del state["positions"][ticker]
                else:
                    state["positions"][ticker]["shares"] = round(remaining, 6)
                    state["positions"][ticker]["last_price"] = price
                    state["positions"][ticker]["target_weight"] = tp["weight"]
                trades.append({
                    "timestamp": now,
                    "ticker": ticker,
                    "action": "SELL",
                    "shares": round(sell_shares, 6),
                    "price": price,
                    "value": round(proceeds, 2),
                    "reason": "trim to target weight",
                })
                log.info("SELL %s: %.2f shares @ $%.2f = $%.2f",
                         ticker, sell_shares, price, proceeds)

        # Update total value
        total = state["cash"]
        for ticker, pos in state["positions"].items():
            price = prices.get(ticker, pos.get("last_price", 0))
            pos["last_price"] = price
            total += pos["shares"] * price
        state["total_value"] = round(total, 2)

        self.save(state)
        self.save_trades(trades)
        return state

    def record_day_trade(self, ticker: str, action: str, shares: float,
                         price: float, reason: str, state: dict | None = None) -> dict:
        """Record a single intraday trade."""
        state = state or self.load()
        trades = self.load_trades()
        now = datetime.now().isoformat()
        value = shares * price

        if action == "BUY":
            cost = value
            if cost > state["cash"]:
                shares = state["cash"] / price
                cost = shares * price
            state["cash"] -= cost
            old = state["positions"].get(ticker, {"shares": 0, "avg_cost": price})
            new_shares = old["shares"] + shares
            avg_cost = (old["avg_cost"] * old["shares"] + cost) / new_shares if new_shares > 0 else price
            state["positions"][ticker] = {
                "shares": round(new_shares, 6),
                "avg_cost": round(avg_cost, 2),
                "last_price": price,
                "sector": state["positions"].get(ticker, {}).get("sector", "Unknown"),
                "target_weight": state["positions"].get(ticker, {}).get("target_weight", 0),
            }
        elif action == "SELL":
            pos = state["positions"].get(ticker)
            if not pos or pos["shares"] < shares:
                log.warning("Cannot sell %.2f shares of %s (have %.2f)",
                            shares, ticker, pos["shares"] if pos else 0)
                return state
            state["cash"] += value
            remaining = pos["shares"] - shares
            if remaining < 0.001:
                del state["positions"][ticker]
            else:
                pos["shares"] = round(remaining, 6)
                pos["last_price"] = price

        trades.append({
            "timestamp": now,
            "ticker": ticker,
            "action": action,
            "shares": round(shares, 6),
            "price": price,
            "value": round(value, 2),
            "reason": reason,
            "type": "day_trade",
        })

        # Update total value
        prices = get_batch_prices(list(state["positions"].keys()))
        total = state["cash"]
        for t, p in state["positions"].items():
            px = prices.get(t, p.get("last_price", 0))
            p["last_price"] = px
            total += p["shares"] * px
        state["total_value"] = round(total, 2)

        self.save(state)
        self.save_trades(trades)
        return state

    def snapshot(self, state: dict | None = None) -> dict:
        """Take a daily snapshot and save to disk."""
        state = state or self.load()
        prices = get_batch_prices(
            list(state["positions"].keys()) + [self.benchmark]
        )

        # Update prices
        total = state["cash"]
        holdings = []
        for ticker, pos in state["positions"].items():
            price = prices.get(ticker, pos.get("last_price", 0))
            pos["last_price"] = price
            value = pos["shares"] * price
            total += value
            pnl = (price - pos["avg_cost"]) * pos["shares"]
            holdings.append({
                "ticker": ticker,
                "shares": pos["shares"],
                "price": price,
                "avg_cost": pos["avg_cost"],
                "value": round(value, 2),
                "pnl": round(pnl, 2),
                "weight": round(value / total * 100, 2) if total > 0 else 0,
                "sector": pos.get("sector", "Unknown"),
            })

        state["total_value"] = round(total, 2)
        self.save(state)

        # Benchmark performance
        bench_price = prices.get(self.benchmark, 0)
        bench_start = state.get("benchmark_start_price", bench_price)
        bench_return = ((bench_price / bench_start) - 1) * 100 if bench_start else 0
        port_return = ((total / self.initial_capital) - 1) * 100

        snap = {
            "date": date.today().isoformat(),
            "timestamp": datetime.now().isoformat(),
            "total_value": round(total, 2),
            "cash": round(state["cash"], 2),
            "portfolio_return_pct": round(port_return, 2),
            "benchmark_return_pct": round(bench_return, 2),
            "alpha": round(port_return - bench_return, 2),
            "benchmark_price": bench_price,
            "num_positions": len(holdings),
            "holdings": sorted(holdings, key=lambda h: h["value"], reverse=True),
        }

        # Save snapshot
        snap_path = os.path.join(self.snapshots_dir, f"{date.today().isoformat()}.json")
        with open(snap_path, "w") as f:
            json.dump(snap, f, indent=2)
        log.info("Snapshot saved: $%.2f (%.2f%% vs SPY %.2f%%, alpha %.2f%%)",
                 total, port_return, bench_return, port_return - bench_return)

        return snap
