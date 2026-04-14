"""Quant book — systematic/signal-driven trading arm.

Separate P&L tracking from the discretionary sector fund. Trades are driven by
statistical signals rather than fundamental thesis. Each strategy is registered
with entry/exit rules and tracked independently.

Future strategies can be plugged in: mean reversion, momentum, gap fills,
stat arb, pairs, volatility, market making.
"""

import json
import logging
import os
from datetime import datetime, date

import yfinance as yf
import pandas as pd

from src.data_sources import get_batch_prices

log = logging.getLogger(__name__)

QUANT_PORTFOLIO_FILE = "data/quant_portfolio.json"
QUANT_TRADES_FILE = "data/quant_trades.json"
QUANT_STRATEGIES_FILE = "data/quant_strategies.json"


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

STRATEGIES = {
    "mean_reversion": {
        "name": "Mean Reversion",
        "description": "Buy oversold stocks (RSI < 30 or >2 std dev below 20d SMA), sell on reversion",
        "holding_period": "1-5 days",
        "signals": ["rsi_oversold", "bollinger_lower", "volume_spike_down"],
    },
    "momentum": {
        "name": "Momentum",
        "description": "Buy stocks breaking out on volume above 20d MA with positive momentum",
        "holding_period": "1-10 days",
        "signals": ["breakout_volume", "macd_cross", "rsi_momentum"],
    },
    "gap_fill": {
        "name": "Gap Fill",
        "description": "Trade overnight gaps that statistically tend to fill during the session",
        "holding_period": "intraday",
        "signals": ["gap_up_fade", "gap_down_fill"],
    },
    "earnings_drift": {
        "name": "Post-Earnings Drift",
        "description": "Ride post-earnings momentum for stocks that beat/miss significantly",
        "holding_period": "1-5 days",
        "signals": ["earnings_beat_drift", "earnings_miss_drift"],
    },
    "stat_arb": {
        "name": "Statistical Arbitrage",
        "description": "Pairs/relative value trades on correlated stocks that diverge",
        "holding_period": "1-10 days",
        "signals": ["pair_divergence", "sector_relative_value"],
    },
    "market_making": {
        "name": "Market Making (simulated)",
        "description": "Capture bid-ask spread on high-volume names. Paper-traded as limit order simulation.",
        "holding_period": "intraday",
        "signals": ["spread_capture", "depth_imbalance"],
    },
}


# ---------------------------------------------------------------------------
# Signal computation (using yfinance data)
# ---------------------------------------------------------------------------

def compute_signals(ticker: str) -> dict:
    """Compute technical signals for a single stock using free yfinance data."""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="3mo")
        if hist.empty or len(hist) < 20:
            return {"ticker": ticker, "error": "insufficient data"}

        close = hist["Close"]
        volume = hist["Volume"]
        high = hist["High"]
        low = hist["Low"]

        # Current values
        price = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        daily_return = (price / prev_close - 1) * 100

        # Moving averages
        sma_20 = float(close.rolling(20).mean().iloc[-1])
        sma_50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
        ema_12 = float(close.ewm(span=12).mean().iloc[-1])
        ema_26 = float(close.ewm(span=26).mean().iloc[-1])

        # RSI (14-period)
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50

        # MACD
        macd = ema_12 - ema_26
        macd_signal = float(close.ewm(span=9).mean().iloc[-1]) - ema_26  # simplified
        macd_val = float(macd)

        # Bollinger Bands (20-period, 2 std dev)
        bb_mid = sma_20
        bb_std = float(close.rolling(20).std().iloc[-1])
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bb_pct = (price - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.5

        # Volume analysis
        avg_volume = float(volume.rolling(20).mean().iloc[-1])
        current_volume = float(volume.iloc[-1])
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1

        # Gap detection (vs previous close)
        open_price = float(hist["Open"].iloc[-1])
        gap_pct = (open_price / prev_close - 1) * 100

        # ATR (14-period) for position sizing
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        atr_pct = (atr / price) * 100

        # Generate signal flags
        signals = []
        if rsi_val < 30:
            signals.append("rsi_oversold")
        elif rsi_val > 70:
            signals.append("rsi_overbought")
        if rsi_val > 50 and rsi_val < 70 and daily_return > 0:
            signals.append("rsi_momentum")

        if price < bb_lower:
            signals.append("bollinger_lower")
        elif price > bb_upper:
            signals.append("bollinger_upper")

        if volume_ratio > 2.0:
            if daily_return < -2:
                signals.append("volume_spike_down")
            elif daily_return > 2:
                signals.append("breakout_volume")

        if macd_val > 0 and price > sma_20:
            signals.append("macd_cross")

        if abs(gap_pct) > 1.0:
            if gap_pct > 0:
                signals.append("gap_up_fade")
            else:
                signals.append("gap_down_fill")

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "daily_return_pct": round(daily_return, 2),
            "rsi": round(rsi_val, 1),
            "macd": round(macd_val, 3),
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2) if sma_50 else None,
            "bb_pct": round(bb_pct, 3),
            "bb_lower": round(bb_lower, 2),
            "bb_upper": round(bb_upper, 2),
            "volume_ratio": round(volume_ratio, 2),
            "gap_pct": round(gap_pct, 2),
            "atr": round(atr, 2),
            "atr_pct": round(atr_pct, 2),
            "signals": signals,
        }
    except Exception as e:
        log.debug("Signal computation failed for %s: %s", ticker, e)
        return {"ticker": ticker, "error": str(e)}


