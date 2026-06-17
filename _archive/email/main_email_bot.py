"""
StockTipsBot — main entry point.

Two background threads run on independent 60-second loops:
  • email-poll  : reads new Google Voice alerts from Gmail
  • pos-track   : records minute bars + checks trailing-stop fills
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

import pytz
from dotenv import load_dotenv

load_dotenv()

from bot.database import (
    close_position,
    get_open_positions,
    init_db,
    is_email_processed,
    is_ticker_on_cooldown,
    mark_email_processed,
    record_ticker_alert,
    save_position,
    save_price_bar,
    update_trailing_stop_order,
)
from bot.discord_notify import send_alert, send_close, send_error, send_tip_received
from bot.email_parser import parse_email
from bot.gmail_reader import GmailReader
from bot.market_data import get_stock_data
from bot.trader import Trader

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("stockbot.log"),
    ],
)
logger = logging.getLogger(__name__)

# ── Config from .env ──────────────────────────────────────────────────────────
ALPACA_KEY      = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET   = os.environ["ALPACA_API_SECRET"]
ALPACA_PAPER    = os.getenv("ALPACA_PAPER", "true").lower() == "true"
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
BUY_AMOUNT      = float(os.getenv("BUY_AMOUNT_USD", "100"))
TRAIL_PCT       = float(os.getenv("TRAILING_STOP_PERCENT", "10"))
POLL_INTERVAL   = int(os.getenv("EMAIL_POLL_INTERVAL", "60"))
EMAIL_MAX_AGE   = int(os.getenv("EMAIL_MAX_AGE_SECONDS", "120"))
TICKER_COOLDOWN  = int(os.getenv("TICKER_COOLDOWN_MINUTES", "30")) * 60
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES", "120"))
DUMP_TIME_ET     = os.getenv("DUMP_TIME_ET", "")            # e.g. "16:30"
HARD_STOP_PCT    = float(os.getenv("HARD_STOP_PCT",   "0")) # 0 = disabled
GMAIL_CREDS     = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN     = os.getenv("GMAIL_TOKEN_FILE", "token.json")

_stop = threading.Event()
gmail: GmailReader
trader: Trader


# ── Email processing ──────────────────────────────────────────────────────────
def process_emails():
    emails = gmail.get_new_voice_emails(max_age_seconds=EMAIL_MAX_AGE)
    logger.info("Gmail poll: %d new Voice email(s) found", len(emails))

    for email in emails:
        msg_id = email["id"]
        if is_email_processed(msg_id):
            logger.info("Skipping duplicate email %s", msg_id)
            continue

        provider, ticker = parse_email(email["body"])
        mark_email_processed(msg_id)

        if not ticker:
            logger.info("Email %s: no ticker pattern matched — body preview: %.80s",
                        msg_id, email["body"].replace("\n", " "))
            continue

        logger.info("Alert  provider=%s  ticker=%s", provider, ticker)
        send_tip_received(DISCORD_WEBHOOK, ticker, provider)

        if is_ticker_on_cooldown(ticker, TICKER_COOLDOWN):
            logger.info(
                "Skipping %s — duplicate alert within %d-minute cooldown window",
                ticker, TICKER_COOLDOWN // 60,
            )
            continue

        record_ticker_alert(ticker)

        data = get_stock_data(ticker, ALPACA_KEY, ALPACA_SECRET)
        if not data:
            send_error(DISCORD_WEBHOOK, f"No market data found for **{ticker}** ({provider}).")
            continue

        price = data["price"]

        order, err = trader.buy_stock(ticker, BUY_AMOUNT, price)
        if err:
            send_error(DISCORD_WEBHOOK, f"Buy order failed for **{ticker}**: {err}")
            continue

        filled = trader.wait_for_fill(str(order.id), timeout=60)
        if not filled:
            send_error(DISCORD_WEBHOOK, f"Buy order for **{ticker}** did not fill within 60 s.")
            continue

        fill_price = float(filled.filled_avg_price)
        fill_qty   = int(float(filled.filled_qty))
        total_cost = fill_price * fill_qty

        pos_id = save_position(
            symbol=ticker,
            provider=provider,
            shares=fill_qty,
            buy_price=fill_price,
            buy_time=datetime.now(timezone.utc),
            buy_order_id=str(filled.id),
        )

        ts_order = trader.submit_trailing_stop(ticker, fill_qty, TRAIL_PCT)
        if ts_order:
            update_trailing_stop_order(pos_id, str(ts_order.id))

        send_alert(
            webhook_url=DISCORD_WEBHOOK,
            symbol=ticker,
            provider=provider,
            price=fill_price,
            rsi=data["rsi"],
            volume=data["volume"],
            momentum=data["momentum"],
            shares_bought=fill_qty,
            total_cost=total_cost,
            paper=ALPACA_PAPER,
        )
        logger.info(
            "Bought %d × %s @ $%.4f  (cost $%.2f)  RSI=%s",
            fill_qty, ticker, fill_price, total_cost,
            f"{data['rsi']:.1f}" if data["rsi"] else "N/A",
        )


# ── Position tracking ─────────────────────────────────────────────────────────
def track_positions():
    positions = get_open_positions()

    for pos in positions:
        symbol = pos["symbol"]

        data = get_stock_data(symbol, ALPACA_KEY, ALPACA_SECRET)
        if data:
            save_price_bar(
                symbol=symbol,
                timestamp=datetime.now(timezone.utc),
                open_=data["open"],
                high=data["high"],
                low=data["low"],
                close=data["close"],
                volume=data["volume"],
            )

        # Hard stop loss
        if HARD_STOP_PCT > 0 and data:
            gain_pct = (data["price"] - pos["buy_price"]) / pos["buy_price"] * 100
            if gain_pct <= -HARD_STOP_PCT:
                ts_oid = pos["trailing_stop_order_id"]
                if ts_oid:
                    trader.cancel_order(ts_oid)
                sell = trader.market_sell(symbol, pos["shares"])
                if sell:
                    filled = trader.wait_for_fill(str(sell.id), timeout=30)
                    if filled:
                        fp  = float(filled.filled_avg_price)
                        pnl = (fp - pos["buy_price"]) * pos["shares"]
                        close_position(pos["id"], fp, datetime.now(timezone.utc), pnl)
                        logger.info("Hard stop: %s %.2f%% @ $%.4f  PnL=$%.2f",
                                    symbol, gain_pct, fp, pnl)
                        send_close(
                            webhook_url=DISCORD_WEBHOOK,
                            symbol=symbol,
                            buy_price=pos["buy_price"],
                            sell_price=fp,
                            shares=pos["shares"],
                            pnl=pnl,
                            paper=ALPACA_PAPER,
                            reason=f"Hard stop -{HARD_STOP_PCT:.0f}%",
                        )
                continue

        # Time exit: force-close after MAX_HOLD_MINUTES
        now_utc  = datetime.now(timezone.utc)
        buy_dt   = datetime.fromisoformat(pos["buy_time"])
        held_min = (now_utc - buy_dt).total_seconds() / 60
        if held_min >= MAX_HOLD_MINUTES:
            ts_oid = pos["trailing_stop_order_id"]
            if ts_oid:
                trader.cancel_order(ts_oid)
            sell = trader.market_sell(symbol, pos["shares"])
            if sell:
                filled = trader.wait_for_fill(str(sell.id), timeout=30)
                if filled:
                    fp  = float(filled.filled_avg_price)
                    pnl = (fp - pos["buy_price"]) * pos["shares"]
                    close_position(pos["id"], fp, datetime.now(timezone.utc), pnl)
                    logger.info("Time exit: %s held %.0fm @ $%.4f  PnL=$%.2f",
                                symbol, held_min, fp, pnl)
                    send_close(
                        webhook_url=DISCORD_WEBHOOK,
                        symbol=symbol,
                        buy_price=pos["buy_price"],
                        sell_price=fp,
                        shares=pos["shares"],
                        pnl=pnl,
                        paper=ALPACA_PAPER,
                        reason="Max hold time exit",
                    )
            continue

        # Dump time: force-close at DUMP_TIME_ET
        if DUMP_TIME_ET:
            now_et    = now_utc.astimezone(pytz.timezone("America/New_York"))
            dump_h, dump_m = map(int, DUMP_TIME_ET.split(":"))
            if (now_et.hour, now_et.minute) >= (dump_h, dump_m):
                ts_oid = pos["trailing_stop_order_id"]
                if ts_oid:
                    trader.cancel_order(ts_oid)
                sell = trader.market_sell(symbol, pos["shares"])
                if sell:
                    filled = trader.wait_for_fill(str(sell.id), timeout=30)
                    if filled:
                        fp  = float(filled.filled_avg_price)
                        pnl = (fp - pos["buy_price"]) * pos["shares"]
                        close_position(pos["id"], fp, datetime.now(timezone.utc), pnl)
                        logger.info("Dump time exit: %s @ $%.4f  PnL=$%.2f", symbol, fp, pnl)
                        send_close(
                            webhook_url=DISCORD_WEBHOOK,
                            symbol=symbol,
                            buy_price=pos["buy_price"],
                            sell_price=fp,
                            shares=pos["shares"],
                            pnl=pnl,
                            paper=ALPACA_PAPER,
                            reason=f"Dump time {DUMP_TIME_ET} ET",
                        )
                continue

        ts_order_id = pos["trailing_stop_order_id"]
        if not ts_order_id:
            continue

        status = trader.get_order_status(ts_order_id)
        if status != "filled":
            continue

        # Trailing stop triggered — close the position record
        sell_price = trader.get_filled_avg_price(ts_order_id) or (
            data["price"] if data else pos["buy_price"]
        )
        pnl = (sell_price - pos["buy_price"]) * pos["shares"]
        close_position(pos["id"], sell_price, datetime.now(timezone.utc), pnl)

        logger.info(
            "Closed %s  buy=%.4f  sell=%.4f  pnl=$%.2f",
            symbol, pos["buy_price"], sell_price, pnl,
        )
        send_close(
            webhook_url=DISCORD_WEBHOOK,
            symbol=symbol,
            buy_price=pos["buy_price"],
            sell_price=sell_price,
            shares=pos["shares"],
            pnl=pnl,
            paper=ALPACA_PAPER,
        )


# ── Background threads ────────────────────────────────────────────────────────
def _email_loop():
    while not _stop.is_set():
        try:
            process_emails()
        except Exception:
            logger.exception("Unhandled error in email loop")
        _stop.wait(POLL_INTERVAL)


def _track_loop():
    while not _stop.is_set():
        try:
            track_positions()
        except Exception:
            logger.exception("Unhandled error in position-track loop")
        _stop.wait(60)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    global gmail, trader

    logger.info("=" * 60)
    logger.info("StockTipsBot starting")
    logger.info("Mode: %s | Buy: $%.0f | Trailing stop: %.0f%%",
                "PAPER" if ALPACA_PAPER else "LIVE", BUY_AMOUNT, TRAIL_PCT)
    logger.info("=" * 60)

    init_db()
    gmail  = GmailReader(GMAIL_CREDS, GMAIL_TOKEN)
    trader = Trader(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)

    threading.Thread(target=_email_loop, name="email-poll", daemon=True).start()
    threading.Thread(target=_track_loop, name="pos-track",  daemon=True).start()

    logger.info("Running — Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down…")
        _stop.set()
        time.sleep(2)
        logger.info("Done.")


if __name__ == "__main__":
    main()
