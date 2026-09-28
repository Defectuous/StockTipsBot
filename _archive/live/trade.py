"""
Manual trading CLI

Usage:
  python trade.py buy TICKER              # buy $100 of TICKER
  python trade.py buy TICKER --amount 250 # buy $250 of TICKER
  python trade.py stop TICKER             # attach trailing stop to open position
  python trade.py positions               # list open positions from the database
"""
import os
import sys
from dotenv import load_dotenv
load_dotenv()

from bot.market_data import get_stock_data
from bot.trader import Trader
from bot.database import init_db, save_position, update_trailing_stop_order, get_open_positions
from datetime import datetime, timezone

ALPACA_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_API_SECRET"]
ALPACA_PAPER  = os.getenv("ALPACA_PAPER", "true").lower() == "true"
TRAIL_PCT     = float(os.getenv("TRAILING_STOP_PERCENT", "25"))

trader = Trader(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
init_db()

MODE = f"{'PAPER' if ALPACA_PAPER else 'LIVE'}"


def cmd_buy(ticker: str, amount: float):
    print(f"[{MODE}] BUY {ticker}  budget=${amount:.0f}\n")

    if not trader.is_market_open():
        print("WARNING: Market is closed — paper orders may still fill.\n")

    print(f"Fetching price for {ticker}...")
    data = get_stock_data(ticker, ALPACA_KEY, ALPACA_SECRET)
    if not data:
        print("ERROR: No market data returned.")
        return

    price  = data["price"]
    shares = int(amount / price)
    print(f"Price : ${price:.4f}")
    print(f"Shares: {shares}  (${shares * price:.2f})\n")

    if shares < 1:
        print(f"ERROR: Price ${price:.4f} too high for ${amount:.0f} budget.")
        return

    order, err = trader.buy_stock(ticker, amount, price)
    if err:
        print(f"ERROR: {err}")
        return

    print(f"Order submitted  id={order.id}")
    print("Waiting for fill...")
    filled = trader.wait_for_fill(str(order.id), timeout=60)
    if not filled:
        print("ERROR: Order did not fill within 60s.")
        return

    fill_price = float(filled.filled_avg_price)
    fill_qty   = int(float(filled.filled_qty))
    print(f"FILLED  {fill_qty} × {ticker} @ ${fill_price:.4f}  (${fill_price * fill_qty:.2f})\n")

    pos_id = save_position(
        symbol=ticker,
        provider="MANUAL",
        shares=fill_qty,
        buy_price=fill_price,
        buy_time=datetime.now(timezone.utc),
        buy_order_id=str(filled.id),
    )

    print(f"Attaching trailing stop at {TRAIL_PCT:.0f}%...")
    ts = trader.submit_trailing_stop(ticker, fill_qty, TRAIL_PCT)
    if ts:
        update_trailing_stop_order(pos_id, str(ts.id))
        print(f"Trailing stop set  id={ts.id}")
    else:
        print("WARNING: Trailing stop failed — set it manually on Alpaca.")

    print(f"\nDone. Position saved (db id={pos_id})")


def cmd_stop(ticker: str):
    print(f"[{MODE}] TRAILING STOP  {ticker}  trail={TRAIL_PCT:.0f}%\n")

    # Find open position in DB
    positions = [p for p in get_open_positions() if p["symbol"] == ticker.upper()]

    if not positions:
        print(f"No open position for {ticker} in the database.")
        print("If you bought manually on Alpaca, enter the share count:")
        try:
            qty = int(input("  Shares: ").strip())
        except (ValueError, KeyboardInterrupt):
            print("Cancelled.")
            return
    else:
        qty = positions[0]["shares"]
        print(f"Found position: {qty} shares @ ${positions[0]['buy_price']:.4f}")

    print(f"Submitting trailing stop: {qty} × {ticker} @ {TRAIL_PCT:.0f}%...")
    ts = trader.submit_trailing_stop(ticker, qty, TRAIL_PCT)
    if ts:
        if positions:
            update_trailing_stop_order(positions[0]["id"], str(ts.id))
        print(f"Trailing stop set  id={ts.id}")
    else:
        print("ERROR: Failed to set trailing stop — check logs.")


def cmd_positions():
    rows = get_open_positions()
    if not rows:
        print("No open positions in the database.")
        return

    print(f"{'Symbol':<8} {'Provider':<10} {'Shares':<8} {'Buy Price':<12} {'Buy Time'}")
    print("-" * 60)
    for p in rows:
        print(f"{p['symbol']:<8} {p['provider']:<10} {p['shares']:<8} ${p['buy_price']:<11.4f} {p['buy_time']}")


# ── Argument parsing ──────────────────────────────────────────────────────────
args = sys.argv[1:]

if not args:
    print(__doc__)
    sys.exit(0)

command = args[0].lower()

if command == "buy":
    if len(args) < 2:
        print("Usage: python trade.py buy TICKER [--amount 250]")
        sys.exit(1)
    ticker = args[1].upper()
    amount = float(os.getenv("BUY_AMOUNT_USD", "100"))
    if "--amount" in args:
        amount = float(args[args.index("--amount") + 1])
    cmd_buy(ticker, amount)

elif command == "stop":
    if len(args) < 2:
        print("Usage: python trade.py stop TICKER")
        sys.exit(1)
    cmd_stop(args[1].upper())

elif command == "positions":
    cmd_positions()

else:
    print(f"Unknown command: {command}")
    print(__doc__)
    sys.exit(1)
