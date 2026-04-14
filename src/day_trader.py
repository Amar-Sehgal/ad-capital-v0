"""Intraday monitoring and day trading module.

Uses yfinance intraday data (5m intervals, free) and Claude to evaluate whether
positions should be adjusted during market hours based on price action and news.
"""

import asyncio
import json
import logging
from datetime import datetime, time

import anthropic
import yfinance as yf

from src.data_sources import get_recent_news

log = logging.getLogger(__name__)

DAY_TRADE_SYSTEM = """You are an intraday trading analyst monitoring a portfolio of stocks.

You will receive:
1. Current portfolio positions with entry prices and weights
2. Intraday price action (5-minute bars) for today
3. Any breaking news from today

Your job: evaluate whether any URGENT intraday trades are warranted. You are NOT a momentum \
day-trader — you are a fundamental portfolio manager who occasionally needs to react quickly to:
- Material news (earnings surprises, FDA decisions, M&A, downgrades)
- Extreme intraday moves (>5% in a position) that may signal problems
- Sudden sector-wide dislocations
- New high-conviction opportunities at temporarily depressed prices

You should be CONSERVATIVE. Most days, the answer is "no trades." Only recommend trades when \
there is a clear, specific catalyst — not just because a stock moved a few percent.

For each recommended trade, specify:
- ticker
- action: BUY or SELL
- pct_of_position: what percentage of the current position to trade (100 = full exit, 50 = half)
- urgency: LOW / MEDIUM / HIGH
- reason: one sentence

Respond with ONLY a JSON object:
{
    "market_assessment": "one sentence on overall market conditions today",
    "trades": [
        {
            "ticker": "...",
            "action": "BUY" or "SELL",
            "pct_of_position": N,
            "urgency": "LOW" or "MEDIUM" or "HIGH",
            "reason": "..."
        }
    ],
    "watch_list": ["tickers to monitor closely for rest of day"]
}

If no trades are warranted, return an empty trades array."""


def _is_market_hours() -> bool:
    """Check if US stock market is currently open (9:30 AM - 4:00 PM ET, weekdays)."""
    from datetime import timezone, timedelta
    et = timezone(timedelta(hours=-4))  # EDT
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    market_open = time(9, 30)
    market_close = time(16, 0)
    return market_open <= now.time() <= market_close


def _get_intraday_data(tickers: list[str]) -> dict[str, dict]:
    """Fetch today's intraday price action for multiple tickers."""
    result = {}
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1d", interval="5m")
            if hist.empty:
                continue
            open_price = float(hist["Open"].iloc[0])
            current_price = float(hist["Close"].iloc[-1])
            high = float(hist["High"].max())
            low = float(hist["Low"].min())
            volume = int(hist["Volume"].sum())
            change_pct = ((current_price / open_price) - 1) * 100 if open_price else 0

            result[ticker] = {
                "open": round(open_price, 2),
                "current": round(current_price, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "change_pct": round(change_pct, 2),
                "volume": volume,
                "num_bars": len(hist),
            }
        except Exception as e:
            log.debug("Intraday fetch failed for %s: %s", ticker, e)
    return result


def _build_day_trade_prompt(positions: dict, intraday: dict, news: dict) -> str:
    """Build the prompt with current positions + intraday data + news."""
    lines = ["# Current Portfolio Positions\n"]
    for ticker, pos in positions.items():
        iday = intraday.get(ticker, {})
        lines.append(
            f"- {ticker}: {pos['shares']:.2f} shares @ ${pos['avg_cost']:.2f} | "
            f"Weight: {pos.get('target_weight', 0):.1f}% | Sector: {pos.get('sector', '?')} | "
            f"Today: ${iday.get('current', '?')} ({iday.get('change_pct', '?'):+.2f}%) | "
            f"Range: ${iday.get('low', '?')}-${iday.get('high', '?')} | Vol: {iday.get('volume', '?')}"
        )

    lines.append("\n# Breaking News Today\n")
    for ticker, articles in news.items():
        if articles:
            lines.append(f"**{ticker}:**")
            for a in articles[:5]:
                lines.append(f"  - {a['title']} ({a['publisher']})")

    return "\n".join(lines)


async def evaluate_day_trades(
    portfolio_state: dict,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Evaluate whether any intraday trades are warranted.

    Returns the Claude analysis with recommended trades (often empty).
    """
    positions = portfolio_state.get("positions", {})
    if not positions:
        return {"market_assessment": "no positions to monitor", "trades": [], "watch_list": []}

    tickers = list(positions.keys())

    # Fetch intraday data and news in parallel
    log.info("Fetching intraday data for %d positions...", len(tickers))
    intraday = _get_intraday_data(tickers)

    news = {}
    for ticker in tickers:
        news[ticker] = get_recent_news(ticker, days=1)

    prompt = _build_day_trade_prompt(positions, intraday, news)

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model=model,
        max_tokens=2048,
        system=[{"type": "text", "text": DAY_TRADE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )

    text = resp.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    result = json.loads(text)
    num_trades = len(result.get("trades", []))
    if num_trades:
        log.info("Day trade evaluation: %d trades recommended", num_trades)
        for t in result["trades"]:
            log.info("  %s %s %d%% - %s (%s)",
                     t["action"], t["ticker"], t["pct_of_position"],
                     t["reason"], t["urgency"])
    else:
        log.info("Day trade evaluation: no trades recommended")

    return result


async def execute_day_trades(
    evaluation: dict,
    portfolio_manager,
    portfolio_state: dict,
    min_urgency: str = "MEDIUM",
) -> list[dict]:
    """Execute recommended day trades that meet the urgency threshold.

    Returns list of executed trades.
    """
    urgency_levels = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    min_level = urgency_levels.get(min_urgency, 1)

    executed = []
    positions = portfolio_state.get("positions", {})

    for trade in evaluation.get("trades", []):
        trade_level = urgency_levels.get(trade.get("urgency", "LOW"), 0)
        if trade_level < min_level:
            log.info("Skipping %s %s (urgency %s < %s)",
                     trade["action"], trade["ticker"], trade["urgency"], min_urgency)
            continue

        ticker = trade["ticker"]
        action = trade["action"]
        pct = trade["pct_of_position"]

        # Get current price
        intraday = _get_intraday_data([ticker])
        price = intraday.get(ticker, {}).get("current")
        if not price:
            log.warning("No price for %s, skipping day trade", ticker)
            continue

        if action == "SELL":
            pos = positions.get(ticker)
            if not pos:
                continue
            shares = pos["shares"] * (pct / 100)
            portfolio_state = portfolio_manager.record_day_trade(
                ticker, "SELL", shares, price, trade["reason"], portfolio_state
            )
            executed.append({**trade, "shares": shares, "price": price})
        elif action == "BUY":
            # For buys, pct_of_position means % of a standard position size
            target_value = portfolio_state["total_value"] * 0.067 * (pct / 100)  # ~6.7% standard position
            shares = target_value / price
            portfolio_state = portfolio_manager.record_day_trade(
                ticker, "BUY", shares, price, trade["reason"], portfolio_state
            )
            executed.append({**trade, "shares": shares, "price": price})

    return executed
