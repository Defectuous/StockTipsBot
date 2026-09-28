"""
Google Sheets integration — appends daily trade rows to per-screener tabs.

Each tab (SML, MID, SUPER) has a header row on first use, then one row per trade:
  Date | Symbol | Shares | Buy Price | Sell Price | P&L
"""
import logging
from pathlib import Path
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

HEADERS = ["Date", "Symbol", "Shares", "Buy Price", "Sell Price", "P&L"]

# Maps provider name in DB → sheet tab name
PROVIDER_TAB = {
    "SML_SCREENER":   "SML",
    "MID_SCREENER":   "MID",
    "SUPER_SCREENER": "SUPER",
}


def _get_creds(credentials_file: str, token_file: str) -> Optional[Credentials]:
    creds: Optional[Credentials] = None

    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, "w") as f:
                f.write(creds.to_json())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_file, "w") as f:
                f.write(creds.to_json())

    return creds


def _ensure_tab(service, sheet_id: str, tab_name: str) -> None:
    """Create the tab if it doesn't exist, then add headers if the sheet is empty."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = [s["properties"]["title"] for s in meta["sheets"]]

    if tab_name not in existing:
        body = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
        service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
        logger.info("Created sheet tab: %s", tab_name)

    # Check if headers are already present
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A1:F1",
    ).execute()

    if not result.get("values"):
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
        logger.info("Added headers to tab: %s", tab_name)


def append_trades(
    trades: List[dict],
    sheet_id: str,
    credentials_file: str,
    token_file: str,
) -> None:
    """
    Append closed trade rows to the appropriate tab for each provider.
    trades: list of dicts with keys: provider, symbol, shares, buy_price, sell_price, pnl, sell_time
    """
    if not trades or not sheet_id:
        return

    creds = _get_creds(credentials_file, token_file)
    if not creds:
        logger.error("Sheets: failed to get credentials")
        return

    service = build("sheets", "v4", credentials=creds)

    # Group by tab
    by_tab: dict = {}
    for t in trades:
        tab = PROVIDER_TAB.get(t["provider"], t["provider"])
        by_tab.setdefault(tab, []).append(t)

    for tab_name, tab_trades in by_tab.items():
        try:
            _ensure_tab(service, sheet_id, tab_name)

            rows = []
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

            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"{tab_name}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()

            logger.info("Sheets: appended %d row(s) to tab '%s'", len(rows), tab_name)

        except Exception as e:
            logger.error("Sheets: failed to write tab '%s': %s", tab_name, e)
