# CLAUDE.md — AD Capital v0

## What This Is
AI-driven paper trading portfolio. Claude Code is the trader — it does research via web search, makes decisions, and manages positions through a Python CLI toolkit. $1,000 starting capital split across two books.

## Two Books
- **Sector Fund** ($700, 70%): Thesis-driven, research-backed positions across 11 GICS sectors. Swing/position trades held days to weeks.
- **Quant Book** ($300, 30%): Signal-driven, statistical trades. Uses ported strategies from ~/personal/stock_prediction/ (MultiFactorV3, TrendPullback, mean reversion, momentum, etc.). Shorter holding periods.
- Capital can move between books based on opportunity. Convergence trades (quant signal + fundamental thesis agree) get higher conviction sizing.

## How to Run a Trading Session

The user starts a session by saying something like "start the overnight/trading session." Use `/loop` to self-pace. The workflow:

### Phase Detection (all times Pacific)
- **1:00-6:00 AM**: Overnight research. Web search all 11 sectors for news, catalysts, earnings. Save research notes and trade ideas to disk. Wake every ~20 min.
- **6:00-6:30 AM**: Pre-market. Finalize trade plan, check futures/oil/gold. Wake every ~5 min.
- **6:30 AM-1:00 PM**: Market open (9:30 AM-4:00 PM ET). Execute trades, monitor positions, react to intraday signals and breaking news. Wake every ~5 min.
- **After 1:00 PM**: Post-market. Generate summary, take snapshot. Longer intervals or stop.

### During Each Loop Iteration
1. Check phase: `python3 ~/personal/ad-capital-v0/portfolio_cli.py session`
2. Based on phase, either:
   - Research: WebSearch for sector news, save via session.save_research(), save_idea()
   - Trade: Execute via `python3 ~/personal/ad-capital-v0/portfolio_cli.py trade BUY/SELL TICKER SHARES "rationale" --conviction HIGH --catalyst news`
   - Monitor: Check status, run quant signals on holdings
3. Update session state: `python3 -c "from src.session import ..."`
4. ScheduleWakeup for next iteration

### Key CLI Commands (always run from ~/personal/ad-capital-v0/)
```bash
python3 ~/personal/ad-capital-v0/portfolio_cli.py status          # portfolio status
python3 ~/personal/ad-capital-v0/portfolio_cli.py overview        # both books combined
python3 ~/personal/ad-capital-v0/portfolio_cli.py trade BUY AVGO 0.5 "rationale" --conviction HIGH --catalyst research
python3 ~/personal/ad-capital-v0/portfolio_cli.py snapshot        # update prices
python3 ~/personal/ad-capital-v0/portfolio_cli.py sectors         # sector breakdown
python3 ~/personal/ad-capital-v0/portfolio_cli.py history --last 10
python3 ~/personal/ad-capital-v0/portfolio_cli.py session         # session state
python3 ~/personal/ad-capital-v0/portfolio_cli.py ideas           # today's trade ideas
python3 ~/personal/ad-capital-v0/portfolio_cli.py research --sector Technology
python3 ~/personal/ad-capital-v0/portfolio_cli.py quant status    # quant book
python3 ~/personal/ad-capital-v0/portfolio_cli.py quant mfv3 --ticker AVGO
python3 ~/personal/ad-capital-v0/portfolio_cli.py quant mfv3 --tickers AVGO,NVDA,TSM
python3 ~/personal/ad-capital-v0/portfolio_cli.py quant pullback --ticker XOM
python3 ~/personal/ad-capital-v0/portfolio_cli.py quant signals AVGO
python3 ~/personal/ad-capital-v0/portfolio_cli.py quant scan --tickers AVGO,NVDA
python3 ~/personal/ad-capital-v0/portfolio_cli.py watchlist --scan  # S&P 500 movers
python3 ~/personal/ad-capital-v0/portfolio_cli.py report          # daily report
```

