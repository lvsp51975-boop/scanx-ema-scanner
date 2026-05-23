"""
ScanX EMA 9/21 Scanner — GitHub Actions Edition
Uses yfinance directly. Sends HTML-formatted Telegram alerts.
"""

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

BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
SESSION      = os.environ.get("SCAN_SESSION", "morning")

SCANX_URL    = "https://scanx.trade/insight/top-deliveries"
HEADERS      = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://scanx.trade/",
}

SYMBOL_OVERRIDES = {
    "M&M": "M&M.NS", "BAJAJ-AUTO": "BAJAJ-AUTO.NS",
    "ETERNAL": "ETERNAL.NS", "ZOMATO": "ZOMATO.NS",
}

FALLBACK_NIFTY50 = [
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK",
    "BAJAJ-AUTO","BAJFINANCE","BAJAJFINSV","BEL","BHARTIARTL",
    "CIPLA","COALINDIA","DRREDDY","EICHERMOT","GRASIM",
    "HCLTECH","HDFCBANK","HDFCLIFE","HINDUNILVR","HINDALCO",
    "ICICIBANK","INDIGO","INFY","ITC","JIOFIN",
    "JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI",
    "MAXHEALTH","NESTLEIND","NTPC","ONGC","POWERGRID",
    "RELIANCE","SBIN","SHRIRAMFIN","SBILIFE","SUNPHARMA",
    "TATASTEEL","TATACONSUM","TRENT","TCS","TECHM",
    "TITAN","ULTRACEMCO","WIPRO",
]

TOP_N        = 5
LOOKBACK     = 2
MIN_DELIVERY = 40.0

# ── SCRAPER ──────────────────────────────────────────────────────────

def fetch_top_deliveries():
    print("Fetching ScanX top deliveries...")
    try:
        resp = requests.get(SCANX_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"ScanX unreachable ({e}). Using fallback.")
        return _fallback_list()

    soup, stocks = BeautifulSoup(resp.text, "html.parser"), []
    table = soup.find("table")
    if table:
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 7: continue
            img = cols[0].find("img")
            symbol = None
            if img and "dhan.co/symbol/" in img.get("src",""):
                symbol = img["src"].split("/symbol/")[-1].replace(".png","").strip()
            if not symbol: continue
            def _n(td):
                t = td.get_text(strip=True).replace(",","").replace("%","").replace("+","")
                try: return float(t)
                except: return None
            stocks.append({"symbol":symbol,"name":cols[0].get_text(strip=True),
                           "ltp":_n(cols[1]),"change_pct":_n(cols[3]),"delivery_pct":_n(cols[6])})

    if not stocks:
        seen = set()
        for img in soup.find_all("img", src=re.compile(r"dhan\.co/symbol/")):
            sym = img["src"].split("/symbol/")[-1].replace(".png","").strip()
            if sym and sym not in seen:
                seen.add(sym)
                stocks.append({"symbol":sym,"name":sym,"ltp":None,"change_pct":None,"delivery_pct":None})

    if not stocks:
        return _fallback_list()
    print(f"  {len(stocks)} stocks found.")
    return stocks

def _fallback_list():
    return [{"symbol":s,"name":s,"ltp":None,"change_pct":None,"delivery_pct":None} for s in FALLBACK_NIFTY50]

def to_ns(sym):
    s = sym.upper()
    if s in SYMBOL_OVERRIDES: return SYMBOL_OVERRIDES[s]
    if s.endswith(".NS") or s.endswith(".BO"): return s
    return f"{s}.NS"

# ── DATA ─────────────────────────────────────────────────────────────

def get_history(symbol, period="3mo"):
    try:
        df = yf.download(symbol, period=period, interval="1d", auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty or len(df) < 25: return None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index)
        return df.sort_index()
    except: return None

def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def crossover(df, lookback):
    df = df.copy()
    df["e9"] = ema(df["Close"], 9)
    df["e21"] = ema(df["Close"], 21)
    for i in range(1, lookback+2):
        try:
            if df["e9"].iloc[-(i+1)] <= df["e21"].iloc[-(i+1)] and df["e9"].iloc[-i] > df["e21"].iloc[-i]:
                d = df.index[-i]
                return str(d.date()) if hasattr(d,"date") else str(d), i-1
        except: continue
    return None, None

