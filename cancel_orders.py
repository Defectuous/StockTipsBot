"""
cancel_orders.py — Cancel all open orders on one or more screener accounts.

Does NOT touch positions, the DB, or wallets.

Usage:
    python cancel_orders.py                    # all accounts (SML, MID, SUPER)
    python cancel_orders.py --screener SML
    python cancel_orders.py --screener SML MID
    python cancel_orders.py --screener LIVE    # live account
"""
import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

ACCOUNTS = {
    "SML": {
        "key_env":    "SML_ALPACA_API_KEY",
        "secret_env": "SML_ALPACA_API_SECRET",
        "paper":      True,
    },
    "MID": {
        "key_env":    "MID_ALPACA_API_KEY",
        "secret_env": "MID_ALPACA_API_SECRET",
        "paper":      True,
    },
    "SUPER": {
        "key_env":    "SUPER_ALPACA_API_KEY",
        "secret_env": "SUPER_ALPACA_API_SECRET",
        "paper":      True,
    },
    "LIVE": {
        "key_env":    "LIVE_ALPACA_API_KEY",
        "secret_env": "LIVE_ALPACA_API_SECRET",
        "paper":      False,
    },
}


def cancel_all(name: str, acct: dict) -> None:
    key    = os.getenv(acct["key_env"])
    secret = os.getenv(acct["secret_env"])
    if not key or not secret:
        logger.warning("[%s] API keys not set (%s / %s) — skipping.",
                       name, acct["key_env"], acct["secret_env"])
        return

    if not acct["paper"] and ALPACA_PAPER:
        logger.warning("[%s] Skipping LIVE account because ALPACA_PAPER=true in .env", name)
        return

    client = TradingClient(key, secret, paper=acct["paper"])

    try:
        orders = client.get_orders()
        if not orders:
            logger.info("[%s] No open orders.", name)
            return

        logger.info("[%s] %d open order(s) found — cancelling...", name, len(orders))
        for o in orders:
            logger.info("  %s  %s  %s × %s", o.id, o.symbol, o.side.value, o.qty)

        client.cancel_orders()
        logger.info("[%s] All orders cancelled.", name)

    except Exception as e:
        logger.error("[%s] Failed: %s", name, e)


def main():
    parser = argparse.ArgumentParser(description="Cancel open orders on screener account(s).")
    parser.add_argument(
        "--screener", nargs="+",
        choices=list(ACCOUNTS),
        metavar="NAME",
        help="Account(s) to cancel orders on: SML, MID, SUPER, LIVE (default: SML MID SUPER)",
    )
    args = parser.parse_args()

    names   = args.screener if args.screener else ["SML", "MID", "SUPER"]
    targets = {n: ACCOUNTS[n] for n in names if n in ACCOUNTS}

    if not targets:
        logger.error("No valid accounts specified.")
        sys.exit(1)

    for name, acct in targets.items():
        cancel_all(name, acct)


if __name__ == "__main__":
    main()
