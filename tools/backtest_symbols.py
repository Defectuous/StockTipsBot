"""
backtest_symbols.py — Replay SML2's entry/exit logic against an explicit
symbol list (e.g. ETFs like YMAG/YMAX) over the last N calendar days,
regardless of whether the live screener's price band would ever have
surfaced them.

Unlike tools/backtest_sml2.py (sources its (day, symbol) universe from past
trades in stockbot.db) and tools/backtest_today_wide.py (sources today's
live most-actives pool), this takes --symbols directly and walks every
weekday in the lookback window — so it works for tickers the bot never
actually traded, including ones outside SML2's $0.50-$5.00 price cap.

Entry/exit simulation logic is unchanged, reused from backtest_sml2.py.

Usage:
    python tools/backtest_symbols.py --symbols YMAG YMAX --days 60
    python tools/backtest_symbols.py --symbols YMAG YMAX --strategy rsi_macd --days 60
"""
import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_sml2 as bt  # noqa: E402
from backtest_sml2 import (  # noqa: E402
    fetch_symbol_data, simulate_entry_hod, simulate_entry_rsi_macd, simulate_exit,
    HOD_CONSOL_BARS, HOD_CONSOL_RANGE_PCT, MIN_RVOL, MAX_ATR, MIN_CHANGE_PCT,
    MAX_ENTRY_MOVE_PCT, RSI_ENTRY_MIN, RSI_ENTRY_MAX, REQUIRE_MACD_FRESH_CROSSOVER,
    HARD_STOP_PCT, TRAIL_PCT, TIGHT_STOP_PCT, PROFIT_LOCK_PCT, MAX_HOLD_MINUTES,
    MIN_GAIN_AT_30M, MIN_GAIN_AT_60M, DUMP_TIME_ET,
)

load_dotenv()
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent

STRATEGIES = {
    "hod": simulate_entry_hod,
    "rsi_macd": simulate_entry_rsi_macd,
}


def weekdays_back(days: int) -> list[date]:
    out, d = [], date.today() - timedelta(days=1)
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def run_strategy(strategy_name: str, symbols: list[str], days: list[date],
                  client: StockHistoricalDataClient, use_cache: bool = True) -> list[dict]:
    simulate_entry = STRATEGIES[strategy_name]
    results = []
    for sym in symbols:
        for d in days:
            data = fetch_symbol_data(client, sym, d, use_cache=use_cache)
            if not data:
                continue
            entry = simulate_entry(sym, data, d)
            if not entry:
                results.append(dict(date=d, symbol=sym, entered=False))
                continue
            exit_ = simulate_exit(data["bars1"], data["bars5"], entry["entry_time"],
                                   entry["entry_price"], entry.get("atr"))
            if strategy_name == "hod":
                signal_desc = (f"HOD={entry['hod']:.4f} consol_range={entry['consol_range_pct']:.1f}% "
                                f"vol={entry['volume_ratio']:.1f}x chg={entry['change_pct']:.1f}% "
                                f"rvol={entry['rvol']:.1f}x")
            else:
                signal_desc = f"RSI={entry['rsi']:.1f} chg={entry['change_pct']:.1f}% rvol={entry['rvol']:.1f}x"
            print(f"  [{strategy_name}] {d} {sym}: ENTER {entry['entry_time'].strftime('%H:%M')} "
                  f"@ ${entry['entry_price']:.4f} ({signal_desc})  "
                  f"-> EXIT {exit_['exit_time'].strftime('%H:%M')} @ ${exit_['exit_price']:.4f}  "
                  f"{exit_['gain_pct']:+.1f}%  held={exit_['held_min']}m  ({exit_['reason']})")
            results.append(dict(date=d, symbol=sym, entered=True, **entry, **exit_))
    return results


