"""
backtest_today_wide.py — One-off: replay TODAY against both SML2 entry
strategies (hod, rsi_macd) over a widened candidate universe.

Unlike tools/backtest_sml2.py (which only replays symbols SML/SML2 actually
traded historically), this pulls TODAY's live most-actives/movers pool via
bot.most_active.get_most_active_penny_stocks with max_price raised from the
live $5.00 cap to $50.00, so it can surface candidates the real screener
would have skipped today. Entry/exit simulation logic is copied from
backtest_sml2.py unchanged.

Usage:
    python tools/backtest_today_wide.py
    python tools/backtest_today_wide.py --max-price 50 --min-price 0.50
"""
import argparse
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytz
from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.most_active import get_most_active_penny_stocks

sys.path.insert(0, str(Path(__file__).resolve().parent))
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
_ET = pytz.timezone("America/New_York")

ALPACA_KEY    = os.getenv("SML_ALPACA_API_KEY") or os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.getenv("SML_ALPACA_API_SECRET") or os.environ["ALPACA_API_SECRET"]

STRATEGIES = {
    "hod": simulate_entry_hod,
    "rsi_macd": simulate_entry_rsi_macd,
}


def run_strategy(strategy_name: str, symbols: list[str], client: StockHistoricalDataClient, d: date) -> list[dict]:
    simulate_entry = STRATEGIES[strategy_name]
    results = []
    for sym in symbols:
        data = fetch_symbol_data(client, sym, d)
        if not data:
            print(f"  [{strategy_name}] {sym}: insufficient data, skipped")
            continue
        entry = simulate_entry(sym, data, d)
        if not entry:
            results.append(dict(symbol=sym, entered=False))
            continue
        exit_ = simulate_exit(data["bars1"], data["bars5"], entry["entry_time"],
                               entry["entry_price"], entry.get("atr"))
        if strategy_name == "hod":
            signal_desc = (f"HOD={entry['hod']:.4f} consol_range={entry['consol_range_pct']:.1f}% "
                            f"vol={entry['volume_ratio']:.1f}x chg={entry['change_pct']:.1f}% "
                            f"rvol={entry['rvol']:.1f}x")
        else:
            signal_desc = f"RSI={entry['rsi']:.1f} chg={entry['change_pct']:.1f}% rvol={entry['rvol']:.1f}x"
        print(f"  [{strategy_name}] {sym}: ENTER {entry['entry_time'].strftime('%H:%M')} "
              f"@ ${entry['entry_price']:.4f} ({signal_desc})  "
              f"-> EXIT {exit_['exit_time'].strftime('%H:%M')} @ ${exit_['exit_price']:.4f}  "
              f"{exit_['gain_pct']:+.1f}%  held={exit_['held_min']}m  ({exit_['reason']})")
        results.append(dict(symbol=sym, entered=True, **entry, **exit_))
    return results


def summarize(strategy_name: str, results: list[dict]) -> None:
    entered = [r for r in results if r["entered"]]
    print(f"\n[{strategy_name}] {len(entered)}/{len(results)} candidates would have triggered an entry")
    if entered:
        wins = [r for r in entered if r["gain_pct"] >= 0]
        avg = sum(r["gain_pct"] for r in entered) / len(entered)
        print(f"[{strategy_name}] Win rate: {len(wins)}/{len(entered)} ({100*len(wins)/len(entered):.0f}%)")
        print(f"[{strategy_name}] Avg return per trade: {avg:+.2f}%")
        best = max(entered, key=lambda r: r['gain_pct'])
        worst = min(entered, key=lambda r: r['gain_pct'])
        print(f"[{strategy_name}] Best: {best['symbol']} {best['gain_pct']:+.1f}%")
        print(f"[{strategy_name}] Worst: {worst['symbol']} {worst['gain_pct']:+.1f}%")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-price", type=float, default=0.50)
    parser.add_argument("--max-price", type=float, default=50.00)
    args = parser.parse_args()

    d = date.today()
    print(f"Date: {d}  |  Screener price range widened to ${args.min_price:.2f}-${args.max_price:.2f} "
          f"(live SML/SML2 cap is $0.50-$5.00)\n")

    actives = get_most_active_penny_stocks(ALPACA_KEY, ALPACA_SECRET,
                                            min_price=args.min_price, max_price=args.max_price)
    symbols = [a.symbol for a in actives]
    print(f"Universe: {len(symbols)} symbols from today's most-actives/movers pool "
          f"(${args.min_price:.2f}-${args.max_price:.2f})")
    print(", ".join(symbols[:60]) + (" ..." if len(symbols) > 60 else ""))
    print()

    api_key, api_secret = os.getenv("SML_ALPACA_API_KEY"), os.getenv("SML_ALPACA_API_SECRET")
    client = StockHistoricalDataClient(api_key, api_secret)

    print(f"hod strategy config: HOD_CONSOL_BARS={HOD_CONSOL_BARS} "
          f"HOD_CONSOL_RANGE_PCT={HOD_CONSOL_RANGE_PCT}% RVOL>={MIN_RVOL}x MAX_ATR={MAX_ATR or 'off'} "
          f"move={MIN_CHANGE_PCT}-{MAX_ENTRY_MOVE_PCT or 'off'}%")
    print(f"rsi_macd strategy config: RSI={RSI_ENTRY_MIN}-{RSI_ENTRY_MAX} "
          f"MACD_fresh={REQUIRE_MACD_FRESH_CROSSOVER} RVOL>={MIN_RVOL}x "
          f"move={MIN_CHANGE_PCT}-{MAX_ENTRY_MOVE_PCT or 'off'}%")
    print(f"shared exits: hard={HARD_STOP_PCT}% trail={TRAIL_PCT}%->({TIGHT_STOP_PCT}% @+{PROFIT_LOCK_PCT}%) "
          f"max_hold={MAX_HOLD_MINUTES}m dump={DUMP_TIME_ET} ET\n")

    all_results = {}
    for strategy_name in ("hod", "rsi_macd"):
        print(f"{'='*70}\nStrategy: {strategy_name}\n{'='*70}")
        results = run_strategy(strategy_name, symbols, client, d)
        summarize(strategy_name, results)
        all_results[strategy_name] = results
        print()

        out_path = _ROOT / "reports" / f"today_wide_{strategy_name}.csv"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("symbol,entered,entry_time,entry_price,exit_time,exit_price,gain_pct,held_min,reason\n")
            for r in results:
                if r["entered"]:
                    f.write(f"{r['symbol']},1,{r['entry_time']},{r['entry_price']},"
                            f"{r['exit_time']},{r['exit_price']},{r['gain_pct']},{r['held_min']},{r['reason']}\n")
                else:
                    f.write(f"{r['symbol']},0,,,,,,,\n")
        print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