def analyse(stock, lookback=LOOKBACK):
    yf_sym = to_ns(stock["symbol"])
    df = get_history(yf_sym)
    if df is None: return None
    cc = next((c for c in df.columns if str(c).lower()=="close"), None)
    if not cc: return None
    if cc != "Close": df = df.rename(columns={cc:"Close"})
    crossed_on, days_ago = crossover(df, lookback)
    if not crossed_on: return None

    df["e9"] = ema(df["Close"],9); df["e21"] = ema(df["Close"],21)
    e9=float(df["e9"].iloc[-1]); e21=float(df["e21"].iloc[-1]); close=float(df["Close"].iloc[-1])

    vc = next((c for c in df.columns if str(c).lower()=="volume"), None)
    vol_ratio = None
    if vc:
        v = df[vc].astype(float)
        if len(v)>=25: vol_ratio = round(v.iloc[-5:].mean()/v.iloc[-25:-5].mean(),2)

    atr = None
    hc = next((c for c in df.columns if str(c).lower()=="high"),None)
    lc = next((c for c in df.columns if str(c).lower()=="low"),None)
    if hc and lc:
        hi=df[hc].astype(float); lo=df[lc].astype(float); cl=df["Close"].astype(float)
        tr=pd.concat([hi-lo,(hi-cl.shift(1)).abs(),(lo-cl.shift(1)).abs()],axis=1).max(axis=1)
        atr=round(float(tr.ewm(span=14,adjust=False).mean().iloc[-1]),2)

    entry=round(e9,2); sl=round(e21-0.5*atr,2) if atr else round(e21,2)
    tp1=round(entry+1.5*atr,2) if atr else None; tp2=round(entry+3.0*atr,2) if atr else None
    risk=round(entry-sl,2)
    rr1=round((tp1-entry)/risk,1) if (atr and risk>0) else None
    rr2=round((tp2-entry)/risk,1) if (atr and risk>0) else None

    return {**stock,"yf_symbol":yf_sym,"close":round(close,2),"ema9":round(e9,2),"ema21":round(e21,2),
            "ema_spread_pct":round((e9-e21)/e21*100,3),"days_ago":days_ago,"crossed_on":crossed_on,
            "vol_ratio":vol_ratio,"atr":atr,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,"rr1":rr1,"rr2":rr2}

def score(r):
    return (max(0,10-r["days_ago"]*3) + (r["delivery_pct"] or 0)/10
            + (min(3,(r["vol_ratio"] or 1)-1) if r.get("vol_ratio") else 0)
            + max(0,3-abs(r["ema_spread_pct"])) + min(2,max(-2,(r["change_pct"] or 0)/2)))

# ── TELEGRAM (HTML mode — no escaping needed) ─────────────────────

