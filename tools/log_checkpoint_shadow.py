"""
log_checkpoint_shadow.py — Track what RUNNER's 30-min/60-min checkpoint exits
would have done if held longer, to build up evidence for whether
MIN_GAIN_AT_30M/MIN_GAIN_AT_60M are cutting winners short or saving losses.

For each RUNNER position closed via a "30-min checkpoint exit" or "60-min
checkpoint exit" (the reason string is only in runner.log, not the DB — the
DB gives entry/exit price/time), pulls the full trading-day 1-min bars for
that symbol and records:
  - gain% at the RUNNER_MAX_HOLD_MINUTES mark (would it have been green by
    the time the max-hold clock would've forced an exit anyway?)
  - peak/trough price and time for the rest of the session
  - session-close price and gain%

Appends one row per trade to reports/checkpoint_shadow_log.csv (created if
missing). Idempotent — skips (date, symbol, buy_time) rows already logged,
so it's safe to re-run.

Usage:
    python tools/log_checkpoint_shadow.py                  # today
    python tools/log_checkpoint_shadow.py --date 2026-08-04
    python tools/log_checkpoint_shadow.py --log-path runner.log
"""
import argparse
import csv
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
from alpaca.data.timeframe import TimeFrame

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
_ET = pytz.timezone("America/New_York")
_UTC = pytz.UTC

MAX_HOLD_MINUTES = int(os.getenv("RUNNER_MAX_HOLD_MINUTES", "25"))

SOLD_RE = re.compile(
    r"^(\d\d:\d\d:\d\d)\s+INFO\s+SOLD\s+(\S+)\s+@\s+\$([\d.]+)\s+PnL=\$([-+\d.]+)\s+\((.*)\)"
)
CHECKPOINT_REASONS = {"30-min checkpoint exit", "60-min checkpoint exit"}

OUT_PATH = _ROOT / "reports" / "checkpoint_shadow_log.csv"
FIELDS = [
    "date", "symbol", "checkpoint", "entry_price", "entry_time",
    "exit_price", "exit_time", "realized_gain_pct", "realized_pnl",
    "gain_at_max_hold_pct", "max_hold_mark_time",
    "peak_gain_pct", "peak_time", "trough_gain_pct", "trough_time",
    "close_gain_pct", "close_time",
]


