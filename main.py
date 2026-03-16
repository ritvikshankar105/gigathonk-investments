"""
Gigathonk Investments
Two-pass AI investment brief delivered to Telegram every weekday at 8:45am EST.

Pass 1: Claude Sonnet extracts ticker insights from Reddit (quality signal only)
Pass 2: Claude Opus synthesizes market data, news, insider activity + memory into a brief
"""

import json
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from xml.etree import ElementTree

import requests
import yfinance as yf
import pandas as pd
from anthropic import Anthropic
from telegram import Bot
from decouple import config

from memory import get_memory_context, save_run

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# ─── Config ───────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY  = config("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = config("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = config("TELEGRAM_CHAT_ID")
WEEKLY_DEPLOY_CAD  = config("WEEKLY_DEPLOY_CAD", default=500, cast=float)
TOTAL_RISK_CAD     = config("TOTAL_RISK_CAD", default=1500, cast=float)
TOTAL_GROWTH_CAD   = config("TOTAL_GROWTH_CAD", default=7500, cast=float)

MACRO_TICKERS = ["SPY", "QQQ", "^VIX", "BTC-USD", "ETH-USD", "GLD", "USO"]
HEADERS = {"User-Agent": "gigathonk-investments/2.0 (personal research tool)"}
client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── Reddit ────────────────────────────────────────────────────────────────────

def fetch_subreddit(subreddit: str, sort: str = "top", time_filter: str = "day",
                    limit: int = 25, min_score: int = 0,
                    required_flair: str | None = None) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&t={time_filter}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        posts = r.json()["data"]["children"]
        result = []
        for p in posts:
            d = p["data"]
            if d["score"] < min_score:
                continue
            if required_flair and required_flair.lower() not in (d.get("link_flair_text") or "").lower():
                continue
            result.append({
                "title": d["title"],
                "score": d["score"],
                "comments": d["num_comments"],
                "text": d.get("selftext", "")[:2500],
                "flair": d.get("link_flair_text", ""),
                "subreddit": subreddit,
                "permalink": d.get("permalink", ""),
            })
        return result
    except Exception as e:
        print(f"  ✗ r/{subreddit}: {e}")
        return []


def fetch_dd_posts(subreddit: str, limit: int = 10) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/search.json?q=flair%3ADD&sort=top&t=week&limit={limit}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        posts = r.json()["data"]["children"]
        return [
            {
                "title": p["data"]["title"],
                "score": p["data"]["score"],
                "comments": p["data"]["num_comments"],
                "text": p["data"].get("selftext", "")[:3000],
                "flair": p["data"].get("link_flair_text", ""),
                "subreddit": subreddit,
                "permalink": p["data"].get("permalink", ""),
            }
            for p in posts
        ]
    except Exception:
        return []


def fetch_comments(permalink: str, limit: int = 8) -> list[str]:
    try:
        url = f"https://www.reddit.com{permalink}.json?limit={limit}&sort=top"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        children = r.json()[1]["data"]["children"]
        return [
            c["data"].get("body", "")[:400]
            for c in children
            if c["kind"] == "t1" and len(c["data"].get("body", "")) > 80
        ][:5]
    except Exception:
        return []


