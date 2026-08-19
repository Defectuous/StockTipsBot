"""
backtest_strategies.py — Backtest the four momentum setups documented in
strategy.md (gap_and_go, vwap_bounce, hod_breakout, abcd_pattern) against
every symbol/day pair recorded in the pi_data DB (positions + ticker_alerts),
pulling 1-minute bars from Alpaca for whatever isn't already cached in
price_bars.

This is NOT a replay of the live SML/SML2 bots (see tools/backtest_sml2.py
for that) — strategy.md's own source files (screener/strategy_selector.py,
signal_generator.py, technical_levels.py, scripts/real_backtest_45_pillars.py)
don't exist in this repo. strategy.md was written to be self-contained
outside this codebase, so this script implements its four setups from
scratch, straight off the doc's entry/stop/target rules.

Simplifications vs. strategy.md (data availability, not lookahead-safety):
  - No catalyst/news source is wired up -> the catalyst hard-gate (section 2)
    is NOT applied. Every setup implicitly assumes a catalyst exists.
  - No float/shares-outstanding source -> "float under 5M" (works-best note)
    and the float-rotation candidate filter (section 4) are both skipped.
  - Alpaca's free/paper feed is IEX, which carries little-to-no pre-market
    volume for most small caps -> pre-market high/low (needed for gap_and_go
    and the ABCD pole) falls back to the first few minutes of regular-session
    trading when no pre-market bars exist. Each fallback is counted and
    reported so you can judge how much this may distort results.
  - ABCD pivot detection uses a simple 3-bar-each-side local-extremum test
    with a 3-bar confirmation delay (a pivot at bar i is only usable from
    bar i+3 onward) — a simplified stand-in for real swing-structure analysis.

Entry/exit mechanics (strategy.md section 3): trailing stop sized at the
entry-to-initial-stop distance, trailing the highest price since entry,
exit triggered on a bar CLOSE crossing the trailing level (not an intrabar
wick — see section 3's backtest note). Session force-close at 12:00 PM ET.
One trade per symbol per day; the first qualifying setup wins, with ties
broken by the priority order in section 1 (gap_and_go > hod_breakout >
vwap_bounce > abcd_pattern).

Candidate filters (section 4) applied in a second, filtered pass: entry
price >= $2, entry before 10:00 AM ET, fast MACD(5,13,9) bullish on the
entry bar, and stop distance between 1x-3x ATR14%. Float rotation is
skipped (no data source).

Usage:
    python tools/backtest_strategies.py
    python tools/backtest_strategies.py --db pi_data/stockbot.db
    python tools/backtest_strategies.py --symbols CHRS NVD --start 2026-08-01
"""
import argparse
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytz
from dotenv import load_dotenv
import os
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from bot.market_data import _ema_series, _rsi_series

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
_ET = pytz.timezone("America/New_York")

PRIORITY = ["gap_and_go", "hod_breakout", "vwap_bounce", "abcd_pattern"]


@dataclass
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


# ── Universe: (symbol, date) pairs from the DB ─────────────────────────────

def find_universe(db_path: Path, symbols_filter, start: Optional[date], end: Optional[date]):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute("SELECT symbol, date(buy_time) FROM positions").fetchall()
    rows += conn.execute("SELECT symbol, date(alerted_at) FROM ticker_alerts").fetchall()
    conn.close()

    by_day: dict[date, set] = {}
    for sym, d in rows:
        if not sym or not d:
            continue
        try:
            dt = date.fromisoformat(d)
        except ValueError:
            continue
        if start and dt < start:
            continue
        if end and dt > end:
            continue
        if symbols_filter and sym not in symbols_filter:
            continue
        by_day.setdefault(dt, set()).add(sym)
    return by_day


# ── Bar caching in price_bars (reuses the schema already in pi_data DB) ────

def day_window_utc(d: date):
    start_et = _ET.localize(datetime.combine(d, time(4, 0)))
    end_et = _ET.localize(datetime.combine(d, time(20, 0)))
    return start_et.astimezone(pytz.UTC), end_et.astimezone(pytz.UTC)