def build_message(results, session_type):
    now = datetime.now()
    is_morning = session_type == "morning"
    emoji = "🌅" if is_morning else "🌆"
    session_name = "PRE-MARKET WATCHLIST" if is_morning else "POST-MARKET SIGNALS"
    action = ("⚡ Watch at market open. Entry on EMA 9 pullback."
              if is_morning else "📌 Confirmed crossovers. Plan entry tomorrow on dip.")

    lines = [
        f"{emoji} <b>ScanX EMA 9/21 — {session_name}</b>",
        f"📅 {now.strftime('%d %b %Y')}  |  🕐 {now.strftime('%H:%M')} IST",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
    for i, r in enumerate(results):
        medal = medals[i] if i < len(medals) else f"#{i+1}"
        chg = r["change_pct"] or 0
        arrow = "▲" if chg>=0 else "▼"
        days_tag = {0:"TODAY ✨",1:"Yesterday"}.get(r["days_ago"],f"{r['days_ago']}d ago")
        vol_str = ""
        if r.get("vol_ratio"):
            vi = "🔥" if r["vol_ratio"]>1.2 else ("⚠️" if r["vol_ratio"]<0.8 else "📊")
            vol_str = f"\n    {vi} Vol: <code>{r['vol_ratio']:.2f}x</code>"
        dlv = f"{r['delivery_pct']:.1f}%" if r["delivery_pct"] else "N/A"
        lines += [
            f"{medal} <b>{r['symbol']}</b>  —  ₹<code>{r['close']:,.2f}</code>  {arrow} {abs(chg):.2f}%",
            f"    EMA9: <code>{r['ema9']:,.2f}</code>  |  EMA21: <code>{r['ema21']:,.2f}</code>",
            f"    📦 Delivery: <code>{dlv}</code>  |  📈 Spread: <code>{r['ema_spread_pct']:+.2f}%</code>",
            f"    🔀 Cross: <code>{r['crossed_on']}</code> ({days_tag}){vol_str}",
            f"    ⭐ Score: <code>{score(r):.1f}</code>",
            "",
        ]
    best = results[0]
    tp1s = f"₹<code>{best['tp1']:,.2f}</code>" if best.get("tp1") else "N/A"
    tp2s = f"₹<code>{best['tp2']:,.2f}</code>" if best.get("tp2") else "N/A"
    rr1s = f"  1:{best['rr1']}" if best.get("rr1") else ""
    rr2s = f"  1:{best['rr2']}" if best.get("rr2") else ""
    atrs = f"<code>{best['atr']:.2f}</code>" if best.get("atr") else "N/A"
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🏆 <b>BEST PICK: {best['symbol']}</b>",
        "",
        f"    🎯 Entry  : ₹<code>{best['entry']:,.2f}</code> (EMA 9 pullback)",
        f"    🛑 SL     : ₹<code>{best['sl']:,.2f}</code> (EMA 21 - 0.5xATR)",
        f"    🎁 TP1    : {tp1s}{rr1s}",
        f"    🚀 TP2    : {tp2s}{rr2s}",
        f"    📐 ATR14  : {atrs}",
        "",
        action,
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "<i>Not SEBI advice. DYOR.</i>",
        "<i>Powered by ScanX + yfinance + GitHub Actions</i>",
    ]
    return "\n".join(lines)

def build_no_signal(session_type):
    emoji = "🌅" if session_type=="morning" else "🌆"
    return (f"{emoji} <b>ScanX EMA Scanner — {datetime.now().strftime('%d %b %Y')}</b>\n\n"
            "❌ No fresh EMA 9/21 crossovers found today.\nMarket consolidating. Stay patient. 🧘\n\n"
            "<i>Not SEBI advice.</i>")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id":CHAT_ID,"text":message,"parse_mode":"HTML"}
    chunks = [message] if len(message)<=4096 else []
    if not chunks:
        parts=message.split("\n\n"); batch=""
        for p in parts:
            if len(batch)+len(p)+2>4000: chunks.append(batch); batch=""
            batch+=p+"\n\n"
        if batch.strip(): chunks.append(batch)
    for chunk in chunks:
        payload["text"]=chunk
        r=requests.post(url,json=payload,timeout=15)
        if not r.ok: print(f"Telegram error: {r.status_code} — {r.text}")

# ── MAIN ─────────────────────────────────────────────────────────────

def main():
    print(f"\nScanX EMA Scanner — {datetime.now().strftime('%d %b %Y %H:%M:%S')} IST")
    print(f"Session: {SESSION}\n")
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set!"); sys.exit(1)

    stocks = fetch_top_deliveries()
    if MIN_DELIVERY>0:
        stocks=[s for s in stocks if s["delivery_pct"] is None or s["delivery_pct"]>=MIN_DELIVERY]
        print(f"Filter delivery >= {MIN_DELIVERY}% -> {len(stocks)} stocks\n")

    print(f"Scanning {len(stocks)} stocks...")
    results = []
    for i,s in enumerate(stocks):
        sys.stdout.write(f"\r  [{i+1}/{len(stocks)}]  {s['symbol']:<14}")
        sys.stdout.flush()
        r = analyse(s)
        if r: results.append(r)
    sys.stdout.write("\r"+" "*60+"\r")

    results.sort(key=score, reverse=True)
    top = results[:TOP_N]
    print(f"Found {len(results)} crossover(s).")

    message = build_message(top, SESSION) if top else build_no_signal(SESSION)
    print("Sending to Telegram...")
    send_telegram(message)
    print("Done!")

if __name__=="__main__":
    main()
