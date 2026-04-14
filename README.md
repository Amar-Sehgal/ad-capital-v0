# AD Capital v0

Multi-agent adversarial stock portfolio. Claude agents debate bull/bear cases across all 11 GICS sectors to construct a concentrated 15-stock paper portfolio.

## How It Works

A 4-stage pipeline inspired by [The Claude Portfolio](https://x.com/theaiportfolios):

### Stage 1 — Screening
Scans the Russell 1000 universe, fetches fundamentals via yfinance, and uses Claude to score and rank stocks. Enforces minimum representation across all 11 GICS sectors so the portfolio never ignores an entire industry.

### Stage 2 — Adversarial Research
For each of the top ~50 candidates, two Claude agents launch in parallel:
- **Bull agent**: makes the strongest possible case for buying
- **Bear agent**: makes the strongest possible case for selling

Each agent receives the stock's fundamentals and recent news (last 7 days only). The adversarial structure prevents confirmation bias.

### Stage 3 — Scenario Modeling
A separate Claude agent synthesizes each bull/bear debate into probability-weighted scenarios (bull/base/bear) with price targets at 1, 3, 6, and 12 months.

### Stage 4 — Portfolio Construction
An "agent of agents" optimizer selects exactly 15 positions subject to:
- 2-12% weight per position
- Max 35% in any single sector
- Every position must have positive expected 6-month return
- Max 3 positions per sector

### Intraday Monitoring
During market hours, a day-trading module monitors positions using 5-minute intraday data and evaluates whether material news or extreme moves warrant urgent trades. Conservative by design — most days it recommends nothing.

## Sector Coverage

Research is enforced across all 11 GICS sectors:

| Sector | Min Candidates |
|--------|---------------|
| Technology | 5 |
| Health Care | 4 |
| Financials | 4 |
| Consumer Discretionary | 4 |
| Industrials | 4 |
| Communication Services | 3 |
| Consumer Staples | 3 |
| Energy | 3 |
| Utilities | 2 |
| Real Estate | 2 |
| Materials | 2 |

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here
```

## Usage

```bash
# First run — full pipeline (screen, research, model, construct, trade)
python run_pipeline.py --force-rebalance

# Daily snapshot (update prices, P&L, sector performance)
python run_pipeline.py --snapshot-only

# Intraday monitoring (day trades during market hours)
python run_pipeline.py --intraday

# Rerun full pipeline and rebuild portfolio
python run_pipeline.py --force-rebalance

# Verbose logging
python run_pipeline.py -v
```

## Data Sources

All free:
- **yfinance** — stock prices, fundamentals, analyst consensus, news, intraday bars
- **Wikipedia** — Russell 1000 / S&P 500 constituent lists (with hardcoded fallback)

## Project Structure

```
ad-capital-v0/
  run_pipeline.py          # Main entry point
  config.yaml              # Portfolio rules and model settings
  src/
    universe.py            # Russell 1000 universe fetcher
    data_sources.py        # yfinance data layer
    screener.py            # Stage 1: fundamental screening
    research.py            # Stage 2: adversarial bull/bear debate
    modeler.py             # Stage 3: scenario modeling
    optimizer.py           # Stage 4: portfolio construction
    portfolio.py           # Paper trading + P&L tracking
    sectors.py             # GICS sector definitions + per-sector tracking
    day_trader.py          # Intraday monitoring + day trades
    report.py              # Daily markdown reports
  data/
    portfolio.json         # Current portfolio state (gitignored)
    trades.json            # Full trade history (gitignored)
    snapshots/             # Daily JSON snapshots (gitignored)
    reports/               # Daily markdown reports (gitignored)
    artifacts/             # Pipeline debug artifacts
  .github/workflows/
    daily_pipeline.yml     # Optional GitHub Actions automation
```

## Paper Trading

This is a paper-trading system only. No real money is involved. Portfolio state is tracked in local JSON files.

## Cost

A full pipeline run makes roughly 100-150 Claude API calls (mostly Sonnet). Expect ~$1-3 per full rebalance. Daily snapshots and intraday monitoring are much cheaper (~$0.05 each).
