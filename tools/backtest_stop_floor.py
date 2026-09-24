"""
backtest_stop_floor.py -- Sweep ATR_MIN_STOP_PCT (the floor under SML2's
ATR-sized hard stop, live default 2.0%) under SML2's current live exit
profile, to test whether low-ATR names are being noise-stopped.

Live stop = clamp(ATR_STOP_MULT x ATR%, ATR_MIN_STOP_PCT, ATR_MAX_STOP_PCT)
and size = min(BUY_AMOUNT x HARD_STOP_PCT / stop%, available cash) -- see
run_sml2_screener.py:_size_by_risk. At $250 x 5% risk, the risk-sized amount
is $625 at a 2% stop and $417 at 3%, both above the ~$350 cash actually
available, so a floor up to ~3.5% changes the stop but not the size. $ P&L
below models that cap (CASH_CAP) so a higher floor's smaller position shows.

Two views, both under the live SML2 exit profile (ATR trail from +2.5%,
30m>=-5% / 60m>=-3% checkpoints, 180m max hold, 15:30 dump, RSI exit gated
at +5%, VWAP-reclaim dwell=9, 17% entry-move cap):
  1. resim  -- walk-forward rsi_macd entry resimulation over every (day,
               symbol) SML/SML2 actually traded (same universe as
               backtest_filter_sweep.py).
  2. replay -- every real closed SML/SML2 trade, at its real entry time,
               price and ATR, re-exited under each floor.

Usage:
    python tools/backtest_stop_floor.py --days 95
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import os
from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient

import backtest_sml2 as bt  # noqa: E402
from backtest_filter_sweep import run_rsi_macd  # noqa: E402

load_dotenv()
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent

LIVE_SML2 = dict(
    ATR_TRAIL_ACTIVATE_PCT=2.5, ATR_TRAIL_MULT=1.5, RSI_EXIT_MIN_GAIN_PCT=5.0,
    MIN_GAIN_AT_30M=-5.0, MIN_GAIN_AT_60M=-3.0, MAX_HOLD_MINUTES=180,
    DUMP_TIME_ET="15:30", DONT_CHASE_PCT=0.0, MAX_ENTRY_MOVE_PCT=17.0,
    ATR_HARD_STOP=True, VWAP_RECLAIM_EXIT_DWELL=9, VWAP_RECLAIM_EXIT_WARMUP=3,
    # Fidelity fixes found 2026-09-23 (see backtest_sml2.py for each):
    NO_LOOKAHEAD=True, REALISTIC_STOP_FILLS=True, LIVE_RVOL_LOOKBACK=True,
    REQUIRE_MACD_FRESH_CROSSOVER=False, MACD_MIN_BARS_ABOVE_SIGNAL=3,
    ENTRY_START_TIME_ET="09:45",
)
FLOORS = [2.0, 2.5, 3.0, 3.5, 4.0]
RISK_DOLLARS = 250 * 5 / 100   # BUY_AMOUNT_USD x HARD_STOP_PCT
CASH_CAP = 350.0               # typical available cash per position on the ~$480-510 account


def stop_pct_for(entry_price, atr, floor):
    if not atr:
        return bt.HARD_STOP_PCT
    return max(floor, min(bt.ATR_MAX_STOP_PCT, bt.ATR_STOP_MULT * atr / entry_price * 100))


def dollars(r, floor):
    notional = min(RISK_DOLLARS * 100 / stop_pct_for(r["entry_price"], r.get("atr"), floor), CASH_CAP)
    return notional * r["gain_pct"] / 100


def summarize(label, rows, floor):
    n = len(rows)
    if not n:
        print(f"  {label:<14} n=0")
        return
    wins = sum(r["gain_pct"] >= 0 for r in rows)
    tot = sum(r["gain_pct"] for r in rows)
    stops = sum(r["reason"] == "Hard stop" for r in rows)
    usd = sum(dollars(r, floor) for r in rows)
    print(f"  {label:<14} n={n:<3} win={100*wins/n:5.1f}%  avg={tot/n:+6.2f}%  total={tot:+7.1f}%  "
          f"hard-stops={stops:<3} $={usd:+8.2f}")


def diff_vs_base(base, other, floor, key):
    b = {key(r): r for r in base}
    changed = []
    for r in other:
        o = b.get(key(r))
        if o and (o["reason"] != r["reason"] or abs(o["gain_pct"] - r["gain_pct"]) > 0.01):
            changed.append((key(r), o, r))
    for k, o, r in changed:
        print(f"      {k[0]} {k[1]:<5} stop {stop_pct_for(r['entry_price'], r.get('atr'), 2.0):.1f}%"
              f"->{stop_pct_for(r['entry_price'], r.get('atr'), floor):.1f}%  "
              f"{o['reason']} {o['gain_pct']:+.1f}%  ->  {r['reason']} {r['gain_pct']:+.1f}% "
              f"(held {r['held_min']}m)")
    if not changed:
        print("      (no trade changed outcome)")


def replay_real(db_path, client, days, floor):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "select provider, symbol, buy_time, buy_price, atr_at_entry from positions "
        "where status='closed' and provider in ('SML_SCREENER','SML2_SCREENER') "
        "and date(buy_time) >= date('now', ?) order by buy_time", (f"-{days} days",)).fetchall()
    saved = {k: getattr(bt, k) for k in LIVE_SML2}
    saved["ATR_MIN_STOP_PCT"] = bt.ATR_MIN_STOP_PCT
    for k, v in LIVE_SML2.items():
        setattr(bt, k, v)
    bt.ATR_MIN_STOP_PCT = floor
    out = []
    try:
        for prov, sym, buy_time, price, atr in rows:
            et = datetime.fromisoformat(buy_time).astimezone(bt._ET)
            data = bt.fetch_symbol_data(client, sym, et.date())
            if not data:
                continue
            ex = bt.simulate_exit(data["bars1"], data["bars5"], et, price, atr)
            out.append(dict(date=et.date(), symbol=sym, provider=prov, entry_time=et, entry_price=price,
                            atr=atr, **ex))
    finally:
        for k, v in saved.items():
            setattr(bt, k, v)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=95)
    ap.add_argument("--providers", nargs="+", default=["sml", "sml2"])
    ap.add_argument("--db", default="pi_data/stockbot.db")
    args = ap.parse_args()

    db_path = _ROOT / args.db
    client = StockHistoricalDataClient(os.getenv("SML_ALPACA_API_KEY"), os.getenv("SML_ALPACA_API_SECRET"))
    by_day = bt.find_traded_symbols_by_day(db_path, args.providers, args.days)
    print(f"Universe: {len(by_day)} days, {sum(len(v) for v in by_day.values())} (day, symbol) pairs\n")

    print("1) Walk-forward resim, live SML2 exit profile")
    resim = {}
    for f in FLOORS:
        res = run_rsi_macd(by_day, client, {**LIVE_SML2, "ATR_MIN_STOP_PCT": f})
        resim[f] = [r for r in res if r["entered"]]
        summarize(f"floor {f:.1f}%", resim[f], f)
    for f in FLOORS[1:]:
        print(f"\n    changed vs 2.0% at floor {f:.1f}%:")
        diff_vs_base(resim[2.0], resim[f], f, key=lambda r: (r["date"], r["symbol"]))

    print("\n2) Real SML/SML2 trades replayed at their actual entries, live SML2 exit profile")
    real = {}
    for f in FLOORS:
        real[f] = replay_real(db_path, client, args.days, f)
        summarize(f"floor {f:.1f}%", real[f], f)
    for f in FLOORS[1:]:
        print(f"\n    changed vs 2.0% at floor {f:.1f}%:")
        diff_vs_base(real[2.0], real[f], f, key=lambda r: (r["date"], r["symbol"], r["provider"]))


if __name__ == "__main__":
    main()