def collect_posts() -> list[dict]:
    """
    Calibrated thresholds based on actual score distributions per subreddit.
    SecurityAnalysis scores max ~18 but every post is substantive — take all.
    WSB median 236 but real DD often scores 50-150 — lower threshold.
    CryptoCurrency max ~121 — was getting 0 posts at 200 threshold.
    """
    all_posts = []

    strategies = [
        # (subreddit, sort, time_filter, limit, min_score)
        ("SecurityAnalysis",  "top",  "week",  25,   0),   # all of it, always high quality
        ("wallstreetbets",    "top",  "day",   30,  50),   # lowered from 200 — real DD scores 50-150
        ("investing",         "top",  "day",   25,  25),   # lowered from 100 — median score is 7
        ("stocks",            "top",  "day",   25,  50),   # lowered from 100
        ("CryptoCurrency",    "top",  "day",   20,  30),   # was 200 — getting 0 posts, max score is 121
        ("CanadianInvestor",  "top",  "week",  20,  25),   # lowered from 50
        ("ValueInvesting",    "top",  "week",  20,  50),   # new — strong fundamental analysis
        ("options",           "top",  "day",   15,   5),   # new — options strategies and plays
    ]

    for sub, sort, tf, limit, min_score in strategies:
        posts = fetch_subreddit(sub, sort, tf, limit, min_score)
        all_posts.extend(posts)
        print(f"  ✓ r/{sub}: {len(posts)} posts")

    # DD posts from WSB + stocks (flair-based search)
    for sub in ["wallstreetbets", "stocks"]:
        dd = fetch_dd_posts(sub)
        all_posts.extend(dd)
        print(f"  ✓ r/{sub} DD: {len(dd)} posts")

    # Daytrading: strategy/trade posts only, not advice or guru drama
    dt = fetch_subreddit("Daytrading", "top", "day", 15, 25, required_flair="Strategy")
    dt += fetch_subreddit("Daytrading", "top", "day", 15, 25, required_flair="Trade")
    if dt:
        all_posts.extend(dt)
    print(f"  ✓ r/Daytrading (Strategy/Trade): {len(dt)} posts")

    # Fetch comments for all posts in parallel — this is where the real DD lives
    def _fetch_and_attach(post):
        if post.get("permalink"):
            comments = fetch_comments(post["permalink"])
            if comments:
                post["top_comments"] = comments

    with ThreadPoolExecutor(max_workers=15) as ex:
        list(ex.map(_fetch_and_attach, all_posts))  # blocks until all complete

    return all_posts


def format_posts_for_claude(posts: list[dict]) -> str:
    lines = []
    for p in sorted(posts, key=lambda x: x["score"], reverse=True):
        flair = f"[{p['flair']}] " if p.get("flair") else ""
        lines.append(f"\n---")
        lines.append(f"r/{p['subreddit']} | {p['score']}↑ {p['comments']}💬 | {flair}{p['title']}")
        if p.get("text"):
            lines.append(p["text"][:2000])
        if p.get("top_comments"):
            lines.append("Top comments:")
            for c in p["top_comments"]:
                lines.append(f"  > {c}")
    return "\n".join(lines)


# ─── Pass 1: Sonnet extracts ticker insights ──────────────────────────────────