## Portfolio Rules
- Long only (buy and sell to 0, no shorting)
- Fractional shares allowed, stocks only
- **No cap on position count** — if 100 stocks have equal conviction, buy all 100. Size proportionally to conviction.
- Max 35% in any single GICS sector
- Every sector fund position needs documented rationale, conviction (LOW/MEDIUM/HIGH), and catalyst type
- Every quant trade needs strategy name, signal, entry rule, exit rule
- 11 GICS sectors: Technology, Health Care, Financials, Consumer Discretionary, Communication Services, Industrials, Consumer Staples, Energy, Utilities, Real Estate, Materials

## Operational Style (user directive, 2026-04-15)
- **High-effort, broad, continuous analysis** — scan the full universe constantly, don't sit idle
- **Proportional sizing by conviction**, not share price. HIGH ~$25-30, MED ~$10-15, LOW ~$3-7. Share count irrelevant; % exposure is what matters.
- **Sector-level conviction shifts allowed**: if a sector thesis turns bullish/bearish, scale all holdings in that sector proportionally and reallocate to sectors with opposite conviction.
  - Example: bearish oil → trim VLO/COP/OXY/HAL proportionally → redeploy to Utilities/AI infra
- **Debate yourself adversarially** on every idea before execution. No hedging language.
- **Serialize quant trades** — parallel `&` execution of `quant trade` causes JSON write race conditions (HAL trade lost once). Always run sequentially.
- **Between check-ins**: run sector deep-dives (parallel Agent subagents OK), refresh MFV3 scans, update theses, identify sector-level shifts.

## Fund Rules (explicit, 2026-04-15)
- **Theses require time horizon + invalidation price**. Log both in trade rationale. Exit when invalidation price hits OR time horizon expires without thesis playing out.
- **No portfolio-level drawdown trigger** — ride through, exit on thesis changes only.
- **Concentration**: 35% sector cap is soft; OK to exceed with conviction. Single positions can exceed 10% if conviction HIGH.
- **Capital between sector/quant books**: shift at your discretion. 70/30 is starting split, not constraint.
- **Earnings**: hold through if thesis IS the earnings play. Otherwise position normally.
- **Restricted names**: none.
- **Cash buffer**: your discretion.
- **Quant book**: mechanical -5% stops, +5% targets (keeps systematic character).

## Time handling (critical)
Your internal clock drifts. Always verify with:
```bash
python3 -c "from datetime import datetime; import zoneinfo; print(datetime.now(zoneinfo.ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M PT'))"
```
During market hours (6:30 AM - 1:00 PM PT), tick as frequently as possible. Outside market hours, loop 1200-1800s.

## Current State (as of April 13, 2026)
- Portfolio initialized: $700 sector fund + $300 quant book = $1,000
- Benchmark: SPY @ $686.10
- No positions yet — first trades planned for Tuesday April 14 market open
- Research complete for all 11 sectors + macro
- 12 trade ideas saved (10 BUY, 2 WATCH)
- Dominant macro themes: Iran/Hormuz blockade (oil $105), CPI spike to 3.3%, earnings season starting (JPM April 14, TSMC April 16), Fed on hold

## Key Files
- `config.yaml` — portfolio rules, capital allocation
- `portfolio_cli.py` — main CLI for all operations
- `src/session.py` — session state, research notes, trade ideas persistence
- `src/quant.py` — quant book, signals, ported strategies (MFV3, TrendPullback)
- `src/portfolio.py` — sector fund portfolio management
- `src/sectors.py` — GICS sector definitions and tracking
- `src/watchlist.py` — S&P 500 watchlist
- `data/` — all state files (portfolio, trades, research, ideas, snapshots)
- `FUTURE_PLANS.md` — roadmap for risk limits, regime detection, factor tracking
- `~/personal/stock_prediction/` — source repo for quant strategies, has scanner.py for daily S&P 500 scans

## When the User Checks In
They'll ask for updates. Give them:
1. Current portfolio status (positions, P&L, alpha vs SPY)
2. Trades executed since they last checked, with rationale
3. Key news/developments across sectors
4. Open ideas you're watching
5. Any concerns or risk flags