def cached_count(cache_conn, symbol: str, start_utc: datetime, end_utc: datetime) -> int:
    return cache_conn.execute(
        "SELECT COUNT(*) FROM price_bars WHERE symbol=? AND timestamp>=? AND timestamp<?",
        (symbol, start_utc.isoformat(), end_utc.isoformat()),
    ).fetchone()[0]


def read_cached(cache_conn, symbol: str, start_utc: datetime, end_utc: datetime) -> List[Bar]:
    rows = cache_conn.execute(
        "SELECT timestamp, open, high, low, close, volume FROM price_bars "
        "WHERE symbol=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
        (symbol, start_utc.isoformat(), end_utc.isoformat()),
    ).fetchall()
    out = []
    for ts, o, h, l, c, v in rows:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = pytz.UTC.localize(t)
        out.append(Bar(t, o, h, l, c, v))
    return out


def ensure_day_cached(cache_conn, client: StockHistoricalDataClient, symbols: set, d: date, stats: Counter):
    start_utc, end_utc = day_window_utc(d)
    missing = [s for s in symbols if cached_count(cache_conn, s, start_utc, end_utc) == 0]
    if not missing:
        return
    try:
        resp = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sorted(missing),
            timeframe=TimeFrame.Minute,
            start=start_utc,
            end=end_utc,
        ))
    except Exception as e:
        print(f"  fetch failed for {d} {missing}: {e}")
        stats["fetch_failed"] += len(missing)
        return

    rows_to_insert = []
    for sym in missing:
        bars = resp.data.get(sym, [])
        for b in bars:
            ts = b.timestamp
            if ts.tzinfo is None:
                ts = pytz.UTC.localize(ts)
            rows_to_insert.append((sym, ts.isoformat(), b.open, b.high, b.low, b.close, int(b.volume)))
        if not bars:
            stats["no_data"] += 1
    if rows_to_insert:
        cache_conn.executemany(
            "INSERT OR IGNORE INTO price_bars (symbol, timestamp, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows_to_insert,
        )
        cache_conn.commit()


def fetch_prev_closes(client: StockHistoricalDataClient, symbols: set, min_date: date, max_date: date):
    start = _ET.localize(datetime.combine(min_date - timedelta(days=10), time(0, 0))).astimezone(pytz.UTC)
    end = _ET.localize(datetime.combine(max_date + timedelta(days=1), time(0, 0))).astimezone(pytz.UTC)
    try:
        resp = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sorted(symbols), timeframe=TimeFrame.Day, start=start, end=end,
        ))
    except Exception as e:
        print(f"  daily-bars fetch failed: {e}")
        return {}
    out = {}
    for sym in symbols:
        bars = resp.data.get(sym, [])
        out[sym] = sorted((b.timestamp.astimezone(_ET).date(), b.close) for b in bars)
    return out


def prev_close_for(daily_map: dict, symbol: str, d: date) -> Optional[float]:
    closes = daily_map.get(symbol, [])
    prev = None
    for bd, c in closes:
        if bd < d:
            prev = c
        else:
            break
    return prev


# ── Indicators (causal / no lookahead) ─────────────────────────────────────

def macd_fast_bullish_series(closes: List[float], fast=5, slow=13, signal=9) -> List[Optional[bool]]:
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    n = len(closes)
    macd_full: List[Optional[float]] = [None] * n
    valid_idx, macd_valid = [], []
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            m = ema_fast[i] - ema_slow[i]
            macd_full[i] = m
            valid_idx.append(i)
            macd_valid.append(m)
    sig_valid = _ema_series(macd_valid, signal)
    out: List[Optional[bool]] = [None] * n
    for j, i in enumerate(valid_idx):
        if sig_valid[j] is not None:
            out[i] = macd_full[i] > sig_valid[j]
    return out


def atr_series(bars: List[Bar], period=14) -> List[Optional[float]]:
    n = len(bars)
    tr: List[Optional[float]] = [None] * n
    for i in range(1, n):
        b, pb = bars[i], bars[i - 1]
        tr[i] = max(b.high - b.low, abs(b.high - pb.close), abs(b.low - pb.close))
    out: List[Optional[float]] = [None] * n
    for i in range(period, n):
        window = [tr[j] for j in range(i - period + 1, i + 1) if tr[j] is not None]
        if len(window) == period:
            out[i] = sum(window) / period
    return out


