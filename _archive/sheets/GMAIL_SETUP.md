# Gmail API Setup Guide

This is a one-time setup to let StockTipsBot read your Gmail inbox.

---

## Step 1 — Create a Google Cloud Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click **Select a project** → **New Project**
3. Name it anything (e.g. `StockTipsBot`) → **Create**

---

## Step 2 — Enable the Gmail API

1. In your new project, go to **APIs & Services → Library**
2. Search for **Gmail API** → click it → **Enable**

---

## Step 3 — Configure the OAuth Consent Screen

1. Go to **APIs & Services → OAuth consent screen**
2. Choose **External** → **Create**
3. Fill in:
   - **App name**: StockTipsBot
   - **User support email**: your Gmail address
   - **Developer contact email**: your Gmail address
4. Click **Save and Continue** through the remaining steps (no scopes needed here)
5. On the **Test users** page, add your Gmail address → **Save**

---

## Step 4 — Create OAuth Credentials

1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Name: anything (e.g. `StockTipsBot Desktop`)
5. Click **Create**
6. In the popup, click **Download JSON**
7. Rename the downloaded file to `credentials.json`
8. Place it in the same folder as `main.py`

---

## Step 5 — Authorize the Bot (one-time, needs a browser)

### Option A — On your PC/laptop (recommended for Raspberry Pi)

Run this on a machine that has a web browser:

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your API keys first
python setup_gmail.py
```

A browser tab opens → log in with your Google account → click **Allow**.

A `token.json` file is created. **Copy it to your Raspberry Pi** next to `main.py`.

### Option B — Headless (on the Pi itself, via SSH)

If you need to authorize directly on the Pi without a browser:

1. Edit `bot/gmail_reader.py` — in `_authenticate()` replace:
   ```python
   creds = flow.run_local_server(port=0)
   ```
   with:
   ```python
   creds = flow.run_console()
   ```
2. Run `python setup_gmail.py` over SSH
3. It prints a URL → open it in a browser on any device → paste the code back into the terminal
4. Revert the change in `gmail_reader.py` (optional — `run_console()` works at runtime too)

---

## Step 6 — Set Up `.env`

```bash
cp .env.example .env
nano .env
```

Fill in:

| Variable | Where to get it |
|---|---|
| `ALPACA_API_KEY` | [app.alpaca.markets](https://app.alpaca.markets) → Paper → API Keys |
| `ALPACA_API_SECRET` | Same page |
| `DISCORD_WEBHOOK_URL` | Discord → channel settings → Integrations → Webhooks |
| Everything else | Defaults are fine to start |

---

## Step 7 — Run the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Start the bot
python main.py
```

Logs go to the console **and** `stockbot.log`.

---

## Auto-start on Raspberry Pi (systemd)

Copy `stockbot.service` to the correct location and enable it:

```bash
sudo cp stockbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable stockbot
sudo systemctl start stockbot

# Watch logs
sudo journalctl -u stockbot -f
```

---

## Notes

- **token.json** contains your OAuth refresh token. Keep it private — do not commit it to git.
- The token auto-refreshes; you only need to re-run `setup_gmail.py` if you revoke access or the refresh token expires (rare).
- Alpaca's free data tier does not cover all OTC/penny stocks. If data is missing the bot will post a notice to Discord and skip the buy.
- Paper trading is enabled by default (`ALPACA_PAPER=true`). Change to `false` only when you are ready to trade with real money.
