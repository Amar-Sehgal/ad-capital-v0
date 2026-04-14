"""GICS sector definitions and per-sector performance tracking.

Uses the 11-sector GICS classification standard used by major hedge funds, index
providers (S&P/MSCI), and institutional investors.
"""

import json
import logging
import os
from datetime import date

log = logging.getLogger(__name__)

# 11 GICS sectors with representative sub-industries for screening diversity
GICS_SECTORS = {
    "Technology": {
        "description": "Software, hardware, semiconductors, IT services",
        "sub_industries": [
            "Semiconductors", "Software", "IT Services", "Hardware",
            "Cloud Computing", "Cybersecurity", "AI/ML Infrastructure",
        ],
        "min_candidates": 5,
    },
    "Health Care": {
        "description": "Pharma, biotech, medical devices, managed care, life sciences",
        "sub_industries": [
            "Pharmaceuticals", "Biotechnology", "Medical Devices",
            "Managed Care", "Life Sciences Tools", "Healthcare Services",
        ],
        "min_candidates": 4,
    },
    "Financials": {
        "description": "Banks, insurance, capital markets, fintech, asset management",
        "sub_industries": [
            "Banks", "Insurance", "Capital Markets", "Consumer Finance",
            "Asset Management", "Fintech", "REITs-Financial",
        ],
        "min_candidates": 4,
    },
    "Consumer Discretionary": {
        "description": "Retail, autos, apparel, travel, restaurants, e-commerce",
        "sub_industries": [
            "E-Commerce", "Restaurants", "Apparel", "Homebuilders",
            "Auto Manufacturers", "Leisure", "Specialty Retail",
        ],
        "min_candidates": 4,
    },
    "Communication Services": {
        "description": "Media, entertainment, telecom, social platforms, streaming",
        "sub_industries": [
            "Interactive Media", "Entertainment", "Telecom",
            "Advertising", "Gaming", "Streaming",
        ],
        "min_candidates": 3,
    },
    "Industrials": {
        "description": "Aerospace/defense, machinery, transportation, engineering",
        "sub_industries": [
            "Aerospace & Defense", "Machinery", "Railroads",
            "Airlines", "Electrical Equipment", "Construction",
        ],
        "min_candidates": 4,
    },
    "Consumer Staples": {
        "description": "Food, beverages, household products, tobacco, personal care",
        "sub_industries": [
            "Beverages", "Food Products", "Household Products",
            "Personal Care", "Tobacco", "Food Retail",
        ],
        "min_candidates": 3,
    },
    "Energy": {
        "description": "Oil & gas, renewables, pipelines, oilfield services",
        "sub_industries": [
            "Integrated Oil", "E&P", "Pipelines/MLPs",
            "Oilfield Services", "Renewables", "Refining",
        ],
        "min_candidates": 3,
    },
    "Utilities": {
        "description": "Electric, gas, water, renewable utilities, independent power",
        "sub_industries": [
            "Electric Utilities", "Gas Utilities", "Water Utilities",
            "Renewable Utilities", "Independent Power Producers",
        ],
        "min_candidates": 2,
    },
    "Real Estate": {
        "description": "REITs (data center, industrial, residential, retail), real estate services",
        "sub_industries": [
            "Data Center REITs", "Industrial REITs", "Residential REITs",
            "Retail REITs", "Office REITs", "Real Estate Services",
        ],
        "min_candidates": 2,
    },
    "Materials": {
        "description": "Chemicals, metals & mining, containers, construction materials, gold",
        "sub_industries": [
            "Specialty Chemicals", "Gold", "Copper/Metals",
            "Construction Materials", "Containers & Packaging",
        ],
        "min_candidates": 2,
    },
}

# Mapping from yfinance sector names to our normalized GICS names
SECTOR_ALIASES = {
    "Technology": "Technology",
    "Information Technology": "Technology",
    "Healthcare": "Health Care",
    "Health Care": "Health Care",
    "Financial Services": "Financials",
    "Financials": "Financials",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Discretionary": "Consumer Discretionary",
    "Communication Services": "Communication Services",
    "Telecommunications": "Communication Services",
    "Industrials": "Industrials",
    "Consumer Defensive": "Consumer Staples",
    "Consumer Staples": "Consumer Staples",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Basic Materials": "Materials",
    "Materials": "Materials",
}


