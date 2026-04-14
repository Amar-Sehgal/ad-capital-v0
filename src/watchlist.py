"""S&P 500 watchlist — track all 500 stocks across 11 GICS sectors."""

import json
import logging
import os
from datetime import date

import pandas as pd
import yfinance as yf

from src.sectors import normalize_sector, GICS_SECTORS

log = logging.getLogger(__name__)

WATCHLIST_FILE = "data/watchlist.json"
SP500_CACHE_FILE = "data/sp500_tickers.json"


def _fetch_sp500_tickers() -> list[dict]:
    """Fetch S&P 500 constituents with sectors from Wikipedia."""
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            match="Symbol",
        )
        if tables:
            df = tables[0]
            results = []
            for _, row in df.iterrows():
                ticker = str(row.get("Symbol", "")).strip().replace(".", "-")
                sector = normalize_sector(str(row.get("GICS Sector", "Unknown")))
                industry = str(row.get("GICS Sub-Industry", ""))
                name = str(row.get("Security", ""))
                results.append({
                    "ticker": ticker,
                    "name": name,
                    "sector": sector,
                    "industry": industry,
                })
            # Cache it
            os.makedirs(os.path.dirname(SP500_CACHE_FILE) or ".", exist_ok=True)
            with open(SP500_CACHE_FILE, "w") as f:
                json.dump({"date": date.today().isoformat(), "stocks": results}, f, indent=2)
            return results
    except Exception as e:
        log.warning("Wikipedia S&P 500 fetch failed: %s", e)

    # Try cache
    if os.path.exists(SP500_CACHE_FILE):
        with open(SP500_CACHE_FILE) as f:
            return json.load(f).get("stocks", [])
    return []


class SP500Watchlist:
    """Track S&P 500 stocks across all sectors."""

    def __init__(self):
        self.stocks = _fetch_sp500_tickers()
        self.by_sector: dict[str, list[dict]] = {}
        for s in self.stocks:
            sec = s["sector"]
            if sec not in self.by_sector:
                self.by_sector[sec] = []
            self.by_sector[sec].append(s)

    def show_summary(self):
        """Print sector-level summary of S&P 500."""
        print(f"\n{'='*60}")
        print(f" S&P 500 Watchlist — {len(self.stocks)} stocks, {len(self.by_sector)} sectors")
        print(f"{'='*60}")
        print(f" {'Sector':<28} {'Count':>6}  Sample Tickers")
        print(f" {'-'*28} {'-'*6}  {'-'*30}")
        for sector in sorted(self.by_sector, key=lambda s: len(self.by_sector[s]), reverse=True):
            stocks = self.by_sector[sector]
            sample = ", ".join(s["ticker"] for s in stocks[:5])
            if len(stocks) > 5:
                sample += f" +{len(stocks)-5} more"
            print(f" {sector:<28} {len(stocks):>6}  {sample}")
        print(f"{'='*60}\n")

    def show_sector(self, sector_name: str):
        """Show all stocks in a given sector."""
        # Fuzzy match sector name
        matched = None
        for sec in self.by_sector:
            if sector_name.lower() in sec.lower():
                matched = sec
                break
        if not matched:
            print(f"Sector '{sector_name}' not found. Available: {', '.join(self.by_sector.keys())}")
            return

        stocks = self.by_sector[matched]
        print(f"\n{'='*70}")
        print(f" {matched} — {len(stocks)} stocks")
        print(f"{'='*70}")
        print(f" {'Ticker':<8} {'Name':<35} {'Industry'}")
        print(f" {'-'*8} {'-'*35} {'-'*30}")
        for s in sorted(stocks, key=lambda x: x["ticker"]):
            print(f" {s['ticker']:<8} {s['name'][:34]:<35} {s['industry'][:30]}")
        print(f"{'='*70}\n")

    def scan_movers(self):
        """Scan for significant daily movers across all sectors."""
        print(f"\nScanning S&P 500 for today's movers...")
        print("(This fetches data for all 500 stocks — takes ~60 seconds)\n")

        all_tickers = [s["ticker"] for s in self.stocks]
        ticker_to_sector = {s["ticker"]: s["sector"] for s in self.stocks}

        # Batch download
        try:
            data = yf.download(all_tickers, period="2d", progress=False, threads=True)
            close = data["Close"]
        except Exception as e:
            print(f"Download failed: {e}")
            return

        movers = []
        for ticker in all_tickers:
            try:
                col = close[ticker].dropna()
                if len(col) >= 2:
                    prev = float(col.iloc[-2])
                    curr = float(col.iloc[-1])
                    chg = ((curr / prev) - 1) * 100
                    movers.append({
                        "ticker": ticker,
                        "price": round(curr, 2),
                        "change_pct": round(chg, 2),
                        "sector": ticker_to_sector.get(ticker, "?"),
                    })
            except Exception:
                pass

        movers.sort(key=lambda m: m["change_pct"])

        # Top gainers
        gainers = movers[-10:][::-1]
        losers = movers[:10]

        print(f"{'='*55}")
        print(f" TOP 10 GAINERS")
        print(f"{'='*55}")
        print(f" {'Ticker':<8} {'Price':>10} {'Change':>10}  {'Sector'}")
        print(f" {'-'*8} {'-'*10} {'-'*10}  {'-'*20}")
        for m in gainers:
            px = f"${m['price']:,.2f}"
            chg = f"{m['change_pct']:+.2f}%"
            print(f" {m['ticker']:<8} {px:>10} {chg:>10}  {m['sector']}")

        print(f"\n{'='*55}")
        print(f" TOP 10 LOSERS")
        print(f"{'='*55}")
        print(f" {'Ticker':<8} {'Price':>10} {'Change':>10}  {'Sector'}")
        print(f" {'-'*8} {'-'*10} {'-'*10}  {'-'*20}")
        for m in losers:
            px = f"${m['price']:,.2f}"
            chg = f"{m['change_pct']:+.2f}"
            print(f" {m['ticker']:<8} {px:>10} {chg:>10}  {m['sector']}")

        # Sector averages
        sector_chg: dict[str, list[float]] = {}
        for m in movers:
            sec = m["sector"]
            if sec not in sector_chg:
                sector_chg[sec] = []
            sector_chg[sec].append(m["change_pct"])

        print(f"\n{'='*55}")
        print(f" SECTOR HEATMAP")
        print(f"{'='*55}")
        print(f" {'Sector':<28} {'Avg Change':>12} {'# Stocks':>10}")
        print(f" {'-'*28} {'-'*12} {'-'*10}")
        for sec in sorted(sector_chg, key=lambda s: sum(sector_chg[s])/len(sector_chg[s]), reverse=True):
            changes = sector_chg[sec]
            avg = sum(changes) / len(changes)
            print(f" {sec:<28} {f'{avg:+.2f}%':>12} {len(changes):>10}")
        print(f"{'='*55}\n")

        # Save for later reference
        os.makedirs("data", exist_ok=True)
        with open(WATCHLIST_FILE, "w") as f:
            json.dump({
                "date": date.today().isoformat(),
                "gainers": gainers,
                "losers": losers,
                "movers": movers,
            }, f, indent=2)

    def get_sector_tickers(self, sector: str) -> list[str]:
        """Get all tickers for a sector."""
        for sec, stocks in self.by_sector.items():
            if sector.lower() in sec.lower():
                return [s["ticker"] for s in stocks]
        return []

    def get_all_tickers(self) -> list[str]:
        """Get all S&P 500 tickers."""
        return [s["ticker"] for s in self.stocks]
