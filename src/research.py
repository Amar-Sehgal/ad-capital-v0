"""Stage 2: Adversarial research — parallel bull/bear agents debate each candidate."""

import asyncio
import json
import logging

import anthropic

from src.data_sources import get_recent_news

log = logging.getLogger(__name__)

BULL_SYSTEM = """You are a senior equity analyst constructing the BULL case for a stock.

You will receive:
1. The stock's fundamental data
2. Recent news headlines from the last 7 days
3. The screener's initial assessment

Your job: make the strongest possible argument for why this stock will OUTPERFORM over the next \
1-12 months. Be specific — cite catalysts, valuation arguments, competitive advantages, and \
upcoming events. Do NOT hedge or mention risks (that's the bear analyst's job).

Respond with ONLY a JSON object:
{
    "ticker": "...",
    "conviction": 1-10,
    "thesis": "2-3 sentence core thesis",
    "catalysts": ["catalyst 1", "catalyst 2", ...],
    "valuation_argument": "why the current price undervalues this stock",
    "upside_target_pct": N
}"""

BEAR_SYSTEM = """You are a senior equity analyst constructing the BEAR case for a stock.

You will receive:
1. The stock's fundamental data
2. Recent news headlines from the last 7 days
3. The screener's initial assessment

Your job: make the strongest possible argument for why this stock will UNDERPERFORM or lose value \
over the next 1-12 months. Be specific — cite risks, valuation concerns, competitive threats, \
macro headwinds, and upcoming dangers. Do NOT mention positives (that's the bull analyst's job).

Respond with ONLY a JSON object:
{
    "ticker": "...",
    "conviction": 1-10,
    "thesis": "2-3 sentence core bear thesis",
    "risks": ["risk 1", "risk 2", ...],
    "valuation_concern": "why the current price overvalues this stock",
    "downside_target_pct": N
}"""


def _build_research_prompt(candidate: dict, news: list[dict]) -> str:
    """Build the user prompt with fundamentals + news."""
    fund = candidate.get("fundamentals", {})
    news_str = "\n".join(
        f"- [{a['published']}] {a['title']} ({a['publisher']})" for a in news
    ) if news else "No recent news available."

    return f"""Stock: {candidate['ticker']}
Screener Score: {candidate.get('score', 'N/A')}/100
Screener Note: {candidate.get('reason', 'N/A')}

Fundamentals:
- Name: {fund.get('name', 'N/A')}
- Sector: {fund.get('sector', 'N/A')} | Industry: {fund.get('industry', 'N/A')}
- Price: ${fund.get('price', 'N/A')} | Market Cap: ${(fund.get('market_cap', 0) or 0) / 1e9:.1f}B
- Forward P/E: {fund.get('pe_forward', 'N/A')} | PEG: {fund.get('peg_ratio', 'N/A')}
- Revenue Growth: {_pct(fund.get('revenue_growth'))} | Earnings Growth: {_pct(fund.get('earnings_growth'))}
- Profit Margin: {_pct(fund.get('profit_margin'))} | ROE: {_pct(fund.get('return_on_equity'))}
- Debt/Equity: {fund.get('debt_to_equity', 'N/A')} | FCF: ${(fund.get('free_cash_flow', 0) or 0) / 1e9:.1f}B
- Beta: {fund.get('beta', 'N/A')}
- Analyst Target: ${fund.get('target_mean_price', 'N/A')} ({fund.get('recommendation', 'N/A')})
- 52w Range: ${fund.get('fifty_two_week_low', 'N/A')} - ${fund.get('fifty_two_week_high', 'N/A')}

Recent News (last 7 days):
{news_str}"""


def _pct(val) -> str:
    if val is None:
        return "N/A"
    return f"{val * 100:.1f}%"


async def _run_agent(
    client: anthropic.AsyncAnthropic,
    model: str,
    system: str,
    prompt: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Run a single bull or bear agent with concurrency limiting."""
    async with semaphore:
        resp = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)


async def run_adversarial_research(
    candidates: list[dict],
    model: str = "claude-sonnet-4-6",
    max_concurrent: int = 10,
    news_days: int = 7,
) -> dict[str, dict]:
    """Run bull and bear agents in parallel for each candidate.

    Returns dict keyed by ticker:
    {
        "AVGO": {
            "bull": { ... bull case ... },
            "bear": { ... bear case ... },
            "net_score": bull_conviction - bear_conviction
        }
    }
    """
    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(max_concurrent)
    results = {}

    # Fetch news for all candidates first
    log.info("Fetching news for %d candidates...", len(candidates))
    news_cache = {}
    for c in candidates:
        ticker = c["ticker"]
        news_cache[ticker] = get_recent_news(ticker, days=news_days)

    # Build all agent tasks
    tasks = []
    task_meta = []  # track (ticker, side) for each task

    for c in candidates:
        ticker = c["ticker"]
        prompt = _build_research_prompt(c, news_cache[ticker])
        # Bull agent
        tasks.append(_run_agent(client, model, BULL_SYSTEM, prompt, semaphore))
        task_meta.append((ticker, "bull"))
        # Bear agent
        tasks.append(_run_agent(client, model, BEAR_SYSTEM, prompt, semaphore))
        task_meta.append((ticker, "bear"))

    log.info("Launching %d research agents (%d bull + %d bear)...",
             len(tasks), len(candidates), len(candidates))

    # Run all agents in parallel
    agent_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collate results
    for (ticker, side), result in zip(task_meta, agent_results):
        if ticker not in results:
            results[ticker] = {"bull": None, "bear": None}

        if isinstance(result, Exception):
            log.error("Agent failed for %s (%s): %s", ticker, side, result)
            if side == "bull":
                results[ticker]["bull"] = {"ticker": ticker, "conviction": 5, "thesis": "analysis failed"}
            else:
                results[ticker]["bear"] = {"ticker": ticker, "conviction": 5, "thesis": "analysis failed"}
        else:
            results[ticker][side] = result

    # Compute net score (bull conviction - bear conviction)
    for ticker, r in results.items():
        bull_conv = r.get("bull", {}).get("conviction", 5) if r.get("bull") else 5
        bear_conv = r.get("bear", {}).get("conviction", 5) if r.get("bear") else 5
        r["net_score"] = bull_conv - bear_conv

    log.info("Research complete. Net scores range: %d to %d",
             min(r["net_score"] for r in results.values()) if results else 0,
             max(r["net_score"] for r in results.values()) if results else 0)

    return results