def pivots(bars: List[Bar], span=3):
    """Local extrema with a `span`-bar confirmation delay. Returns two dicts
    {confirmed_index -> pivot_index} for highs and lows — a pivot at
    pivot_index only becomes usable once confirmed_index has been reached,
    so the scan loop never looks into its own future."""
    highs, lows = {}, {}
    n = len(bars)
    for i in range(span, n - span):
        window = bars[i - span:i + span + 1]
        if bars[i].high == max(b.high for b in window):
            highs[i + span] = i
        if bars[i].low == min(b.low for b in window):
            lows[i + span] = i
    return highs, lows


# ── Setup detectors ─────────────────────────────────────────────────────
# Each returns (entry_price, stop_price, target1, target2) or None.
# Called once per bar index i (0-based into `session` bars, session[0] is
# the 9:30 ET bar); only bars up to and including i may be examined.

def try_gap_and_go(ctx, i):
    if ctx["gap_pct"] is None or ctx["gap_pct"] < ctx["gap_min_pct"]:
        return None
    pm_high, pm_low = ctx["pm_high"], ctx["pm_low"]
    if pm_high is None or pm_low is None:
        return None
    trigger = pm_high * 1.0015
    stop = pm_low * 0.998
    # Kill: price dipped through the pre-market low before ever tagging the
    # trigger -> "gap fills immediately at open."
    lows_so_far = min(b.low for b in ctx["session"][: i + 1])
    if lows_so_far <= stop:
        return None
    if ctx["session"][i].high >= trigger:
        entry = trigger
        return entry, stop, entry * 1.20, entry * 1.50
    return None


def try_hod_breakout(ctx, i):
    ts_et = ctx["session"][i].timestamp.astimezone(_ET)
    if ts_et.time() > time(11, 30):
        return None
    if i < ctx["consol_bars"]:
        return None
    running_hod = max(b.high for b in ctx["session"][:i])  # HOD strictly before this bar
    consol = ctx["session"][i - ctx["consol_bars"]:i]
    consol_hi = max(b.high for b in consol)
    consol_lo = min(b.low for b in consol)
    if consol_hi == 0:
        return None
    consol_range_pct = (consol_hi - consol_lo) / consol_hi * 100
    if consol_range_pct > ctx["consol_range_pct"] or consol_hi < running_hod * 0.98:
        return None
    avg_consol_vol = sum(b.volume for b in consol) / len(consol)
    bar = ctx["session"][i]
    trigger = running_hod * 1.0015
    if bar.high >= trigger and avg_consol_vol > 0 and bar.volume >= 2 * avg_consol_vol:
        entry = trigger
        stop = min(consol_lo, running_hod * 0.98)
        return entry, stop, entry * 1.10, entry * 1.20
    return None


def try_vwap_bounce(ctx, i):
    if i < 1:
        return None
    session = ctx["session"]
    day_open = session[0].open
    up_10_seen = any((b.high - day_open) / day_open >= 0.10 for b in session[: i + 1])
    if not up_10_seen:
        return None
    vwap = ctx["vwap"]
    if vwap[i] is None or vwap[i - 1] is None:
        return None
    base = max(0, i - ctx["reclaim_lookback"])
    dip_lows = [session[j].low for j in range(base, i) if vwap[j] is not None and session[j].low < vwap[j]]
    if not dip_lows:
        return None
    dip_low = min(dip_lows)
    bar = session[i]
    prev = session[i - 1]
    was_below = prev.close < vwap[i - 1]
    reclaimed = bar.close > vwap[i] * 1.001 and bar.close > dip_low * 1.001
    if was_below and reclaimed:
        entry = bar.close
        stop = dip_low
        return entry, stop, ctx["hod_running"][i], ctx["hod_running"][i] * 1.03
    return None


