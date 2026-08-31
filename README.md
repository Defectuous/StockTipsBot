# StockTipsBot

A Python trading bot that monitors stock alert SMS messages forwarded via Google Voice to Gmail, parses the ticker, pulls market data from Alpaca, sends a Discord notification, buys shares, and manages the exit with a trailing stop-loss.

Designed to run 24/7 on a **Raspberry Pi 5** or any Linux machine.

---

## How It Works

```
Google Voice SMS alert
        │
        ▼
 Gmail (voice-noreply@google.com)
        │  poll every 60s, ignore emails > 2 min old
        ▼
  Parse ticker + provider
  (STT / BULLSEYE / ...)
        │
        ▼
  Alpaca market data
  RSI · Price · Volume · Momentum · OHLC
        │
        ▼
  Discord notification
        │
        ▼
  Buy $100 of shares (whole shares, no fractials)
        │
        ▼
  Trailing Stop-Loss (default 50%)
  → target: 2× return
        │
        ▼
  SQLite — minute-by-minute price bars logged
  until position closes
```

---

## Supported Alert Formats

### STT (StocksToTrade)
```
STT:
Genius Group Ltd (GNS)
-
Grab more ideas (& gifts) LIVE:
https://...
```

### BULLSEYE
```
BULLSEYE (Ndaq: TDTH)

*ABSOLUTE RIPPER*

TDTH Launching Over 40% From Alert!
```

New providers can be added by appending a regex tuple to `bot/email_parser.py`.

---

## Requirements

- Python 3.9+
- Alpaca Markets account (paper or live) — [app.alpaca.markets](https://app.alpaca.markets)
- Google Cloud project with Gmail API enabled — see [GMAIL_SETUP.md](GMAIL_SETUP.md)
- Discord webhook URL

---

## Setup

### 1. Clone & install dependencies

```bash
git clone https://github.com/Defectuous/StockTipsBot.git
cd StockTipsBot

# Create virtual environment
python -m venv .venv

# Activate (Windows PowerShell)
.venv\Scripts\Activate.ps1

# Activate (Linux/macOS)
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
nano .env
```

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Alpaca paper (or live) API key |
| `ALPACA_API_SECRET` | Alpaca API secret |
| `ALPACA_PAPER` | `true` for paper trading, `false` for live |
| `DISCORD_WEBHOOK_URL` | Discord channel webhook URL |
| `BUY_AMOUNT_USD` | Dollar amount per trade (default `100`) |
| `TRAILING_STOP_PERCENT` | Trailing stop distance in % (default `50`) |
| `EMAIL_POLL_INTERVAL` | Seconds between Gmail checks (default `60`) |
| `EMAIL_MAX_AGE_SECONDS` | Ignore emails older than this (default `120`) |

### 3. Authorize Gmail (one-time)

Follow **[GMAIL_SETUP.md](GMAIL_SETUP.md)** to create `credentials.json`, then run:

```bash
python setup_gmail.py
```

A browser tab opens → log in → grant read-only Gmail access → `token.json` is saved.
Copy both files to the Raspberry Pi.

### 4. Run

```bash
python main.py
```

Logs print to the console and to `stockbot.log`.

---

## Auto-start on Raspberry Pi (systemd)

Edit `stockbot.service` — update `User` and `WorkingDirectory` to match your Pi — then:

```bash
sudo cp stockbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable stockbot
sudo systemctl start stockbot

# Live logs
sudo journalctl -u stockbot -f
```

### Daily market-data capture

`store-daily-bars.timer` runs `tools/store_daily_bars.py` at 20:15 ET on
weekdays, after the post-market close. It stores full-session (04:00–20:00 ET)
1-minute bars for every symbol the SML/SML2 screeners traded or green-flagged
that day into a standalone `market_data.db` (kept separate from `stockbot.db`
so the trading DB stays small). Idempotent — safe to re-run any day.

```bash
sudo cp store-daily-bars.service store-daily-bars.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now store-daily-bars.timer

# Run once by hand (today, or a past day whose log still exists)
python tools/store_daily_bars.py
python tools/store_daily_bars.py --date 2026-08-31
python tools/store_daily_bars.py --backfill-days 30   # traded symbols only
```

`tools/backtest_strategies.py --cache market_data.db` reuses these bars instead
of re-fetching from Alpaca.

---

## Project Structure

```
StockTipsBot/
├── main.py                  # Entry point — runs both background threads
├── setup_gmail.py           # One-time Gmail OAuth helper
├── requirements.txt
├── .env.example
├── stockbot.service         # systemd unit file
├── GMAIL_SETUP.md           # Google Cloud step-by-step guide
└── bot/
    ├── gmail_reader.py      # Gmail API polling
    ├── email_parser.py      # Ticker + provider extraction
    ├── market_data.py       # Alpaca bars, RSI, momentum
    ├── trader.py            # Market buy + trailing stop orders
    ├── discord_notify.py    # Discord embed notifications
    └── database.py          # SQLite — positions + price bars
```

---

## Database

SQLite file: `stockbot.db`

| Table | Contents |
|---|---|
| `processed_emails` | Gmail message IDs already acted on (dedup) |
| `positions` | Every trade — entry, exit, P&L |
| `price_bars` | Minute-by-minute OHLC (backtest cache) |

Full-day 1-minute history for every traded/green-flagged symbol lives in a
separate `market_data.db` (`price_bars` + `daily_capture`), populated by
`store-daily-bars.timer` — see [Daily market-data capture](#daily-market-data-capture).

---

## Disclaimer

This bot is provided for educational purposes. Paper trade and validate the strategy thoroughly before using real money. Past alert performance does not guarantee future results.