def normalize_sector(raw_sector: str) -> str:
    """Normalize a yfinance sector name to GICS standard."""
    return SECTOR_ALIASES.get(raw_sector, raw_sector)


def get_sector_list() -> list[str]:
    """Return list of all 11 GICS sectors."""
    return list(GICS_SECTORS.keys())


def get_min_candidates_per_sector() -> dict[str, int]:
    """Return minimum screening candidates required per sector."""
    return {sector: info["min_candidates"] for sector, info in GICS_SECTORS.items()}


def enforce_sector_coverage(candidates: list[dict], all_stocks: list[dict]) -> list[dict]:
    """Ensure candidates include minimum representation from each sector.

    If a sector is under-represented, pull the highest-scored stocks from that
    sector in all_stocks to fill the gap. This prevents the portfolio from being
    blind to entire sectors.
    """
    min_per_sector = get_min_candidates_per_sector()

    # Count candidates per sector
    sector_counts: dict[str, int] = {}
    for c in candidates:
        sector = normalize_sector(c.get("fundamentals", {}).get("sector", "Unknown"))
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    candidate_tickers = {c["ticker"] for c in candidates}
    added = []

    for sector, min_count in min_per_sector.items():
        current = sector_counts.get(sector, 0)
        if current >= min_count:
            continue

        # Find stocks from this sector not already in candidates
        sector_pool = [
            s for s in all_stocks
            if normalize_sector(s.get("sector", "Unknown")) == sector
            and s["ticker"] not in candidate_tickers
        ]
        # Sort by a simple quality heuristic: analyst count * market cap
        sector_pool.sort(
            key=lambda s: (s.get("analyst_count", 0) or 0) * (s.get("market_cap", 0) or 0),
            reverse=True,
        )

        needed = min_count - current
        for s in sector_pool[:needed]:
            added.append({
                "ticker": s["ticker"],
                "score": 45,  # Default score for sector-fill candidates
                "reason": f"added for {sector} sector coverage",
                "fundamentals": s,
            })
            candidate_tickers.add(s["ticker"])
            log.info("Added %s for %s sector coverage", s["ticker"], sector)

    return candidates + added


class SectorTracker:
    """Track per-sector portfolio performance over time."""

    def __init__(self, tracker_file: str = "data/sector_performance.json"):
        self.tracker_file = tracker_file
        os.makedirs(os.path.dirname(tracker_file) or ".", exist_ok=True)

    def load(self) -> dict:
        if os.path.exists(self.tracker_file):
            with open(self.tracker_file) as f:
                return json.load(f)
        return {"history": [], "sector_allocations": {}}

    def save(self, data: dict):
        with open(self.tracker_file, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def record(self, snapshot: dict):
        """Record per-sector performance from a daily snapshot."""
        data = self.load()
        holdings = snapshot.get("holdings", [])
        total_value = snapshot.get("total_value", 0)

        sector_perf: dict[str, dict] = {}
        for h in holdings:
            sector = normalize_sector(h.get("sector", "Unknown"))
            if sector not in sector_perf:
                sector_perf[sector] = {
                    "value": 0, "pnl": 0, "weight": 0,
                    "tickers": [], "count": 0,
                }
            sp = sector_perf[sector]
            sp["value"] += h["value"]
            sp["pnl"] += h["pnl"]
            sp["weight"] += h["weight"]
            sp["tickers"].append(h["ticker"])
            sp["count"] += 1

        entry = {
            "date": date.today().isoformat(),
            "portfolio_return_pct": snapshot.get("portfolio_return_pct", 0),
            "benchmark_return_pct": snapshot.get("benchmark_return_pct", 0),
            "sectors": sector_perf,
        }
        data["history"].append(entry)
        data["sector_allocations"] = sector_perf

        self.save(data)
        return sector_perf

    def get_sector_summary(self) -> str:
        """Generate a text summary of sector performance."""
        data = self.load()
        alloc = data.get("sector_allocations", {})
        if not alloc:
            return "No sector data available yet."

        lines = ["Sector | Weight | P&L | Positions"]
        lines.append("-------|--------|-----|----------")
        for sector in sorted(alloc.keys(), key=lambda s: alloc[s]["weight"], reverse=True):
            sp = alloc[sector]
            lines.append(
                f"{sector} | {sp['weight']:.1f}% | ${sp['pnl']:+,.2f} | "
                f"{', '.join(sp['tickers'])}"
            )
        return "\n".join(lines)
