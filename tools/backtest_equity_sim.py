"""
backtest_equity_sim.py -- Walk a filter-sweep entry list in chronological
order as an actual $-equity simulation, instead of the flat weekly-rate
guesswork: real 2-slot position cap (skips a signal if both slots are
already open), real ATR-based position sizing (mirrors
run_sml2_screener.py's _compute_buy_amount()/_size_by_risk() exactly),
and reinvestment of realized P&L into the next trade's sizing.

Position sizing per trade (same formula as live):
    day_start_balance   ~= cash + cost-basis of currently open positions
                            (approximates the live "balance snapshotted at
                            start of day" without a real day-boundary loop --
                            fine for weekly reporting, not for justifying an
                            exact day-start balance figure).
    base_amount          = day_start_balance * (1 - RESERVE_PCT/100) / MAX_POSITIONS
    stop_pct              = clamp(ATR_STOP_MULT * atr/price * 100, ATR_MIN_STOP_PCT, ATR_MAX_STOP_PCT)
    sized_amount          = base_amount * HARD_STOP_PCT / stop_pct   (falls back to
                            base_amount when atr is missing, same as live)
    position cost          = min(sized_amount, cash available)

Runs the same MAX_RVOL=4.0 config the filter sweep identified as the best
candidate, alongside the unfiltered baseline, on the same widened
(15:00 buy cutoff / 15:30 dump) universe so the two are directly comparable.

Usage:
    python tools/backtest_equity_sim.py --days 95 --start-balance 500
"""
import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient

import backtest_sml2 as bt  # noqa: E402
from backtest_filter_sweep import run_rsi_macd  # noqa: E402

load_dotenv()
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent

MAX_POSITIONS    = int(os.getenv("MAX_POSITIONS", "2"))
RESERVE_PCT      = float(os.getenv("RESERVE_PCT", "25"))
ATR_STOP_MULT    = float(os.getenv("ATR_STOP_MULT", "2.0"))
ATR_MIN_STOP_PCT = float(os.getenv("ATR_MIN_STOP_PCT", "2.0"))
ATR_MAX_STOP_PCT = float(os.getenv("ATR_MAX_STOP_PCT", "10.0"))


def _position_size(day_start_balance: float, cash: float, price: float, atr: float | None) -> float:
    base_amount = day_start_balance * (1 - RESERVE_PCT / 100) / MAX_POSITIONS
    if atr and price > 0 and bt.HARD_STOP_PCT > 0:
        raw_stop_pct = ATR_STOP_MULT * atr / price * 100
        stop_pct = max(ATR_MIN_STOP_PCT, min(ATR_MAX_STOP_PCT, raw_stop_pct))
        sized = base_amount * bt.HARD_STOP_PCT / stop_pct
    else:
        sized = base_amount
    return max(0.0, min(sized, cash))


def simulate_equity(entries: list[dict], start_balance: float) -> dict:
    trades = sorted([e for e in entries if e["entered"]], key=lambda e: e["entry_time"])
    cash = start_balance
    open_positions: list[dict] = []  # {'exit_time', 'cost', 'exit_value', 'symbol'}
    log = []
    skipped_no_slot = 0

    def release_through(t):
        nonlocal cash
        still_open = []
        for p in open_positions:
            if p["exit_time"] <= t:
                cash += p["exit_value"]
                log.append(dict(event="exit", time=p["exit_time"], symbol=p["symbol"],
                                 cash_after=cash, pnl=p["exit_value"] - p["cost"]))
            else:
                still_open.append(p)
        open_positions[:] = still_open

    for e in trades:
        release_through(e["entry_time"])
        if len(open_positions) >= MAX_POSITIONS:
            skipped_no_slot += 1
            continue
        day_start_balance = cash + sum(p["cost"] for p in open_positions)
        cost = _position_size(day_start_balance, cash, e["entry_price"], e.get("atr"))
        if cost < 1.0:
            continue
        cash -= cost
        exit_value = cost * (1 + e["gain_pct"] / 100)
        open_positions.append(dict(exit_time=e["exit_time"], cost=cost, exit_value=exit_value,
                                    symbol=e["symbol"]))
        log.append(dict(event="entry", time=e["entry_time"], symbol=e["symbol"],
                         cost=round(cost, 2), day_start_balance=round(day_start_balance, 2),
                         gain_pct=e["gain_pct"], cash_after=round(cash, 2)))

    # release anything still open at the very end
    if open_positions:
        release_through(max(p["exit_time"] for p in open_positions) + timedelta(days=1))

    final_equity = cash
    return dict(log=log, final_equity=final_equity, trades_taken=len([r for r in log if r["event"] == "entry"]),
                skipped_no_slot=skipped_no_slot, total_signals=len(trades))


