"""
End-of-day trading report — sends a Discord summary of all closed and open positions.

Usage:
    python report_day.py              # today's report (ET timezone)
    python report_day.py 2026-06-10   # specific date

Config (env or .env):
    DISCORD_WEBHOOK_URL   required
    ALPACA_PAPER          true/false  (shown in footer)
"""
import os
import sys
from datetime import date, datetime

import pytz
from dotenv import load_dotenv

from bot.database import _connect
from bot.discord_notify import _post
from bot.sheets import append_trades

load_dotenv()

DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK_URL", "")
ALPACA_PAPER     = os.getenv("ALPACA_PAPER", "true").lower() == "true"
GOOGLE_SHEET_ID  = os.getenv("GOOGLE_SHEET_ID", "")
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE       = os.getenv("GMAIL_TOKEN_FILE", "token.json")

_GREEN = 0x00FF7F
_RED   = 0xFF4444
_GOLD  = 0xFFAA00
_BLUE  = 0x5865F2


def _fetch_closed(report_date: date) -> list:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT symbol, provider, shares, buy_price, sell_price, pnl, sell_time
               FROM positions
               WHERE status = 'closed'
                 AND date(sell_time) = ?
               ORDER BY provider, sell_time""",
            (report_date.isoformat(),),
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_open() -> list:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT symbol, provider, shares, buy_price, buy_time
               FROM positions
               WHERE status = 'open'
               ORDER BY provider, buy_time""",
        ).fetchall()
    return [dict(r) for r in rows]


def _send_provider_report(provider: str, trades: list, report_date: date):
    total_pnl = sum(t["pnl"] or 0 for t in trades)
    wins      = sum(1 for t in trades if (t["pnl"] or 0) >= 0)
    losses    = sum(1 for t in trades if (t["pnl"] or 0) < 0)
    mode_tag  = "[PAPER] " if ALPACA_PAPER else ""

    lines = []
    for t in trades:
        pnl_str = f"${t['pnl']:+.2f}" if t["pnl"] is not None else "n/a"
        lines.append(
            f"`{t['symbol']:<6}` "
            f"buy ${t['buy_price']:.4f}  "
            f"sell ${t['sell_price']:.4f}  "
            f"**{pnl_str}**"
        )

    embed = {
        "title":       f"{mode_tag}{provider} — {report_date.strftime('%Y-%m-%d')}",
        "description": "\n".join(lines),
        "color":       _GREEN if total_pnl >= 0 else _RED,
        "fields": [
            {"name": "Trades",      "value": str(len(trades)),       "inline": True},
            {"name": "P&L",         "value": f"${total_pnl:+.2f}",  "inline": True},
            {"name": "Wins/Losses", "value": f"{wins}W / {losses}L", "inline": True},
        ],
        "footer": {"text": f"StockTipsBot | {'Paper' if ALPACA_PAPER else 'Live'} Trading"},
    }
    _post(DISCORD_WEBHOOK, {"embeds": [embed]})




def _send_open_positions(open_positions: list):
    now   = datetime.now(pytz.UTC)
    lines = []
    for p in open_positions:
        buy_dt = datetime.fromisoformat(p["buy_time"])
        if buy_dt.tzinfo is None:
            buy_dt = buy_dt.replace(tzinfo=pytz.UTC)
        held_min = int((now - buy_dt).total_seconds() / 60)
        lines.append(
            f"• **{p['symbol']}** [{p['provider']}]  ${p['buy_price']:.4f}  {p['shares']}sh  held {held_min}m"
        )
    mode_tag = "[PAPER] " if ALPACA_PAPER else ""
    embed = {
        "title":       f"{mode_tag}Open Positions",
        "description": "\n".join(lines),
        "color":       _GOLD,
        "footer":      {"text": "StockTipsBot | Still open at report time"},
    }
    _post(DISCORD_WEBHOOK, {"embeds": [embed]})


def main():
    if not DISCORD_WEBHOOK:
        print("DISCORD_WEBHOOK_URL not set — nothing to send.")
        return

    if len(sys.argv) > 1:
        report_date = date.fromisoformat(sys.argv[1])
    else:
        report_date = datetime.now(pytz.timezone("America/New_York")).date()

    print(f"Reporting for {report_date}...")

    closed   = _fetch_closed(report_date)
    open_pos = _fetch_open()

    if not closed and not open_pos:
        print("No trades found for that date.")
        _post(DISCORD_WEBHOOK, {"embeds": [{
            "title":       f"Daily Report — {report_date}",
            "description": "_No trades today._",
            "color":       _BLUE,
            "footer":      {"text": "StockTipsBot"},
        }]})
        return

    # Per-provider embeds
    providers: dict[str, list] = {}
    for t in closed:
        providers.setdefault(t["provider"], []).append(t)

    for provider, trades in sorted(providers.items()):
        print(f"  {provider}: {len(trades)} closed trades")
        _send_provider_report(provider, trades, report_date)

    if open_pos:
        print(f"  {len(open_pos)} position(s) still open")
        _send_open_positions(open_pos)

    if GOOGLE_SHEET_ID and closed:
        print("Appending to Google Sheets...")
        append_trades(closed, GOOGLE_SHEET_ID, CREDENTIALS_FILE, TOKEN_FILE)

    print("Done.")


if __name__ == "__main__":
    main()
