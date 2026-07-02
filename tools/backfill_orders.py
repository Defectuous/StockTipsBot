"""
backfill_orders.py — Reconstruct closed positions from Alpaca order history.

Pairs filled buy and sell orders by symbol, inserts them as closed positions
in the DB. Skips any pair whose buy_order_id already exists in the DB.

Usage:
    python backfill_orders.py                        # Jun 17 & 18, SML + SML2 + MID
    python backfill_orders.py --since 2026-06-17     # specific start date
    python backfill_orders.py --screener SML         # one account
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
from alpaca.trading.enums import OrderStatus, OrderSide
from alpaca.trading.requests import GetOrdersRequest

from bot.database import init_db, _connect

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ACCOUNTS = {
    "SML": {
        "key_env":    "SML_ALPACA_API_KEY",
        "secret_env": "SML_ALPACA_API_SECRET",
        "provider":   "SML_SCREENER",
        "paper":      True,
    },
    "SML2": {
        "key_env":    "SML2_ALPACA_API_KEY",
        "secret_env": "SML2_ALPACA_API_SECRET",
        "provider":   "SML2_SCREENER",
        "paper":      True,
    },
    "MID": {
        "key_env":    "MID_ALPACA_API_KEY",
        "secret_env": "MID_ALPACA_API_SECRET",
        "provider":   "MID_SCREENER",
        "paper":      True,
    },
}


def _already_in_db(buy_order_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM positions WHERE buy_order_id = ?", (buy_order_id,)
        ).fetchone()
    return row is not None


def _insert_closed(
    symbol: str,
    provider: str,
    shares: int,
    buy_price: float,
    buy_time: datetime,
    buy_order_id: str,
    sell_price: float,
    sell_time: datetime,
    sell_order_id: str,
    pnl: float,
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO positions
               (symbol, provider, shares, buy_price, buy_time, buy_order_id,
                trailing_stop_order_id, status, sell_price, sell_time, pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)""",
            (symbol, provider, shares, buy_price,
             buy_time.isoformat(), buy_order_id,
             sell_order_id,
             sell_price, sell_time.isoformat(), pnl),
        )


def backfill_account(name: str, acct: dict, since: datetime) -> None:
    key    = os.getenv(acct["key_env"])
    secret = os.getenv(acct["secret_env"])
    if not key or not secret:
        logger.warning("[%s] API keys not set — skipping.", name)
        return

    provider = acct["provider"]
    client   = TradingClient(key, secret, paper=acct["paper"])

    req    = GetOrdersRequest(status="closed", after=since, limit=500)
    orders = client.get_orders(filter=req)

    # Separate filled buys and sells
    buys  = {}  # symbol -> list of buy orders
    sells = {}  # symbol -> list of sell orders

    for o in orders:
        if o.status != OrderStatus.FILLED:
            continue
        if o.side == OrderSide.BUY:
            buys.setdefault(o.symbol, []).append(o)
        else:
            sells.setdefault(o.symbol, []).append(o)

    # Sort each by filled_at so we can pair FIFO
    for sym in buys:
        buys[sym].sort(key=lambda o: o.filled_at)
    for sym in sells:
        sells[sym].sort(key=lambda o: o.filled_at)

    inserted = 0
    skipped  = 0

    for sym, buy_list in buys.items():
        sell_list = sells.get(sym, [])
        pairs = min(len(buy_list), len(sell_list))

        for i in range(pairs):
            b = buy_list[i]
            s = sell_list[i]

            buy_qty  = int(float(b.filled_qty))
            sell_qty = int(float(s.filled_qty))
            if buy_qty != sell_qty:
                logger.warning(
                    "[%s] %s qty mismatch buy=%d sell=%d — skipping pair",
                    name, sym, buy_qty, sell_qty,
                )
                continue

            if _already_in_db(str(b.id)):
                logger.info("[%s] %s buy_order_id=%s already in DB — skip", name, sym, b.id)
                skipped += 1
                continue

            buy_price  = float(b.filled_avg_price)
            sell_price = float(s.filled_avg_price)
            pnl        = round((sell_price - buy_price) * buy_qty, 6)

            _insert_closed(
                symbol       = sym,
                provider     = provider,
                shares       = buy_qty,
                buy_price    = buy_price,
                buy_time     = b.filled_at,
                buy_order_id = str(b.id),
                sell_price   = sell_price,
                sell_time    = s.filled_at,
                sell_order_id= str(s.id),
                pnl          = pnl,
            )
            logger.info(
                "[%s]  INSERTED  %s  %d sh  buy=$%.4f  sell=$%.4f  PnL=$%+.2f",
                name, sym, buy_qty, buy_price, sell_price, pnl,
            )
            inserted += 1

        if len(buy_list) > len(sell_list):
            logger.warning(
                "[%s] %s has %d buys but only %d sells — %d unpaired buy(s) skipped",
                name, sym, len(buy_list), len(sell_list),
                len(buy_list) - len(sell_list),
            )

    logger.info("[%s] Done — %d inserted, %d already existed.", name, inserted, skipped)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-06-17",
                        help="Start date (YYYY-MM-DD), default 2026-06-17")
    parser.add_argument("--screener", nargs="+", choices=list(ACCOUNTS),
                        help="Accounts to backfill (default: all)")
    args = parser.parse_args()

    since   = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    targets = {k: ACCOUNTS[k] for k in (args.screener or ACCOUNTS)}

    init_db()
    for name, acct in targets.items():
        logger.info("=" * 55)
        backfill_account(name, acct, since)


if __name__ == "__main__":
    main()
