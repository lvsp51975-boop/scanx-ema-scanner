"""
╔══════════════════════════════════════════════════════════════════════╗
║     ScanX EMA 9/21 Scanner — GitHub Actions Edition                  ║
║                                                                      ║
║  Uses yfinance directly (no MCP server needed).                      ║
║  Run by GitHub Actions on schedule → sends Telegram alert.          ║
╚══════════════════════════════════════════════════════════════════════╝

Set these GitHub Secrets:
    TELEGRAM_BOT_TOKEN   — from @BotFather
    TELEGRAM_CHAT_ID     — your chat/group/channel ID
"""

import asyncio
import os
import re
import sys
import warnings
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# morning = 9AM IST watchlist | evening = 4PM IST signals
SESSION   = os.environ.get("SCAN_SESSION", "morning")

SCANX_URL = "https://scanx.trade/insight/top-deliveries"
HEADERS   = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://scanx.trade/",
}

SYMBOL_OVERRIDES = {
    "M&M":        "M&M.NS",
    "BAJAJ-AUTO": "BAJAJ-AUTO.NS",
    "ETERNAL":    "ETERNAL.NS",
    "ZOMATO":     "ZOMATO.NS",
}

FALLBACK_NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "GRASIM",
    "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDUNILVR", "HINDALCO",
    "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN",
    "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI",
    "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
    "RELIANCE", "SBIN", "SHRIRAMFIN", "SBILIFE", "SUNPHARMA",
    "TATASTEEL", "TATACONSUM", "TRENT", "TCS", "TECHM",
    "TITAN", "ULTRACEMCO", "WIPRO",
]

CONCURRENCY = 8
TOP_N       = 5
LOOKBACK    = 2
MIN_DELIVERY = 40.0

# ─────────────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────────────

