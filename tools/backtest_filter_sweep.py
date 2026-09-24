"""
backtest_filter_sweep.py -- Test the four candidate ideas from the
2026-09-20 strategy research (see memory project_strategy_research_2026-09-20)
against SML/SML2's actual trade history, using the same walk-forward
resimulation machinery as backtest_sml2.py/backtest_symbols.py:

  1. VWAP slope filter       -- require rising VWAP for entries (new gate,
                                 added to simulate_entry_rsi_macd, off by
                                 default via VWAP_SLOPE_MIN_PCT).
  2. Opening-range-width gate -- reject entries where the first N minutes'
                                 high-low range is too wide (new gate,
                                 OPENING_RANGE_MAX_PCT).
  3. MAX_RVOL sweep           -- the filter already exists in code
                                 (commented out live at SML_MAX_RVOL=6); this
                                 sweeps candidate cap values against the
                                 rsi_macd baseline.
  4. VWAP-reclaim entry       -- an entirely separate entry signal
                                 (simulate_entry_vwap_reclaim), run head to
                                 head against the rsi_macd baseline on the
                                 same (day, symbol) universe.

For (1) and (2), also runs one "diagnostic" baseline pass that buckets every
baseline entry by its vwap_slope_pct / opening_range_pct value (computed but
not gated) to show the raw win-rate/avg-return correlation before committing
to a specific threshold -- same method as the existing RVOL leak analysis in
todo.md's 2026-08-14 review.

Universe: every (day, symbol) pair either SML or SML2 actually traded, same
as backtest_sml2.py --days. Re-running each config replays the rsi_macd
entry gate independently at every 5-min bar of that day for that symbol, so
a tighter gate can shift the entry to a later bar (or drop it entirely) --
this is a full resimulation, not a filter applied after the fact.

Usage:
    python tools/backtest_filter_sweep.py --days 95
    python tools/backtest_filter_sweep.py --days 95 --db pi_data/stockbot.db
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import os
from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient

import backtest_sml2 as bt  # noqa: E402

load_dotenv()
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent


def run_rsi_macd(symbols_by_day, client, overrides: dict, use_cache=True) -> list[dict]:
    """Run simulate_entry_rsi_macd + simulate_exit across the whole universe
    under the given module-attribute overrides (reset after)."""
    saved = {k: getattr(bt, k) for k in overrides}
    for k, v in overrides.items():
        setattr(bt, k, v)
    try:
        results = []
        for d in sorted(symbols_by_day):
            for sym in sorted(symbols_by_day[d]):
                data = bt.fetch_symbol_data(client, sym, d, use_cache=use_cache)
                if not data:
                    continue
                entry = bt.simulate_entry_rsi_macd(sym, data, d)
                if not entry:
                    results.append(dict(date=d, symbol=sym, entered=False))
                    continue
                exit_ = bt.simulate_exit(data["bars1"], data["bars5"], entry["entry_time"],
                                          entry["entry_price"], entry.get("atr"))
                results.append(dict(date=d, symbol=sym, entered=True, **entry, **exit_))
        return results
    finally:
        for k, v in saved.items():
            setattr(bt, k, v)


def run_vwap_reclaim(symbols_by_day, client, use_cache=True) -> list[dict]:
    results = []
    for d in sorted(symbols_by_day):
        for sym in sorted(symbols_by_day[d]):
            data = bt.fetch_symbol_data(client, sym, d, use_cache=use_cache)
            if not data:
                continue
            entry = bt.simulate_entry_vwap_reclaim(sym, data, d)
            if not entry:
                results.append(dict(date=d, symbol=sym, entered=False))
                continue
            exit_ = bt.simulate_exit(data["bars1"], data["bars5"], entry["entry_time"],
                                      entry["entry_price"], entry.get("atr"))
            results.append(dict(date=d, symbol=sym, entered=True, **entry, **exit_))
    return results


def summarize(label: str, results: list[dict]) -> dict:
    entered = [r for r in results if r["entered"]]
    n = len(entered)
    if n == 0:
        print(f"  {label:<34} 0 entries")
        return dict(label=label, n=0, win_rate=None, avg_pct=None, total_pct=None)
    wins = [r for r in entered if r["gain_pct"] >= 0]
    avg = sum(r["gain_pct"] for r in entered) / n
    total = sum(r["gain_pct"] for r in entered)
    win_rate = 100 * len(wins) / n
    print(f"  {label:<34} n={n:<4} win={win_rate:5.1f}%  avg={avg:+6.2f}%  total={total:+7.1f}%")
    return dict(label=label, n=n, win_rate=win_rate, avg_pct=avg, total_pct=total)


def bucket_diagnostic(results: list[dict], field: str, buckets: list[tuple[float, float, str]]):
    entered = [r for r in results if r["entered"] and r.get(field) is not None]
    print(f"\n  Baseline entries bucketed by {field} ({len(entered)}/{len([r for r in results if r['entered']])} have a value):")
    for lo, hi, label in buckets:
        bucket = [r for r in entered if lo <= r[field] < hi]
        if not bucket:
            print(f"    {label:<12} n=0")
            continue
        wins = [r for r in bucket if r["gain_pct"] >= 0]
        avg = sum(r["gain_pct"] for r in bucket) / len(bucket)
        print(f"    {label:<12} n={len(bucket):<3} win={100*len(wins)/len(bucket):5.1f}%  avg={avg:+6.2f}%")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=95)
    parser.add_argument("--providers", nargs="+", default=["sml", "sml2"])
    parser.add_argument("--db", default="pi_data/stockbot.db")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--stop-buy-time-et", default=None,
                         help="Override bt.STOP_BUY_TIME_ET (hardcoded '11:45', SML2's cutoff) for "
                              "the whole sweep. SML ran all-day ('off' cutoff) before 2026-09-05, so "
                              "the default 11:45 systematically excludes SML's afternoon entries from "
                              "most of this window. Pass e.g. '15:00' to remove that bias uniformly "
                              "across baseline + every config (bar cache is unaffected, no refetch).")
    parser.add_argument("--dump-time-et", default=None,
                         help="Override bt.DUMP_TIME_ET (default reads shared .env DUMP_TIME_ET, "
                              "currently '12:00'). Pairs with --stop-buy-time-et: a late entry "
                              "allowed by a widened buy cutoff will otherwise force-exit almost "
                              "immediately at the old 12:00 dump time. Pass e.g. '15:30' to match "
                              "SML2's current live SML2_DUMP_TIME_ET.")
    args = parser.parse_args()
    use_cache = not args.no_cache
    if args.stop_buy_time_et:
        bt.STOP_BUY_TIME_ET = args.stop_buy_time_et
    if args.dump_time_et:
        bt.DUMP_TIME_ET = args.dump_time_et

    db_path = (_ROOT / args.db) if not Path(args.db).is_absolute() else Path(args.db)
    by_day = bt.find_traded_symbols_by_day(db_path, args.providers, args.days)
    total_pairs = sum(len(v) for v in by_day.values())
    print(f"Universe: {len(by_day)} trading days, {total_pairs} (day, symbol) pairs "
          f"(from {'+'.join(args.providers)} actual trades, last {args.days}d)")
    print(f"Entry cutoff (STOP_BUY_TIME_ET): {bt.STOP_BUY_TIME_ET}  |  Dump time (DUMP_TIME_ET): {bt.DUMP_TIME_ET}\n")

    api_key = os.getenv("SML_ALPACA_API_KEY")
    api_secret = os.getenv("SML_ALPACA_API_SECRET")
    client = StockHistoricalDataClient(api_key, api_secret)

    print("=" * 100)
    print("BASELINE: rsi_macd, all candidate filters off")
    print("=" * 100)
    baseline = run_rsi_macd(by_day, client, {}, use_cache=use_cache)
    summarize("baseline (current live gates)", baseline)

    bucket_diagnostic(baseline, "opening_range_pct", [
        (0, 5, "<5%"), (5, 10, "5-10%"), (10, 15, "10-15%"),
        (15, 20, "15-20%"), (20, 999, ">=20%"),
    ])
    bucket_diagnostic(baseline, "vwap_slope_pct", [
        (-999, -0.1, "falling"), (-0.1, 0.0, "~flat-"), (0.0, 0.1, "~flat+"),
        (0.1, 0.3, "rising"), (0.3, 999, "rising fast"),
    ])
    bucket_diagnostic(baseline, "rvol", [
        (0, 1.5, "<1.5x"), (1.5, 3, "1.5-3x"), (3, 5, "3-5x"),
        (5, 7, "5-7x"), (7, 10, "7-10x"), (10, 999, ">=10x"),
    ])

    print("\n" + "=" * 100)
    print("1. VWAP SLOPE FILTER SWEEP (require rising VWAP over trailing 15min)")
    print("=" * 100)
    for min_pct in (0.0, 0.05, 0.1, 0.2):
        label = "off (baseline)" if min_pct == 0.0 else f"VWAP_SLOPE_MIN_PCT={min_pct}"
        if min_pct == 0.0:
            summarize(label, baseline)
            continue
        res = run_rsi_macd(by_day, client, {"VWAP_SLOPE_MIN_PCT": min_pct}, use_cache=use_cache)
        summarize(label, res)

    print("\n" + "=" * 100)
    print("2. OPENING-RANGE-WIDTH GATE SWEEP (first 15min high-low range)")
    print("=" * 100)
    for max_pct in (0.0, 20.0, 15.0, 10.0, 7.0):
        label = "off (baseline)" if max_pct == 0.0 else f"OPENING_RANGE_MAX_PCT={max_pct}"
        if max_pct == 0.0:
            summarize(label, baseline)
            continue
        res = run_rsi_macd(by_day, client, {"OPENING_RANGE_MAX_PCT": max_pct}, use_cache=use_cache)
        summarize(label, res)

    print("\n" + "=" * 100)
    print("3. MAX_RVOL CAP SWEEP (existing filter, currently disabled live)")
    print("=" * 100)
    for max_rvol in (0.0, 10.0, 8.0, 6.0, 5.0, 4.0, 3.0):
        label = "off (baseline)" if max_rvol == 0.0 else f"MAX_RVOL={max_rvol}"
        if max_rvol == 0.0:
            summarize(label, baseline)
            continue
        res = run_rsi_macd(by_day, client, {"MAX_RVOL": max_rvol}, use_cache=use_cache)
        summarize(label, res)

    print("\n" + "=" * 100)
    print("4. VWAP-RECLAIM ENTRY (separate strategy, vs. rsi_macd baseline)")
    print("=" * 100)
    reclaim = run_vwap_reclaim(by_day, client, use_cache=use_cache)
    summarize("rsi_macd baseline", baseline)
    summarize("vwap_reclaim", reclaim)
    fired = [r for r in reclaim if r["entered"]]
    if fired:
        print("\n  vwap_reclaim entries (all):")
        for r in sorted(fired, key=lambda r: (r["date"], r["symbol"])):
            print(f"    {r['date']} {r['symbol']:<6} {r['entry_time'].strftime('%H:%M')} "
                  f"@ ${r['entry_price']:.4f}  fade={r['fade_minutes']}m  vol={r['volume_ratio']:.1f}x  "
                  f"-> {r['gain_pct']:+.2f}%  ({r['reason']})")

    out_path = _ROOT / "reports" / "filter_sweep_baseline.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("date,symbol,entered,entry_time,entry_price,rsi,change_pct,rvol,"
                "vwap_slope_pct,opening_range_pct,exit_time,exit_price,gain_pct,held_min,reason\n")
        for r in baseline:
            if r["entered"]:
                f.write(f"{r['date']},{r['symbol']},1,{r['entry_time']},{r['entry_price']},"
                        f"{r['rsi']:.2f},{r['change_pct']:.2f},{r['rvol']:.2f},"
                        f"{r.get('vwap_slope_pct')},{r.get('opening_range_pct')},"
                        f"{r['exit_time']},{r['exit_price']},{r['gain_pct']},{r['held_min']},{r['reason']}\n")
            else:
                f.write(f"{r['date']},{r['symbol']},0,,,,,,,,,,,,\n")
    print(f"\nBaseline detail saved to {out_path}")


if __name__ == "__main__":
    main()
