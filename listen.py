"""
Gigathonk listener.
Polls Telegram for position updates from the user and saves to positions.json.
Run once at 9:05pm via cron — reads all unprocessed messages, updates positions.

Message formats:
  OXY 750 ADBE 500          — bought these today
  nothing                    — no trades
  sold OXY                   — exit full position
  sold OXY 300               — partial exit ($300 CAD)
  March 15: OXY 750 ADBE 500 — backfill a specific date
"""

import json
import re
import logging
from datetime import datetime
from pathlib import Path

import requests
from decouple import config

logging.basicConfig(level=logging.WARNING)

TELEGRAM_BOT_TOKEN = config("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = config("TELEGRAM_CHAT_ID")

POSITIONS_FILE   = Path(__file__).parent / "positions.json"
LEDGER_FILE      = Path(__file__).parent / "ledger.json"
MEMORY_FILE      = Path(__file__).parent / "memory.json"
LAST_UPDATE_FILE = Path(__file__).parent / ".last_update_id"

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ─── Storage ──────────────────────────────────────────────────────────────────

def load_positions() -> dict:
    if not POSITIONS_FILE.exists():
        return {}
    with open(POSITIONS_FILE) as f:
        return json.load(f)


def save_positions(positions: dict):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


def load_ledger() -> list:
    if not LEDGER_FILE.exists():
        return []
    with open(LEDGER_FILE) as f:
        return json.load(f)


def append_ledger(entry: dict):
    ledger = load_ledger()
    ledger.append(entry)
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def load_last_update_id() -> int:
    if not LAST_UPDATE_FILE.exists():
        return 0
    return int(LAST_UPDATE_FILE.read_text().strip())


def save_last_update_id(update_id: int):
    LAST_UPDATE_FILE.write_text(str(update_id))


# ─── Fetch most recent recommendation for a ticker ───────────────────────────

def get_recent_recommendation(ticker: str) -> dict:
    """Look up the most recent thesis + conviction for a ticker from memory.json."""
    if not MEMORY_FILE.exists():
        return {}
    with open(MEMORY_FILE) as f:
        memory = json.load(f)
    for run in reversed(memory):
        for rec in run.get("recommendations", []):
            if rec.get("ticker") == ticker:
                return {
                    "thesis": rec.get("thesis", ""),
                    "conviction": rec.get("conviction", ""),
                    "catalyst": rec.get("catalyst", ""),
                    "rec_date": run.get("date", ""),
                }
    return {}


# ─── Telegram polling ─────────────────────────────────────────────────────────

def get_updates(offset: int = 0) -> list[dict]:
    try:
        r = requests.get(
            f"{BASE_URL}/getUpdates",
            params={"offset": offset, "timeout": 10},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"  ✗ Telegram poll error: {e}")
        return []


def send_confirmation(text: str):
    try:
        requests.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass


# ─── Message parsing ──────────────────────────────────────────────────────────

def parse_date_prefix(text: str) -> tuple[str | None, str]:
    match = re.match(r"^([A-Za-z]+ \d{1,2}):\s*(.+)$", text.strip(), re.DOTALL)
    if match:
        try:
            date = datetime.strptime(match.group(1) + f" {datetime.now().year}", "%B %d %Y")
            return date.strftime("%Y-%m-%d"), match.group(2).strip()
        except ValueError:
            pass
    return None, text.strip()


def parse_buys(text: str) -> list[tuple[str, float]]:
    tokens = text.upper().split()
    results = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if re.match(r"^[A-Z]{1,5}(\.[A-Z]{2})?$", token):
            amount = 0.0
            if i + 1 < len(tokens):
                try:
                    amount = float(tokens[i + 1])
                    i += 1
                except ValueError:
                    pass
            if amount > 0:
                results.append((token, amount))
        i += 1
    return results


def parse_sells(text: str) -> tuple[str, float | None]:
    match = re.match(r"^sold\s+([A-Z]{1,5}(?:\.[A-Z]{2})?)\s*(\d+(?:\.\d+)?)?$",
                     text.strip(), re.IGNORECASE)
    if match:
        ticker = match.group(1).upper()
        amount = float(match.group(2)) if match.group(2) else None
        return ticker, amount
    return "", None


# ─── Price fetch ──────────────────────────────────────────────────────────────

def get_price(ticker: str) -> float | None:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("regularMarketPrice") or info.get("currentPrice")
    except Exception:
        return None


# ─── Process a single message ─────────────────────────────────────────────────

def process_message(text: str, msg_date: str, positions: dict) -> str:
    text = text.strip()
    date, body = parse_date_prefix(text)
    trade_date = date or msg_date

    # "nothing"
    if body.lower() == "nothing":
        return f"Got it. No trades on {trade_date}. Positions unchanged."

    # "sold OXY" or "sold OXY 300"
    if body.lower().startswith("sold "):
        ticker, amount = parse_sells(body)
        if not ticker:
            return "Could not parse sell. Try: sold OXY or sold OXY 300"

        if ticker not in positions:
            return f"{ticker} is not in your positions."

        pos = positions[ticker]
        price_at_buy = pos.get("price_usd_at_buy")
        current_price = get_price(ticker)
        cad_deployed = pos.get("cad_deployed", 0)

        if amount is None:
            exit_cad = cad_deployed
            del positions[ticker]
            result_msg = f"Removed {ticker} from positions."
        else:
            exit_cad = min(amount, cad_deployed)
            new_amount = max(0, cad_deployed - amount)
            if new_amount == 0:
                del positions[ticker]
                result_msg = f"Removed {ticker} from positions (fully exited)."
            else:
                positions[ticker]["cad_deployed"] = new_amount
                result_msg = f"Reduced {ticker} to ${new_amount:.0f} CAD deployed."

        # Calculate gain for ledger
        gain_cad = None
        if price_at_buy and current_price and price_at_buy > 0:
            pct = (current_price - price_at_buy) / price_at_buy
            gain_cad = round(exit_cad * pct, 2)

        append_ledger({
            "date": trade_date,
            "action": "sell",
            "ticker": ticker,
            "cad": exit_cad,
            "price_usd_at_buy": price_at_buy,
            "price_usd_at_sell": round(current_price, 4) if current_price else None,
            "gain_cad": gain_cad,
        })

        gain_str = f" | gain: ${gain_cad:+.0f} CAD" if gain_cad is not None else ""
        return result_msg + gain_str

    # "OXY 750 ADBE 500"
    buys = parse_buys(body)
    if buys:
        added = []
        for ticker, cad_amount in buys:
            price = get_price(ticker)
            rec = get_recent_recommendation(ticker)

            if ticker in positions:
                existing = positions[ticker]
                old_cad = existing["cad_deployed"]
                old_price = existing.get("price_usd_at_buy", 0)
                new_total = old_cad + cad_amount
                if old_price and price:
                    avg_price = ((old_cad * old_price) + (cad_amount * price)) / new_total
                    positions[ticker]["price_usd_at_buy"] = round(avg_price, 4)
                positions[ticker]["cad_deployed"] = round(new_total, 2)
                positions[ticker]["last_added"] = trade_date
                added.append(f"{ticker} +${cad_amount:.0f} CAD (avg price updated)")
            else:
                positions[ticker] = {
                    "cad_deployed": cad_amount,
                    "price_usd_at_buy": round(price, 4) if price else None,
                    "date": trade_date,
                    "thesis": rec.get("thesis", ""),
                    "conviction": rec.get("conviction", ""),
                    "catalyst": rec.get("catalyst", ""),
                    "rec_date": rec.get("rec_date", ""),
                }
                price_str = f"at ${price:.2f}" if price else "price unavailable"
                added.append(f"{ticker} ${cad_amount:.0f} CAD {price_str}")

            append_ledger({
                "date": trade_date,
                "action": "buy",
                "ticker": ticker,
                "cad": cad_amount,
                "price_usd": round(price, 4) if price else None,
                "thesis": rec.get("thesis", ""),
            })

        return "Saved:\n" + "\n".join(added)

    return f"Could not parse: {body}\nTry: OXY 750 ADBE 500  |  sold OXY  |  nothing"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n📬 Gigathonk Listener | {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    last_id = load_last_update_id()
    updates = get_updates(offset=last_id + 1)

    if not updates:
        print("  No new messages.")
        return

    positions = load_positions()
    processed = 0

    for update in updates:
        update_id = update["update_id"]
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        ts = msg.get("date", 0)
        msg_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else datetime.now().strftime("%Y-%m-%d")

        if chat_id != str(TELEGRAM_CHAT_ID):
            save_last_update_id(update_id)
            continue

        if not text:
            save_last_update_id(update_id)
            continue

        print(f"  Message [{msg_date}]: {text}")
        reply = process_message(text, msg_date, positions)
        print(f"  Reply: {reply}")
        send_confirmation(reply)
        save_last_update_id(update_id)
        processed += 1

    if processed > 0:
        save_positions(positions)
        print(f"\n  Saved {processed} update(s) to positions.json")
    else:
        print("  No position updates found.")


if __name__ == "__main__":
    main()
