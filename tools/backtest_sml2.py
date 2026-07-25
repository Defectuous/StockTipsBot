"""
backtest_sml2.py — Replay SML2's *current* entry/exit config against the last
N days of price data for symbols traded in that window, per symbol per day.

Scope (see conversation for why): single-symbol standalone backtest — each
(day, symbol) pair is tested independently, as if it had its own dedicated
bot slot. It does NOT simulate the shared MAX_POSITIONS=2 cap across the
day's whole candidate universe (that universe isn't reconstructable — it's
a live "most active" snapshot that was never persisted), and it reports %
returns, not $ PnL (which would need day-by-day account-equity simulation).

Entry logic replicates the two layers scan_and_trade() actually applies:
  Layer 1 (bot/screener.py _analyze().passes) — RSI 50-65 & rising over the
    last 2 bars, MACD above signal with expanding histogram, price > VWAP.
  Layer 2 (SML2's own gates on top) — cooldown (N/A, single symbol/day),
    change_pct in [MIN_CHANGE_PCT, MAX_ENTRY_MOVE_PCT], RSI in
    [RSI_ENTRY_MIN, RSI_ENTRY_MAX], fresh MACD crossover, RVOL >= MIN_RVOL.
  Net effect: layer 1's hard 65-cap means the *effective* RSI band is
  60-65, not 60-70 — RSI_ENTRY_MAX=70 never actually binds.

Exit logic replicates monitor_positions(): hard stop / trailing stop treated
as broker-resting orders (checked against each bar's low, whichever trigger
price is higher wins), then per-bar checks in priority order — max hold,
60m/30m checkpoints, dump time 12:00 ET, RSI-75-falling exit, profit-lock
tightening (10% trail -> 5% trail at +15% gain).

Usage:
    python tools/backtest_sml2.py --days 30
    python tools/backtest_sml2.py --days 30 --db stockbot.db
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
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.market_data import _rsi_series, _rvol_time_adjusted
from bot.screener import _analyze

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
_ET = pytz.timezone("America/New_York")
_5MIN = TimeFrame(5, TimeFrameUnit.Minute)
_15MIN = TimeFrame(15, TimeFrameUnit.Minute)

# ── Current SML2 config (mirrors .env + run_sml2_screener.py defaults) ────────
START_TIME_ET    = "09:30"
STOP_BUY_TIME_ET = "11:45"
DUMP_TIME_ET     = "12:00"
MIN_RVOL         = float(os.getenv("MIN_RVOL", "1.5"))
MAX_RVOL         = float(os.getenv("MAX_RVOL", "0"))
MIN_CHANGE_PCT   = float(os.getenv("MIN_CHANGE_PCT", "2.0"))
MAX_ENTRY_MOVE_PCT = float(os.getenv("SML2_MAX_ENTRY_MOVE_PCT") or os.getenv("MAX_ENTRY_MOVE_PCT", "0"))
RSI_ENTRY_MIN    = float(os.getenv("RSI_ENTRY_MIN", "60"))
RSI_ENTRY_MAX    = float(os.getenv("RSI_ENTRY_MAX", "70"))
REQUIRE_MACD_FRESH_CROSSOVER = os.getenv("REQUIRE_MACD_FRESH_CROSSOVER", "true").lower() == "true"

TRAIL_PCT        = float(os.getenv("TRAILING_STOP_PERCENT", "10"))
HARD_STOP_PCT    = float(os.getenv("HARD_STOP_PCT", "5"))
PROFIT_LOCK_PCT  = float(os.getenv("PROFIT_LOCK_PCT", "15"))
TIGHT_STOP_PCT   = float(os.getenv("TIGHT_STOP_PCT", "5"))
RSI_EXIT_LEVEL   = float(os.getenv("RSI_EXIT_LEVEL", "75"))
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES", "90"))
MIN_GAIN_AT_30M  = float(os.getenv("MIN_GAIN_AT_30M", "-2.0"))
MIN_GAIN_AT_60M  = float(os.getenv("MIN_GAIN_AT_60M", "0.0"))

PROVIDER_MAP = {"sml": "SML_SCREENER", "sml2": "SML2_SCREENER"}


def find_traded_symbols_by_day(db_path: Path, providers: list[str], days: int) -> dict[date, set[str]]:
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


def _parse_hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def fetch_symbol_data(client: StockHistoricalDataClient, symbol: str, d: date) -> dict | None:
    """Pull all bar data needed to simulate one symbol's day: 1-min (price path),
    5-min (RSI/VWAP/ATR), 15-min (MACD/RVOL, needs 3 prior calendar days), daily
    (previous close for change_pct)."""
    day_start_et = _ET.localize(datetime.combine(d, time(9, 30)))
    day_end_et   = _ET.localize(datetime.combine(d, time(16, 0)))
    premkt_start_et = _ET.localize(datetime.combine(d, time(4, 0)))

    try:
        bars1 = list(client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=day_start_et.astimezone(pytz.UTC), end=day_end_et.astimezone(pytz.UTC),
        )).data.get(symbol, []))
        bars5 = list(client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=_5MIN,
            start=premkt_start_et.astimezone(pytz.UTC), end=day_end_et.astimezone(pytz.UTC),
        )).data.get(symbol, []))
        bars15 = list(client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=_15MIN,
            start=(day_start_et - timedelta(days=4)).astimezone(pytz.UTC), end=day_end_et.astimezone(pytz.UTC),
        )).data.get(symbol, []))
        daily = list(client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=(day_start_et - timedelta(days=10)).astimezone(pytz.UTC), end=day_end_et.astimezone(pytz.UTC),
        )).data.get(symbol, []))
    except Exception as e:
        print(f"  {symbol} {d}: fetch failed ({e})")
        return None

    if not bars1 or not bars5 or not bars15:
        return None

    prev_close = None
    for b in daily:
        b_date = b.timestamp.astimezone(_ET).date()
        if b_date < d:
            prev_close = b.close
    if prev_close is None:
        return None

    return dict(bars1=bars1, bars5=bars5, bars15=bars15, prev_close=prev_close)


def simulate_entry(symbol: str, data: dict, d: date) -> dict | None:
    """Walk 1-min bars from 09:30 to STOP_BUY_TIME_ET, re-running the exact
    scan_and_trade() gate sequence at each minute, on close prices. Returns
    the first qualifying entry, or None."""
    bars1, bars5, bars15 = data["bars1"], data["bars5"], data["bars15"]
    prev_close = data["prev_close"]
    stop_buy = _ET.localize(datetime.combine(d, _parse_hm(STOP_BUY_TIME_ET)))
    market_open = _ET.localize(datetime.combine(d, time(9, 30)))

    for bar in bars1:
        ts_et = bar.timestamp.astimezone(_ET)
        if ts_et >= stop_buy:
            break

        price = bar.close
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0

        # Rolling 120-min window for RSI/ATR (mirrors the live "now-120min" fetch)
        window_start = ts_et - timedelta(minutes=120)
        b5_window = [b for b in bars5 if window_start <= b.timestamp.astimezone(_ET) <= ts_et]
        # Market-open-anchored window for VWAP
        b5_vwap = [b for b in bars5 if market_open <= b.timestamp.astimezone(_ET) <= ts_et]
        # 3-calendar-day trailing window for MACD/RVOL
        b15_window = [b for b in bars15
                      if (ts_et - timedelta(days=3)) <= b.timestamp.astimezone(_ET) <= ts_et]

        stock = _analyze(symbol, b5_window, b15_window, price, change_pct, vwap_bars=b5_vwap)
        if not stock or not stock.passes:
            continue

        # ── Layer 2: SML2's own gates ──────────────────────────────────────
        if MAX_ENTRY_MOVE_PCT > 0 and stock.change_pct > MAX_ENTRY_MOVE_PCT:
            continue
        if MIN_CHANGE_PCT > 0 and (stock.change_pct is None or stock.change_pct < MIN_CHANGE_PCT):
            continue
        if not (RSI_ENTRY_MIN <= stock.rsi <= RSI_ENTRY_MAX):
            continue
        if REQUIRE_MACD_FRESH_CROSSOVER and not stock.macd_crossover:
            continue
        rvol = _rvol_time_adjusted(b15_window, ts_et)
        if MIN_RVOL > 0 and (rvol is None or rvol < MIN_RVOL):
            continue
        if MAX_RVOL > 0 and rvol and rvol > MAX_RVOL:
            continue

        return dict(entry_time=ts_et, entry_price=price, rsi=stock.rsi,
                     change_pct=stock.change_pct, rvol=rvol)

    return None


def simulate_exit(bars1: list, entry_time: datetime, entry_price: float) -> dict:
    high_water = entry_price
    trail_pct = TRAIL_PCT
    tightened = False
    hard_stop_price = entry_price * (1 - HARD_STOP_PCT / 100)
    dump_t = _parse_hm(DUMP_TIME_ET)

    post_entry = [b for b in bars1 if b.timestamp.astimezone(_ET) > entry_time]

    for bar in post_entry:
        ts_et = bar.timestamp.astimezone(_ET)
        held_min = (ts_et - entry_time).total_seconds() / 60

        # ── Resting stop orders: trailing stop trails high_water BEFORE this
        #    bar; hard stop is fixed. Whichever trigger price is higher gets
        #    hit first as price falls. Check against the bar's low. ────────
        trail_stop_price = high_water * (1 - trail_pct / 100)
        stop_price = max(hard_stop_price, trail_stop_price)
        if bar.low <= stop_price:
            reason = "Hard stop" if hard_stop_price >= trail_stop_price else "Trailing stop"
            gain = (stop_price - entry_price) / entry_price * 100
            return dict(exit_time=ts_et, exit_price=stop_price, reason=reason,
                        gain_pct=round(gain, 2), held_min=round(held_min))

        high_water = max(high_water, bar.high)
        price = bar.close
        gain_pct = (price - entry_price) / entry_price * 100

        if held_min >= MAX_HOLD_MINUTES:
            return dict(exit_time=ts_et, exit_price=price, reason="Max hold time exit",
                        gain_pct=round(gain_pct, 2), held_min=round(held_min))
        elif held_min >= 60 and gain_pct < MIN_GAIN_AT_60M:
            return dict(exit_time=ts_et, exit_price=price, reason="60-min checkpoint exit",
                        gain_pct=round(gain_pct, 2), held_min=round(held_min))
        elif held_min >= 30 and gain_pct < MIN_GAIN_AT_30M:
            return dict(exit_time=ts_et, exit_price=price, reason="30-min checkpoint exit",
                        gain_pct=round(gain_pct, 2), held_min=round(held_min))

        if (ts_et.hour, ts_et.minute) >= (dump_t.hour, dump_t.minute):
            return dict(exit_time=ts_et, exit_price=price, reason="Dump time exit",
                        gain_pct=round(gain_pct, 2), held_min=round(held_min))

        if not tightened and gain_pct >= PROFIT_LOCK_PCT:
            tightened = True
            trail_pct = TIGHT_STOP_PCT

    # Ran out of bars (day ended) before any exit condition fired
    if post_entry:
        last = post_entry[-1]
        gain_pct = (last.close - entry_price) / entry_price * 100
        held_min = (last.timestamp.astimezone(_ET) - entry_time).total_seconds() / 60
        return dict(exit_time=last.timestamp.astimezone(_ET), exit_price=last.close,
                    reason="End of day (no exit signal)", gain_pct=round(gain_pct, 2),
                    held_min=round(held_min))
    return dict(exit_time=entry_time, exit_price=entry_price, reason="No post-entry bars",
                gain_pct=0.0, held_min=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--providers", nargs="+", default=["sml", "sml2"],
                         help="Which providers' historical trade symbols to source the universe from")
    parser.add_argument("--db", default="stockbot.db")
    args = parser.parse_args()

    print(f"SML2 config: RSI={RSI_ENTRY_MIN}-{RSI_ENTRY_MAX} (effective 60-65, layer-1 caps at 65)  "
          f"MACD_fresh={REQUIRE_MACD_FRESH_CROSSOVER}  RVOL>={MIN_RVOL}x  "
          f"move={MIN_CHANGE_PCT}-{MAX_ENTRY_MOVE_PCT}%  |  "
          f"stops: hard={HARD_STOP_PCT}% trail={TRAIL_PCT}%->({TIGHT_STOP_PCT}% @+{PROFIT_LOCK_PCT}%)  "
          f"time: 30m>={MIN_GAIN_AT_30M}% 60m>={MIN_GAIN_AT_60M}% max={MAX_HOLD_MINUTES}m  "
          f"RSI exit={RSI_EXIT_LEVEL} falling  dump={DUMP_TIME_ET} ET\n")

    db_path = (_ROOT / args.db) if not Path(args.db).is_absolute() else Path(args.db)
    by_day = find_traded_symbols_by_day(db_path, args.providers, args.days)
    total_pairs = sum(len(v) for v in by_day.values())
    print(f"Universe: {len(by_day)} trading days, {total_pairs} (day, symbol) pairs "
          f"(from {'+'.join(args.providers)} actual trades)\n")

    api_key, api_secret = os.getenv("SML_ALPACA_API_KEY"), os.getenv("SML_ALPACA_API_SECRET")
    client = StockHistoricalDataClient(api_key, api_secret)

    results = []
    for d in sorted(by_day):
        for sym in sorted(by_day[d]):
            data = fetch_symbol_data(client, sym, d)
            if not data:
                print(f"  {d} {sym}: insufficient data, skipped")
                continue
            entry = simulate_entry(sym, data, d)
            if not entry:
                print(f"  {d} {sym}: no qualifying entry under current SML2 gates")
                results.append(dict(date=d, symbol=sym, entered=False))
                continue
            exit_ = simulate_exit(data["bars1"], entry["entry_time"], entry["entry_price"])
            print(f"  {d} {sym}: ENTER {entry['entry_time'].strftime('%H:%M')} @ ${entry['entry_price']:.4f} "
                  f"(RSI={entry['rsi']:.1f} chg={entry['change_pct']:.1f}% rvol={entry['rvol']:.1f}x)  "
                  f"-> EXIT {exit_['exit_time'].strftime('%H:%M')} @ ${exit_['exit_price']:.4f}  "
                  f"{exit_['gain_pct']:+.1f}%  held={exit_['held_min']}m  ({exit_['reason']})")
            results.append(dict(date=d, symbol=sym, entered=True, **entry, **exit_))

    entered = [r for r in results if r["entered"]]
    print(f"\n{'=' * 70}")
    print(f"{len(entered)}/{len(results)} pairs would have triggered an SML2 entry under current config")
    if entered:
        wins = [r for r in entered if r["gain_pct"] >= 0]
        avg = sum(r["gain_pct"] for r in entered) / len(entered)
        print(f"Win rate: {len(wins)}/{len(entered)} ({100*len(wins)/len(entered):.0f}%)")
        print(f"Avg return per trade: {avg:+.2f}%")
        print(f"Best: {max(entered, key=lambda r: r['gain_pct'])['symbol']} "
              f"{max(r['gain_pct'] for r in entered):+.1f}%")
        print(f"Worst: {min(entered, key=lambda r: r['gain_pct'])['symbol']} "
              f"{min(r['gain_pct'] for r in entered):+.1f}%")

    out_path = _ROOT / "reports" / "sml2_backtest.csv"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("date,symbol,entered,entry_time,entry_price,rsi,change_pct,rvol,"
                "exit_time,exit_price,gain_pct,held_min,reason\n")
        for r in results:
            if r["entered"]:
                f.write(f"{r['date']},{r['symbol']},1,{r['entry_time']},{r['entry_price']},"
                        f"{r['rsi']:.2f},{r['change_pct']:.2f},{r['rvol']:.2f},"
                        f"{r['exit_time']},{r['exit_price']},{r['gain_pct']},{r['held_min']},{r['reason']}\n")
            else:
                f.write(f"{r['date']},{r['symbol']},0,,,,,,,,,,\n")
    print(f"\nSaved {out_path}")


if __name__ == "__main__":
    main()