def try_abcd(ctx, i):
    highs, lows = ctx["pivot_highs"], ctx["pivot_lows"]
    session = ctx["session"]
    confirmed_highs = sorted(idx for conf, idx in highs.items() if conf <= i)
    confirmed_lows = sorted(idx for conf, idx in lows.items() if conf <= i)
    if not confirmed_highs or len(confirmed_lows) < 2:
        return None
    # B = most recent confirmed pivot high; A = most recent confirmed low before B
    b_idx = confirmed_highs[-1]
    a_candidates = [idx for idx in confirmed_lows if idx < b_idx]
    if not a_candidates:
        return None
    a_idx = a_candidates[-1]
    c_candidates = [idx for idx in confirmed_lows if idx > b_idx]
    if not c_candidates:
        return None
    c_idx = c_candidates[-1]
    if c_idx >= i:
        return None
    A, B, C = session[a_idx].low, session[b_idx].high, session[c_idx].low
    ab = B - A
    if ab <= 0 or A <= 0 or (ab / A) < 0.10:
        return None
    bc = B - C
    if bc <= 0:
        return None
    bc_retrace = bc / ab
    if bc_retrace > 0.90:
        return None  # BC retracement too deep, kills the setup
    d_target = C + ab
    trigger = d_target * 1.002
    bar = session[i]
    if bar.high >= trigger:
        entry = trigger
        stop = d_target * 0.99
        return entry, stop, C + ab * 0.618, B
    return None


SETUP_FNS = {
    "gap_and_go": try_gap_and_go,
    "hod_breakout": try_hod_breakout,
    "vwap_bounce": try_vwap_bounce,
    "abcd_pattern": try_abcd,
}


# ── Per-symbol-day simulation ───────────────────────────────────────────

def simulate_day(symbol: str, d: date, session: List[Bar], premkt: List[Bar], prev_close: Optional[float],
                  args, stats: Counter):
    if len(session) < 10 or prev_close is None:
        return None

    gap_pct = (session[0].open - prev_close) / prev_close * 100

    if premkt:
        pm_high = max(b.high for b in premkt)
        pm_low = min(b.low for b in premkt)
    else:
        stats["pm_fallback"] += 1
        n0 = min(5, len(session))
        pm_high = max(b.high for b in session[:n0])
        pm_low = min(b.low for b in session[:n0])

    closes = [b.close for b in session]
    macd_bull = macd_fast_bullish_series(closes)
    atr = atr_series(session, 14)
    pivot_highs, pivot_lows = pivots(session, span=3)

    running_vwap: List[Optional[float]] = []
    running_hod: List[float] = []
    total_pv = total_vol = 0.0
    hod = 0.0
    for b in session:
        tp = (b.high + b.low + b.close) / 3.0
        total_pv += tp * b.volume
        total_vol += b.volume
        running_vwap.append(total_pv / total_vol if total_vol else None)
        hod = max(hod, b.high)
        running_hod.append(hod)

    ctx = dict(
        session=session, gap_pct=gap_pct, gap_min_pct=args.gap_min_pct,
        pm_high=pm_high, pm_low=pm_low, vwap=running_vwap, hod_running=running_hod,
        consol_bars=args.consol_bars, consol_range_pct=args.consol_range_pct,
        reclaim_lookback=args.reclaim_lookback, pivot_highs=pivot_highs, pivot_lows=pivot_lows,
    )

    entry_i = entry_setup = None
    entry_price = stop_price = target1 = target2 = None
    for i in range(len(session)):
        for setup in PRIORITY:
            result = SETUP_FNS[setup](ctx, i)
            if result:
                entry_price, stop_price, target1, target2 = result
                entry_i, entry_setup = i, setup
                break
        if entry_i is not None:
            break

    if entry_i is None:
        return dict(date=d, symbol=symbol, entered=False)

    entry_time = session[entry_i].timestamp.astimezone(_ET)
    entry_atr = atr[entry_i]

    exit_ = simulate_exit(session[entry_i:], entry_price, stop_price, entry_time, args.dump_time)

    return dict(
        date=d, symbol=symbol, entered=True, setup=entry_setup,
        entry_time=entry_time, entry_price=entry_price, stop_price=stop_price,
        target1=target1, target2=target2, entry_macd_bull=macd_bull[entry_i], entry_atr=entry_atr,
        gap_pct=round(gap_pct, 2), pm_fallback=not bool(premkt),
        **exit_,
    )


