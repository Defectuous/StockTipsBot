"""
Reconcile DB wallet current_balance against Alpaca live cash for MID and SML.
Corrects current_balance only — day_start_balance is left untouched so
tomorrow's per-trade sizing isn't affected until the normal day reset fires.
"""
import os
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from bot.database import init_db, get_wallet, update_wallet_cash

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv()

ACCOUNTS = [
    {
        "id":     "MID",
        "key":    os.getenv("MID_ALPACA_API_KEY")  or os.environ["ALPACA_API_KEY"],
        "secret": os.getenv("MID_ALPACA_API_SECRET") or os.environ["ALPACA_API_SECRET"],
    },
    {
        "id":     "SML",
        "key":    os.getenv("SML_ALPACA_API_KEY")  or os.environ["ALPACA_API_KEY"],
        "secret": os.getenv("SML_ALPACA_API_SECRET") or os.environ["ALPACA_API_SECRET"],
    },
]


def reconcile(account: dict) -> None:
    sid = account["id"]
    client = TradingClient(account["key"], account["secret"], paper=True)
    acct = client.get_account()
    alpaca_cash = float(acct.cash)

    wallet = get_wallet(sid)
    if not wallet:
        logger.warning("[%s] No wallet in DB — skipping", sid)
        return

    db_cash = wallet["current_balance"]
    delta   = alpaca_cash - db_cash

    logger.info(
        "[%s] DB=$%.2f  Alpaca=$%.2f  delta=%+.2f",
        sid, db_cash, alpaca_cash, delta,
    )

    if abs(delta) < 0.01:
        logger.info("[%s] In sync — no update needed", sid)
        return

    update_wallet_cash(sid, delta)
    logger.info("[%s] current_balance updated to $%.2f", sid, alpaca_cash)


if __name__ == "__main__":
    init_db()
    for acct in ACCOUNTS:
        try:
            reconcile(acct)
        except Exception as e:
            logger.error("[%s] Reconcile failed: %s", acct["id"], e)