def extract_ticker_insights(posts_text: str) -> list[dict]:
    prompt = f"""You are reading Reddit investment communities to identify stocks, ETFs, and crypto being discussed with real investment theses.

Extract every ticker where someone is making an actual investment case — bullish or bearish. Ignore casual mentions with no thesis.

Also flag options opportunities (binary catalysts, squeeze setups, high IV situations) and day trade setups (gap plays, momentum breakouts, news catalysts with same-day resolution).

Return ONLY a valid JSON array. No other text, no markdown, no explanation.

Each element:
{{
  "ticker": "ASTS",
  "company_name": "AST SpaceMobile",
  "asset_type": "stock|etf|crypto",
  "thesis": "One sentence. The actual investment case.",
  "narrative_type": "fundamental|short_squeeze|earnings_catalyst|technical_breakout|sector_tailwind|turnaround|macro|options_play|day_trade",
  "sentiment": "bullish|bearish|mixed",
  "sentiment_strength": 7,
  "discussion_quality": "substantive_dd|mixed|mostly_hype",
  "momentum": "building|peaked|fading",
  "catalyst": "Specific event with date if mentioned, or null",
  "time_horizon": "intraday|days|weeks|months",
  "bull_case": "Specific argument",
  "bear_case": "Counter-argument or null",
  "options_potential": "calls|puts|spread|none",
  "options_rationale": "Why options make sense here, or null",
  "day_trade_potential": false,
  "day_trade_setup": "Specific entry condition and catalyst, or null",
  "subreddits": ["SecurityAnalysis"],
  "post_count": 4,
  "highest_post_score": 2840,
  "top_quote": "Most insightful verbatim quote, or null"
}}

Rules:
- Only tickers with real investment discussion
- Include bearish theses — they are signal
- sentiment_strength 1-10 (10 = extremely strong community conviction)
- discussion_quality: substantive_dd means actual analysis with numbers
- options_potential: flag calls when there is a near-term binary catalyst (earnings, launch, court date); puts when there is a known breakdown catalyst; none for steady-state plays
- day_trade_potential: true only if there is a specific same-day or next-open catalyst with clear entry/exit logic

REDDIT CONTENT:
{posts_text}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=12000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        insights = json.loads(text)
        return insights if isinstance(insights, list) else []
    except json.JSONDecodeError as e:
        print(f"  ✗ Pass 1 JSON parse error: {e}")
        try:
            last_brace = text.rfind("},")
            if last_brace > 0:
                salvaged = text[:last_brace + 1] + "]"
                insights = json.loads(salvaged)
                print(f"  ✓ Salvaged {len(insights)} tickers from truncated JSON")
                return insights if isinstance(insights, list) else []
        except Exception:
            pass
        return []


# ─── Market Data ──────────────────────────────────────────────────────────────

def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
    try:
        delta = prices.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        return round((100 - (100 / (1 + gain / loss))).iloc[-1], 1)
    except Exception:
        return 0.0


def fetch_single_ticker(ticker: str) -> tuple[str, dict | None]:
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        hist = stock.history(period="3mo")
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        if not price or hist.empty:
            return ticker, None

        avg_vol = info.get("averageVolume", 1)
        cur_vol = info.get("regularMarketVolume", 0)

        # Pre-market data
        fast = stock.fast_info
        pre_price = getattr(fast, "pre_market_price", None)
        pre_change_pct = round(((pre_price - price) / price) * 100, 2) if pre_price else None

        # Nearest-expiry IV from options chain
        atm_iv = None
        try:
            if stock.options:
                chain = stock.option_chain(stock.options[0])
                calls = chain.calls
                atm_calls = calls[calls["inTheMoney"] == False]
                if not atm_calls.empty:
                    atm_iv = round(atm_calls.iloc[0]["impliedVolatility"] * 100, 1)
        except Exception:
            pass

        return ticker, {
            "name": info.get("shortName", ticker),
            "price_usd": round(price, 2),
            "change_pct_24h": round(info.get("regularMarketChangePercent", 0), 2),
            "pre_market_change_pct": pre_change_pct,
            "volume_ratio": round(cur_vol / max(avg_vol, 1), 2),
            "rsi_14": calculate_rsi(hist["Close"]),
            "market_cap_b": round((info.get("marketCap", 0) or 0) / 1e9, 2),
            "sector": info.get("sector", "Unknown"),
            "52w_high": info.get("fiftyTwoWeekHigh", 0),
            "52w_low": info.get("fiftyTwoWeekLow", 0),
            "pct_from_52w_low": round(
                ((price - (info.get("fiftyTwoWeekLow") or price)) /
                 max(info.get("fiftyTwoWeekLow") or price, 0.01)) * 100, 1
            ),
            "short_pct_float": round((info.get("shortPercentOfFloat") or 0) * 100, 1),
            "short_ratio_days": info.get("shortRatio") or 0,
            "analyst_target_usd": info.get("targetMeanPrice") or 0,
            "analyst_upside_pct": round(
                ((info.get("targetMeanPrice", price) - price) / price) * 100, 1
            ) if info.get("targetMeanPrice") else 0,
            "recommendation": info.get("recommendationKey", "none"),
            "atm_iv_pct": atm_iv,
            "pe_ratio": info.get("trailingPE") or info.get("forwardPE"),
            "revenue_growth_pct": round((info.get("revenueGrowth") or 0) * 100, 1),
        }
    except Exception:
        return ticker, None


def fetch_single_ticker_resolved(ticker: str) -> tuple[str, dict | None]:
    """Fetch ticker, falling back to .TO suffix for TSX-listed stocks."""
    t, data = fetch_single_ticker(ticker)
    if data:
        return t, data
    t2, data2 = fetch_single_ticker(ticker + ".TO")
    if data2:
        return t2, data2
    return ticker, None


def fetch_market_data(tickers: list[str]) -> dict:
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(fetch_single_ticker_resolved, tickers))
    return {t: d for t, d in results if d}


def fetch_macro() -> dict:
    macro = {}
    for ticker in MACRO_TICKERS:
        _, data = fetch_single_ticker(ticker)
        if data:
            label = ticker.replace("^", "").replace("-USD", "")
            macro[label] = {
                "price": data["price_usd"],
                "change_24h_pct": data["change_pct_24h"],
                "rsi": data["rsi_14"],
                "volume_ratio": data["volume_ratio"],
            }
    return macro


def fetch_fear_greed() -> dict:
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=8)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception:
        return {}


# ─── Earnings Calendar ────────────────────────────────────────────────────────

def fetch_earnings(tickers: list[str]) -> list[dict]:
    upcoming = []
    today = datetime.now().date()
    window = today + timedelta(days=14)
    for ticker in tickers:
        try:
            cal = yf.Ticker(ticker).calendar
            if not cal:
                continue
            dates = cal.get("Earnings Date", [])
            for ed in dates:
                ed = ed.date() if hasattr(ed, "date") else ed
                if today <= ed <= window:
                    upcoming.append({
                        "ticker": ticker,
                        "date": str(ed),
                        "days_away": (ed - today).days,
                    })
                    break
        except Exception:
            pass
    return upcoming


# ─── Insider Data ─────────────────────────────────────────────────────────────

def fetch_insider_data(tickers: list[str]) -> list[dict]:
    signals = []
    cutoff = pd.Timestamp(datetime.now() - timedelta(days=45))
    for ticker in tickers[:12]:
        try:
            stock = yf.Ticker(ticker)
            txns = stock.insider_transactions
            if txns is None or txns.empty:
                continue
            if "Start Date" not in txns.columns:
                continue
            recent = txns[txns["Start Date"] >= cutoff]
            if "Transaction" in recent.columns:
                buys = recent[recent["Transaction"] == "Buy"]
            else:
                buys = recent
            if buys.empty:
                continue
            value = buys["Value"].sum() if "Value" in buys.columns else 0
            shares = buys["Shares"].sum() if "Shares" in buys.columns else 0
            if value > 50000:
                signals.append({
                    "ticker": ticker,
                    "buy_count": len(buys),
                    "total_shares": int(shares),
                    "total_value_usd": int(value),
                })
        except Exception:
            pass
    return signals


# ─── News ─────────────────────────────────────────────────────────────────────

def fetch_news(tickers: list[str]) -> dict:
    news = {}
    for ticker in tickers[:10]:
        items = []
        try:
            for item in (yf.Ticker(ticker).news or [])[:4]:
                items.append(item.get("title", ""))
        except Exception:
            pass
        try:
            url = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
            r = requests.get(url, headers=HEADERS, timeout=8)
            root = ElementTree.fromstring(r.content)
            for item in root.findall(".//item")[:3]:
                title = item.findtext("title", "")
                if title:
                    items.append(title)
        except Exception:
            pass
        if items:
            news[ticker] = items[:6]
    return news


# ─── Pass 2: Opus full synthesis ──────────────────────────────────────────────

def build_brief(
    ticker_insights: list[dict],
    market_data: dict,
    macro: dict,
    news: dict,
    earnings: list[dict],
    insider: list[dict],
    memory_context: str,
    fear_greed: dict,
) -> str:

    date_str = datetime.now().strftime("%A, %B %d")
    usd_cad = 1.36

    enriched = []
    for insight in ticker_insights:
        ticker = insight.get("ticker", "")
        mkt = market_data.get(ticker, {})
        enriched.append({**insight, "market": mkt})

    prompt = f"""You are a world-class private investment analyst. You find opportunities others miss by combining deep fundamental analysis, macro regime awareness, and understanding of how retail narratives interact with price action.