def simulate_exit(post_entry: List[Bar], entry_price: float, stop_price: float, entry_time: datetime,
                   dump_time: time):
    trail_dist = entry_price - stop_price
    peak = post_entry[0].high
    for bar in post_entry[1:]:
        ts_et = bar.timestamp.astimezone(_ET)
        peak = max(peak, bar.high)
        trail_level = peak - trail_dist
        held_min = round((ts_et - entry_time).total_seconds() / 60)
        if bar.close < trail_level:
            gain_pct = (bar.close - entry_price) / entry_price * 100
            return dict(exit_time=ts_et, exit_price=bar.close, reason="Trailing stop",
                        gain_pct=round(gain_pct, 2), held_min=held_min)
        if ts_et.time() >= dump_time:
            gain_pct = (bar.close - entry_price) / entry_price * 100
            return dict(exit_time=ts_et, exit_price=bar.close, reason="Session force-close",
                        gain_pct=round(gain_pct, 2), held_min=held_min)
    last = post_entry[-1]
    ts_et = last.timestamp.astimezone(_ET)
    gain_pct = (last.close - entry_price) / entry_price * 100
    held_min = round((ts_et - entry_time).total_seconds() / 60)
    return dict(exit_time=ts_et, exit_price=last.close, reason="End of data (no exit signal)",
                gain_pct=round(gain_pct, 2), held_min=held_min)


# ── Candidate filters (strategy.md section 4) ───────────────────────────

def passes_filters(r: dict, args) -> bool:
    if r["entry_price"] < args.price_floor:
        return False
    if r["entry_time"].time() >= args.time_cutoff:
        return False
    if not r["entry_macd_bull"]:
        return False
    if r["entry_atr"] is None or r["entry_price"] <= 0:
        return False
    stop_dist_pct = (r["entry_price"] - r["stop_price"]) / r["entry_price"] * 100
    atr_pct = r["entry_atr"] / r["entry_price"] * 100
    if atr_pct <= 0:
        return False
    ratio = stop_dist_pct / atr_pct
    return 1.0 <= ratio <= 3.0


# ── Reporting ────────────────────────────────────────────────────────────