def print_weekly(label: str, sim: dict, start_balance: float):
    print(f"\n{'=' * 90}\n{label}\n{'=' * 90}")
    print(f"  Signals: {sim['total_signals']}  |  Taken: {sim['trades_taken']}  |  "
          f"Skipped (both slots full): {sim['skipped_no_slot']}")

    entries = [r for r in sim["log"] if r["event"] == "entry"]
    exits = [r for r in sim["log"] if r["event"] == "exit"]
    print(f"\n  {'Date/Time':<18}{'Sym':<7}{'Cost':>9}{'Gain%':>8}{'Cash after exit':>18}")
    # interleave for a readable trade tape, sorted by time
    tape = sorted(sim["log"], key=lambda r: r["time"])
    for r in tape:
        if r["event"] == "entry":
            print(f"  {r['time'].strftime('%Y-%m-%d %H:%M'):<18}{r['symbol']:<7}"
                  f"${r['cost']:>7.2f}  ENTER (day_start_bal=${r['day_start_balance']:.2f})")
        else:
            print(f"  {r['time'].strftime('%Y-%m-%d %H:%M'):<18}{r['symbol']:<7}"
                  f"{'':>9}{'':>8}  EXIT pnl=${r['pnl']:+.2f}  cash=${r['cash_after']:.2f}")

    # weekly buckets from exit events (realized P&L lands in cash on exit)
    weekly: dict = {}
    running_cash = start_balance
    for r in tape:
        if r["event"] == "exit":
            running_cash = r["cash_after"]
        wk = r["time"].isocalendar()[:2]  # (iso_year, iso_week)
        weekly[wk] = running_cash

    print(f"\n  {'ISO week':<12}{'Equity at week end':>20}")
    prev = start_balance
    for wk in sorted(weekly):
        eq = weekly[wk]
        delta = eq - prev
        pct = delta / prev * 100 if prev else 0
        print(f"  {wk[0]}-W{wk[1]:<8}{eq:>15.2f}   ({delta:+.2f}, {pct:+.1f}%)")
        prev = eq

    total_gain = sim["final_equity"] - start_balance
    n_weeks = max(1, len(weekly))
    print(f"\n  Start: ${start_balance:.2f}  ->  Final: ${sim['final_equity']:.2f}  "
          f"({total_gain:+.2f}, {total_gain/start_balance*100:+.1f}% over ~{n_weeks} active weeks)")
    print(f"  Avg $/active-week: ${total_gain/n_weeks:+.2f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=95)
    parser.add_argument("--providers", nargs="+", default=["sml", "sml2"])
    parser.add_argument("--db", default="pi_data/stockbot.db")
    parser.add_argument("--start-balance", type=float, default=500.0)
    parser.add_argument("--stop-buy-time-et", default="15:00")
    parser.add_argument("--dump-time-et", default="15:30")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    use_cache = not args.no_cache

    bt.STOP_BUY_TIME_ET = args.stop_buy_time_et
    bt.DUMP_TIME_ET = args.dump_time_et

    db_path = (_ROOT / args.db) if not Path(args.db).is_absolute() else Path(args.db)
    by_day = bt.find_traded_symbols_by_day(db_path, args.providers, args.days)
    print(f"Universe: {len(by_day)} trading days (from {'+'.join(args.providers)} actual trades, "
          f"last {args.days}d)  |  buy cutoff={bt.STOP_BUY_TIME_ET}  dump={bt.DUMP_TIME_ET}")
    print(f"Sizing: MAX_POSITIONS={MAX_POSITIONS}  RESERVE_PCT={RESERVE_PCT}%  "
          f"HARD_STOP_PCT={bt.HARD_STOP_PCT}%  ATR_STOP_MULT={ATR_STOP_MULT}  "
          f"stop_pct clamp=[{ATR_MIN_STOP_PCT},{ATR_MAX_STOP_PCT}]")

    api_key = os.getenv("SML_ALPACA_API_KEY")
    api_secret = os.getenv("SML_ALPACA_API_SECRET")
    client = StockHistoricalDataClient(api_key, api_secret)

    baseline_entries = run_rsi_macd(by_day, client, {}, use_cache=use_cache)
    rvol4_entries = run_rsi_macd(by_day, client, {"MAX_RVOL": 4.0}, use_cache=use_cache)

    sim_baseline = simulate_equity(baseline_entries, args.start_balance)
    sim_rvol4 = simulate_equity(rvol4_entries, args.start_balance)

    print_weekly(f"BASELINE (rsi_macd, no MAX_RVOL cap) -- ${args.start_balance:.0f} start", sim_baseline, args.start_balance)
    print_weekly(f"MAX_RVOL=4.0 (backtest's best candidate) -- ${args.start_balance:.0f} start", sim_rvol4, args.start_balance)


if __name__ == "__main__":
    main()
