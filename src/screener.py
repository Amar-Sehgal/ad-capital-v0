"""Stage 1: Screen the universe down to top N candidates using fundamentals + Claude ranking."""

import asyncio
import json
import logging
import math

import anthropic

from src.data_sources import get_bulk_fundamentals
from src.sectors import enforce_sector_coverage, normalize_sector

log = logging.getLogger(__name__)

SCREENER_SYSTEM = """You are a quantitative equity screener. Your job is to rank stocks by investment \
quality based on their fundamental data.

You will receive a batch of stocks with their fundamentals. Score each stock from 0-100 based on:
- Valuation (forward P/E, PEG, EV/EBITDA, price-to-book) — 25%
- Growth (revenue growth, earnings growth) — 25%
- Profitability (profit margin, operating margin, ROE) — 20%
- Financial health (debt-to-equity, current ratio, free cash flow) — 15%
- Analyst sentiment (recommendation, target price upside) — 15%

Respond with ONLY a JSON array of objects: [{"ticker": "...", "score": N, "reason": "one sentence"}, ...]
Sort by score descending. Include ALL tickers from the input."""


def _format_fundamentals_batch(stocks: list[dict]) -> str:
    """Format a batch of stocks into a compact string for Claude."""
    lines = []
    for s in stocks:
        price = s.get("price") or 0
        target = s.get("target_mean_price") or 0
        upside = ((target / price) - 1) * 100 if price and target else 0
        lines.append(
            f"{s['ticker']} | {s['name']} | Sector: {s['sector']} | "
            f"MktCap: ${s.get('market_cap', 0) / 1e9:.1f}B | "
            f"FwdPE: {s.get('pe_forward', 'N/A')} | PEG: {s.get('peg_ratio', 'N/A')} | "
            f"EV/EBITDA: {s.get('ev_to_ebitda', 'N/A')} | P/B: {s.get('price_to_book', 'N/A')} | "
            f"RevGrowth: {_pct(s.get('revenue_growth'))} | EarnGrowth: {_pct(s.get('earnings_growth'))} | "
            f"ProfitMargin: {_pct(s.get('profit_margin'))} | OpMargin: {_pct(s.get('operating_margin'))} | "
            f"ROE: {_pct(s.get('return_on_equity'))} | D/E: {s.get('debt_to_equity', 'N/A')} | "
            f"CurrentRatio: {s.get('current_ratio', 'N/A')} | "
            f"FCF: ${(s.get('free_cash_flow') or 0) / 1e9:.1f}B | "
            f"Beta: {s.get('beta', 'N/A')} | "
            f"Rec: {s.get('recommendation', 'N/A')} | TargetUpside: {upside:+.1f}% | "
            f"Analysts: {s.get('analyst_count', 0)}"
        )
    return "\n".join(lines)


def _pct(val) -> str:
    if val is None:
        return "N/A"
    return f"{val * 100:.1f}%"


async def _score_batch(client: anthropic.AsyncAnthropic, model: str, stocks: list[dict]) -> list[dict]:
    """Send a batch of stocks to Claude for scoring."""
    prompt = f"Score these {len(stocks)} stocks:\n\n{_format_fundamentals_batch(stocks)}"
    resp = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=[{"type": "text", "text": SCREENER_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # Extract JSON from response (handle markdown code blocks)
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


async def screen_universe(
    tickers: list[str],
    num_candidates: int = 50,
    model: str = "claude-sonnet-4-6",
) -> list[dict]:
    """Screen the full universe down to top candidates.

    1. Fetch fundamentals for all tickers (quantitative pre-filter)
    2. Send batches to Claude for qualitative scoring
    3. Return top N candidates with scores
    """
    log.info("Fetching fundamentals for %d tickers...", len(tickers))
    all_fundamentals = get_bulk_fundamentals(tickers)
    log.info("Got fundamentals for %d tickers", len(all_fundamentals))

    # Quantitative pre-filter: must have basic data and >$1B market cap
    filtered = [
        s for s in all_fundamentals
        if s.get("market_cap", 0) > 1_000_000_000
        and s.get("price")
        and s.get("sector") != "Unknown"
    ]
    log.info("After pre-filter: %d tickers", len(filtered))

    # Sort by market cap descending and take top 200 for Claude scoring
    filtered.sort(key=lambda s: s.get("market_cap", 0), reverse=True)
    filtered = filtered[:200]

    # Score in batches of 40 (fits comfortably in context)
    batch_size = 40
    batches = [filtered[i:i + batch_size] for i in range(0, len(filtered), batch_size)]

    client = anthropic.AsyncAnthropic()
    all_scored = []

    for i, batch in enumerate(batches):
        log.info("Scoring batch %d/%d (%d stocks)...", i + 1, len(batches), len(batch))
        try:
            scored = await _score_batch(client, model, batch)
            all_scored.extend(scored)
        except Exception as e:
            log.error("Batch %d scoring failed: %s", i + 1, e)
            # Assign default scores for failed batches
            for s in batch:
                all_scored.append({"ticker": s["ticker"], "score": 50, "reason": "scoring failed"})

    # Sort by score and take top N
    all_scored.sort(key=lambda s: s.get("score", 0), reverse=True)
    top = all_scored[:num_candidates]

    # Enrich with fundamental data for downstream stages
    fund_lookup = {s["ticker"]: s for s in all_fundamentals}
    for candidate in top:
        ticker = candidate["ticker"]
        if ticker in fund_lookup:
            candidate["fundamentals"] = fund_lookup[ticker]

    # Enforce minimum sector coverage across all 11 GICS sectors
    top = enforce_sector_coverage(top, all_fundamentals)

    # Log sector distribution
    sector_dist: dict[str, int] = {}
    for c in top:
        sec = normalize_sector(c.get("fundamentals", {}).get("sector", "Unknown"))
        sector_dist[sec] = sector_dist.get(sec, 0) + 1
    for sec in sorted(sector_dist, key=sector_dist.get, reverse=True):
        log.info("  %s: %d candidates", sec, sector_dist[sec])

    log.info("Final candidate pool: %d (scores %d-%d, %d sectors)",
             len(top),
             top[0].get("score", 0) if top else 0,
             top[-1].get("score", 0) if top else 0,
             len(sector_dist))
    return top
