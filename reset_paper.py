"""
reset_paper.py — Paper account reset.

Cancels all open Alpaca orders, closes all Alpaca positions,
marks open DB positions closed (pnl=0), and clears the wallet
so the screener re-initializes it on next run.

Usage:
    python reset_paper.py                       # reset all three accounts
    python reset_paper.py --screener SML        # reset one account
    python reset_paper.py --screener SML MID    # reset two accounts
"""
import os
import sys
import time
import logging
import argparse
from datetime import datetime, timezone

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

from bot.database import _connect

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

if not ALPACA_PAPER:
    logger.error("ALPACA_PAPER is not 'true' — refusing to reset a live account.")
    sys.exit(1)

ACCOUNTS = [
    {
        "name":               "SML",
        "key_env":            "SML_ALPACA_API_KEY",
        "secret_env":         "SML_ALPACA_API_SECRET",
        "screener_id_env":    "SCREENER_ID",
        "screener_id_default":"SML",
    },
    {
        "name":               "MID",
        "key_env":            "MID_ALPACA_API_KEY",
        "secret_env":         "MID_ALPACA_API_SECRET",
        "screener_id_env":    "MID_SCREENER_ID",
        "screener_id_default":"MID",
    },
    {
        "name":               "SUPER",
        "key_env":            "SUPER_ALPACA_API_KEY",
        "secret_env":         "SUPER_ALPACA_API_SECRET",
        "screener_id_env":    "SUPER_SCREENER_ID",
        "screener_id_default":"SUPER",
    },
]


def reset_account(acct: dict) -> None:
    key    = os.getenv(acct["key_env"])
    secret = os.getenv(acct["secret_env"])
    if not key or not secret:
        logger.warning(
            "[%s] API keys not set (%s / %s) — skipping.",
            acct["name"], acct["key_env"], acct["secret_env"],
        )
        return

    screener_id = os.getenv(acct["screener_id_env"], acct["screener_id_default"])
    provider    = f"{screener_id}_SCREENER"

    logger.info("=== Resetting %s  screener_id=%s  provider=%s ===",
                acct["name"], screener_id, provider)

    client = TradingClient(key, secret, paper=True)

    logger.info("[%s] Cancelling all open orders...", acct["name"])
    try:
        client.cancel_orders()
        logger.info("[%s] Orders cancelled.", acct["name"])
    except Exception as e:
        logger.warning("[%s] cancel_orders failed: %s", acct["name"], e)

    logger.info("[%s] Closing all positions...", acct["name"])
    try:
        client.close_all_positions(cancel_orders=True)
        logger.info("[%s] Close-all submitted.", acct["name"])
    except Exception as e:
        logger.warning("[%s] close_all_positions failed: %s", acct["name"], e)

    time.sleep(3)

    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, symbol FROM positions WHERE status = 'open' AND provider = ?",
            (provider,),
        ).fetchall()
        if rows:
            conn.executemany(
                """UPDATE positions
                   SET status = 'closed', sell_price = buy_price, sell_time = ?, pnl = 0
                   WHERE id = ?""",
                [(now, r["id"]) for r in rows],
            )
            logger.info("[%s] Marked %d position(s) closed: %s",
                        acct["name"], len(rows), ", ".join(r["symbol"] for r in rows))
        else:
            logger.info("[%s] No open positions in DB.", acct["name"])

        conn.execute("DELETE FROM wallets WHERE screener_id = ?", (screener_id,))
        logger.info("[%s] Wallet cleared (will reinitialize on next screener run).", acct["name"])

    logger.info("[%s] Reset complete.", acct["name"])


def main():
    parser = argparse.ArgumentParser(description="Reset paper trading account(s).")
    parser.add_argument(
        "--screener", nargs="+", choices=["SML", "MID", "SUPER"],
        metavar="NAME",
        help="Screener(s) to reset: SML, MID, SUPER (default: all)",
    )
    args = parser.parse_args()

    names   = set(args.screener) if args.screener else {"SML", "MID", "SUPER"}
    targets = [a for a in ACCOUNTS if a["name"] in names]

    for acct in targets:
        reset_account(acct)


if __name__ == "__main__":
    main()