def summarize(strategy_name: str, results: list[dict]) -> None:
    entered = [r for r in results if r["entered"]]
    print(f"\n[{strategy_name}] {len(entered)}/{len(results)} (day, symbol) pairs triggered an entry")
    if entered:
        wins = [r for r in entered if r["gain_pct"] >= 0]
        avg = sum(r["gain_pct"] for r in entered) / len(entered)
        print(f"[{strategy_name}] Win rate: {len(wins)}/{len(entered)} ({100*len(wins)/len(entered):.0f}%)")
        print(f"[{strategy_name}] Avg return per trade: {avg:+.2f}%")
        best = max(entered, key=lambda r: r["gain_pct"])
        worst = min(entered, key=lambda r: r["gain_pct"])
        print(f"[{strategy_name}] Best: {best['symbol']} {best['date']} {best['gain_pct']:+.1f}%")
        print(f"[{strategy_name}] Worst: {worst['symbol']} {worst['date']} {worst['gain_pct']:+.1f}%")
    else:
        print(f"[{strategy_name}] No entries — symbols never met the entry gate in this window "
              f"(check RVOL/change_pct/ATR: low-volatility income ETFs like YMAG/YMAX rarely will).")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--strategy", choices=["hod", "rsi_macd", "both"], default="both")
    parser.add_argument("--days", type=int, default=60, help="lookback window in weekdays")
    parser.add_argument("--improved", action="store_true",
                         help="Shorthand for --profile improved.")
    parser.add_argument("--profile", choices=["baseline", "entry_only", "exits_only", "improved"],
                         default=None,
                         help="baseline: current live config. entry_only: just the don't-chase "
                              "entry ceiling (change #1). exits_only: the 4 exit-side changes "
                              "(ATR trail, looser checkpoints, later dump/max-hold, gain-gated "
                              "RSI exit — changes #2-5). improved: all 5 bundled. Default: baseline.")
    parser.add_argument("--no-cache", action="store_true",
                         help="Force a live refetch instead of reusing cached bar data.")
    args = parser.parse_args()

    profile_name = args.profile or ("improved" if args.improved else "baseline")

    ENTRY_ONLY = dict(DONT_CHASE_PCT=9.0)
    EXITS_ONLY = dict(ATR_TRAIL_ACTIVATE_PCT=2.5, RSI_EXIT_MIN_GAIN_PCT=5.0,
                       MIN_GAIN_AT_30M=-5.0, MIN_GAIN_AT_60M=-3.0,
                       MAX_HOLD_MINUTES=180, DUMP_TIME_ET="15:30")
    IMPROVED = {**ENTRY_ONLY, **EXITS_ONLY}
    PROFILES = {"baseline": {}, "entry_only": ENTRY_ONLY, "exits_only": EXITS_ONLY, "improved": IMPROVED}
    for k, v in PROFILES[profile_name].items():
        setattr(bt, k, v)
    profile = profile_name.upper() if profile_name != "baseline" else "baseline (current live config)"

    days = weekdays_back(args.days)
    print(f"Profile: {profile}")
    print(f"Symbols: {', '.join(args.symbols)}  |  Window: {days[0]} to {days[-1]} ({len(days)} weekdays)\n")
    print("NOTE: this bypasses SML/SML2's live price-band gate ($0.50-$5.00 / $2-$20 / $2-$50) — "
          "it only replays the RSI/MACD/VWAP or HOD-breakout entry logic and stop/exit rules. "
          "A symbol trading outside that band would never actually be scanned live.\n")

    api_key = os.getenv("SML_ALPACA_API_KEY") or os.environ["ALPACA_API_KEY"]
    api_secret = os.getenv("SML_ALPACA_API_SECRET") or os.environ["ALPACA_API_SECRET"]
    client = StockHistoricalDataClient(api_key, api_secret)

    strategies = ["hod", "rsi_macd"] if args.strategy == "both" else [args.strategy]
    for strategy_name in strategies:
        results = run_strategy(strategy_name, args.symbols, days, client, use_cache=not args.no_cache)
        summarize(strategy_name, results)

        out_path = _ROOT / "reports" / f"backtest_symbols_{strategy_name}_{profile_name}.csv"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("date,symbol,entered,entry_time,entry_price,gain_pct,held_min,reason\n")
            for r in results:
                if r["entered"]:
                    f.write(f"{r['date']},{r['symbol']},1,{r['entry_time']},{r['entry_price']},"
                            f"{r['gain_pct']},{r['held_min']},{r['reason']}\n")
                else:
                    f.write(f"{r['date']},{r['symbol']},0,,,,,\n")
        print(f"Saved {out_path}")
        print()


if __name__ == "__main__":
    main()
