"""
Backfill rsi_at_entry, atr_at_entry, change_pct_at_entry, macd_crossover_fresh,
and rvol_at_entry for positions that have NULL values.

Uses Alpaca historical bar data to reconstruct market state at buy time.
API keys are resolved from the first available screener account in .env.

Usage:
    python backfill_entry_stats.py                    # all providers
    python backfill_entry_stats.py --provider SML     # one provider only
    python backfill_entry_stats.py --provider MID_SCREENER
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytz
from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.market_data import _atr, _macd_analysis, _rsi_series

load_dotenv()

def _resolve_keys() -> tuple[str, str]:
    """Return the first available Alpaca key pair across all screener accounts."""
    for prefix in ("SML", "MID", "SUPER", "LIVE"):
        key    = os.getenv(f"{prefix}_ALPACA_API_KEY")
        secret = os.getenv(f"{prefix}_ALPACA_API_SECRET")
        if key and secret:
            return key, secret
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_API_SECRET")
    if key and secret:
        return key, secret
    raise EnvironmentError("No Alpaca API keys found in .env — set at least one of SML_ALPACA_API_KEY etc.")

ALPACA_KEY, ALPACA_SECRET = _resolve_keys()
DB_PATH = Path(__file__).resolve().parent.parent / "stockbot.db"

_1MIN  = TimeFrame(1,  TimeFrameUnit.Minute)
_5MIN  = TimeFrame(5,  TimeFrameUnit.Minute)
_15MIN = TimeFrame(15, TimeFrameUnit.Minute)
_DAY   = TimeFrame.Day

ET = pytz.timezone("America/New_York")


def _compute_entry_stats(
    symbol: str,
    buy_price: float,
    buy_time: datetime,
    client: StockHistoricalDataClient,
) -> dict:
    end = buy_time

    # 5-min bars (last 2 hours) → RSI + ATR
    try:
        resp  = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_5MIN,
            start=buy_time - timedelta(minutes=120),
            end=end,
        ))
        bars5 = list(resp.data.get(symbol, []))
    except Exception as e:
        print(f"    5-min fetch failed: {e}")
        bars5 = []

    # 15-min bars (last 3 days) → MACD crossover
    try:
        resp   = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_15MIN,
            start=buy_time - timedelta(days=3),
            end=end,
        ))
        bars15 = list(resp.data.get(symbol, []))
    except Exception as e:
        print(f"    15-min fetch failed: {e}")
        bars15 = []

    # Daily bars (last 7 days) → previous close + previous volume
    try:
        resp   = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_DAY,
            start=buy_time - timedelta(days=7),
            end=end,
        ))
        daily  = list(resp.data.get(symbol, []))
    except Exception as e:
        print(f"    daily fetch failed: {e}")
        daily = []

    # RSI from 5-min closes
    rsi = None
    if len(bars5) >= 20:
        closes   = [b.close for b in bars5]
        rsi_vals = [r for r in _rsi_series(closes) if r is not None]
        if rsi_vals:
            rsi = rsi_vals[-1]

    # ATR from 5-min bars
    atr = _atr(bars5) if len(bars5) > 14 else None

    # MACD crossover from 15-min closes
    macd_crossover_fresh = None
    if bars15:
        macd = _macd_analysis([b.close for b in bars15])
        if macd:
            macd_crossover_fresh = int(macd["crossover"])

    # Previous close + volume (last bar strictly before buy date)
    buy_date   = buy_time.astimezone(ET).date()
    prev_close = None
    prev_vol   = None
    for bar in reversed(daily):
        if bar.timestamp.astimezone(ET).date() < buy_date:
            prev_close = bar.close
            prev_vol   = bar.volume
            break

    change_pct_at_entry = None
    if prev_close:
        change_pct_at_entry = round((buy_price - prev_close) / prev_close * 100, 2)

    # RVOL: cumulative intraday volume up to buy_time / previous day total volume
    rvol_at_entry = None
    if prev_vol:
        try:
            market_open = ET.localize(datetime(buy_date.year, buy_date.month, buy_date.day, 4, 0))
            resp        = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=_1MIN,
                start=market_open,
                end=buy_time,
            ))
            intraday = list(resp.data.get(symbol, []))
            if intraday:
                rvol_at_entry = round(sum(b.volume for b in intraday) / prev_vol, 3)
        except Exception as e:
            print(f"    RVOL fetch failed: {e}")

    return {
        "rsi_at_entry":         rsi,
        "atr_at_entry":         atr,
        "change_pct_at_entry":  change_pct_at_entry,
        "macd_crossover_fresh": macd_crossover_fresh,
        "rvol_at_entry":        rvol_at_entry,
    }


def main():
    parser = argparse.ArgumentParser(description="Backfill entry stats for positions.")
    parser.add_argument(
        "--provider", "-p",
        help="Filter by provider name, e.g. SML, MID, SML_SCREENER (default: all)",
        default=None,
    )
    args = parser.parse_args()

    client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
    conn   = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    if args.provider:
        # Accept both short form (SML) and full form (SML_SCREENER)
        provider_filter = args.provider if "_SCREENER" in args.provider else f"{args.provider}_SCREENER"
        rows = conn.execute(
            """SELECT id, symbol, buy_price, buy_time
               FROM positions
               WHERE rsi_at_entry IS NULL
                 AND provider = ?
               ORDER BY buy_time""",
            (provider_filter,),
        ).fetchall()
        print(f"Provider filter: {provider_filter}")
    else:
        rows = conn.execute(
            """SELECT id, symbol, buy_price, buy_time
               FROM positions
               WHERE rsi_at_entry IS NULL
               ORDER BY buy_time"""
        ).fetchall()

    if not rows:
        print("Nothing to backfill — all positions already have entry stats.")
        conn.close()
        return

    print(f"Backfilling {len(rows)} positions...\n")

    for row in rows:
        pos_id    = row["id"]
        symbol    = row["symbol"]
        buy_price = row["buy_price"]
        buy_time  = datetime.fromisoformat(row["buy_time"])
        if buy_time.tzinfo is None:
            buy_time = buy_time.replace(tzinfo=pytz.UTC)

        print(f"  [{pos_id:>3}] {symbol:<6}  buy=${buy_price:.4f}  {buy_time.astimezone(ET).strftime('%H:%M ET')}")
        stats = _compute_entry_stats(symbol, buy_price, buy_time, client)
        print(f"         RSI={stats['rsi_at_entry']}  ATR={stats['atr_at_entry']}  "
              f"chg={stats['change_pct_at_entry']}%  cross={stats['macd_crossover_fresh']}  "
              f"rvol={stats['rvol_at_entry']}")

        conn.execute(
            """UPDATE positions SET
               rsi_at_entry         = ?,
               atr_at_entry         = ?,
               change_pct_at_entry  = ?,
               macd_crossover_fresh = ?,
               rvol_at_entry        = ?
               WHERE id = ?""",
            (stats["rsi_at_entry"], stats["atr_at_entry"],
             stats["change_pct_at_entry"], stats["macd_crossover_fresh"],
             stats["rvol_at_entry"], pos_id),
        )
        conn.commit()

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
