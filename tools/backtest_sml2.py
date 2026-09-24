"""
backtest_sml2.py — Replay one of SML2's two historical entry strategies
against the last N days of price data for symbols traded in that window,
per symbol per day. Pick which strategy with --strategy before it starts —
SML2 ran RSI/MACD/VWAP through 2026-08-19 and switched to HOD-breakout on
2026-08-20 (see run_sml2_screener.py), so this tool no longer assumes one
or the other.

Scope (see conversation for why): single-symbol standalone backtest — each
(day, symbol) pair is tested independently, as if it had its own dedicated
bot slot. It does NOT simulate the shared MAX_POSITIONS=2 cap across the
day's whole candidate universe (that universe isn't reconstructable — it's
a live "most active" snapshot that was never persisted), and it reports %
returns, not $ PnL (which would need day-by-day account-equity simulation).

Entry logic replicates the two layers scan_and_trade() actually applies,
depending on --strategy:

  --strategy rsi_macd (SML2's entry gate through 2026-08-19):
    Layer 1 (bot/screener.py _analyze().passes) — RSI 50-65 & rising over
      the last 2 bars, MACD above signal with expanding histogram, price
      > VWAP.
    Layer 2 (SML2's own gates on top) — cooldown (N/A, single symbol/day),
      change_pct in [MIN_CHANGE_PCT, MAX_ENTRY_MOVE_PCT], RSI in
      [RSI_ENTRY_MIN, RSI_ENTRY_MAX], fresh MACD crossover, RVOL >= MIN_RVOL.
    Net effect: layer 1's hard 65-cap means the *effective* RSI band is
    60-65, not 60-70 — RSI_ENTRY_MAX=70 never actually binds.

  --strategy hod (SML2's entry gate from 2026-08-20, same as SML):
    Layer 1 (bot/screener.py _detect_hod_breakout()) — price breaks above a
      tight HOD-anchored consolidation (HOD_CONSOL_BARS/HOD_CONSOL_RANGE_PCT)
      on above-average volume, before 11:30 ET.
    Layer 2 (SML2's own gates on top) — change_pct in [MIN_CHANGE_PCT,
      MAX_ENTRY_MOVE_PCT], ATR <= MAX_ATR, RVOL >= MIN_RVOL.

Exit logic replicates monitor_positions() and is identical for both
strategies (unchanged by the entry-signal switch): hard stop / trailing
stop treated as broker-resting orders (checked against each bar's low,
whichever trigger price is higher wins), then per-bar checks in priority
order — max hold, 60m/30m checkpoints, dump time 12:00 ET, RSI-75-falling
exit, profit-lock tightening (10% trail -> 5% trail at +15% gain).

Usage:
    python tools/backtest_sml2.py --strategy hod --days 30
    python tools/backtest_sml2.py --strategy rsi_macd --days 30
    python tools/backtest_sml2.py --strategy hod --days 30 --db stockbot.db
"""
import argparse
import os
import pickle
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytz
from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.market_data import _rsi_series, _rvol_time_adjusted, _vwap
from bot.screener import _analyze, _detect_hod_breakout

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
_ET = pytz.timezone("America/New_York")
_5MIN = TimeFrame(5, TimeFrameUnit.Minute)
_15MIN = TimeFrame(15, TimeFrameUnit.Minute)

# ── Current SML2 config (mirrors .env + run_sml2_screener.py defaults) ────────
START_TIME_ET    = os.getenv("START_TIME_ET", "09:45")  # was hardcoded "09:30" — didn't match
                                                          # the live 09:45 gate (added 2026-07-30
                                                          # after the 9:30-9:59 window accounted
                                                          # for >100% of SML2's losses), so the
                                                          # entry replay could "take" trades the
                                                          # live bot would never have scanned for.
STOP_BUY_TIME_ET = "11:45"
DUMP_TIME_ET     = os.getenv("DUMP_TIME_ET", "12:00")
MIN_RVOL         = float(os.getenv("MIN_RVOL", "1.5"))
MAX_RVOL         = float(os.getenv("MAX_RVOL", "0"))
MIN_CHANGE_PCT   = float(os.getenv("MIN_CHANGE_PCT", "2.0"))
MAX_ENTRY_MOVE_PCT = float(os.getenv("SML2_MAX_ENTRY_MOVE_PCT") or os.getenv("MAX_ENTRY_MOVE_PCT", "0"))
MAX_ATR          = float(os.getenv("MAX_ATR", "0"))

