"""
sync_db.py — Reconcile the local DB against live Alpaca account state.

For each screener account this script:
  1. Compares open DB positions against open Alpaca positions
     - DB open, Alpaca closed  → looks up fill price from trailing stop or
                                  order history and closes the DB record
     - DB open, Alpaca open    → checks share count matches; warns on mismatch
     - Alpaca open, not in DB  → warns (cannot auto-add without full entry data)
  2. Reconciles wallet current_balance against Alpaca cash

Does NOT modify Alpaca — read-only on the broker side.

Usage:
    python sync_db.py                    # all accounts (SML, MID, SUPER)
    python sync_db.py --screener SML
    python sync_db.py --screener SML MID
    python sync_db.py --screener LIVE
    python sync_db.py --screener SML2
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderStatus

from bot.database import (
    close_position,
    get_open_positions,
    get_wallet,
    init_db,
    update_wallet_cash,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ACCOUNTS = {
    "SML": {
        "key_env":            "SML_ALPACA_API_KEY",
        "secret_env":         "SML_ALPACA_API_SECRET",
        "screener_id":        "SML",
        "provider":           "SML_SCREENER",
        "paper":              True,
    },
    "SML2": {
        "key_env":            "SML2_ALPACA_API_KEY",
        "secret_env":         "SML2_ALPACA_API_SECRET",
        "screener_id":        "SML2",
        "provider":           "SML2_SCREENER",
        "paper":              True,
    },
    "MID": {
        "key_env":            "MID_ALPACA_API_KEY",
        "secret_env":         "MID_ALPACA_API_SECRET",
        "screener_id":        "MID",
        "provider":           "MID_SCREENER",
        "paper":              True,
    },
    "SUPER": {
        "key_env":            "SUPER_ALPACA_API_KEY",
        "secret_env":         "SUPER_ALPACA_API_SECRET",
        "screener_id":        "SUPER",
        "provider":           "SUPER_SCREENER",
        "paper":              True,
    },
    "LIVE": {
        "key_env":            "LIVE_ALPACA_API_KEY",
        "secret_env":         "LIVE_ALPACA_API_SECRET",
        "screener_id":        "LIVE",
        "provider":           "LIVE_SCREENER",
        "paper":              False,
    },
}


def _get_fill_price(client: TradingClient, order_id: str) -> float | None:
    """Return the filled avg price for an order, or None if not filled."""
    try:
        order = client.get_order_by_id(order_id)
        if order.status == OrderStatus.FILLED and order.filled_avg_price:
            return float(order.filled_avg_price)
    except Exception as e:
        logger.debug("Order lookup failed for %s: %s", order_id, e)
    return None


def sync_account(name: str, acct: dict) -> None:
    key    = os.getenv(acct["key_env"])
    secret = os.getenv(acct["secret_env"])
    if not key or not secret:
        logger.warning("[%s] API keys not set — skipping.", name)
        return

    screener_id = acct["screener_id"]
    provider    = acct["provider"]

    logger.info("=" * 55)
    logger.info("[%s]  screener=%s  provider=%s", name, screener_id, provider)
    logger.info("=" * 55)

    client = TradingClient(key, secret, paper=acct["paper"])

    # ── 1. Build Alpaca position map: symbol → qty ────────────────────────────
    try:
        alpaca_positions = client.get_all_positions()
        alpaca_map = {p.symbol: int(float(p.qty)) for p in alpaca_positions}
    except Exception as e:
        logger.error("[%s] Failed to fetch Alpaca positions: %s", name, e)
        return

    # ── 2. DB open positions ──────────────────────────────────────────────────
    db_positions = [dict(r) for r in get_open_positions(provider)]

    if not db_positions and not alpaca_map:
        logger.info("[%s] No open positions in DB or Alpaca — in sync.", name)
    else:
        logger.info("[%s] DB open: %d  |  Alpaca open: %d",
                    name, len(db_positions), len(alpaca_map))

    wallet_delta = 0.0

    for pos in db_positions:
        sym           = pos["symbol"]
        pos_id        = pos["id"]
        buy_price     = pos["buy_price"]
        shares        = pos["shares"]
        stop_order_id = pos.get("trailing_stop_order_id")

        if sym not in alpaca_map:
            # ── DB open, Alpaca closed ────────────────────────────────────────
            fill_price = None

            # Try trailing stop order first
            if stop_order_id:
                fill_price = _get_fill_price(client, stop_order_id)

            # Fall back to buy order if stop wasn't the closer
            if fill_price is None and pos.get("buy_order_id"):
                pass  # buy order won't tell us the sell price

            if fill_price is not None:
                pnl = (fill_price - buy_price) * shares
                close_position(pos_id, fill_price, datetime.now(timezone.utc), pnl)
                wallet_delta += fill_price * shares
                logger.info(
                    "  CLOSED  %s [id=%d]  fill=$%.4f  PnL=$%+.2f  (stop filled while offline)",
                    sym, pos_id, fill_price, pnl,
                )
            else:
                # Can't determine fill price — close at buy price (pnl=0) and warn
                close_position(pos_id, buy_price, datetime.now(timezone.utc), 0.0)
                wallet_delta += buy_price * shares
                logger.warning(
                    "  CLOSED  %s [id=%d]  fill price unknown — closed at buy price $%.4f  PnL=$0.00",
                    sym, pos_id, buy_price,
                )

        else:
            # ── DB open, Alpaca open — check share count ──────────────────────
            alpaca_qty = alpaca_map[sym]
            if alpaca_qty != shares:
                logger.warning(
                    "  MISMATCH  %s [id=%d]  DB=%d shares  Alpaca=%d shares — manual review needed",
                    sym, pos_id, shares, alpaca_qty,
                )
            else:
                logger.info("  OK  %s [id=%d]  %d shares", sym, pos_id, shares)

    # ── 3. Alpaca positions not in DB ─────────────────────────────────────────
    db_symbols = {pos["symbol"] for pos in db_positions}
    for sym, qty in alpaca_map.items():
        if sym not in db_symbols:
            logger.warning(
                "  UNTRACKED  %s  %d shares on Alpaca but no open DB record — manual review needed",
                sym, qty,
            )

    # ── 4. Apply wallet delta if any positions were closed ────────────────────
    if wallet_delta > 0:
        update_wallet_cash(screener_id, wallet_delta)
        logger.info("[%s] Wallet updated by +$%.2f from closed positions.", name, wallet_delta)

    # ── 5. Reconcile wallet cash against Alpaca ───────────────────────────────
    try:
        acct_info   = client.get_account()
        alpaca_cash = float(acct_info.cash)
        wallet      = get_wallet(screener_id)
        if wallet:
            db_cash = wallet["current_balance"]
            diff    = alpaca_cash - db_cash
            if abs(diff) < 0.01:
                logger.info("[%s] Wallet cash in sync: $%.2f", name, db_cash)
            else:
                logger.warning(
                    "[%s] Cash mismatch — DB=$%.2f  Alpaca=$%.2f  diff=%+.2f",
                    name, db_cash, alpaca_cash, diff,
                )
                update_wallet_cash(screener_id, diff)
                logger.info("[%s] Wallet cash corrected to $%.2f", name, alpaca_cash)
        else:
            logger.info("[%s] No wallet record in DB yet.", name)
    except Exception as e:
        logger.error("[%s] Failed to reconcile wallet cash: %s", name, e)


def main():
    parser = argparse.ArgumentParser(description="Sync DB positions and wallet against Alpaca.")
    parser.add_argument(
        "--screener", nargs="+",
        choices=list(ACCOUNTS),
        metavar="NAME",
        help="Account(s) to sync: SML, SML2, MID, SUPER, LIVE (default: SML SML2 MID SUPER)",
    )
    args = parser.parse_args()

    names   = args.screener if args.screener else ["SML", "SML2", "MID", "SUPER"]
    targets = {n: ACCOUNTS[n] for n in names if n in ACCOUNTS}

    if not targets:
        logger.error("No valid accounts specified.")
        sys.exit(1)

    init_db()
    for name, acct in targets.items():
        sync_account(name, acct)


if __name__ == "__main__":
    main()
