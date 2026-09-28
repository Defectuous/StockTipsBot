"""
Full sync of all closed DB trades → Google Sheets.

Clears and rewrites each provider tab from scratch so there are no duplicates.
SML2_SCREENER trades are merged into the SML tab.
Run this once to backfill, or any time the sheet gets out of sync.

Usage:
    python sync_sheets.py

If token.json is missing or expired, a browser window will open for re-auth.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from bot.database import _connect
from bot.sheets import _get_creds, _ensure_tab, HEADERS

load_dotenv()

GOOGLE_SHEET_ID  = os.getenv("GOOGLE_SHEET_ID", "")
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE       = os.getenv("GMAIL_TOKEN_FILE",        "token.json")

PROVIDER_TAB = {
    "SML_SCREENER":    "SML",
    "SML2_SCREENER":   "SML",
    "MID_SCREENER":    "MID",
    "SUPER_SCREENER":  "SUPER",
    "ACTIVE_SCREENER": "ACTIVE",
}


def fetch_all_closed() -> list:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT provider, symbol, shares, buy_price, sell_price, pnl, sell_time
               FROM positions
               WHERE status = 'closed'
               ORDER BY provider, sell_time"""
        ).fetchall()
    return [dict(r) for r in rows]


def clear_tab(service, sheet_id: str, tab_name: str) -> None:
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A:Z",
    ).execute()


def main():
    if not GOOGLE_SHEET_ID:
        print("GOOGLE_SHEET_ID not set in .env")
        return

    from googleapiclient.discovery import build

    print("Authenticating with Google...")
    creds = _get_creds(CREDENTIALS_FILE, TOKEN_FILE)
    if not creds:
        print("Failed to get credentials.")
        return

    service = build("sheets", "v4", credentials=creds)

    print("Fetching all closed trades from DB...")
    trades = fetch_all_closed()
    if not trades:
        print("No closed trades found in DB.")
        return

    # Group by tab (SML2 merges into SML, sorted by sell_time within each tab)
    by_tab: dict = {}
    for t in trades:
        tab = PROVIDER_TAB.get(t["provider"], t["provider"])
        by_tab.setdefault(tab, []).append(t)

    # Sort each tab's trades by sell_time
    for tab_trades in by_tab.values():
        tab_trades.sort(key=lambda t: t["sell_time"] or "")

    total_written = 0
    for tab_name, tab_trades in sorted(by_tab.items()):
        print(f"  {tab_name}: {len(tab_trades)} trades — clearing and rewriting...")
        try:
            _ensure_tab(service, GOOGLE_SHEET_ID, tab_name)
            clear_tab(service, GOOGLE_SHEET_ID, tab_name)

            rows = [HEADERS]
            for t in tab_trades:
                sell_date = t["sell_time"][:10] if t["sell_time"] else ""
                rows.append([
                    sell_date,
                    t["symbol"],
                    t["shares"],
                    round(t["buy_price"],  4),
                    round(t["sell_price"], 4),
                    round(t["pnl"] or 0,   2),
                ])

            service.spreadsheets().values().update(
                spreadsheetId=GOOGLE_SHEET_ID,
                range=f"{tab_name}!A1",
                valueInputOption="RAW",
                body={"values": rows},
            ).execute()

            print(f"    -> wrote {len(tab_trades)} rows")
            total_written += len(tab_trades)
        except Exception as e:
            print(f"    ERROR writing {tab_name}: {e}")

    print(f"\nDone. {total_written} trades synced across {len(by_tab)} tabs.")


if __name__ == "__main__":
    main()