# rsi_macd strategy only (SML2's entry gate through 2026-08-19)
RSI_ENTRY_MIN    = float(os.getenv("RSI_ENTRY_MIN", "60"))
RSI_ENTRY_MAX    = float(os.getenv("RSI_ENTRY_MAX", "70"))
REQUIRE_MACD_FRESH_CROSSOVER = os.getenv("REQUIRE_MACD_FRESH_CROSSOVER", "true").lower() == "true"
# Live SML2 doesn't require a fresh crossover — it requires MACD to have been
# above signal for >= N bars (run_sml2_screener.py MACD_MIN_BARS_ABOVE_SIGNAL=3).
# 0 = off (legacy behavior); live-fidelity runs set this to 3 and turn
# REQUIRE_MACD_FRESH_CROSSOVER off.
MACD_MIN_BARS_ABOVE_SIGNAL = 0
# Live fetches 15-min bars (MACD + RVOL) from midnight 7 days back (the
# 2026-08-17 Monday/post-holiday RVOL=None fix). The legacy window here was
# "now minus 3 days", which reproduces that exact bug. True = live window.
LIVE_RVOL_LOOKBACK = False
# rsi_macd entry never enforced START_TIME_ET (only the vwap_reclaim entry
# did), so it could take 09:30-09:44 entries live SML2 has skipped since
# 2026-07-30. None = legacy (no gate); live-fidelity runs set "09:45".
ENTRY_START_TIME_ET: str | None = None
MIN_ENTRY_PRICE = 0.0   # SML-style min-price filter for sweeps; 0 = off

# hod strategy only (SML2's entry gate from 2026-08-20)
HOD_CONSOL_BARS      = int(os.getenv("HOD_CONSOL_BARS", "5"))
HOD_CONSOL_RANGE_PCT = float(os.getenv("HOD_CONSOL_RANGE_PCT", "2.0"))

TRAIL_PCT        = float(os.getenv("TRAILING_STOP_PERCENT", "10"))
HARD_STOP_PCT    = float(os.getenv("HARD_STOP_PCT", "5"))
PROFIT_LOCK_PCT  = float(os.getenv("PROFIT_LOCK_PCT", "15"))
TIGHT_STOP_PCT   = float(os.getenv("TIGHT_STOP_PCT", "5"))
RSI_EXIT_LEVEL   = float(os.getenv("RSI_EXIT_LEVEL", "75"))
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES", "90"))
MIN_GAIN_AT_30M  = float(os.getenv("MIN_GAIN_AT_30M", "-2.0"))
MIN_GAIN_AT_60M  = float(os.getenv("MIN_GAIN_AT_60M", "0.0"))

# ── "--improved" profile — the 5 exit/entry changes proposed after the SML2
# 50%-win-rate review (2026-09-14). Every value below 0/None means "off" and
# reproduces current live behavior exactly; --improved overwrites them (see
# main()). Baseline also got two accuracy fixes as part of building this:
#   - a trailing stop no longer rests from minute 1 — live only ever rests
#     the fixed HARD_STOP_PCT stop until PROFIT_LOCK_PCT fires (previously
#     simulate_exit modeled a live TRAIL_PCT trail from entry, which doesn't
#     match run_sml2_screener.py's actual order-placement logic).
#   - the RSI-overbought exit (RSI_EXIT_LEVEL, falling) is now modeled at
#     all — it was in the module docstring but never implemented.
DONT_CHASE_PCT         = 0.0   # improved: 9.0 — tighter entry ceiling than MAX_ENTRY_MOVE_PCT;
                                # SST (+14.9% chg) and AMTX (+11.5%) are 2 of the account's 3
                                # worst trades and both were the most-extended entries taken.
ATR_TRAIL_ACTIVATE_PCT = 0.0   # improved: 2.5 — %gain at which an ATR-sized trail engages,
                                # replacing the "no trail until +15% profit-lock" gap that was
                                # capping winners at 16-34% of their intraday peak.
ATR_TRAIL_MULT         = 1.5   # trail distance once active = ATR_TRAIL_MULT x (ATR / entry_price)
RSI_EXIT_MIN_GAIN_PCT  = 0.0   # improved: 5.0 — don't let the RSI-overbought exit fire on a
                                # trade that's barely green (EMPD sold at +0.8% the moment RSI
                                # ran hot — exactly when a momentum trade should be working)

# ── Live-fidelity exit options (2026-09-23) — both default off so existing
# callers are unchanged. Live SML2 doesn't rest a flat HARD_STOP_PCT stop: it
# uses run_sml2_screener.py:_size_by_risk's ATR-sized stop, clamped to
# [ATR_MIN_STOP_PCT, ATR_MAX_STOP_PCT], and runs the VWAP-reclaim early exit
# (bot/market_data.py:vwap_reclaim_exit_price) ahead of the time checkpoints.
ATR_HARD_STOP            = False  # True = hard stop = clamp(ATR_STOP_MULT x ATR%, min, max)
ATR_STOP_MULT            = 2.0
ATR_MIN_STOP_PCT         = 2.0
ATR_MAX_STOP_PCT         = 10.0
VWAP_RECLAIM_EXIT_DWELL  = 0      # live: 9 — consecutive 1-min closes below VWAP AND entry; 0 = off
VWAP_RECLAIM_EXIT_WARMUP = 3      # live: 3 — minutes after entry ignored by the reclaim check

# ── Lookahead fix (2026-09-23). Alpaca labels bars by START time, so filtering
# 5/15-min bars on `timestamp <= ts` hands the gate the still-forming bar's
# FINAL OHLC — up to 4 (5-min) / 14 (15-min) minutes of the future — while
# the entry price is the current 1-min close. When True, that forming bar is
# rebuilt from 1-min bars up to the decision time instead, which matches what
# live sees (Alpaca returns the partial bar as of now, never beyond it).
NO_LOOKAHEAD = False

