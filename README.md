# 🔍 ScanX EMA 9/21 Scanner — GitHub Actions Edition

Auto-runs on GitHub's free servers every trading day and sends EMA 9/21 crossover alerts to your Telegram. **PC off hone pe bhi kaam karta hai.**

---

## ⚡ Setup (5 minutes)

### Step 1 — Create a GitHub repo

1. Go to [github.com](https://github.com) → **New repository**
2. Name it `scanx-ema-scanner` (private recommended)
3. Upload these files:
   - `scanner.py`
   - `.github/workflows/scan.yml`
   - `README.md`

### Step 2 — Add Telegram secrets

In your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these two secrets:

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your chat/group ID (use [@userinfobot](https://t.me/userinfobot)) |

### Step 3 — Enable Actions

Go to your repo → **Actions tab** → Click **"I understand my workflows, go ahead and enable them"**

---

## 🕐 Schedule

Runs automatically on **Monday–Friday** (NSE trading days):

| Time (IST) | Session | What it does |
|---|---|---|
| 9:00 AM | Morning | Pre-market watchlist — yesterday's crossovers |
| 4:00 PM | Evening | Post-market signals — today's confirmed crossovers |

---

## 🧪 Test manually

Go to **Actions → ScanX EMA 9/21 Scanner → Run workflow** → choose `morning` or `evening` → click **Run workflow**.

Result comes in Telegram within ~2 minutes.

---

## ⚙️ Customize

Edit `scanner.py` top section:

```python
MIN_DELIVERY = 40.0   # Min delivery% filter (0 = no filter)
TOP_N        = 5      # How many stocks to show
LOOKBACK     = 2      # How many sessions to check for crossover
CONCURRENCY  = 8      # Parallel downloads
```

---

## 📦 Dependencies

```
yfinance
pandas
requests
beautifulsoup4
```

No MCP server needed — runs 100% on GitHub's free runners.

---

## 💡 Notes

- GitHub Actions free tier gives **2000 minutes/month** — scanner uses ~2 min per run × 2 runs/day × 22 days = ~88 min/month. Well within free limits.
- ScanX scraping may fail if their page structure changes — fallback Nifty 50 list kicks in automatically.
- Weekends/holidays: GitHub still tries to run on schedule. Scanner will run but market data will be Friday's close (harmless).

---

> ⚠️ Not SEBI-registered advice. For educational purposes. DYOR.
