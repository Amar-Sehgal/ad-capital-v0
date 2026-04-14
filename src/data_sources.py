"""Free data sources layer — yfinance for fundamentals, prices, and news."""

import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


def get_stock_info(ticker: str) -> dict | None:
    """Fetch comprehensive fundamental data for a single ticker."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        if not info or not info.get("regularMarketPrice") and not info.get("currentPrice"):
            return None
        return {
            "ticker": ticker,
            "name": info.get("longName", info.get("shortName", ticker)),
            "sector": info.get("sector", "Unknown"),
            "industry": info.get("industry", ""),
            "market_cap": info.get("marketCap", 0),
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "pe_forward": info.get("forwardPE"),
            "pe_trailing": info.get("trailingPE"),
            "peg_ratio": info.get("pegRatio"),
            "price_to_book": info.get("priceToBook"),
            "ev_to_ebitda": info.get("enterpriseToEbitda"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "profit_margin": info.get("profitMargins"),
            "operating_margin": info.get("operatingMargins"),
            "return_on_equity": info.get("returnOnEquity"),
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "free_cash_flow": info.get("freeCashflow"),
            "beta": info.get("beta"),
            "dividend_yield": info.get("dividendYield"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
            "target_mean_price": info.get("targetMeanPrice"),
            "target_high_price": info.get("targetHighPrice"),
            "target_low_price": info.get("targetLowPrice"),
            "recommendation": info.get("recommendationKey", ""),
            "analyst_count": info.get("numberOfAnalystOpinions", 0),
        }
    except Exception as e:
        log.debug("Failed to fetch info for %s: %s", ticker, e)
        return None


def get_recent_news(ticker: str, days: int = 7) -> list[dict]:
    """Fetch recent news headlines for a ticker from yfinance."""
    try:
        stock = yf.Ticker(ticker)
        news = stock.news or []
        cutoff = datetime.now() - timedelta(days=days)
        recent = []
        for article in news:
            pub_time = article.get("providerPublishTime", 0)
            if pub_time:
                pub_dt = datetime.fromtimestamp(pub_time)
                if pub_dt >= cutoff:
                    recent.append({
                        "title": article.get("title", ""),
                        "publisher": article.get("publisher", ""),
                        "published": pub_dt.strftime("%Y-%m-%d"),
                    })
            else:
                # Include if we can't parse the date (better to have too much than too little)
                recent.append({
                    "title": article.get("title", ""),
                    "publisher": article.get("publisher", ""),
                    "published": "unknown",
                })
        return recent[:15]
    except Exception as e:
        log.debug("Failed to fetch news for %s: %s", ticker, e)
        return []


def get_batch_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch current closing prices for a list of tickers."""
    if not tickers:
        return {}
    try:
        data = yf.download(tickers, period="5d", progress=False, threads=True)
        prices = {}
        close = data["Close"] if "Close" in data.columns or isinstance(data.columns, pd.MultiIndex) else data
        if isinstance(close, pd.Series):
            # Single ticker
            val = close.dropna().iloc[-1] if not close.dropna().empty else None
            if val is not None:
                prices[tickers[0]] = round(float(val), 2)
        else:
            for ticker in tickers:
                try:
                    col = close[ticker].dropna()
                    if not col.empty:
                        prices[ticker] = round(float(col.iloc[-1]), 2)
                except Exception:
                    pass
        return prices
    except Exception as e:
        log.warning("Batch price fetch failed: %s", e)
        return {}


def get_benchmark_history(ticker: str = "SPY", start: str | None = None) -> pd.DataFrame:
    """Fetch benchmark price history from a start date."""
    stock = yf.Ticker(ticker)
    if start:
        return stock.history(start=start)
    return stock.history(period="1y")


def get_bulk_fundamentals(tickers: list[str]) -> list[dict]:
    """Fetch fundamentals for many tickers. Returns list of valid results."""
    results = []
    for ticker in tickers:
        info = get_stock_info(ticker)
        if info and info.get("market_cap") and info["market_cap"] > 0:
            results.append(info)
    return results