# Stop-fill realism (2026-09-23): 1-min bars can't show whether a bar's high
# or low came first, and the original model always assumed low-first (most
# favorable for a trailing stop). Checked against 14 live SML2 trades on the
# current exit config, that made trail exits read ~30 points too good in
# aggregate (XRTX sim +24.8% vs actual +6.2%, VHUB -0.1% vs -7.1%). When True:
# gap-through stops fill at the bar open, and a new-high bar that also trades
# down through the trail stops out within that bar.
REALISTIC_STOP_FILLS = False

# Breakeven floor under the ATR trail (2026-09-23 proposal): once the trail
# is active, the stop never sits below entry (+ offset). Live would enforce
# this from the monitor loop (Alpaca can't hold a hard stop and a trailing
# stop on the same shares), so this is slightly optimistic on fill timing.
ATR_TRAIL_BE_FLOOR = False
ATR_TRAIL_BE_OFFSET_PCT = 0.0

# ── Candidate filters from 2026-09-20 strategy research (see memory
# project_strategy_research_2026-09-20) — all default to 0/off so importing
# this module changes no existing behavior. Only rsi_macd entry consumes
# these two; vwap_reclaim is a separate entry strategy entirely.
VWAP_SLOPE_MIN_PCT        = 0.0   # min required rise in VWAP over the lookback window, as a
                                    # % of the earlier VWAP value; 0 = off, negative = disallowed
VWAP_SLOPE_LOOKBACK_MIN   = 15     # window over which slope is measured
OPENING_RANGE_MAX_PCT     = 0.0    # max allowed (high-low)/low over the opening window, as a %;
                                    # 0 = off. "Wide opening range" was flagged as a loss leak in
                                    # project_bad_trade_filters_analysis.
OPENING_RANGE_MINUTES     = 15     # opening window width, anchored to 09:30 ET

# ── vwap_reclaim entry strategy params (mirrors the live VWAP-reclaim EXIT's
# dwell=9 tuning in spirit — symmetric "sustained break, not a single wobble"
# requirement — see project_vwap_reclaim_exit)
VWAP_RECLAIM_FADE_MIN          = 15    # consecutive 1-min closes below VWAP required immediately
                                         # before the reclaim bar, to count as a real fade+consolidate
VWAP_RECLAIM_VOL_MULT          = 2.0   # reclaim bar volume must be >= this x the preceding
                                         # consolidation window's average 1-min volume
VWAP_RECLAIM_CONSOL_LOOKBACK_MIN = 15  # window used to compute the consolidation baseline volume

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


_BAR_CACHE_DIR = _ROOT / "reports" / "_bar_cache"


def fetch_symbol_data(client: StockHistoricalDataClient, symbol: str, d: date,
                       use_cache: bool = True) -> dict | None:
    """Pull all bar data needed to simulate one symbol's day: 1-min (price path),
    5-min (RSI/VWAP/ATR), 15-min (MACD/RVOL, needs 3 prior calendar days), daily
    (previous close for change_pct).

    Bar data for a given (symbol, day) doesn't change between runs, but
    comparing several exit profiles (baseline / improved / entry-only /
    exits-only) against the same wide symbol universe means calling this
    4x per pair — so results are cached to disk (including a cached "no
    data" miss) and reused across runs instead of re-hitting the Alpaca API
    every time. Pass use_cache=False to force a live refetch.
    """
    cache_path = _BAR_CACHE_DIR / f"{symbol}_{d.isoformat()}.pkl"
    if use_cache and cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            # Entries cached before 2026-09-23 only hold 4 days of 15-min
            # bars; refetch so LIVE_RVOL_LOOKBACK has its 7-day window.
            if cached is None or cached.get("bars15_days") == 7:
                return cached
        except Exception:
            pass  # corrupt cache entry — fall through and refetch

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
            start=(day_start_et - timedelta(days=7)).replace(hour=0, minute=0).astimezone(pytz.UTC),
            end=day_end_et.astimezone(pytz.UTC),
        )).data.get(symbol, []))
        daily = list(client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=(day_start_et - timedelta(days=10)).astimezone(pytz.UTC), end=day_end_et.astimezone(pytz.UTC),
        )).data.get(symbol, []))
    except Exception as e:
        print(f"  {symbol} {d}: fetch failed ({e})")
        return None  # transient/API error — don't cache, worth retrying next run

    def _save(value: dict | None) -> dict | None:
        if use_cache:
            _BAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump(value, f)
        return value

    if not bars1 or not bars5 or not bars15:
        return _save(None)

    prev_close = None
    for b in daily:
        b_date = b.timestamp.astimezone(_ET).date()
        if b_date < d:
            prev_close = b.close
    if prev_close is None:
        return _save(None)

    return _save(dict(bars1=bars1, bars5=bars5, bars15=bars15, prev_close=prev_close,
                      bars15_days=7))


