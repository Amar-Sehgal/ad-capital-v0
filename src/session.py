"""Session state management for overnight autonomous trading loops.

Tracks current phase, accumulated research, trade ideas, and execution state
so that work persists across loop iterations (ScheduleWakeup cycles).
"""

import json
import os
from datetime import datetime, date, time, timezone, timedelta

SESSION_FILE = "data/session.json"
RESEARCH_DIR = "data/research"
IDEAS_DIR = "data/ideas"

# US Eastern Time
ET = timezone(timedelta(hours=-4))  # EDT
PT = timezone(timedelta(hours=-7))  # PDT


def now_et() -> datetime:
    return datetime.now(ET)


def now_pt() -> datetime:
    return datetime.now(PT)


def get_phase() -> str:
    """Determine current trading phase based on time.

    Returns one of:
        overnight_research - 1:00 AM to 6:00 AM PT (research, no trading)
        pre_market         - 6:00 AM to 6:30 AM PT (finalize plans)
        market_open        - 6:30 AM to 1:00 PM PT (9:30 AM - 4:00 PM ET, active trading)
        post_market        - 1:00 PM+ PT (summaries, next-day prep)
        off_hours          - before 1:00 AM PT (user hasn't started session yet)
    """
    now = now_pt()
    t = now.time()
    weekday = now.weekday()  # 0=Monday

    # Weekend
    if weekday >= 5:
        return "weekend_research"

    if t < time(1, 0):
        return "off_hours"
    elif t < time(6, 0):
        return "overnight_research"
    elif t < time(6, 30):
        return "pre_market"
    elif t < time(13, 0):
        return "market_open"
    else:
        return "post_market"


def get_loop_interval_seconds(phase: str) -> int:
    """Return the appropriate ScheduleWakeup interval for the current phase.

    Optimized for prompt cache TTL (5 min = 300s):
    - Under 270s: cache stays warm (good for active phases)
    - Over 300s: cache miss, but saves cost for idle phases
    """
    intervals = {
        "overnight_research": 1200,  # 20 min — research pace, cache miss is fine
        "weekend_research": 1800,    # 30 min — weekend research
        "pre_market": 270,           # 4.5 min — prep mode, cache warm
        "market_open": 270,          # 4.5 min — active trading, cache warm
        "post_market": 1800,         # 30 min — idle, summaries on demand
        "off_hours": 1800,           # 30 min — minimal
    }
    return intervals.get(phase, 1200)


def load_session() -> dict:
    """Load current session state."""
    os.makedirs(os.path.dirname(SESSION_FILE) or ".", exist_ok=True)
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f:
            return json.load(f)
    return _new_session()


def save_session(state: dict):
    """Persist session state."""
    state["last_updated"] = datetime.now().isoformat()
    with open(SESSION_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _new_session() -> dict:
    return {
        "date": date.today().isoformat(),
        "started_at": None,
        "phase": "off_hours",
        "loop_count": 0,
        "sectors_researched": [],
        "ideas_generated": 0,
        "trades_executed_today": 0,
        "last_research_sector": None,
        "research_queue": [
            "Technology", "Health Care", "Financials",
            "Consumer Discretionary", "Communication Services",
            "Industrials", "Consumer Staples", "Energy",
            "Utilities", "Real Estate", "Materials",
        ],
        "last_updated": None,
        "notes": [],
    }


def reset_session() -> dict:
    """Start a fresh session for today."""
    state = _new_session()
    state["started_at"] = datetime.now().isoformat()
    state["phase"] = get_phase()
    save_session(state)
    return state


def record_loop(state: dict, phase: str, action_summary: str):
    """Record a loop iteration."""
    state["loop_count"] += 1
    state["phase"] = phase
    state["notes"].append({
        "loop": state["loop_count"],
        "time": datetime.now().strftime("%H:%M:%S PT"),
        "phase": phase,
        "action": action_summary,
    })
    # Keep last 50 notes to avoid bloat
    if len(state["notes"]) > 50:
        state["notes"] = state["notes"][-50:]
    save_session(state)


# --- Research notes ---

def save_research(sector: str, content: str):
    """Save research notes for a sector."""
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    today = date.today().isoformat()
    filepath = os.path.join(RESEARCH_DIR, f"{today}_{sector.lower().replace(' ', '_')}.md")

    # Append if file exists (multiple research passes)
    mode = "a" if os.path.exists(filepath) else "w"
    with open(filepath, mode) as f:
        if mode == "w":
            f.write(f"# {sector} Research — {today}\n\n")
        f.write(f"## Update {datetime.now().strftime('%H:%M PT')}\n\n")
        f.write(content)
        f.write("\n\n")


def load_research(sector: str) -> str | None:
    """Load today's research for a sector."""
    today = date.today().isoformat()
    filepath = os.path.join(RESEARCH_DIR, f"{today}_{sector.lower().replace(' ', '_')}.md")
    if os.path.exists(filepath):
        with open(filepath) as f:
            return f.read()
    return None


def load_all_research() -> dict[str, str]:
    """Load all of today's research notes."""
    today = date.today().isoformat()
    research = {}
    if not os.path.exists(RESEARCH_DIR):
        return research
    for f in os.listdir(RESEARCH_DIR):
        if f.startswith(today) and f.endswith(".md"):
            sector = f.replace(today + "_", "").replace(".md", "").replace("_", " ").title()
            with open(os.path.join(RESEARCH_DIR, f)) as fh:
                research[sector] = fh.read()
    return research


# --- Trade ideas ---

def save_idea(idea: dict):
    """Save a trade idea."""
    os.makedirs(IDEAS_DIR, exist_ok=True)
    today = date.today().isoformat()
    filepath = os.path.join(IDEAS_DIR, f"{today}.json")

    ideas = []
    if os.path.exists(filepath):
        with open(filepath) as f:
            ideas = json.load(f)

    idea["timestamp"] = datetime.now().isoformat()
    idea["id"] = len(ideas) + 1
    ideas.append(idea)

    with open(filepath, "w") as f:
        json.dump(ideas, f, indent=2, default=str)


def load_ideas() -> list[dict]:
    """Load today's trade ideas."""
    today = date.today().isoformat()
    filepath = os.path.join(IDEAS_DIR, f"{today}.json")
    if os.path.exists(filepath):
        with open(filepath) as f:
            return json.load(f)
    return []


def get_unexecuted_ideas() -> list[dict]:
    """Get ideas that haven't been traded yet."""
    ideas = load_ideas()
    return [i for i in ideas if not i.get("executed")]


def mark_idea_executed(idea_id: int):
    """Mark a trade idea as executed."""
    today = date.today().isoformat()
    filepath = os.path.join(IDEAS_DIR, f"{today}.json")
    if not os.path.exists(filepath):
        return
    with open(filepath) as f:
        ideas = json.load(f)
    for i in ideas:
        if i.get("id") == idea_id:
            i["executed"] = True
            i["executed_at"] = datetime.now().isoformat()
    with open(filepath, "w") as f:
        json.dump(ideas, f, indent=2, default=str)