You write a daily brief for one investor: a 20-something in Toronto, investing via Wealthsimple TFSA. Smart, learning fast, wants real actionable signal. Explain every metric in plain English — assume they are smart but not a finance professional.

PLATFORM:
- Wealthsimple supports: NYSE, NASDAQ, TSX, TSX-V stocks, ETFs, crypto, AND options
- No OTC or pink sheets
- All amounts in CAD. USD/CAD: ~{usd_cad}
- TFSA: all gains are completely tax-free (this matters — hold winners longer)
- Default weekly deploy: ${WEEKLY_DEPLOY_CAD:.0f} CAD
- Go higher (up to $2,000 CAD) only on unusually high conviction
- Risk bucket total: ${TOTAL_RISK_CAD:.0f} CAD | Growth bucket: ${TOTAL_GROWTH_CAD:.0f} CAD

MARKET SENTIMENT:
Fear & Greed Index: {json.dumps(fear_greed)} (0=Extreme Fear, 100=Extreme Greed — contrarian signal)

MACRO:
{json.dumps(macro, indent=2)}

REDDIT ANALYSIS (extracted from {len(ticker_insights)} tickers across quality-filtered posts):
{json.dumps(enriched, indent=2)}

NEWS:
{json.dumps(news, indent=2)}

EARNINGS (next 14 days):
{json.dumps(earnings, indent=2) if earnings else "None"}

