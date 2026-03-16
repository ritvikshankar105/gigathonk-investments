"""
Gigathonk memory system.
Tracks actual positions (from Telegram replies) and past recommendations.
Designed for daily runs.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

MEMORY_FILE    = Path(__file__).parent / "memory.json"
POSITIONS_FILE = Path(__file__).parent / "positions.json"


def load_memory() -> list[dict]:
    if not MEMORY_FILE.exists():
        return []
    with open(MEMORY_FILE) as f:
        return json.load(f)


def load_positions() -> dict:
    if not POSITIONS_FILE.exists():
        return {}
    with open(POSITIONS_FILE) as f:
        return json.load(f)


def save_run(recommendations: list[dict], avoided: list[str], macro_snapshot: dict):
    memory = load_memory()
    memory.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "recommendations": recommendations,
        "avoided": avoided,
        "macro": macro_snapshot,
    })
    # Cap at 90 days — ledger.json is the permanent record, memory.json is operational
    cutoff = datetime.now() - timedelta(days=90)
    memory = [m for m in memory if datetime.strptime(m["date"], "%Y-%m-%d") >= cutoff]
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


def get_current_price(ticker: str) -> float | None:
    try:
        info = yf.Ticker(ticker).info
        return info.get("regularMarketPrice") or info.get("currentPrice")
    except Exception:
        return None


def get_memory_context() -> str:
    positions = load_positions()
    memory    = load_memory()
    lines     = []

    # ── Actual positions ──────────────────────────────────────────────────────
    if positions:
        lines.append("ACTUAL POSITIONS (confirmed by investor):")
        total_deployed = 0.0

        for ticker, pos in positions.items():
            cad_deployed  = pos.get("cad_deployed", 0)
            price_at_buy  = pos.get("price_usd_at_buy")
            buy_date      = pos.get("date", "unknown")
            days_held     = (datetime.now() - datetime.strptime(buy_date, "%Y-%m-%d")).days if buy_date != "unknown" else 0
            current_price = get_current_price(ticker)
            total_deployed += cad_deployed

            if price_at_buy and current_price:
                pct      = ((current_price - price_at_buy) / price_at_buy) * 100
                cad_gain = cad_deployed * (pct / 100)
                perf     = f"{pct:+.1f}% ({cad_gain:+.0f} CAD) | was ${price_at_buy:.2f}, now ${current_price:.2f}"
            else:
                perf = "price unavailable"

            lines.append(f"  {ticker} | ${cad_deployed:.0f} CAD deployed | {days_held}d held | {perf}")
            if pos.get("last_added"):
                lines.append(f"    Last added: {pos['last_added']}")

        lines.append(f"  Total deployed: ${total_deployed:.0f} CAD")
        lines.append("")
        lines.append("For each position: is the thesis intact? Should the investor add, hold, or exit?")
        lines.append("Flag any position up >20% for partial profit consideration.")
        lines.append("Flag any position down >10% — is the thesis broken or is the entry better now?")
    else:
        lines.append("ACTUAL POSITIONS: None yet. Investor has not confirmed any buys.")

    # ── Recent recommendations (last 14 days) for context ────────────────────
    cutoff = datetime.now() - timedelta(days=14)
    recent = [
        m for m in memory
        if datetime.strptime(m["date"], "%Y-%m-%d") >= cutoff
    ]

    if recent:
        lines.append("\nRECENT RECOMMENDATIONS (last 14 days, may or may not have been acted on):")
        # Deduplicate — show each ticker once with its original rec date
        seen: dict[str, str] = {}
        for run in recent:
            for rec in run.get("recommendations", []):
                ticker = rec.get("ticker", "")
                if ticker and ticker not in seen:
                    seen[ticker] = run["date"]

        for ticker, rec_date in seen.items():
            in_positions = ticker in positions
            status = "BOUGHT" if in_positions else "not confirmed bought"
            lines.append(f"  {ticker} — recommended {rec_date} | {status}")

        # Recently avoided (last 3 days only)
        avoid_cutoff = datetime.now() - timedelta(days=3)
        recent_avoided: set[str] = set()
        for run in recent:
            if datetime.strptime(run["date"], "%Y-%m-%d") >= avoid_cutoff:
                recent_avoided.update(run.get("avoided", []))
        if recent_avoided:
            lines.append(f"\nRecently avoided (last 3 days): {', '.join(sorted(recent_avoided))}")

    return "\n".join(lines)
