# AD Capital v0 — Future Plans

Ideas drawn from how multi-strategy hedge funds operate, prioritized by effort-to-value ratio. To be implemented incrementally as the portfolio matures.

---

## Priority 1: High Value, Easy to Build

### Risk Limits / Circuit Breakers
Every fund has hard stops. We currently have none.

- **Daily drawdown limit**: If portfolio drops X% in a day, stop all trading and go to cash. Prevents blowup days from compounding. Start with 3% daily.
- **Per-position max loss**: If a single position hits -8% from entry, cut it regardless of thesis. Thesis can be wrong.
- **Correlation cap / factor concentration**: If multiple positions are the same underlying bet (e.g., DVN + VLO = one oil bet), enforce a max notional exposure to any single factor. Prevents 40% energy getting wiped on a peace deal.
- **Max single-position size**: Hard cap at 12% of total portfolio.
- **Implementation**: Config params in config.yaml, pre-trade checks in portfolio_cli.py, automatic alerts during overnight/trading sessions.

### Event Calendar
Structured forward-looking calendar of scheduled catalysts, maintained as a JSON file and checked each session.

- Earnings dates for all holdings and watchlist names
- FOMC meetings and Fed speaker schedule
- Macro releases: CPI, PPI, jobs report, GDP
- Options expiry dates (quad witching = volatility)
- Sector-specific: FDA PDUFA dates, OPEC meetings, contract expirations, index rebalances
- **Implementation**: `data/event_calendar.json`, checked at start of each session, web-searched weekly to refresh.

### Exit Journaling / Post-Mortems
Every closed trade gets a post-mortem — was the thesis right, was the timing right, what would you do differently?

- Add exit notes and a grade (A through F) on each completed round-trip
- Track: thesis accuracy, timing quality, was the catalyst what we expected
- Over time builds a feedback loop across sessions
- **Implementation**: Add `--exit-notes` and `--grade` flags to SELL trades in portfolio_cli.py. Periodic summary of win/loss patterns.

### Regime Tag
One field in session state classifying the current market environment. Updated manually each session based on research.

- **Risk-on**: VIX low, breadth expanding, credit tight. Lean into growth, quant momentum, reduce defensives.
- **Risk-off**: VIX spiking, correlations rising, flight to quality. Lean into staples, gold, utilities, reduce beta.
- **Inflationary**: CPI rising, commodities up, real rates falling. Energy, materials, avoid long-duration.
- **Deflationary**: CPI falling, yields collapsing. Growth/tech, avoid commodities.
- **Implementation**: Field in `data/session.json`, logged in daily snapshots, influences allocation tilt between sector fund and quant book.

---

## Priority 2: Medium Value, Moderate Effort

### Gross/Net Exposure Tracking
Actively manage what percentage of capital is deployed vs. cash. This should be a conscious, tracked decision.

- High conviction environment: 80-90% deployed
- Uncertain environment: 50-60% deployed
- Crisis: 20-30% deployed or less
- **Implementation**: Track in daily snapshots, alert when deployment drifts from target range. Add target deployment to session state based on regime.

### Regime Detection (Automated)
Move from manual regime tagging to a quantitative classifier using:

- VIX level and trend
- CPI trend (rising/falling/stable)
- Yield curve (inverted/steep/flat)
- Oil price trend
- Credit spreads (HY-IG spread)
- Market breadth (advance/decline ratio)
- **Implementation**: Fetch VIX, TNX, oil from yfinance. Classify into regime buckets. Auto-adjust sector/quant allocation tilt.

### Convergence Scoring
Formalize the signal convergence across books (quant signal + fundamental thesis = higher conviction).

- When MFV3 flashes BUY on a name that also has a sector fund thesis, flag it as convergence
- Convergence trades get larger position sizing (up to 10-12% combined)
- Track convergence hit rate separately to validate the approach
- **Implementation**: Cross-reference quant scan results with sector fund ideas, auto-flag overlaps in daily research.