INSIDER BUYING (last 45 days, >$50k):
{json.dumps(insider, indent=2) if insider else "None significant"}

MEMORY:
{memory_context}

SIGNAL INTERPRETATION GUIDE (use this to inform your analysis, and explain signals in plain English in the output):
- RSI (Relative Strength Index): momentum gauge 0-100. Below 30 = oversold/cheap momentum, above 70 = extended/hot. Not a buy/sell signal alone.
- Volume ratio: today's volume vs 30-day average. Above 2x means unusual interest — someone is accumulating or distributing.
- Short % float: percentage of shares borrowed and sold short by people betting against it. Above 15% + positive catalyst = squeeze risk (shorts forced to buy, amplifying upside).
- Days to cover: how many days of average volume it would take shorts to close their positions. Higher = more violent potential squeeze.
- IV (implied volatility): options market's expectation of future price movement. High IV = options expensive. Buy options when IV is low, sell when high.
- Analyst target: Wall Street consensus price target. Useful as a reference, not gospel.
- Insider buying: when executives buy their own stock with personal money, it is a strong signal — they know the company better than anyone.
- Pre-market change: price movement before market opens, usually on news.
- P/E ratio: price divided by earnings — how much you pay for $1 of profit. High P/E = growth expectations priced in. Low P/E = value or value trap.
- Fear & Greed below 30: market is fearful — historically a good time to buy quality names. Above 70: greedy — be more selective.

YOUR ANALYSIS JOBS:
1. Find every high-conviction opportunity the data supports. Zero plays is a valid answer.
2. For each play: connect Reddit narrative to market data. Where do they agree? Where do they conflict? The conflict is often the real insight.
3. For memory: if you recommended something before, tell me — is the thesis playing out? Add, hold, or exit?
4. Identify options plays where a binary catalyst makes options superior to stock (earnings in <14 days, launch date, legal ruling, etc.)
5. Identify day trade setups only if the catalyst is specific, the setup is clean, and the risk is clearly defined.
6. Flag when the right answer is to hold cash and why.

WRITING RULES — non-negotiable:
- No em dashes
- No rhetorical questions
- No filler phrases ("it's worth noting", "importantly", "notably", "it's clear that", "diving into", "certainly", "of course")
- No AI language of any kind
- No contrastive reframes ("not X, but Y" constructions)
- No exaggeration unless the number itself justifies it — let data speak
- Short sentences. Every word earns its place.
- Explain every metric in plain English, one clause, in the same line
- No jargon without immediate plain-English translation in the same sentence

OUTPUT FORMAT — follow exactly, use Telegram Markdown:

*GIGATHONK* | {date_str}

*MACRO* 🌍
[3 sentences max. What is happening in markets right now and what it means for risk-taking today. Plain English.]
Fear & Greed: [value] ([label]) | SPY RSI [x] | VIX [x]

---

[For each high-conviction stock/ETF/crypto play:]

[🎯 for high conviction | 📈 for medium] *$TICKER* | [Company Name]
$[price] USD (~$[price x usd_cad] CAD) | Deploy: $[X] CAD (~[X] shares)

[2-3 sentences. Plain English thesis. No jargon without explanation. Why this, why now.]

[Only signals relevant to this specific play type — not all signals every time:]
📊 [Signal name]: [value] — [plain English explanation of what this number means]
[repeat for 3-5 most relevant signals only]

[If earnings <14 days]: ⏰ Earnings: [date], [X] days away — [how this changes risk]
[If insider buying]: 👤 Insiders: [specific details in plain English]
[If in memory]: 🔁 Previous call: [what was said, performance, what to do now]

Action: [Specific. Buy now / Add / Hold / Exit if X. One sentence.]
Downside: [One specific sentence. What breaks this trade.]

---

[For each options play — only when a binary catalyst makes options clearly better than stock:]

⚡ *OPTIONS: $TICKER [CALLS/PUTS]*
[Why options are better than stock here — specific catalyst and timing]
Suggested: [strike] [expiry] — [cost estimate in CAD per contract]
[2-3 relevant signals]
Action: [Specific. Buy X contracts. Target: X% gain. Hard stop: X% loss.]
Risk: [Options can go to zero. One sentence on the specific risk here.]

---

[For each day trade setup — only if catalyst is specific and setup is clean:]