def find_exit_reasons(log_path: Path, d: date) -> dict[tuple[str, str], str]:
    """Map (symbol, HH:MM:SS sell time) -> exit reason, scoped to lines whose
    timestamp falls within the session that started most recently before/on
    the target date (runner.log accumulates multiple days, never rotates)."""
    if not log_path.exists():
        return {}
    reasons: dict[tuple[str, str], str] = {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SOLD_RE.match(line)
        if not m:
            continue
        ts_str, symbol, _price, _pnl, reason = m.groups()
        if reason in CHECKPOINT_REASONS:
            reasons[(symbol, ts_str)] = reason
    return reasons


def find_runner_trades(db_path: Path, d: date) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT symbol, buy_price, buy_time, sell_price, sell_time, pnl
           FROM positions
           WHERE provider = 'RUNNER_SCREENER' AND status = 'closed'
             AND date(sell_time) = ?""",
        (d.isoformat(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_day_bars(client: StockHistoricalDataClient, symbol: str, d: date) -> list:
    start = _ET.localize(datetime.combine(d, time(9, 30))).astimezone(_UTC)
    end   = _ET.localize(datetime.combine(d, time(16, 0))).astimezone(_UTC)
    try:
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=start, end=end,
        )).data.get(symbol, [])
    except Exception as e:
        print(f"  {symbol}: bar fetch failed ({e})")
        return []
    return [(b.timestamp.astimezone(_ET), float(b.open), float(b.high),
              float(b.low), float(b.close)) for b in bars]


def analyze(entry_price: float, entry_dt_et: datetime, bars: list) -> dict | None:
    entry_t = entry_dt_et.time()
    after = [b for b in bars if b[0].time() >= entry_t]
    if not after:
        return None

    max_hold_cutoff = entry_dt_et + timedelta(minutes=MAX_HOLD_MINUTES)
    price_at_max_hold = None
    peak_price, peak_t = entry_price, entry_t
    trough_price, trough_t = entry_price, entry_t

    for ts, o, h, l, c in after:
        if h > peak_price:
            peak_price, peak_t = h, ts.time()
        if l < trough_price:
            trough_price, trough_t = l, ts.time()
        if price_at_max_hold is None and ts >= max_hold_cutoff:
            price_at_max_hold = c

    close_price = after[-1][4]
    close_t = after[-1][0].time()

    def pct(p):
        return round((p - entry_price) / entry_price * 100, 2)

    return dict(
        gain_at_max_hold_pct=pct(price_at_max_hold) if price_at_max_hold is not None else None,
        max_hold_mark_time=max_hold_cutoff.time().strftime("%H:%M") if price_at_max_hold is not None else None,
        peak_gain_pct=pct(peak_price), peak_time=peak_t.strftime("%H:%M"),
        trough_gain_pct=pct(trough_price), trough_time=trough_t.strftime("%H:%M"),
        close_gain_pct=pct(close_price), close_time=close_t.strftime("%H:%M"),
    )


def load_existing_keys(path: Path) -> set[tuple[str, str, str]]:
    if not path.exists():
        return set()
    keys = set()
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            keys.add((row["date"], row["symbol"], row["entry_time"]))
    return keys


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--db", default="stockbot.db")
    parser.add_argument("--log-path", default="runner.log")
    args = parser.parse_args()

    db_path = _ROOT / args.db
    log_path = _ROOT / args.log_path

    trades = find_runner_trades(db_path, args.date)
    if not trades:
        print(f"No closed RUNNER trades found for {args.date}.")
        return

    reasons = find_exit_reasons(log_path, args.date)

    api_key = os.getenv("RUNNER_ALPACA_API_KEY")
    api_secret = os.getenv("RUNNER_ALPACA_API_SECRET")
    client = StockHistoricalDataClient(api_key, api_secret)

    existing = load_existing_keys(OUT_PATH)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not OUT_PATH.exists()

    rows_written = 0
    with open(OUT_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()

        for t in trades:
            symbol = t["symbol"]
            entry_time_str = t["buy_time"]
            sell_dt_utc = datetime.fromisoformat(t["sell_time"])
            sell_t_et = sell_dt_utc.astimezone(_ET).strftime("%H:%M:%S")
            reason = reasons.get((symbol, sell_t_et))
            if reason is None:
                continue  # not a checkpoint exit (hard stop / max hold / dump time) — nothing to learn here

            key = (args.date.isoformat(), symbol, entry_time_str)
            if key in existing:
                print(f"  {symbol}: already logged for {args.date}, skipping")
                continue

            entry_dt_et = datetime.fromisoformat(entry_time_str).astimezone(_ET)
            bars = fetch_day_bars(client, symbol, args.date)
            if not bars:
                continue
            result = analyze(t["buy_price"], entry_dt_et, bars)
            if not result:
                continue

            realized_gain_pct = round((t["sell_price"] - t["buy_price"]) / t["buy_price"] * 100, 2)
            writer.writerow(dict(
                date=args.date.isoformat(), symbol=symbol,
                checkpoint="30m" if "30-min" in reason else "60m",
                entry_price=t["buy_price"], entry_time=entry_time_str,
                exit_price=t["sell_price"], exit_time=t["sell_time"],
                realized_gain_pct=realized_gain_pct, realized_pnl=round(t["pnl"], 2),
                **result,
            ))
            rows_written += 1
            print(f"  {symbol}: logged ({reason}, realized {realized_gain_pct:+.1f}%, "
                  f"at max-hold {result['gain_at_max_hold_pct']}%, "
                  f"peak {result['peak_gain_pct']}%, close {result['close_gain_pct']}%)")

    print(f"\n{rows_written} row(s) appended to {OUT_PATH}")


if __name__ == "__main__":
    main()
