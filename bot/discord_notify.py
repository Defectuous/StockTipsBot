import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_GREEN = 0x00FF7F
_RED   = 0xFF4444
_BLUE  = 0x5865F2


def _post(webhook_url: str, payload: dict):
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            logger.warning("Discord returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Discord post failed: %s", e)


def send_alert(
    webhook_url: str,
    symbol: str,
    provider: str,
    price: float,
    rsi: Optional[float],
    volume: int,
    momentum: Optional[float],
    shares_bought: int,
    total_cost: float,
    paper: bool = True,
):
    mode_tag = "[PAPER] " if paper else ""
    rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
    mom_str = f"{momentum:+.2f}%" if momentum is not None else "N/A"

    embed = {
        "title": f"{mode_tag}Alert: {symbol}",
        "color": _BLUE,
        "fields": [
            {"name": "Stock",    "value": symbol,                          "inline": True},
            {"name": "Provider", "value": provider,                        "inline": True},
            {"name": "Price",    "value": f"${price:.4f}",                 "inline": True},
            {"name": "RSI",      "value": rsi_str,                         "inline": True},
            {"name": "Volume",   "value": f"{volume:,}",                   "inline": True},
            {"name": "Momentum", "value": mom_str,                         "inline": True},
            {"name": "Bought",   "value": f"{shares_bought} shares",       "inline": True},
            {"name": "Cost",     "value": f"${total_cost:.2f}",            "inline": True},
        ],
        "footer": {"text": f"StockTipsBot | {'Paper' if paper else 'Live'} Trading"},
    }
    _post(webhook_url, {"embeds": [embed]})


def send_close(
    webhook_url: str,
    symbol: str,
    buy_price: float,
    sell_price: float,
    shares: int,
    pnl: float,
    paper: bool = True,
    reason: str = "Trailing Stop triggered",
):
    mode_tag = "[PAPER] " if paper else ""
    gain = sell_price / buy_price
    embed = {
        "title": f"{mode_tag}Position Closed: {symbol}",
        "color": _GREEN if pnl >= 0 else _RED,
        "fields": [
            {"name": "Result",     "value": "PROFIT" if pnl >= 0 else "LOSS", "inline": True},
            {"name": "Buy Price",  "value": f"${buy_price:.4f}",               "inline": True},
            {"name": "Sell Price", "value": f"${sell_price:.4f}",              "inline": True},
            {"name": "Shares",     "value": str(shares),                       "inline": True},
            {"name": "P&L",        "value": f"${pnl:+.2f}",                   "inline": True},
            {"name": "Return",     "value": f"{gain:.2f}x",                    "inline": True},
        ],
        "footer": {"text": f"StockTipsBot | {reason}"},
    }
    _post(webhook_url, {"embeds": [embed]})


def send_error(webhook_url: str, message: str):
    _post(webhook_url, {"embeds": [{"title": "StockTipsBot Notice", "description": message, "color": _RED}]})
