"""
pull_day_candidates.py — Pull full-day 1-min bar data for every symbol that
cleared at least one screener gate (a "green flag") on a given day, or for
every symbol actually traded over the last N days.

Log mode (default, single day): a symbol counts as a green flag if it shows
up in a SML/SML2 screener log as either a BUY or a SKIP line — both mean it
passed the initial MACD+RSI screen, since candidates that never clear
MACD+RSI aren't named individually in the log. Only works for "today", since
pi_data/*.log gets overwritten daily — there's no rotated history.

Trade-history mode (--days N): screener logs don't persist past SKIP-level
candidates, so for historical days the only symbol data available is what
was actually bought — pulled from stockbot.db's positions table instead of
a log file.

Pulls 1-minute OHLCV bars for the full trading session (09:30-16:00 ET) via
Alpaca and writes one CSV per symbol per day, plus a summary CSV with day
open/high/low/close/volume/change%.

Usage:
    python tools/pull_day_candidates.py                  # today, sml + sml2, from logs
    python tools/pull_day_candidates.py --date 2026-07-22
    python tools/pull_day_candidates.py --providers sml sml2 mid super
    python tools/pull_day_candidates.py --dir pi_data
    python tools/pull_day_candidates.py --out reports/daily_data
    python tools/pull_day_candidates.py --days 30          # last 30 days, from trade history
    python tools/pull_day_candidates.py --days 30 --db stockbot.db
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytz
from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
_ET = pytz.timezone("America/New_York")

SKIP_RE = re.compile(r"^\d\d:\d\d:\d\d\s+INFO\s+SKIP\s+(\S+)")
BUY_RE  = re.compile(r"^\d\d:\d\d:\d\d\s+INFO\s+BUY\s+(\S+)")

PROVIDER_MAP = {
    "sml": "SML_SCREENER", "sml2": "SML2_SCREENER",
    "mid": "MID_SCREENER", "super": "SUPER_SCREENER", "live": "LIVE_SCREENER",
}


def find_traded_symbols_by_day(db_path: Path, providers: list[str], days: int) -> dict[date, set[str]]:
    """Group symbols actually bought over the last *days* days, per trading day."""
    provider_names = [PROVIDER_MAP.get(p, p.upper() + "_SCREENER") for p in providers]
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    placeholders = ",".join("?" * len(provider_names))
    rows = conn.execute(
        f"""SELECT symbol, date(buy_time) as buy_date FROM positions
            WHERE provider IN ({placeholders}) AND date(buy_time) >= ?""",
        (*provider_names, cutoff),
    ).fetchall()
    conn.close()

    by_day: dict[date, set[str]] = {}
    for symbol, buy_date in rows:
        d = date.fromisoformat(buy_date)
        by_day.setdefault(d, set()).add(symbol)
    return by_day


def find_green_flag_symbols(data_dir: Path, providers: list[str]) -> set[str]:
    symbols: set[str] = set()
    for p in providers:
        log_path = data_dir / f"{p}.log"
        if not log_path.exists():
            print(f"  (no log for {p} at {log_path})")
            continue
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = SKIP_RE.match(line) or BUY_RE.match(line)
            if m:
                symbols.add(m.group(1))
    return symbols


def session_bounds(report_date: date) -> tuple[datetime, datetime]:
    start_et = _ET.localize(datetime.combine(report_date, time(9, 30)))
    end_et   = _ET.localize(datetime.combine(report_date, time(16, 0)))
    now_et   = datetime.now(_ET)
    if end_et > now_et:
        end_et = now_et
    return start_et.astimezone(pytz.UTC), end_et.astimezone(pytz.UTC)


def fetch_and_save(client: StockHistoricalDataClient, symbol: str,
                    start: datetime, end: datetime, out_dir: Path) -> dict | None:
    try:
        resp = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
        ))
        bars = list(resp.data.get(symbol, []))
    except Exception as e:
        print(f"  {symbol}: FAILED ({e})")
        return None

    if not bars:
        print(f"  {symbol}: no bars returned")
        return None

    csv_path = out_dir / f"{symbol}.csv"
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("timestamp_et,open,high,low,close,volume\n")
        for b in bars:
            ts_et = b.timestamp.astimezone(_ET)
            f.write(f"{ts_et.isoformat()},{b.open},{b.high},{b.low},{b.close},{b.volume}\n")

    day_open  = bars[0].open
    day_close = bars[-1].close
    day_high  = max(b.high for b in bars)
    day_low   = min(b.low for b in bars)
    day_vol   = sum(b.volume for b in bars)
    change_pct = round((day_close - day_open) / day_open * 100, 2) if day_open else None

    print(f"  {symbol}: {len(bars)} bars  open={day_open}  close={day_close}  "
          f"chg={change_pct}%  vol={day_vol}")

    return dict(symbol=symbol, bars=len(bars), open=day_open, high=day_high,
                low=day_low, close=day_close, change_pct=change_pct, volume=day_vol)


def write_summary(out_dir: Path, summary: list[dict]):
    summary_path = out_dir / "_summary.csv"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("symbol,bars,open,high,low,close,change_pct,volume\n")
        for r in summary:
            f.write(f"{r['symbol']},{r['bars']},{r['open']},{r['high']},{r['low']},"
                    f"{r['close']},{r['change_pct']},{r['volume']}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--days", type=int, default=None,
                         help="Trade-history mode: pull every symbol actually bought in the last N days")
    parser.add_argument("--providers", nargs="+", default=["sml", "sml2"])
    parser.add_argument("--dir", default="pi_data", help="Log-mode only: dir holding synced logs")
    parser.add_argument("--db", default="stockbot.db", help="Trade-history mode: path to stockbot.db")
    parser.add_argument("--out", default=None,
                         help="Output base dir (default: reports/daily_data)")
    args = parser.parse_args()

    api_key    = os.getenv("SML_ALPACA_API_KEY")
    api_secret = os.getenv("SML_ALPACA_API_SECRET")
    if not api_key or not api_secret:
        print("SML_ALPACA_API_KEY/SECRET not set — cannot pull market data.")
        sys.exit(1)
    client = StockHistoricalDataClient(api_key, api_secret)

    out_base = Path(args.out) if args.out else _ROOT / "reports" / "daily_data"

    if args.days is not None:
        db_path = (_ROOT / args.db) if not Path(args.db).is_absolute() else Path(args.db)
        print(f"Finding symbols actually traded in the last {args.days} days from {db_path} "
              f"({', '.join(args.providers)})...")
        by_day = find_traded_symbols_by_day(db_path, args.providers, args.days)
        if not by_day:
            print("No trades found in that window — nothing to pull.")
            return
        total_pairs = sum(len(v) for v in by_day.values())
        print(f"{len(by_day)} trading day(s), {total_pairs} (day, symbol) pair(s)\n")

        grand_total = 0
        for d in sorted(by_day):
            symbols = by_day[d]
            out_dir = out_base / d.isoformat()
            out_dir.mkdir(parents=True, exist_ok=True)
            start, end = session_bounds(d)
            print(f"=== {d} — {len(symbols)} symbol(s): {', '.join(sorted(symbols))} ===")
            summary = []
            for sym in sorted(symbols):
                row = fetch_and_save(client, sym, start, end, out_dir)
                if row:
                    summary.append(row)
            write_summary(out_dir, summary)
            grand_total += len(summary)
            print()

        print(f"Saved {grand_total}/{total_pairs} symbol-day CSVs under {out_base}")
        return

    data_dir = (_ROOT / args.dir) if not Path(args.dir).is_absolute() else Path(args.dir)
    out_dir = out_base / args.date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Finding green-flag symbols for {args.date} from {data_dir} ({', '.join(args.providers)})...")
    symbols = find_green_flag_symbols(data_dir, args.providers)
    if not symbols:
        print("No candidate symbols found — nothing to pull.")
        return
    print(f"{len(symbols)} symbol(s): {', '.join(sorted(symbols))}\n")

    start, end = session_bounds(args.date)
    print(f"Pulling 1-min bars {start.astimezone(_ET).strftime('%H:%M')}-"
          f"{end.astimezone(_ET).strftime('%H:%M')} ET into {out_dir}\n")

    summary = []
    for sym in sorted(symbols):
        row = fetch_and_save(client, sym, start, end, out_dir)
        if row:
            summary.append(row)

    write_summary(out_dir, summary)
    print(f"\nSaved {len(summary)}/{len(symbols)} symbol CSVs + summary to {out_dir}")


if __name__ == "__main__":
    main()