def scan_signals(tickers: list[str], signal_filter: str | None = None) -> list[dict]:
    """Scan multiple tickers for signals. Optionally filter by signal name."""
    results = []
    for ticker in tickers:
        sigs = compute_signals(ticker)
        if sigs.get("error"):
            continue
        if signal_filter:
            if signal_filter in sigs.get("signals", []):
                results.append(sigs)
        elif sigs.get("signals"):
            results.append(sigs)
    results.sort(key=lambda s: len(s.get("signals", [])), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Quant portfolio management
# ---------------------------------------------------------------------------

class QuantBook:
    """Manages the quant/systematic trading book separately from the sector fund."""

    def __init__(self, capital_allocation: float = 300.0):
        self.capital_allocation = capital_allocation
        os.makedirs("data", exist_ok=True)

    def load(self) -> dict:
        if os.path.exists(QUANT_PORTFOLIO_FILE):
            with open(QUANT_PORTFOLIO_FILE) as f:
                return json.load(f)
        return {
            "inception_date": date.today().isoformat(),
            "cash": self.capital_allocation,
            "positions": {},
            "total_value": self.capital_allocation,
            "realized_pnl": 0,
            "total_trades": 0,
            "winning_trades": 0,
        }

    def save(self, state: dict):
        with open(QUANT_PORTFOLIO_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)

    def load_trades(self) -> list[dict]:
        if os.path.exists(QUANT_TRADES_FILE):
            with open(QUANT_TRADES_FILE) as f:
                return json.load(f)
        return []

    def save_trades(self, trades: list[dict]):
        with open(QUANT_TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2, default=str)

    def trade(self, ticker: str, action: str, shares: float, price: float,
              strategy: str, signal: str, entry_rule: str, exit_rule: str,
              state: dict | None = None) -> dict:
        """Execute a quant trade with strategy metadata."""
        state = state or self.load()
        trades = self.load_trades()
        now = datetime.now()
        value = shares * price

        realized = 0
        if action == "BUY":
            if value > state["cash"]:
                shares = state["cash"] / price
                value = shares * price
            state["cash"] -= value
            old = state["positions"].get(ticker, {"shares": 0, "avg_cost": price})
            new_shares = old["shares"] + shares
            avg = ((old["avg_cost"] * old["shares"]) + value) / new_shares if new_shares > 0 else price
            state["positions"][ticker] = {
                "shares": round(new_shares, 6),
                "avg_cost": round(avg, 4),
                "last_price": price,
                "strategy": strategy,
                "entry_time": now.isoformat(),
                "entry_rule": entry_rule,
                "exit_rule": exit_rule,
            }
        elif action == "SELL":
            pos = state["positions"].get(ticker)
            if not pos:
                return state
            if shares > pos["shares"]:
                shares = pos["shares"]
                value = shares * price
            realized = (price - pos["avg_cost"]) * shares
            state["cash"] += value
            state["realized_pnl"] = round(state.get("realized_pnl", 0) + realized, 2)
            state["total_trades"] = state.get("total_trades", 0) + 1
            if realized > 0:
                state["winning_trades"] = state.get("winning_trades", 0) + 1
            remaining = pos["shares"] - shares
            if remaining < 0.001:
                del state["positions"][ticker]
            else:
                pos["shares"] = round(remaining, 6)
                pos["last_price"] = price

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
            "strategy": strategy,
            "signal": signal,
            "entry_rule": entry_rule,
            "exit_rule": exit_rule,
            "realized_pnl": round(realized, 2) if action == "SELL" else None,
            "book": "quant",
        }
        trades.append(trade_record)

        # Update total value
        prices = get_batch_prices(list(state["positions"].keys())) if state["positions"] else {}
        total = state["cash"]
        for t, p in state["positions"].items():
            px = prices.get(t, p.get("last_price", 0))
            p["last_price"] = px
            total += p["shares"] * px
        state["total_value"] = round(total, 2)

        self.save(state)
        self.save_trades(trades)
        return state

    def status(self) -> str:
        """Return formatted quant book status."""
        state = self.load()
        prices = get_batch_prices(list(state["positions"].keys())) if state["positions"] else {}

        total = state["cash"]
        lines = []
        lines.append(f"{'='*65}")
        lines.append(f" Quant Book Status ({date.today().isoformat()})")
        lines.append(f"{'='*65}")

        if state["positions"]:
            lines.append(f" {'Ticker':<8} {'Shares':>8} {'Price':>9} {'Cost':>9} "
                         f"{'P&L':>9} {'Strategy':<20} {'Exit Rule'}")
            lines.append(f" {'-'*8} {'-'*8} {'-'*9} {'-'*9} {'-'*9} {'-'*20} {'-'*20}")
            for ticker, pos in state["positions"].items():
                px = prices.get(ticker, pos.get("last_price", 0))
                pnl = (px - pos["avg_cost"]) * pos["shares"]
                total += pos["shares"] * px
                pnl_str = f"${pnl:+,.2f}"
                lines.append(
                    f" {ticker:<8} {pos['shares']:>8.2f} ${px:>7.2f} "
                    f"${pos['avg_cost']:>7.2f} {pnl_str:>9} "
                    f"{pos.get('strategy', '?'):<20} {pos.get('exit_rule', '?')}"
                )

        total_trades = state.get("total_trades", 0)
        wins = state.get("winning_trades", 0)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        ret = ((total / self.capital_allocation) - 1) * 100

        lines.append(f"")
        lines.append(f" Cash:           ${state['cash']:>10,.2f}")
        lines.append(f" Total Value:    ${total:>10,.2f}")
        lines.append(f" Return:         {ret:>+10.2f}%")
        lines.append(f" Realized P&L:   ${state.get('realized_pnl', 0):>+10,.2f}")
        lines.append(f" Win Rate:       {win_rate:>9.1f}% ({wins}/{total_trades})")
        lines.append(f" Open Positions: {len(state['positions']):>10}")
        lines.append(f"{'='*65}")
        return "\n".join(lines)
