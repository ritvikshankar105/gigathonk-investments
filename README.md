# Gigathonk Investments

![Gigathonk](gigathonk.png)

A daily AI investment brief that lands in your Telegram at 8:45am. It reads Reddit so you don't have to.

---

## What it does

Every morning before market open:

1. Scrapes ~100 high-signal Reddit posts across r/SecurityAnalysis, r/wallstreetbets, r/ValueInvesting, r/options, r/CanadianInvestor, and others — with calibrated score thresholds per subreddit so you get actual DD, not memes
2. Fetches top comments too, because that's where the real analysis lives
3. Sends all of it to Claude Sonnet, which extracts structured investment theses — ticker, sentiment, catalyst, bull/bear case, options potential, day trade setups
4. Pulls live market data: RSI, volume ratio, short interest, analyst targets, IV, pre-market moves, insider buying, earnings calendar, Fear & Greed index
5. Sends everything to Claude Opus, which writes a tight brief with specific plays, options suggestions, day trade setups, and a macro read

You get a Telegram message that looks like a hedge fund analyst wrote it. Except it runs on your laptop and costs ~$0.40/day.

---

## Position tracking

Reply to your Telegram bot after market close with what you bought:

```
OXY 750 ADBE 500
```

The listener wakes your Mac at 9pm, catches the message, saves your positions with the original thesis attached, and averages your cost basis if you add. Next morning's brief knows what you own and whether the thesis is still intact.

```
sold OXY          — full exit, records gain
sold OXY 300      — partial exit
nothing           — no trades, positions unchanged
March 15: OXY 750 — backfill a past date
```

Every trade goes into `ledger.json`. Append-only. That's your track record.

---

## Setup

**1. Clone and install**
```bash
git clone https://github.com/yourusername/gigathonk-investments
cd gigathonk-investments
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
```

**2. Create your `.env`**
```bash
cp .env.example .env
```
Fill in:
- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `TELEGRAM_BOT_TOKEN` — create a bot via @BotFather
- `TELEGRAM_CHAT_ID` — your chat ID (message @userinfobot)
- `WEEKLY_DEPLOY_CAD` — your default weekly budget in CAD (default: 500)
- `TOTAL_RISK_CAD` / `TOTAL_GROWTH_CAD` — your bucket limits

**3. Set up cron**
```bash
crontab -e
```
Add:
```
# Morning brief — weekdays 8:45am
45 8 * * 1-5 cd /path/to/gigathonk-investments && .venv/bin/python main.py >> gigathonk.log 2>&1

# Sunday brief — 7pm
0 19 * * 0 cd /path/to/gigathonk-investments && .venv/bin/python main.py >> gigathonk.log 2>&1

# Evening listener — 9:05pm daily
5 21 * * * cd /path/to/gigathonk-investments && .venv/bin/python listen.py >> gigathonk.log 2>&1
```

**4. Schedule Mac wake times**
```bash
sudo pmset repeat wakeorpoweron MTWRFSU 08:40:00
sudo pmset repeat wakeorpoweron MTWRFSU 21:00:00
```

---

## Architecture

```
Reddit (~100 posts + comments)
        ↓
  Claude Sonnet 4.6
  (ticker extraction, options/day trade flagging)
        ↓
  yfinance + Fear & Greed API
  (RSI, volume, short interest, IV, insider buying, earnings)
        ↓
  Claude Opus 4.6
  (synthesis, plain-English brief)
        ↓
  Telegram
```

**Files:**
- `main.py` — the daily brief
- `listen.py` — the 9pm position listener
- `memory.py` — position P&L and recommendation history
- `positions.json` — what you actually own (gitignored)
- `ledger.json` — permanent trade record (gitignored)
- `memory.json` — rolling 90-day recommendation history (gitignored)

---

## Built for

Wealthsimple TFSA investing. Supports NYSE, NASDAQ, TSX, crypto, and options. All amounts in CAD. Tax-free gains only.

---

## Cost

~$0.40 USD per run. Claude Sonnet for extraction, Claude Opus for synthesis. Both via the Anthropic API.