def _b15_start(ts_et: datetime) -> datetime:
    if LIVE_RVOL_LOOKBACK:
        return (ts_et - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    return ts_et - timedelta(days=3)


def _asof(bars: list, bar_min: int, ts_et: datetime, bars1: list) -> list:
    """Bars as live would see them at the close of the 1-min bar starting at
    ts_et: completed bars unchanged, the forming bar rebuilt from bars1."""
    decided_at = ts_et + timedelta(minutes=1)
    out = []
    for b in bars:
        start = b.timestamp.astimezone(_ET)
        if start > ts_et:
            break
        if start + timedelta(minutes=bar_min) <= decided_at:
            out.append(b)
            continue
        parts = [m for m in bars1 if start <= m.timestamp.astimezone(_ET) <= ts_et]
        if parts:
            out.append(SimpleNamespace(
                timestamp=b.timestamp, open=parts[0].open, close=parts[-1].close,
                high=max(m.high for m in parts), low=min(m.low for m in parts),
                volume=sum(m.volume for m in parts)))
    return out


def simulate_entry_rsi_macd(symbol: str, data: dict, d: date) -> dict | None:
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
        if ENTRY_START_TIME_ET and ts_et < _ET.localize(datetime.combine(d, _parse_hm(ENTRY_START_TIME_ET))):
            continue

        price = bar.close
        if MIN_ENTRY_PRICE and price < MIN_ENTRY_PRICE:
            continue
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0

        b5_seen = _asof(bars5, 5, ts_et, bars1) if NO_LOOKAHEAD else bars5
        b15_seen = _asof(bars15, 15, ts_et, bars1) if NO_LOOKAHEAD else bars15
        # Rolling 120-min window for RSI/ATR (mirrors the live "now-120min" fetch)
        window_start = ts_et - timedelta(minutes=120)
        b5_window = [b for b in b5_seen if window_start <= b.timestamp.astimezone(_ET) <= ts_et]
        # Market-open-anchored window for VWAP
        b5_vwap = [b for b in b5_seen if market_open <= b.timestamp.astimezone(_ET) <= ts_et]
        # 3-calendar-day trailing window for MACD/RVOL
        b15_window = [b for b in b15_seen
                      if _b15_start(ts_et) <= b.timestamp.astimezone(_ET) <= ts_et]

        stock = _analyze(symbol, b5_window, b15_window, price, change_pct, vwap_bars=b5_vwap)
        if not stock or not stock.passes:
            continue

        # ── Diagnostics, always computed (used for both gating, when the
        # matching *_MIN_PCT/*_MAX_PCT is set, and bucket analysis when not) ─
        slope_cutoff = ts_et - timedelta(minutes=VWAP_SLOPE_LOOKBACK_MIN)
        b5_vwap_earlier = [b for b in b5_vwap if b.timestamp.astimezone(_ET) <= slope_cutoff]
        vwap_earlier = _vwap(b5_vwap_earlier) if len(b5_vwap_earlier) >= 2 else None
        slope_pct = ((stock.vwap - vwap_earlier) / vwap_earlier * 100
                     if vwap_earlier and stock.vwap else None)

        or_cutoff = market_open + timedelta(minutes=OPENING_RANGE_MINUTES)
        or_bars = [b for b in b5_vwap if b.timestamp.astimezone(_ET) <= or_cutoff]
        or_high = max((b.high for b in or_bars), default=None)
        or_low = min((b.low for b in or_bars), default=None)
        or_pct = (or_high - or_low) / or_low * 100 if or_bars and or_low else None

        # ── Candidate filter: VWAP slope (off by default) ───────────────────
        if VWAP_SLOPE_MIN_PCT != 0.0:
            if slope_pct is None or slope_pct < VWAP_SLOPE_MIN_PCT:
                continue

        # ── Candidate filter: opening-range width (off by default) ─────────
        if OPENING_RANGE_MAX_PCT > 0:
            if or_pct is None or or_pct > OPENING_RANGE_MAX_PCT:
                continue

        # ── Layer 2: SML2's own gates ──────────────────────────────────────
        if MAX_ENTRY_MOVE_PCT > 0 and stock.change_pct > MAX_ENTRY_MOVE_PCT:
            continue
        if DONT_CHASE_PCT > 0 and stock.change_pct > DONT_CHASE_PCT:
            continue
        if MIN_CHANGE_PCT > 0 and (stock.change_pct is None or stock.change_pct < MIN_CHANGE_PCT):
            continue
        if not (RSI_ENTRY_MIN <= stock.rsi <= RSI_ENTRY_MAX):
            continue
        if REQUIRE_MACD_FRESH_CROSSOVER and not stock.macd_crossover:
            continue
        if MACD_MIN_BARS_ABOVE_SIGNAL > 0 and stock.macd_bars_above_signal < MACD_MIN_BARS_ABOVE_SIGNAL:
            continue
        rvol = _rvol_time_adjusted(b15_window, ts_et)
        if MIN_RVOL > 0 and (rvol is None or rvol < MIN_RVOL):
            continue
        if MAX_RVOL > 0 and rvol and rvol > MAX_RVOL:
            continue

        return dict(entry_time=ts_et, entry_price=price, rsi=stock.rsi,
                     change_pct=stock.change_pct, rvol=rvol, atr=stock.atr,
                     vwap_slope_pct=round(slope_pct, 3) if slope_pct is not None else None,
                     opening_range_pct=round(or_pct, 2) if or_pct is not None else None)

    return None


def simulate_entry_hod(symbol: str, data: dict, d: date) -> dict | None:
    """Same walk-forward shape as simulate_entry_rsi_macd, but re-running
    _detect_hod_breakout() at each minute instead of _analyze(). bars1 is
    already market-open-anchored for the whole day, so bars1[:i+1] (this
    bar as the still-forming last bar) is exactly what the live scan passes
    as bars_1m — no separate fetch needed."""
    bars1, bars5, bars15 = data["bars1"], data["bars5"], data["bars15"]
    prev_close = data["prev_close"]
    stop_buy = _ET.localize(datetime.combine(d, _parse_hm(STOP_BUY_TIME_ET)))

    for i, bar in enumerate(bars1):
        ts_et = bar.timestamp.astimezone(_ET)
        if ts_et >= stop_buy:
            break

        price = bar.close
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0

        # Rolling 120-min window for ATR (mirrors the live "now-120min" fetch)
        window_start = ts_et - timedelta(minutes=120)
        b5_window = [b for b in bars5 if window_start <= b.timestamp.astimezone(_ET) <= ts_et]

        signal = _detect_hod_breakout(
            symbol, bars1[:i + 1], price, change_pct,
            bars_5m=b5_window, consol_bars=HOD_CONSOL_BARS,
            consol_range_pct_max=HOD_CONSOL_RANGE_PCT,
        )
        if not signal:
            continue

        # ── Layer 2: SML2's own gates ──────────────────────────────────────
        if MAX_ENTRY_MOVE_PCT > 0 and signal.change_pct > MAX_ENTRY_MOVE_PCT:
            continue
        if MAX_ATR > 0 and signal.atr and signal.atr > MAX_ATR:
            continue
        if MIN_CHANGE_PCT > 0 and (signal.change_pct is None or signal.change_pct < MIN_CHANGE_PCT):
            continue

        # 3-calendar-day trailing window for RVOL
        b15_window = [b for b in bars15
                      if _b15_start(ts_et) <= b.timestamp.astimezone(_ET) <= ts_et]
        rvol = _rvol_time_adjusted(b15_window, ts_et)
        if MIN_RVOL > 0 and (rvol is None or rvol < MIN_RVOL):
            continue
        if MAX_RVOL > 0 and rvol and rvol > MAX_RVOL:
            continue

        return dict(entry_time=ts_et, entry_price=price, hod=signal.hod,
                     consol_range_pct=signal.consol_range_pct,
                     volume_ratio=signal.volume_ratio,
                     change_pct=signal.change_pct, rvol=rvol, atr=signal.atr)

    return None


def simulate_entry_vwap_reclaim(symbol: str, data: dict, d: date) -> dict | None:
    """Candidate entry strategy from 2026-09-20 research (snappchart VWAP
    playbook 'Setup 1: VWAP Reclaim'): price fades below the session VWAP,
    holds below it for VWAP_RECLAIM_FADE_MIN consecutive 1-min closes (the
    'consolidation'), then a reclaim bar closes back above VWAP on
    >= VWAP_RECLAIM_VOL_MULT x the trailing consolidation's average volume.

    Uses 1-min bars (bars1, already market-open-anchored) for both the VWAP
    calc and the fade/reclaim detection, matching the granularity the real
    setup is described at (vs. the 5-min bars rsi_macd/hod use).
    """
    bars1 = data["bars1"]
    prev_close = data["prev_close"]
    stop_buy = _ET.localize(datetime.combine(d, _parse_hm(STOP_BUY_TIME_ET)))
    start_time = _ET.localize(datetime.combine(d, _parse_hm(START_TIME_ET)))

    total_pv = 0.0
    total_vol = 0.0
    below_streak = 0

    for i, bar in enumerate(bars1):
        ts_et = bar.timestamp.astimezone(_ET)
        if ts_et >= stop_buy:
            break

        tp = (bar.high + bar.low + bar.close) / 3.0
        vwap_before = (total_pv / total_vol) if total_vol else None
        total_pv += tp * bar.volume
        total_vol += bar.volume
        vwap_now = total_pv / total_vol if total_vol else None

        if vwap_before is None or vwap_now is None or ts_et < start_time:
            below_streak = 0
            continue

        reclaimed = bar.close >= vwap_before and below_streak >= VWAP_RECLAIM_FADE_MIN
        if not reclaimed:
            if bar.close < vwap_before:
                below_streak += 1
            else:
                below_streak = 0
            continue

        consol_cutoff = ts_et - timedelta(minutes=VWAP_RECLAIM_CONSOL_LOOKBACK_MIN)
        consol_bars = [b for b in bars1[:i] if b.timestamp.astimezone(_ET) >= consol_cutoff]
        if not consol_bars:
            below_streak = 0
            continue
        consol_avg_vol = sum(b.volume for b in consol_bars) / len(consol_bars)
        volume_ratio = (bar.volume / consol_avg_vol) if consol_avg_vol else 0.0
        if volume_ratio < VWAP_RECLAIM_VOL_MULT:
            below_streak = 0
            continue

        price = bar.close
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0

        # ── Layer 2 gates, kept consistent with the other strategies so
        # results are comparable (not an unfair advantage/disadvantage) ─────
        if MAX_ENTRY_MOVE_PCT > 0 and change_pct > MAX_ENTRY_MOVE_PCT:
            below_streak = 0
            continue
        if MIN_CHANGE_PCT > 0 and change_pct < MIN_CHANGE_PCT:
            below_streak = 0
            continue

        b15_window = [b for b in data["bars15"]
                      if _b15_start(ts_et) <= b.timestamp.astimezone(_ET) <= ts_et]
        rvol = _rvol_time_adjusted(b15_window, ts_et)
        if MIN_RVOL > 0 and (rvol is None or rvol < MIN_RVOL):
            below_streak = 0
            continue
        if MAX_RVOL > 0 and rvol and rvol > MAX_RVOL:
            below_streak = 0
            continue

        return dict(entry_time=ts_et, entry_price=price, vwap=round(vwap_before, 4),
                    fade_minutes=below_streak, volume_ratio=round(volume_ratio, 2),
                    change_pct=change_pct, rvol=rvol, atr=None)

    return None


def simulate_exit(bars1: list, bars5: list, entry_time: datetime, entry_price: float,
                   entry_atr: float | None = None) -> dict:
    """
    bars5 is used only for the RSI-overbought exit, evaluated over the same
    rolling-120-minute window monitor_positions() fetches live. trail_pct is
    None (no resting trailing stop) until either ATR_TRAIL_ACTIVATE_PCT or
    PROFIT_LOCK_PCT engages one — see the "--improved profile" block above
    for why the flat "TRAIL_PCT from minute 1" behavior this replaced didn't
    match what run_sml2_screener.py actually rests at entry (hard stop only).
    """
    high_water = entry_price
    trail_pct: float | None = None
    atr_trail_active = False
    atr_pct = (entry_atr / entry_price * 100) if entry_atr else None
    tightened = False
    hard_stop_pct = HARD_STOP_PCT
    if ATR_HARD_STOP and atr_pct:
        hard_stop_pct = max(ATR_MIN_STOP_PCT, min(ATR_MAX_STOP_PCT, ATR_STOP_MULT * atr_pct))
    hard_stop_price = entry_price * (1 - hard_stop_pct / 100)
    dump_t = _parse_hm(DUMP_TIME_ET)

    # Session-anchored running VWAP per bar (bars1 starts at 09:30 ET), same
    # typical-price formula as bot/market_data.py:vwap_reclaim_exit_price.
    vwap_at: dict[int, float | None] = {}
    total_pv = total_vol = 0.0
    for b in bars1:
        total_pv += (b.high + b.low + b.close) / 3.0 * b.volume
        total_vol += b.volume
        vwap_at[id(b)] = total_pv / total_vol if total_vol else None
    reclaim_streak = 0

    post_entry = [b for b in bars1 if b.timestamp.astimezone(_ET) > entry_time]

    for bar in post_entry:
        ts_et = bar.timestamp.astimezone(_ET)
        held_min = (ts_et - entry_time).total_seconds() / 60

        # ── Resting stop orders: trailing stop (if any is active) trails
        #    high_water BEFORE this bar; hard stop is fixed. Whichever
        #    trigger price is higher gets hit first as price falls. ───────
        stop_price = hard_stop_price
        trail_binding = False
        if trail_pct is not None:
            trail_stop_price = high_water * (1 - trail_pct / 100)
            if trail_stop_price > stop_price:
                stop_price = trail_stop_price
                trail_binding = True
        be_binding = False
        if ATR_TRAIL_BE_FLOOR and atr_trail_active:
            be_price = entry_price * (1 + ATR_TRAIL_BE_OFFSET_PCT / 100)
            if be_price > stop_price:
                stop_price, be_binding = be_price, True
        if bar.low <= stop_price:
            reason = ("Breakeven floor" if be_binding
                      else "Trailing stop" if trail_binding else "Hard stop")
            # A bar that opens through the stop fills near the open, not at the stop.
            fill = min(stop_price, bar.open) if REALISTIC_STOP_FILLS else stop_price
            gain = (fill - entry_price) / entry_price * 100
            return dict(exit_time=ts_et, exit_price=fill, reason=reason,
                        gain_pct=round(gain, 2), held_min=round(held_min))

        # Pessimistic intrabar order: a bar that sets a new high and then
        # sells off by the trail width inside the same minute stops out.
        if REALISTIC_STOP_FILLS and trail_pct is not None and bar.high > high_water:
            intrabar_stop = bar.high * (1 - trail_pct / 100)
            if bar.low <= intrabar_stop and intrabar_stop > hard_stop_price:
                gain = (intrabar_stop - entry_price) / entry_price * 100
                return dict(exit_time=ts_et, exit_price=intrabar_stop, reason="Trailing stop",
                            gain_pct=round(gain, 2), held_min=round(held_min))

        high_water = max(high_water, bar.high)
        price = bar.close
        gain_pct = (price - entry_price) / entry_price * 100

        # ── ATR trail activation (improved mode only) ──────────────────────
        if (ATR_TRAIL_ACTIVATE_PCT > 0 and not atr_trail_active
                and gain_pct >= ATR_TRAIL_ACTIVATE_PCT and atr_pct):
            atr_trail_active = True
            tightened = True  # supersedes the flat profit-lock tighten below
            trail_pct = ATR_TRAIL_MULT * atr_pct

        # ── VWAP-reclaim exit — mirrors live monitor step 2 (runs before the
        #    time checkpoints): N consecutive closes below both VWAP and entry.
        if VWAP_RECLAIM_EXIT_DWELL > 0 and held_min >= VWAP_RECLAIM_EXIT_WARMUP:
            vwap = vwap_at.get(id(bar))
            if vwap is not None and price < vwap and price < entry_price:
                reclaim_streak += 1
                if reclaim_streak >= VWAP_RECLAIM_EXIT_DWELL:
                    return dict(exit_time=ts_et, exit_price=price, reason="VWAP-reclaim exit",
                                gain_pct=round(gain_pct, 2), held_min=round(held_min))
            else:
                reclaim_streak = 0

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

        # ── RSI-overbought exit — mirrors run_sml2_screener.py's monitor
        #    step 5, using the same rolling-120min bars5 window. ───────────
        window_start = ts_et - timedelta(minutes=120)
        b5_seen = _asof(bars5, 5, ts_et, bars1) if NO_LOOKAHEAD else bars5
        b5_window = [b for b in b5_seen if window_start <= b.timestamp.astimezone(_ET) <= ts_et]
        if len(b5_window) >= 20:
            closes = [b.close for b in b5_window]
            rsi_vals = [r for r in _rsi_series(closes) if r is not None]
            if len(rsi_vals) >= 4:
                rsi = rsi_vals[-1]
                rsi_falling = rsi_vals[-1] < rsi_vals[-3]
                if (rsi > RSI_EXIT_LEVEL and rsi_falling
                        and gain_pct >= RSI_EXIT_MIN_GAIN_PCT):
                    return dict(exit_time=ts_et, exit_price=price, reason="RSI overbought exit",
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
    parser.add_argument("--strategy", choices=["hod", "rsi_macd"], default="hod",
                         help="Entry gate to replay — hod (SML2's strategy since 2026-08-20, "
                              "same as SML) or rsi_macd (SML2's strategy through 2026-08-19). "
                              "Default: hod, matching what's live now.")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--providers", nargs="+", default=["sml", "sml2"],
                         help="Which providers' historical trade symbols to source the universe from")
    parser.add_argument("--db", default="stockbot.db")
    parser.add_argument("--improved", action="store_true",
                         help="Apply the 5 proposed SML2 changes (don't-chase entry ceiling, "
                              "ATR trail from +2.5%%, looser 30/60m checkpoints, later "
                              "dump/max-hold, gain-gated RSI exit) instead of current live config.")
    args = parser.parse_args()

    global DONT_CHASE_PCT, ATR_TRAIL_ACTIVATE_PCT, RSI_EXIT_MIN_GAIN_PCT
    global MIN_GAIN_AT_30M, MIN_GAIN_AT_60M, MAX_HOLD_MINUTES, DUMP_TIME_ET
    if args.improved:
        DONT_CHASE_PCT         = 9.0
        ATR_TRAIL_ACTIVATE_PCT = 2.5
        RSI_EXIT_MIN_GAIN_PCT  = 5.0
        MIN_GAIN_AT_30M        = -5.0
        MIN_GAIN_AT_60M        = -3.0
        MAX_HOLD_MINUTES       = 180
        DUMP_TIME_ET           = "15:30"

    profile = "IMPROVED" if args.improved else "baseline (current live config)"
    if args.strategy == "hod":
        simulate_entry = simulate_entry_hod
        print(f"SML2 strategy: hod  [{profile}]\n"
              f"  HOD_CONSOL_BARS={HOD_CONSOL_BARS}  HOD_CONSOL_RANGE_PCT={HOD_CONSOL_RANGE_PCT}%  "
              f"RVOL>={MIN_RVOL}x  MAX_ATR={MAX_ATR or 'off'}  "
              f"move={MIN_CHANGE_PCT}-{MAX_ENTRY_MOVE_PCT or 'off'}%"
              f"{f' (dont-chase<={DONT_CHASE_PCT}%)' if DONT_CHASE_PCT else ''}  |  \n"
              f"  stops: hard={HARD_STOP_PCT}%  "
              f"trail={'off until +' + str(PROFIT_LOCK_PCT) + '% (then ' + str(TIGHT_STOP_PCT) + '%)' if not ATR_TRAIL_ACTIVATE_PCT else f'off until +{ATR_TRAIL_ACTIVATE_PCT}% (then {ATR_TRAIL_MULT}x ATR)'}  \n"
              f"  time: 30m>={MIN_GAIN_AT_30M}% 60m>={MIN_GAIN_AT_60M}% max={MAX_HOLD_MINUTES}m  "
              f"dump={DUMP_TIME_ET} ET\n")
    else:
        simulate_entry = simulate_entry_rsi_macd
        print(f"SML2 strategy: rsi_macd  [{profile}]\n"
              f"  RSI={RSI_ENTRY_MIN}-{RSI_ENTRY_MAX} (effective 60-65, layer-1 caps at 65)  "
              f"MACD_fresh={REQUIRE_MACD_FRESH_CROSSOVER}  RVOL>={MIN_RVOL}x  "
              f"move={MIN_CHANGE_PCT}-{MAX_ENTRY_MOVE_PCT}%"
              f"{f' (dont-chase<={DONT_CHASE_PCT}%)' if DONT_CHASE_PCT else ''}  |  \n"
              f"  stops: hard={HARD_STOP_PCT}%  "
              f"trail={'off until +' + str(PROFIT_LOCK_PCT) + '% (then ' + str(TIGHT_STOP_PCT) + '%)' if not ATR_TRAIL_ACTIVATE_PCT else f'off until +{ATR_TRAIL_ACTIVATE_PCT}% (then {ATR_TRAIL_MULT}x ATR)'}  \n"
              f"  time: 30m>={MIN_GAIN_AT_30M}% 60m>={MIN_GAIN_AT_60M}% max={MAX_HOLD_MINUTES}m  "
              f"RSI exit={RSI_EXIT_LEVEL} falling"
              f"{f' (only if gain>={RSI_EXIT_MIN_GAIN_PCT}%)' if RSI_EXIT_MIN_GAIN_PCT else ''}  "
              f"dump={DUMP_TIME_ET} ET\n")

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
                print(f"  {d} {sym}: no qualifying entry under current SML2 ({args.strategy}) gates")
                results.append(dict(date=d, symbol=sym, entered=False))
                continue
            exit_ = simulate_exit(data["bars1"], data["bars5"], entry["entry_time"],
                                   entry["entry_price"], entry.get("atr"))
            if args.strategy == "hod":
                signal_desc = (f"HOD={entry['hod']:.4f} consol_range={entry['consol_range_pct']:.1f}% "
                                f"vol={entry['volume_ratio']:.1f}x chg={entry['change_pct']:.1f}% "
                                f"rvol={entry['rvol']:.1f}x")
            else:
                signal_desc = (f"RSI={entry['rsi']:.1f} chg={entry['change_pct']:.1f}% "
                                f"rvol={entry['rvol']:.1f}x")
            print(f"  {d} {sym}: ENTER {entry['entry_time'].strftime('%H:%M')} @ ${entry['entry_price']:.4f} "
                  f"({signal_desc})  "
                  f"-> EXIT {exit_['exit_time'].strftime('%H:%M')} @ ${exit_['exit_price']:.4f}  "
                  f"{exit_['gain_pct']:+.1f}%  held={exit_['held_min']}m  ({exit_['reason']})")
            results.append(dict(date=d, symbol=sym, entered=True, **entry, **exit_))

    entered = [r for r in results if r["entered"]]
    print(f"\n{'=' * 70}")
    print(f"{len(entered)}/{len(results)} pairs would have triggered an SML2 ({args.strategy}, {profile}) entry")
    if entered:
        wins = [r for r in entered if r["gain_pct"] >= 0]
        avg = sum(r["gain_pct"] for r in entered) / len(entered)
        print(f"Win rate: {len(wins)}/{len(entered)} ({100*len(wins)/len(entered):.0f}%)")
        print(f"Avg return per trade: {avg:+.2f}%")
        print(f"Best: {max(entered, key=lambda r: r['gain_pct'])['symbol']} "
              f"{max(r['gain_pct'] for r in entered):+.1f}%")
        print(f"Worst: {min(entered, key=lambda r: r['gain_pct'])['symbol']} "
              f"{min(r['gain_pct'] for r in entered):+.1f}%")

    suffix = "_improved" if args.improved else "_baseline"
    out_path = _ROOT / "reports" / f"sml2_backtest_{args.strategy}{suffix}.csv"
    with open(out_path, "w", encoding="utf-8") as f:
        if args.strategy == "hod":
            f.write("date,symbol,entered,entry_time,entry_price,hod,consol_range_pct,volume_ratio,"
                     "change_pct,rvol,exit_time,exit_price,gain_pct,held_min,reason\n")
            for r in results:
                if r["entered"]:
                    f.write(f"{r['date']},{r['symbol']},1,{r['entry_time']},{r['entry_price']},"
                            f"{r['hod']:.4f},{r['consol_range_pct']:.2f},{r['volume_ratio']:.2f},"
                            f"{r['change_pct']:.2f},{r['rvol']:.2f},"
                            f"{r['exit_time']},{r['exit_price']},{r['gain_pct']},{r['held_min']},{r['reason']}\n")
                else:
                    f.write(f"{r['date']},{r['symbol']},0,,,,,,,,,,,,\n")
        else:
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