### Capital Transfer Tracking
Log inter-book capital transfers explicitly so per-book returns remain honest.

- Record: date, amount, direction (sector->quant or quant->sector), reason
- Adjust book-level return calculations to account for transfers
- Weekly reconciliation of book-level P&L
- **Implementation**: `data/transfers.json`, adjusted return calculation in overview command.

---

## Priority 3: High Value, Requires History

### Factor Exposure Tracking
Think in factor terms, not just ticker terms. Two seemingly different stocks might be the same factor bet.

- **Momentum**: Are all picks recent winners? Exposed to momentum reversal.
- **Value**: Tilted toward cheap or expensive stocks?
- **Quality**: Profitability, low debt, stable earnings exposure.
- **Size**: Large cap vs small cap tilt.
- **Volatility/Beta**: High-beta or low-beta portfolio?
- **Implementation**: Compute factor loadings for each position using yfinance fundamentals. Aggregate to portfolio level. Alert when any single factor exceeds threshold. Requires 2-4 weeks of trading history to be meaningful.

### Performance Attribution
Decompose returns into sources: what's driving P&L?

- Sector allocation effect (over/underweight vs benchmark)
- Stock selection effect (picking winners within sectors)
- Factor attribution (how much return came from momentum vs value vs quality)
- Timing attribution (did entry/exit timing add or subtract value)
- **Implementation**: Weekly attribution report comparing portfolio vs SPY sector weights and returns.

### Strategy Decay Monitoring
Track whether each quant strategy's edge is degrading over time.

- Rolling Sharpe ratio per strategy (30-day, 90-day)
- Win rate trend
- Average win/loss ratio trend
- Alert when a strategy's rolling metrics fall below thresholds
- **Implementation**: Requires 30+ trades per strategy to be statistically meaningful.

---

## Priority 4: Aspirational / Complex

### Pairs Trading / Stat Arb
Already registered as a strategy but not implemented.

- Identify correlated pairs (e.g., XOM/CVX, GOOG/META, JPM/GS)
- Compute rolling z-score of the spread
- Enter when z-score > 2, exit on mean reversion
- **Implementation**: Need cointegration testing, rolling correlation, spread computation. Port from stock_prediction if backtests look good.

### Market Making Simulation
Simulate capturing bid-ask spread on high-volume names.

- Place simulated limit orders at bid/ask
- Track fill probability based on volume and price movement
- Compute theoretical spread capture net of adverse selection
- **Implementation**: Requires intraday data (yfinance 1m bars), fill simulation model. More of a research project.

### Options-Implied Signals
Use options market data to inform equity trades.

- Put/call ratio as sentiment indicator
- Implied volatility vs realized volatility (IV crush/expansion)
- Unusual options activity as a leading indicator
- **Implementation**: Need free options data source. Yahoo Finance has some, but quality varies.

### Multi-Timeframe Regime Blending
Run regime detection at multiple timeframes (daily, weekly, monthly) and blend.

- Daily regime for intraday/day trades
- Weekly regime for swing trades
- Monthly regime for sector allocation
- Conflicts between timeframes signal caution (reduce size)
- **Implementation**: Extension of automated regime detection.

### Backtesting Integration
Run strategies against historical data directly from the CLI.

- Port the simulator.py engine from stock_prediction
- `quant backtest --strategy multifactor_v3 --tickers AAPL,MSFT --period 2y`
- Compare live performance vs backtest expectations
- **Implementation**: Significant effort, but stock_prediction already has the engine. Adapt to work without backtesting.py dependency.

---

## Notes

- Start with Priority 1 items after the first week of live trading
- Factor exposure and performance attribution need 2-4 weeks of history
- The convergence scoring (quant + fundamental agreement) is the most unique edge of our two-book setup — prioritize formalizing it
- All implementations should be CLI commands and/or automatic checks during trading sessions, not standalone scripts
