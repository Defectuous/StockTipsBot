"""
store_daily_bars.py — Persist full-day 1-min OHLCV bars for every symbol the
SML/SML2 screeners traded or green-flagged on a given day into a standalone
market-data database (``market_data.db`` by default), kept separate from the
trading ``stockbot.db`` so the trading DB stays small and syncable.

"Green-flagged" = the symbol appears as a ``BUY`` or ``SKIP`` line in that
day's screener log (both mean it cleared the first screen; candidates that
never clear it are not named individually in the log). Symbols in the
``positions`` table for the date are unioned in as a backstop so an actual
fill is never missed even if the log is truncated.

Bars land in a ``price_bars`` table with the same schema ``bot/database.py``
uses, so ``tools/backtest_strategies.py`` can point its ``--cache`` at this
file. Timestamps are stored as UTC ISO-8601. Idempotent: ``INSERT OR IGNORE``
on ``UNIQUE(symbol, timestamp)`` means re-running a day only fills gaps.

Intended to run once per day after the post-market session closes (see
``deploy/store-daily-bars.*`` for the systemd timer), but equally fine to
run by hand for any past day whose log still exists under ``--dir``.

Usage:
    python tools/store_daily_bars.py                       # today, sml + sml2
    python tools/store_daily_bars.py --date 2026-08-31
    python tools/store_daily_bars.py --backfill-days 30    # last N days, traded symbols only
    python tools/store_daily_bars.py --session rth         # 09:30-16:00 (default: 04:00-20:00)
    python tools/store_daily_bars.py --db /data/market_data.db
"""
import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytz
from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from tools.pull_day_candidates import (
    PROVIDER_MAP,
    find_green_flag_symbols,
)

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
_ET = pytz.timezone("America/New_York")

