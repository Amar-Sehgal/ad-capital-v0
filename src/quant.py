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
    "multifactor_v3": {
        "name": "MultiFactorV3 (ported from stock_prediction)",
        "description": "6-factor composite: 3 ensemble MA speeds + RSI + volume + ADX. "
                       "Backtested Sharpe ~1.39 on S&P 500 tech. Currently scanning 100 stocks daily.",
        "holding_period": "1-4 weeks",
        "signals": ["mfv3_buy", "mfv3_sell", "mfv3_hold"],
        "entry": "Score >= 4/6 factors",
        "exit": "Score <= 1/6 factors",
    },
    "trend_pullback": {
        "name": "TrendPullback (ported from stock_prediction)",
        "description": "Buy pullbacks to SMA(20) in confirmed uptrends (price > SMA(50), ADX > 25). "
                       "Chandelier trailing stop exit. Better entry timing than crossover.",
        "holding_period": "1-4 weeks",
        "signals": ["pullback_entry", "chandelier_exit"],
        "entry": "Uptrend confirmed + pullback to SMA(20)",
        "exit": "Chandelier stop: highest_high(22) - 2.5*ATR",
    },
    "mean_reversion": {
        "name": "Mean Reversion",
        "description": "Buy oversold stocks (RSI < 30 or >2 std dev below 20d SMA), sell on reversion",
        "holding_period": "1-5 days",
        "signals": ["rsi_oversold", "bollinger_lower", "volume_spike_down"],
        "entry": "RSI < 30 or price < BB lower",
        "exit": "RSI > 50 or price > SMA(20)",
    },
    "momentum": {
        "name": "Momentum",
        "description": "Buy stocks breaking out on volume above 20d MA with positive momentum",
        "holding_period": "1-10 days",
        "signals": ["breakout_volume", "macd_cross", "rsi_momentum"],
        "entry": "Price breaks above SMA(20) on 2x volume",
        "exit": "Price closes below SMA(20) or RSI > 75",
    },
    "gap_fill": {
        "name": "Gap Fill",
        "description": "Trade overnight gaps that statistically tend to fill during the session",
        "holding_period": "intraday",
        "signals": ["gap_up_fade", "gap_down_fill"],
        "entry": "Gap > 1% from previous close",
        "exit": "Gap fills to previous close or EOD",
    },
    "earnings_drift": {
        "name": "Post-Earnings Drift",
        "description": "Ride post-earnings momentum for stocks that beat/miss significantly",
        "holding_period": "1-5 days",
        "signals": ["earnings_beat_drift", "earnings_miss_drift"],
        "entry": "Earnings beat/miss > 5%, momentum continues",
        "exit": "3-5 day hold or reversal",
    },
    "stat_arb": {
        "name": "Statistical Arbitrage",
        "description": "Pairs/relative value trades on correlated stocks that diverge",
        "holding_period": "1-10 days",
        "signals": ["pair_divergence", "sector_relative_value"],
        "entry": "Z-score > 2 on spread between correlated pair",
        "exit": "Spread reverts to mean",
    },
    "market_making": {
        "name": "Market Making (simulated)",
        "description": "Capture bid-ask spread on high-volume names. Paper-traded as limit order simulation.",
        "holding_period": "intraday",
        "signals": ["spread_capture", "depth_imbalance"],
        "entry": "Limit order at bid/ask in high-volume name",
        "exit": "Fill on opposite side or timeout",
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
# Ported strategies from ~/personal/stock_prediction/strategies.py
# Adapted from backtesting.py Strategy classes to standalone signal generators.
# ---------------------------------------------------------------------------

def _adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Average Directional Index — ported from stock_prediction."""
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where(plus_dm > 0, 0)
    minus_dm = minus_dm.where(minus_dm > 0, 0)
    mask = plus_dm > minus_dm
    minus_dm = minus_dm.where(~(mask & (plus_dm > 0)), 0)
    plus_dm = plus_dm.where(~(~mask & (minus_dm > 0)), 0)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr_s = tr.rolling(n).mean()

    plus_di = 100 * plus_dm.ewm(alpha=1 / n, min_periods=n).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, min_periods=n).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / n, min_periods=n).mean()


def multifactor_v3_score(ticker: str) -> dict | None:
    """Run MultiFactorV3 scoring on a ticker.

    Ported from stock_prediction. 6-factor composite:
      1-3. Ensemble MA trend (10/30, 20/60, 40/100)
      4.   RSI in buy zone (30-70)
      5.   Volume above 20d average
      6.   ADX > 25 (trending)

    Returns dict with score, factor breakdown, and signal.
    """
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1y")
        if hist.empty or len(hist) < 100:
            return None

        close = hist["Close"]
        volume = hist["Volume"]
        high = hist["High"]
        low = hist["Low"]

        price = float(close.iloc[-1])

        # Ensemble MAs
        sma_10 = float(close.rolling(10).mean().iloc[-1])
        sma_30 = float(close.rolling(30).mean().iloc[-1])
        sma_20 = float(close.rolling(20).mean().iloc[-1])
        sma_60 = float(close.rolling(60).mean().iloc[-1])
        sma_40 = float(close.rolling(40).mean().iloc[-1])
        sma_100 = float(close.rolling(100).mean().iloc[-1])

        # RSI
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = float(rsi.iloc[-1])

        # ADX
        adx = _adx(high, low, close, 14)
        adx_val = float(adx.iloc[-1])

        # Volume
        vol_avg = float(volume.rolling(20).mean().iloc[-1])
        cur_vol = float(volume.iloc[-1])

        # Score
        factors = {}
        score = 0
        factors["sma_10_30"] = "BULL" if sma_10 > sma_30 else "BEAR"
        if sma_10 > sma_30:
            score += 1
        factors["sma_20_60"] = "BULL" if sma_20 > sma_60 else "BEAR"
        if sma_20 > sma_60:
            score += 1
        factors["sma_40_100"] = "BULL" if sma_40 > sma_100 else "BEAR"
        if sma_40 > sma_100:
            score += 1
        factors["rsi"] = round(rsi_val, 1)
        factors["rsi_zone"] = "OK" if 30 < rsi_val < 70 else "EXTREME"
        if 30 < rsi_val < 70:
            score += 1
        factors["volume_ratio"] = round(cur_vol / vol_avg, 2) if vol_avg > 0 else 0
        factors["volume_elevated"] = cur_vol > vol_avg * 1.1
        if cur_vol > vol_avg * 1.1:
            score += 1
        factors["adx"] = round(adx_val, 1)
        factors["trending"] = adx_val > 25
        if not pd.isna(adx_val) and adx_val > 25:
            score += 1

        # Signal
        if score >= 4:
            signal = "mfv3_buy"
        elif score <= 1:
            signal = "mfv3_sell"
        else:
            signal = "mfv3_hold"

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "score": score,
            "max_score": 6,
            "signal": signal,
            "factors": factors,
        }
    except Exception as e:
        log.debug("MFV3 scoring failed for %s: %s", ticker, e)
        return None


def trend_pullback_signal(ticker: str) -> dict | None:
    """Run TrendPullback analysis on a ticker.

    Ported from stock_prediction. Detects pullback entries in confirmed uptrends.
    Entry: price > SMA(50), ADX > 25, price within 2% of SMA(20)
    Exit: Chandelier stop = highest_high(22) - 2.5*ATR(14)
    """
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="6mo")
        if hist.empty or len(hist) < 60:
            return None

        close = hist["Close"]
        high = hist["High"]
        low = hist["Low"]

        price = float(close.iloc[-1])
        sma_50 = float(close.rolling(50).mean().iloc[-1])
        sma_20 = float(close.rolling(20).mean().iloc[-1])

        # ADX
        adx = _adx(high, low, close, 14)
        adx_val = float(adx.iloc[-1])

        # ATR
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr_val = float(tr.rolling(14).mean().iloc[-1])

        # Chandelier stop
        highest_22 = float(high.rolling(22).max().iloc[-1])
        chandelier_stop = highest_22 - 2.5 * atr_val

        in_uptrend = price > sma_50 and adx_val > 25
        at_pullback = price <= sma_20 * 1.02
        above_stop = price > chandelier_stop

        if in_uptrend and at_pullback:
            signal = "pullback_entry"
        elif not above_stop:
            signal = "chandelier_exit"
        elif in_uptrend:
            signal = "in_trend"
        else:
            signal = "no_trend"

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "signal": signal,
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2),
            "adx": round(adx_val, 1),
            "atr": round(atr_val, 2),
            "chandelier_stop": round(chandelier_stop, 2),
            "in_uptrend": in_uptrend,
            "at_pullback": at_pullback,
        }
    except Exception as e:
        log.debug("TrendPullback failed for %s: %s", ticker, e)
        return None


def scan_multifactor_v3(tickers: list[str]) -> list[dict]:
    """Run MultiFactorV3 on a list of tickers. Returns sorted by score."""
    results = []
    for ticker in tickers:
        r = multifactor_v3_score(ticker)
        if r:
            results.append(r)
    results.sort(key=lambda r: r["score"], reverse=True)
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
