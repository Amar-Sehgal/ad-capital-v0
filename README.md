# AD Capital v0

AI-driven paper trading portfolio. Claude Code does the research (web search, news, fundamentals analysis) and uses a CLI toolkit to manage a $1,000 paper portfolio across all 11 GICS sectors of the S&P 500.

## How It Works

**Claude Code is the trader.** It uses web search for real-time news and research, analyzes fundamentals via yfinance, and makes trading decisions. The Python CLI handles portfolio state, trade logging, and reporting.

### Research Approach
- Adversarial thinking: considers both bull and bear cases for every position
- Covers all 11 GICS sectors (Technology, Healthcare, Financials, Consumer Discretionary, Communication Services, Industrials, Consumer Staples, Energy, Utilities, Real Estate, Materials)
- 7-day rolling news window for catalyst identification
- S&P 500 watchlist tracks movers and sector rotation across all 500 stocks

### Trading Rules
- $1,000 starting capital, fractional shares allowed, stocks only
- Max 15 concurrent positions
- Max 35% in any single sector
- Every position requires documented rationale, conviction level, and catalyst type
- Both swing trades and intraday trades during market hours

## CLI Commands

```bash
# Initialize portfolio
python portfolio_cli.py init

# Portfolio status with live prices
python portfolio_cli.py status

# Execute a paper trade
python portfolio_cli.py trade BUY AVGO 2.5 "AI chip demand catalyst" --conviction HIGH --catalyst earnings
python portfolio_cli.py trade SELL MSFT 1.0 "Rotating out after weak guidance" --price 420.00

# Daily snapshot (update prices + P&L)
python portfolio_cli.py snapshot

# Sector allocation breakdown
python portfolio_cli.py sectors

# Trade history with rationale
python portfolio_cli.py history --last 20

# S&P 500 watchlist
python portfolio_cli.py watchlist              # sector summary
python portfolio_cli.py watchlist --sector Tech  # drill into sector
python portfolio_cli.py watchlist --scan         # scan for movers

# Generate daily markdown report
python portfolio_cli.py report
```

## Setup

```bash
pip install -r requirements.txt
```

No API keys needed for the CLI. Claude Code handles all AI research directly.

## Sector Coverage

| Sector | GICS | Focus Areas |
|--------|------|-------------|
| Technology | IT | Semis, software, cloud, cybersecurity, AI infra |
| Health Care | HC | Pharma, biotech, devices, managed care |
| Financials | FN | Banks, insurance, capital markets, fintech |
| Consumer Discretionary | CD | Retail, autos, travel, e-commerce |
| Communication Services | CS | Media, telecom, gaming, streaming |
| Industrials | IN | A&D, machinery, transport, engineering |
| Consumer Staples | CS | Food, beverages, household products |
| Energy | EN | Oil & gas, renewables, pipelines |
| Utilities | UT | Electric, gas, water, renewables |
| Real Estate | RE | REITs (data center, industrial, residential) |
| Materials | MT | Chemicals, metals, mining, gold |

## Data

All free via yfinance:
- Real-time and historical stock prices
- Company fundamentals and analyst consensus
- News headlines
- Intraday 5-minute bars (during market hours)
- S&P 500 constituents from Wikipedia

## Paper Trading

No real money. All trades are simulated with fractional share support. Portfolio state persists in local JSON files.
