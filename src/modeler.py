"""Stage 3: Scenario modeling — probability-weighted bull/base/bear with price targets."""

import asyncio
import json
import logging

import anthropic

log = logging.getLogger(__name__)

MODELER_SYSTEM = """You are a quantitative scenario modeler for equity investments.

You will receive a stock's fundamentals, the bull case from a bull analyst, and the bear case from \
a bear analyst. Your job is to synthesize both perspectives into probability-weighted scenarios.

Build THREE scenarios for each stock:
1. BULL scenario — things go right (catalysts hit, growth accelerates)
2. BASE scenario — business continues roughly as expected
3. BEAR scenario — risks materialize (competitive threats, macro headwinds)

For each scenario, estimate:
- Probability (all three must sum to 100%)
- Price target at 1 month, 3 months, 6 months, and 12 months (as % change from current price)
- Key assumptions driving the scenario

Also compute the probability-weighted expected return at each time horizon.

Respond with ONLY a JSON object:
{
    "ticker": "...",
    "current_price": N,
    "sector": "...",
    "scenarios": {
        "bull": {
            "probability": N,
            "assumptions": ["...", "..."],
            "returns": {"1m": N, "3m": N, "6m": N, "12m": N}
        },
        "base": {
            "probability": N,
            "assumptions": ["...", "..."],
            "returns": {"1m": N, "3m": N, "6m": N, "12m": N}
        },
        "bear": {
            "probability": N,
            "assumptions": ["...", "..."],
            "returns": {"1m": N, "3m": N, "6m": N, "12m": N}
        }
    },
    "expected_returns": {"1m": N, "3m": N, "6m": N, "12m": N},
    "risk_score": N,
    "summary": "one sentence synthesis"
}

Where returns are percentage values (e.g., 15 means +15%, -10 means -10%).
risk_score is 1-10 (10 = highest risk)."""


def _build_modeler_prompt(candidate: dict, research: dict) -> str:
    """Build the prompt with fundamentals + bull/bear research."""
    fund = candidate.get("fundamentals", {})
    bull = research.get("bull", {})
    bear = research.get("bear", {})

    return f"""Stock: {candidate['ticker']} ({fund.get('name', 'N/A')})
Sector: {fund.get('sector', 'N/A')} | Industry: {fund.get('industry', 'N/A')}
Current Price: ${fund.get('price', 'N/A')} | Market Cap: ${(fund.get('market_cap', 0) or 0) / 1e9:.1f}B
Forward P/E: {fund.get('pe_forward', 'N/A')} | Revenue Growth: {_pct(fund.get('revenue_growth'))}

=== BULL ANALYST (conviction {bull.get('conviction', 'N/A')}/10) ===
Thesis: {bull.get('thesis', 'N/A')}
Catalysts: {json.dumps(bull.get('catalysts', []))}
Valuation: {bull.get('valuation_argument', 'N/A')}
Upside Target: {bull.get('upside_target_pct', 'N/A')}%

=== BEAR ANALYST (conviction {bear.get('conviction', 'N/A')}/10) ===
Thesis: {bear.get('thesis', 'N/A')}
Risks: {json.dumps(bear.get('risks', []))}
Valuation Concern: {bear.get('valuation_concern', 'N/A')}
Downside Target: {bear.get('downside_target_pct', 'N/A')}%"""


def _pct(val) -> str:
    if val is None:
        return "N/A"
    return f"{val * 100:.1f}%"


async def _model_stock(
    client: anthropic.AsyncAnthropic,
    model: str,
    candidate: dict,
    research: dict,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Run scenario modeling for one stock."""
    async with semaphore:
        prompt = _build_modeler_prompt(candidate, research)
        resp = await client.messages.create(
            model=model,
            max_tokens=1500,
            system=[{"type": "text", "text": MODELER_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)


async def build_scenarios(
    candidates: list[dict],
    research: dict[str, dict],
    model: str = "claude-sonnet-4-6",
    max_concurrent: int = 10,
) -> dict[str, dict]:
    """Build probability-weighted scenarios for each candidate.

    Only models candidates that passed adversarial research (positive net score).
    Returns dict keyed by ticker with scenario data.
    """
    # Filter to candidates with positive net research score
    viable = [
        c for c in candidates
        if c["ticker"] in research and research[c["ticker"]].get("net_score", 0) > 0
    ]
    # If too few pass the filter, relax to net_score >= 0
    if len(viable) < 20:
        viable = [
            c for c in candidates
            if c["ticker"] in research and research[c["ticker"]].get("net_score", 0) >= 0
        ]
    # Still take top candidates by net_score if too many
    viable.sort(key=lambda c: research[c["ticker"]].get("net_score", 0), reverse=True)
    viable = viable[:40]

    log.info("Modeling scenarios for %d viable candidates...", len(viable))

    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(max_concurrent)

    tasks = []
    tickers = []
    for c in viable:
        ticker = c["ticker"]
        tasks.append(_model_stock(client, model, c, research[ticker], semaphore))
        tickers.append(ticker)

    results_raw = await asyncio.gather(*tasks, return_exceptions=True)

    scenarios = {}
    for ticker, result in zip(tickers, results_raw):
        if isinstance(result, Exception):
            log.error("Scenario modeling failed for %s: %s", ticker, result)
            continue
        scenarios[ticker] = result

    log.info("Scenarios built for %d stocks", len(scenarios))
    return scenarios