def summarize(label: str, trades: List[dict]):
    print(f"\n{'=' * 70}\n{label}: {len(trades)} trades")
    if not trades:
        return
    wins = [t for t in trades if t["gain_pct"] >= 0]
    avg = sum(t["gain_pct"] for t in trades) / len(trades)
    total = sum(t["gain_pct"] for t in trades)
    print(f"  Win rate: {len(wins)}/{len(trades)} ({100 * len(wins) / len(trades):.0f}%)")
    print(f"  Avg P&L/trade: {avg:+.2f}%   Total (unweighted sum): {total:+.2f}%")
    print(f"  Best: {max(trades, key=lambda t: t['gain_pct'])['symbol']} "
          f"{max(t['gain_pct'] for t in trades):+.1f}%   "
          f"Worst: {min(trades, key=lambda t: t['gain_pct'])['symbol']} "
          f"{min(t['gain_pct'] for t in trades):+.1f}%")
    by_setup = Counter()
    pl_by_setup: dict = {}
    for t in trades:
        by_setup[t["setup"]] += 1
        pl_by_setup.setdefault(t["setup"], []).append(t["gain_pct"])
    for setup in PRIORITY:
        if setup not in by_setup:
            continue
        pls = pl_by_setup[setup]
        w = sum(1 for p in pls if p >= 0)
        print(f"    {setup:14s} {by_setup[setup]:3d} trades  "
              f"wr={100 * w / len(pls):.0f}%  avg={sum(pls) / len(pls):+.2f}%")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="pi_data/stockbot.db")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--gap-min-pct", type=float, default=10.0)
    parser.add_argument("--consol-bars", type=int, default=5)
    parser.add_argument("--consol-range-pct", type=float, default=2.0)
    parser.add_argument("--reclaim-lookback", type=int, default=10)
    parser.add_argument("--price-floor", type=float, default=2.0)
    parser.add_argument("--time-cutoff", type=lambda s: time.fromisoformat(s), default=time(10, 0))
    parser.add_argument("--dump-time", type=lambda s: time.fromisoformat(s), default=time(12, 0))
    parser.add_argument("--report", default="reports/strategy_backtest.csv")
    args = parser.parse_args()

    db_path = (_ROOT / args.db) if not Path(args.db).is_absolute() else Path(args.db)
    by_day = find_universe(db_path, set(args.symbols) if args.symbols else None, args.start, args.end)
    total_pairs = sum(len(v) for v in by_day.values())
    all_symbols = set().union(*by_day.values()) if by_day else set()
    print(f"Universe: {len(by_day)} trading days, {total_pairs} (day, symbol) pairs, "
          f"{len(all_symbols)} distinct symbols\n")
    if not by_day:
        print("Nothing to backtest — no positions/ticker_alerts rows matched the filters.")
        return

    api_key = os.getenv("SML_ALPACA_API_KEY")
    api_secret = os.getenv("SML_ALPACA_API_SECRET")
    client = StockHistoricalDataClient(api_key, api_secret)

    cache_conn = sqlite3.connect(str(db_path))
    stats = Counter()

    print("Fetching / caching 1-min bars ...")
    for d in sorted(by_day):
        ensure_day_cached(cache_conn, client, by_day[d], d, stats)

    print("Fetching prior-close daily bars ...")
    min_d, max_d = min(by_day), max(by_day)
    daily_map = fetch_prev_closes(client, all_symbols, min_d, max_d)

    results = []
    for d in sorted(by_day):
        for sym in sorted(by_day[d]):
            start_utc, end_utc = day_window_utc(d)
            all_bars = read_cached(cache_conn, sym, start_utc, end_utc)
            if not all_bars:
                stats["no_bars"] += 1
                continue
            open_et = _ET.localize(datetime.combine(d, time(9, 30)))
            session = [b for b in all_bars if b.timestamp.astimezone(_ET) >= open_et]
            premkt = [b for b in all_bars if b.timestamp.astimezone(_ET) < open_et]
            prev_close = prev_close_for(daily_map, sym, d)
            r = simulate_day(sym, d, session, premkt, prev_close, args, stats)
            if r is None:
                stats["insufficient_data"] += 1
                continue
            if r["entered"]:
                print(f"  {d} {sym}: ENTER {r['setup']} "
                      f"{r['entry_time'].strftime('%H:%M')} @ ${r['entry_price']:.4f} -> "
                      f"EXIT {r['exit_time'].strftime('%H:%M')} @ ${r['exit_price']:.4f}  "
                      f"{r['gain_pct']:+.1f}%  ({r['reason']})")
            results.append(r)

    entered = [r for r in results if r["entered"]]
    filtered = [r for r in entered if passes_filters(r, args)]

    summarize("ALL SIGNALS (no candidate filters)", entered)
    summarize("FILTERED (price>=$2, entry<10:00 ET, fast-MACD bullish, 1-3x ATR stop)", filtered)

    print(f"\nData notes:")
    print(f"  pre-market-high/low fallback used (no PM bars) on {stats['pm_fallback']} symbol-days")
    print(f"  no cached/fetched bars at all: {stats['no_bars']} symbol-days")
    print(f"  insufficient data to simulate: {stats['insufficient_data']} symbol-days")
    if stats["fetch_failed"]:
        print(f"  Alpaca fetch failures: {stats['fetch_failed']} symbols")
    print(f"  Catalyst gate NOT applied (no news source wired up) -- see script docstring.")
    print(f"  Float-rotation filter NOT applied (no float data source) -- see script docstring.")

    out_path = _ROOT / args.report
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("date,symbol,entered,setup,entry_time,entry_price,stop_price,"
                "exit_time,exit_price,gain_pct,held_min,reason,gap_pct,pm_fallback,passes_filters\n")
        for r in results:
            if r["entered"]:
                f.write(f"{r['date']},{r['symbol']},1,{r['setup']},{r['entry_time']},{r['entry_price']:.4f},"
                        f"{r['stop_price']:.4f},{r['exit_time']},{r['exit_price']:.4f},{r['gain_pct']},"
                        f"{r['held_min']},{r['reason']},{r['gap_pct']},{int(r['pm_fallback'])},"
                        f"{int(passes_filters(r, args))}\n")
            else:
                f.write(f"{r['date']},{r['symbol']},0,,,,,,,,,,,,\n")
    print(f"\nSaved {out_path}")

    cache_conn.close()


if __name__ == "__main__":
    main()