SESSION_HOURS = {
    "rth": (time(9, 30), time(16, 0)),
    "extended": (time(4, 0), time(20, 0)),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_bars (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    open      REAL NOT NULL,
    high      REAL NOT NULL,
    low       REAL NOT NULL,
    close     REAL NOT NULL,
    volume    INTEGER NOT NULL,
    UNIQUE(symbol, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_price_bars_sym_ts ON price_bars(symbol, timestamp);

CREATE TABLE IF NOT EXISTS daily_capture (
    day          TEXT PRIMARY KEY,
    session      TEXT NOT NULL,
    symbols      INTEGER NOT NULL,
    rows_written INTEGER NOT NULL,
    captured_at  TEXT NOT NULL
);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    return conn


def traded_symbols_on(db_path: Path, providers: list[str], day: date) -> set[str]:
    """Symbols with a position opened or closed on *day* (belt-and-braces)."""
    if not db_path.exists():
        return set()
    names = [PROVIDER_MAP.get(p, p.upper() + "_SCREENER") for p in providers]
    ph = ",".join("?" * len(names))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            f"""SELECT symbol FROM positions
                WHERE provider IN ({ph})
                  AND (date(buy_time) = ? OR date(sell_time) = ?)""",
            (*names, day.isoformat(), day.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return {r[0] for r in rows}


def session_window_utc(day: date, session: str) -> tuple[datetime, datetime]:
    start_t, end_t = SESSION_HOURS[session]
    start_et = _ET.localize(datetime.combine(day, start_t))
    end_et = _ET.localize(datetime.combine(day, end_t))
    now_et = datetime.now(_ET)
    if end_et > now_et:
        end_et = now_et
    return start_et.astimezone(pytz.UTC), end_et.astimezone(pytz.UTC)


def fetch_bars(client: StockHistoricalDataClient, symbols: list[str],
               start: datetime, end: datetime) -> dict[str, list]:
    """One request per <=200-symbol chunk; the SDK paginates each internally."""
    out: dict[str, list] = {}
    for i in range(0, len(symbols), 200):
        chunk = symbols[i:i + 200]
        try:
            resp = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
            ))
        except Exception as e:
            print(f"  fetch failed for {chunk}: {e}")
            continue
        for sym in chunk:
            out[sym] = list(resp.data.get(sym, []))
    return out


def store_day(conn: sqlite3.Connection, client: StockHistoricalDataClient,
              day: date, symbols: set[str], session: str) -> tuple[int, int]:
    if not symbols:
        print(f"  {day}: no symbols — skipped")
        return 0, 0

    start, end = session_window_utc(day, session)
    if end <= start:
        print(f"  {day}: session has not started yet — skipped")
        return 0, 0

    bars_by_sym = fetch_bars(client, sorted(symbols), start, end)

    rows = []
    filled = 0
    for sym in sorted(symbols):
        bars = bars_by_sym.get(sym, [])
        if not bars:
            print(f"  {sym}: no bars")
            continue
        filled += 1
        for b in bars:
            ts = b.timestamp
            if ts.tzinfo is None:
                ts = pytz.UTC.localize(ts)
            rows.append((sym, ts.astimezone(pytz.UTC).isoformat(),
                         b.open, b.high, b.low, b.close, int(b.volume)))
        d_open, d_close = bars[0].open, bars[-1].close
        chg = round((d_close - d_open) / d_open * 100, 2) if d_open else None
        print(f"  {sym}: {len(bars)} bars  open={d_open}  close={d_close}  chg={chg}%")

    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO price_bars
           (symbol, timestamp, open, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    written = conn.total_changes - before
    conn.execute(
        """INSERT INTO daily_capture (day, session, symbols, rows_written, captured_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(day) DO UPDATE SET
             session=excluded.session,
             symbols=max(daily_capture.symbols, excluded.symbols),
             rows_written=daily_capture.rows_written + excluded.rows_written,
             captured_at=excluded.captured_at""",
        (day.isoformat(), session, filled, written,
         datetime.now(pytz.UTC).isoformat()),
    )
    conn.commit()
    print(f"  {day}: {filled}/{len(symbols)} symbols, {written} new bar rows")
    return filled, written


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=date.fromisoformat, default=date.today())
    ap.add_argument("--backfill-days", type=int, default=None,
                    help="Instead of one day: every day in the last N, traded symbols only")
    ap.add_argument("--providers", nargs="+", default=["sml", "sml2"])
    ap.add_argument("--dir", default="pi_data", help="Dir holding synced screener logs")
    ap.add_argument("--trading-db", default="stockbot.db",
                    help="Trading DB to read the positions backstop from")
    ap.add_argument("--db", default="market_data.db", help="Market-data DB to write bars into")
    ap.add_argument("--session", choices=list(SESSION_HOURS), default="extended")
    args = ap.parse_args()

    api_key = os.getenv("SML_ALPACA_API_KEY")
    api_secret = os.getenv("SML_ALPACA_API_SECRET")
    if not api_key or not api_secret:
        print("SML_ALPACA_API_KEY/SECRET not set — cannot pull market data.")
        sys.exit(1)
    client = StockHistoricalDataClient(api_key, api_secret)

    def _resolve(p: str) -> Path:
        return Path(p) if Path(p).is_absolute() else _ROOT / p

    data_dir = _resolve(args.dir)
    trading_db = _resolve(args.trading_db)
    market_db = _resolve(args.db)
    conn = open_db(market_db)

    print(f"Market-data DB: {market_db}  (session: {args.session})")

    total_syms = total_rows = 0
    if args.backfill_days is not None:
        for n in range(args.backfill_days, -1, -1):
            day = date.today() - timedelta(days=n)
            if day.weekday() >= 5:
                continue
            syms = traded_symbols_on(trading_db, args.providers, day)
            print(f"\n=== {day} ({len(syms)} traded symbol(s)) ===")
            s, r = store_day(conn, client, day, syms, args.session)
            total_syms += s
            total_rows += r
    else:
        day = args.date
        syms = find_green_flag_symbols(data_dir, args.providers, day)
        syms |= traded_symbols_on(trading_db, args.providers, day)
        print(f"\n=== {day} ({len(syms)} symbol(s): {', '.join(sorted(syms)) or '—'}) ===")
        s, r = store_day(conn, client, day, syms, args.session)
        total_syms += s
        total_rows += r

    conn.close()
    print(f"\nDone — {total_syms} symbol-days, {total_rows} new bar rows into {market_db}")


if __name__ == "__main__":
    main()