🔥 *DAY TRADE: $TICKER*
Entry: [specific price or condition] | Target: [price] | Stop: [price]
[1-2 sentences on the setup and catalyst]
[2-3 signals]
Action: [Buy at X. Scale out at Y. Hard stop at Z.]
Risk: [One sentence.]

---

*WATCHING* 👁
[Each ticker on one line: $TICKER — what specific signal triggers entry]

*AVOID* 🚫
[Each ticker on one line: $TICKER — specific reason, not generic caution]

*THIS WEEK* 🎯
[One sentence. The single most important thing to do or not do.]"""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ─── Telegram ─────────────────────────────────────────────────────────────────

async def send_telegram(message: str):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    max_len = 4000

    if len(message) <= max_len:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
        return

    chunks = []
    current = ""
    for paragraph in message.split("\n\n"):
        candidate = (current + "\n\n" + paragraph).strip() if current else paragraph
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(paragraph) > max_len:
                for line in paragraph.split("\n"):
                    if len(current) + len(line) + 1 <= max_len:
                        current = (current + "\n" + line).strip() if current else line
                    else:
                        if current:
                            chunks.append(current)
                        current = line
            else:
                current = paragraph
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(0.5)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=chunk, parse_mode="Markdown")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n⚡ Gigathonk Investments v2 | {datetime.now().strftime('%A %B %d %Y %I:%M %p')}\n")

    # Step 1: Reddit
    print("STEP 1: Collecting Reddit signal...")
    posts = collect_posts()
    print(f"  Total: {len(posts)} posts\n")

    posts_text = format_posts_for_claude(posts)

    # Step 2: Pass 1 - Sonnet extracts tickers
    print("STEP 2: Extracting ticker insights (Sonnet)...")
    ticker_insights = extract_ticker_insights(posts_text)
    tickers = [t["ticker"] for t in ticker_insights if t.get("ticker")]
    print(f"  Found {len(ticker_insights)} tickers: {tickers}\n")

    if not tickers:
        print("  No tickers found. Sending hold-cash brief.")
        asyncio.run(send_telegram("*GIGATHONK* | No strong signals today. Hold cash."))
        return

    # Step 3: Fetch everything in parallel
    print("STEP 3: Fetching market data, macro, news, earnings, insider, fear/greed...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        market_future   = ex.submit(fetch_market_data, tickers)
        macro_future    = ex.submit(fetch_macro)
        news_future     = ex.submit(fetch_news, tickers)
        earnings_future = ex.submit(fetch_earnings, tickers)
        insider_future  = ex.submit(fetch_insider_data, tickers)
        fg_future       = ex.submit(fetch_fear_greed)

    market_data = market_future.result()
    macro       = macro_future.result()
    news        = news_future.result()
    earnings    = earnings_future.result()
    insider     = insider_future.result()
    fear_greed  = fg_future.result()

    print(f"  Market data: {list(market_data.keys())}")
    print(f"  Macro: {list(macro.keys())}")
    print(f"  News: {sum(len(v) for v in news.values())} articles")
    print(f"  Earnings: {len(earnings)} upcoming")
    print(f"  Insider: {len(insider)} signals")
    print(f"  Fear & Greed: {fear_greed}\n")

    # Step 4: Memory context
    print("STEP 4: Loading memory...")
    memory_context = get_memory_context()

    # Step 5: Pass 2 - Opus full synthesis
    print("STEP 5: Building brief (Opus)...")
    brief = build_brief(
        ticker_insights, market_data, macro,
        news, earnings, insider, memory_context, fear_greed
    )

    # Step 6: Save to memory
    save_run(
        recommendations=[
            {
                "ticker": t.get("ticker"),
                "price_at_rec_usd": market_data.get(t.get("ticker", ""), {}).get("price_usd", 0),
                "thesis": t.get("thesis", ""),
                "conviction": t.get("sentiment_strength", 5),
            }
            for t in ticker_insights
            if t.get("sentiment") == "bullish" and t.get("sentiment_strength", 0) >= 6
        ],
        avoided=[
            t.get("ticker") for t in ticker_insights
            if t.get("sentiment") == "bearish"
        ],
        macro_snapshot={k: v["price"] for k, v in macro.items()},
    )

    # Step 7: Send
    print("STEP 6: Sending to Telegram...")
    asyncio.run(send_telegram(brief))
    print("  Done!\n")


if __name__ == "__main__":
    main()
