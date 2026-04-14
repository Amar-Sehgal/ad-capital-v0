"""Stage 4: Portfolio construction — agent-of-agents selects 15 positions with constraints."""

import json
import logging

import anthropic

log = logging.getLogger(__name__)

OPTIMIZER_SYSTEM = """You are the Chief Investment Officer — the "agent of agents" optimizer. \
You receive scenario models from your research team and must construct a concentrated 15-stock \
portfolio that maximizes risk-adjusted expected returns.

HARD CONSTRAINTS (you MUST follow these):
1. Exactly 15 positions
2. Each position weight between 2% and 12%
3. Weights must sum to 100% (fully invested, no cash)
4. Maximum 35% total weight in any single GICS sector
5. Every position must have a POSITIVE probability-weighted expected return at the 6-month horizon
6. No more than 3 positions from the same sector

OPTIMIZATION OBJECTIVES (prioritize in order):
1. Maximize portfolio-level expected return (6-month horizon)
2. Diversify across sectors and risk factors
3. Favor higher conviction (higher bull-bear net score) names
4. Prefer lower risk_score when expected returns are similar
5. Balance between growth and value styles

Respond with ONLY a JSON object:
{
    "positions": [
        {
            "ticker": "...",
            "weight": N,
            "sector": "...",
            "rationale": "one sentence on why this name and this weight"
        },
        ...
    ],
    "sector_weights": {"Technology": N, "Healthcare": N, ...},
    "portfolio_expected_return_6m": N,
    "portfolio_risk_score": N,
    "strategy_summary": "2-3 sentences on portfolio thesis and construction logic"
}

Where weight is a percentage (e.g., 8.5 means 8.5% of portfolio).
Verify your weights sum to 100 and no sector exceeds 35%."""


def _build_optimizer_prompt(scenarios: dict[str, dict], research: dict[str, dict]) -> str:
    """Build the comprehensive prompt for the optimizer agent."""
    lines = ["# Candidate Stocks with Scenario Models\n"]

    for ticker, sc in sorted(scenarios.items(), key=lambda x: x[1].get("expected_returns", {}).get("6m", 0), reverse=True):
        er = sc.get("expected_returns", {})
        rs = research.get(ticker, {})
        net_score = rs.get("net_score", 0)
        bull_conv = rs.get("bull", {}).get("conviction", "?")
        bear_conv = rs.get("bear", {}).get("conviction", "?")

        lines.append(
            f"## {ticker} ({sc.get('sector', 'Unknown')})\n"
            f"Price: ${sc.get('current_price', '?')}\n"
            f"Bull conviction: {bull_conv}/10 | Bear conviction: {bear_conv}/10 | Net: {net_score:+d}\n"
            f"Expected returns: 1m={er.get('1m', '?')}% | 3m={er.get('3m', '?')}% | "
            f"6m={er.get('6m', '?')}% | 12m={er.get('12m', '?')}%\n"
            f"Risk score: {sc.get('risk_score', '?')}/10\n"
            f"Summary: {sc.get('summary', 'N/A')}\n"
            f"Bull thesis: {rs.get('bull', {}).get('thesis', 'N/A')}\n"
            f"Bear thesis: {rs.get('bear', {}).get('thesis', 'N/A')}\n"
        )

    return "\n".join(lines)


async def construct_portfolio(
    scenarios: dict[str, dict],
    research: dict[str, dict],
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Run the optimizer agent to construct the final 15-stock portfolio.

    Returns the full portfolio allocation with rationale.
    """
    log.info("Running portfolio optimizer on %d scenario-modeled candidates...", len(scenarios))

    client = anthropic.AsyncAnthropic()
    prompt = _build_optimizer_prompt(scenarios, research)

    resp = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=[{"type": "text", "text": OPTIMIZER_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )

    text = resp.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    portfolio = json.loads(text)

    # Validate constraints
    positions = portfolio.get("positions", [])
    total_weight = sum(p["weight"] for p in positions)
    sector_totals: dict[str, float] = {}
    for p in positions:
        sector = p.get("sector", "Unknown")
        sector_totals[sector] = sector_totals.get(sector, 0) + p["weight"]

    log.info("Portfolio: %d positions, total weight: %.1f%%", len(positions), total_weight)
    for sector, weight in sorted(sector_totals.items(), key=lambda x: x[1], reverse=True):
        marker = " [!]" if weight > 35 else ""
        log.info("  %s: %.1f%%%s", sector, weight, marker)

    if len(positions) != 15:
        log.warning("Portfolio has %d positions (expected 15)", len(positions))
    if abs(total_weight - 100) > 1:
        log.warning("Portfolio weights sum to %.1f%% (expected 100%%)", total_weight)
    for sector, weight in sector_totals.items():
        if weight > 35:
            log.warning("Sector %s exceeds 35%% cap at %.1f%%", sector, weight)

    return portfolio