def fetch_top_deliveries() -> list[dict]:
    print("📡  Fetching ScanX top deliveries...")
    try:
        resp = requests.get(SCANX_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    ⚠  ScanX unreachable ({e}). Using fallback Nifty 50.")
        return _fallback_list()

    soup   = BeautifulSoup(resp.text, "html.parser")
    stocks = []
    table  = soup.find("table")

    if table:
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 7:
                continue
            img    = cols[0].find("img")
            symbol = None
            if img and "dhan.co/symbol/" in img.get("src", ""):
                symbol = img["src"].split("/symbol/")[-1].replace(".png", "").strip()
            if not symbol:
                continue

            def _num(td):
                t = td.get_text(strip=True).replace(",", "").replace("%", "").replace("+", "")
                try:
                    return float(t)
                except ValueError:
                    return None

            stocks.append({
                "symbol":       symbol,
                "name":         cols[0].get_text(strip=True),
                "ltp":          _num(cols[1]),
                "change_pct":   _num(cols[3]),
                "delivery_pct": _num(cols[6]),
            })

    if not stocks:
        seen = set()
        for img in soup.find_all("img", src=re.compile(r"dhan\.co/symbol/")):
            sym = img["src"].split("/symbol/")[-1].replace(".png", "").strip()
            if sym and sym not in seen:
                seen.add(sym)
                stocks.append({"symbol": sym, "name": sym,
                               "ltp": None, "change_pct": None, "delivery_pct": None})

    if not stocks:
        print("    ⚠  Parse failed. Using fallback Nifty 50.")
        return _fallback_list()

    print(f"    ✅  {len(stocks)} stocks found.")
    return stocks


def _fallback_list():
    return [{"symbol": s, "name": s, "ltp": None,
             "change_pct": None, "delivery_pct": None}
            for s in FALLBACK_NIFTY50]


def to_ns_symbol(symbol: str) -> str:
    sym = symbol.upper()
    if sym in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[sym]
    if sym.endswith(".NS") or sym.endswith(".BO"):
        return sym
    return f"{sym}.NS"


# ─────────────────────────────────────────────────────────────────────
# DATA FETCH — direct yfinance (no MCP)
# ─────────────────────────────────────────────────────────────────────

def get_price_history(symbol: str, period: str = "3mo") -> pd.DataFrame | None:
    try:
        df = yf.download(symbol, period=period, interval="1d",
                         auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty or len(df) < 25:
            return None
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index)
        df.sort_index(inplace=True)
        return df
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# EMA + CROSSOVER
# ─────────────────────────────────────────────────────────────────────

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def detect_crossover(df: pd.DataFrame, lookback: int):
    df = df.copy()
    df["EMA9"]  = compute_ema(df["Close"], 9)
    df["EMA21"] = compute_ema(df["Close"], 21)

    for i in range(1, lookback + 2):
        try:
            e9p  = df["EMA9"].iloc[-(i + 1)]
            e21p = df["EMA21"].iloc[-(i + 1)]
            e9c  = df["EMA9"].iloc[-i]
            e21c = df["EMA21"].iloc[-i]
        except IndexError:
            continue
        if (e9p <= e21p) and (e9c > e21c):
            crossed_date = df.index[-(i)]
            days_ago     = i - 1
            return (
                str(crossed_date.date()) if hasattr(crossed_date, "date") else str(crossed_date),
                days_ago,
            )

    return None, None


# ─────────────────────────────────────────────────────────────────────
# ANALYSE ONE STOCK
# ─────────────────────────────────────────────────────────────────────

def analyse_stock(stock: dict, lookback: int = LOOKBACK, period: str = "3mo") -> dict | None:
    yf_sym = to_ns_symbol(stock["symbol"])
    df     = get_price_history(yf_sym, period=period)

    if df is None:
        return None

    # Ensure Close column
    close_col = next((c for c in df.columns if str(c).lower() == "close"), None)
    if close_col is None:
        return None
    if close_col != "Close":
        df = df.rename(columns={close_col: "Close"})

    crossed_on, days_ago = detect_crossover(df, lookback)
    if crossed_on is None:
        return None

    df["EMA9"]  = compute_ema(df["Close"], 9)
    df["EMA21"] = compute_ema(df["Close"], 21)
    ema9  = float(df["EMA9"].iloc[-1])
    ema21 = float(df["EMA21"].iloc[-1])
    close = float(df["Close"].iloc[-1])

    # Volume ratio
    vol_col   = next((c for c in df.columns if str(c).lower() == "volume"), None)
    vol_ratio = None
    if vol_col:
        vols = df[vol_col].astype(float)
        if len(vols) >= 25:
            vol_ratio = round(vols.iloc[-5:].mean() / vols.iloc[-25:-5].mean(), 2)

    # ATR(14)
    atr      = None
    high_col = next((c for c in df.columns if str(c).lower() == "high"), None)
    low_col  = next((c for c in df.columns if str(c).lower() == "low"),  None)
    if high_col and low_col:
        hi = df[high_col].astype(float)
        lo = df[low_col].astype(float)
        cl = df["Close"].astype(float)
        tr = pd.concat([
            hi - lo,
            (hi - cl.shift(1)).abs(),
            (lo - cl.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = round(float(tr.ewm(span=14, adjust=False).mean().iloc[-1]), 2)

    entry = round(ema9, 2)
    sl    = round(ema21 - 0.5 * atr, 2) if atr else round(ema21, 2)
    tp1   = round(entry + 1.5 * atr, 2) if atr else None
    tp2   = round(entry + 3.0 * atr, 2) if atr else None
    risk  = round(entry - sl, 2)
    rr1   = round((tp1 - entry) / risk, 1) if (atr and risk > 0) else None
    rr2   = round((tp2 - entry) / risk, 1) if (atr and risk > 0) else None

    return {
        **stock,
        "yf_symbol":      yf_sym,
        "close":          round(close, 2),
        "ema9":           round(ema9, 2),
        "ema21":          round(ema21, 2),
        "ema_spread_pct": round((ema9 - ema21) / ema21 * 100, 3),
        "days_ago":       days_ago,
        "crossed_on":     crossed_on,
        "vol_ratio":      vol_ratio,
        "atr":            atr,
        "entry":          entry,
        "sl":             sl,
        "tp1":            tp1,
        "tp2":            tp2,
        "rr1":            rr1,
        "rr2":            rr2,
    }


# ─────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────

def score(r: dict) -> float:
    recency  = max(0, 10 - r["days_ago"] * 3)
    delivery = (r["delivery_pct"] or 0) / 10
    volume   = min(3, (r["vol_ratio"] or 1) - 1) if r.get("vol_ratio") else 0
    spread   = max(0, 3 - abs(r["ema_spread_pct"]))
    momentum = min(2, max(-2, (r["change_pct"] or 0) / 2))
    return recency + delivery + volume + spread + momentum


# ─────────────────────────────────────────────────────────────────────
# TELEGRAM MESSAGE BUILDER
# ─────────────────────────────────────────────────────────────────────

def build_message(results: list[dict], session_type: str) -> str:
    now        = datetime.now()
    today      = now.strftime("%d %b %Y")
    time_str   = now.strftime("%H:%M")
    is_morning = session_type == "morning"

    header_emoji = "🌅" if is_morning else "🌆"
    session_name = "PRE-MARKET WATCHLIST" if is_morning else "POST-MARKET SIGNALS"
    action_note  = (
        "⚡ *Watch these at market open\\. Entry on EMA 9 pullback\\.*"
        if is_morning else
        "📌 *Confirmed crossovers today\\. Plan entry tomorrow on dip\\.*"
    )

    lines = [
        f"{header_emoji} *ScanX EMA 9/21 Scanner — {session_name}*",
        f"📅 {today}  |  🕐 {time_str} IST",
        "━" * 30,
        "",
    ]

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

    for i, r in enumerate(results):
        medal    = medals[i] if i < len(medals) else f"#{i+1}"
        chg      = r["change_pct"] or 0
        arrow    = "▲" if chg >= 0 else "▼"
        chg_str  = f"{arrow} {abs(chg):.2f}%"
        days_tag = {0: "TODAY ✨", 1: "Yesterday"}.get(r["days_ago"], f"{r['days_ago']}d ago")
        vol_str  = ""
        if r.get("vol_ratio"):
            vol_icon = "🔥" if r["vol_ratio"] > 1.2 else ("⚠️" if r["vol_ratio"] < 0.8 else "📊")
            vol_str  = f"\n    {vol_icon} Vol: `{r['vol_ratio']:.2f}x`"

        delivery_str = f"{r['delivery_pct']:.1f}%" if r["delivery_pct"] else "N/A"

        lines += [
            f"{medal} *{r['symbol']}*  —  ₹`{r['close']:,.2f}`  {chg_str}",
            f"    EMA9: `{r['ema9']:,.2f}`  |  EMA21: `{r['ema21']:,.2f}`",
            f"    📦 Delivery: `{delivery_str}`  |  📈 Spread: `{r['ema_spread_pct']:+.2f}%`",
            f"    🔀 Cross: `{r['crossed_on']}` \\({days_tag}\\){vol_str}",
            f"    ⭐ Score: `{score(r):.1f}`",
            "",
        ]

    best    = results[0]
    tp1_str = f"₹`{best['tp1']:,.2f}`" if best.get("tp1") else "N/A"
    tp2_str = f"₹`{best['tp2']:,.2f}`" if best.get("tp2") else "N/A"
    rr1_str = f"`1:{best['rr1']}`"      if best.get("rr1") else ""
    rr2_str = f"`1:{best['rr2']}`"      if best.get("rr2") else ""
    atr_str = f"`{best['atr']:.2f}`"    if best.get("atr") else "N/A"

    lines += [
        "━" * 30,
        f"🏆 *BEST PICK: {best['symbol']}*",
        "",
        f"    🎯 Entry  : ₹`{best['entry']:,.2f}` \\(EMA 9 pullback\\)",
        f"    🛑 SL     : ₹`{best['sl']:,.2f}` \\(EMA 21 − 0\\.5×ATR\\)",
        f"    🎁 TP1    : {tp1_str}  {rr1_str}",
        f"    🚀 TP2    : {tp2_str}  {rr2_str}",
        f"    📐 ATR14  : {atr_str}",
        "",
        action_note,
        "",
        "━" * 30,
        "⚠️ _Not SEBI advice\\. DYOR\\._",
        "🤖 _Powered by ScanX \\+ yfinance \\+ GitHub Actions_",
    ]

    return "\n".join(lines)


def build_no_signal_message(session_type: str) -> str:
    now        = datetime.now()
    is_morning = session_type == "morning"
    emoji      = "🌅" if is_morning else "🌆"
    return (
        f"{emoji} *ScanX EMA Scanner — {now.strftime('%d %b %Y')}*\n\n"
        "❌ No fresh EMA 9/21 crossovers found today\\.\n"
        "Market consolidating\\. Stay patient\\. 🧘\n\n"
        "⚠️ _Not SEBI advice\\._"
    )


# ─────────────────────────────────────────────────────────────────────
# SEND TO TELEGRAM (MarkdownV2)
# ─────────────────────────────────────────────────────────────────────

def send_telegram(message: str, bot_token: str, chat_id: str):
    url     = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": "MarkdownV2",
    }
    # Split if too long (Telegram limit: 4096)
    chunks = []
    if len(message) <= 4096:
        chunks = [message]
    else:
        parts = message.split("\n\n")
        batch = ""
        for part in parts:
            if len(batch) + len(part) + 2 > 4000:
                chunks.append(batch)
                batch = ""
            batch += part + "\n\n"
        if batch.strip():
            chunks.append(batch)

    for chunk in chunks:
        payload["text"] = chunk
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            print(f"    ⚠  Telegram error: {resp.status_code} — {resp.text}")
            # Retry without parse_mode if markdown fails
            payload2 = {**payload, "parse_mode": ""}
            requests.post(url, json=payload2, timeout=15)


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────

def main():
    session = SESSION
    print(f"\n🚀  ScanX EMA Scanner — GitHub Actions")
    print(f"📅  {datetime.now().strftime('%d %b %Y  %H:%M:%S')} IST")
    print(f"📌  Session: {session}\n")

    # Validate tokens
    if not BOT_TOKEN or not CHAT_ID:
        print("❌  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in env!")
        sys.exit(1)

    # 1. Get stocks
    stocks = fetch_top_deliveries()
    if MIN_DELIVERY > 0:
        stocks = [s for s in stocks
                  if s["delivery_pct"] is None or s["delivery_pct"] >= MIN_DELIVERY]
        print(f"    Filter: delivery ≥ {MIN_DELIVERY}% → {len(stocks)} stocks remain.\n")

    # 2. Analyse (threaded via yfinance download, sequential here for simplicity)
    print(f"🔍  Scanning {len(stocks)} stocks...")
    results_raw = []
    for i, s in enumerate(stocks):
        sys.stdout.write(f"\r   [{i+1}/{len(stocks)}]  {s['symbol']:<14}")
        sys.stdout.flush()
        result = analyse_stock(s, lookback=LOOKBACK, period="3mo")
        if result:
            results_raw.append(result)
    sys.stdout.write("\r" + " " * 60 + "\r")

    # 3. Filter, rank, display
    results_raw.sort(key=score, reverse=True)
    top = results_raw[:TOP_N]

    print(f"\n✅  Found {len(results_raw)} crossover(s). Showing top {len(top)}.")

    if top:
        message = build_message(top, session)
    else:
        message = build_no_signal_message(session)

    # 4. Send
    print("📤  Sending to Telegram...")
    send_telegram(message, BOT_TOKEN, CHAT_ID)
    print("✅  Done!")


if __name__ == "__main__":
    main()
