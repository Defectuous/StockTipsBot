"""
vwap_reclaim_shadow.py -- Shadow-test a candidate early-exit rule: "if price
closes back below VWAP for --dwell consecutive 1-min bars within the hold,
exit immediately" -- against every closed SML/SML2 trade in stockbot.db, to
see whether it would have improved outcomes (catching fades earlier/cheaper
than the 30-min checkpoint or hard stop) or hurt them (cutting winners short
on a normal above/below-VWAP wobble).

All SML/SML2 entries already require price > VWAP at entry (ScreenedStock.passes
in bot/screener.py), so "closes back below VWAP" is a real thesis break, not
just noise around a level the trade was never anchored to.

VWAP is recomputed here from 1-min bars anchored to market open (09:30 ET),
matching the anchor the live screeners use (see run_sml_screener.py's
market_open-anchored vwap_bars) -- same formula as bot/market_data.py:_vwap,
just finer-grained (1-min vs the live 5-min bars) so the simulated break can
be timed precisely.

For each trade, simulates truncating the hold at the first VWAP-reclaim-fail
(if it occurs before the trade's actual exit) and compares the resulting P&L
to what actually happened. Trades where the rule never fires, or fires after
the actual exit already happened, are unaffected by construction.

Usage:
    python tools/vwap_reclaim_shadow.py                    # all closed SML/SML2 trades
    python tools/vwap_reclaim_shadow.py --dwell 1           # fire on first close below VWAP
    python tools/vwap_reclaim_shadow.py --warmup-min 3      # ignore the first N min post-entry
    python tools/vwap_reclaim_shadow.py --db pi_data/stockbot.db
    python tools/vwap_reclaim_shadow.py --out reports/vwap_reclaim_shadow.csv
"""
import argparse
import csv
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
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
_UTC = pytz.UTC

PROVIDERS = ("SML_SCREENER", "SML2_SCREENER")


def fetch_trades(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(PROVIDERS))
    rows = conn.execute(
        f"""SELECT provider, symbol, shares, buy_price, buy_time, sell_price, sell_time, pnl
            FROM positions
            WHERE provider IN ({placeholders}) AND status = 'closed'
            ORDER BY buy_time""",
        PROVIDERS,
    ).fetchall()
    conn.close()
    trades = []
    for r in rows:
        trades.append({
            "provider": r["provider"], "symbol": r["symbol"],
            "buy_price": r["buy_price"], "buy_time": datetime.fromisoformat(r["buy_time"]),
            "sell_price": r["sell_price"], "sell_time": datetime.fromisoformat(r["sell_time"]),
            "pnl": r["pnl"],
        })
    return trades


def running_vwap(bars: list) -> list[float]:
    """Cumulative VWAP at each bar, same formula as bot/market_data.py:_vwap."""
    out = []
    total_pv = 0.0
    total_vol = 0.0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3.0
        total_pv += tp * b.volume
        total_vol += b.volume
        out.append(total_pv / total_vol if total_vol else None)
    return out


def simulate_trade(bars: list, trade: dict, dwell: int, warmup_min: int,
                    require_negative_gain: bool = False, vwap_buffer_pct: float = 0.0) -> dict | None:
    market_open = _ET.localize(
        datetime.combine(trade["buy_time"].astimezone(_ET).date(), datetime.min.time())
        .replace(hour=9, minute=30)
    ).astimezone(_UTC)
    session_bars = [b for b in bars if b.timestamp >= market_open]
    if not session_bars:
        return None
    vwaps = running_vwap(session_bars)

    entry_idx = None
    for i, b in enumerate(session_bars):
        if b.timestamp >= trade["buy_time"]:
            entry_idx = i
            break
    if entry_idx is None:
        return None

    warmup_cutoff = trade["buy_time"] + timedelta(minutes=warmup_min)
    below_streak = 0
    break_idx = None
    for i in range(entry_idx, len(session_bars)):
        b = session_bars[i]
        if b.timestamp >= trade["sell_time"]:
            break
        if b.timestamp < warmup_cutoff:
            continue
        below_vwap = vwaps[i] is not None and b.close < vwaps[i] * (1 - vwap_buffer_pct / 100)
        underwater = (not require_negative_gain) or b.close < trade["buy_price"]
        if below_vwap and underwater:
            below_streak += 1
            if below_streak >= dwell:
                break_idx = i
                break
        else:
            below_streak = 0

    actual_pnl_pct = round(100 * (trade["sell_price"] - trade["buy_price"]) / trade["buy_price"], 2)

    if break_idx is None:
        return {**trade, "rule_fired": False, "actual_pnl_pct": actual_pnl_pct,
                "hyp_pnl_pct": None, "delta_pct": 0.0, "hyp_exit_time": None}

    hyp_exit_price = session_bars[break_idx].close
    hyp_pnl_pct = round(100 * (hyp_exit_price - trade["buy_price"]) / trade["buy_price"], 2)
    delta_pct = round(hyp_pnl_pct - actual_pnl_pct, 2)
    return {**trade, "rule_fired": True, "actual_pnl_pct": actual_pnl_pct,
            "hyp_pnl_pct": hyp_pnl_pct, "delta_pct": delta_pct,
            "hyp_exit_time": session_bars[break_idx].timestamp}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="pi_data/stockbot.db")
    parser.add_argument("--dwell", type=int, default=2,
                         help="consecutive 1-min closes below VWAP required to fire (default: 2)")
    parser.add_argument("--warmup-min", type=int, default=3,
                         help="ignore VWAP breaks in the first N minutes post-entry (default: 3)")
    parser.add_argument("--require-negative-gain", action="store_true",
                         help="only let the rule fire while price is below entry (can't cut a green trade)")
    parser.add_argument("--vwap-buffer-pct", type=float, default=0.0,
                         help="require close this many %% below VWAP to count as 'below' (default: 0.0, any close below)")
    parser.add_argument("--out", default="reports/vwap_reclaim_shadow.csv")
    parser.add_argument("--bar-cache", default=None,
                         help="pickle file to cache fetched 1-min bars across runs (speeds up parameter sweeps)")
    args = parser.parse_args()

    db_path = (_ROOT / args.db) if not Path(args.db).is_absolute() else Path(args.db)
    trades = fetch_trades(db_path)
    print(f"{len(trades)} closed SML/SML2 trades\n")

    api_key = os.getenv("SML_ALPACA_API_KEY")
    api_secret = os.getenv("SML_ALPACA_API_SECRET")
    client = StockHistoricalDataClient(api_key, api_secret)

    bars_cache: dict[tuple[str, date], list] = {}
    cache_path = Path(args.bar_cache) if args.bar_cache else None
    if cache_path and cache_path.exists():
        import pickle
        with open(cache_path, "rb") as f:
            bars_cache = pickle.load(f)
        print(f"Loaded {len(bars_cache)} cached symbol-day bar sets from {cache_path}")

    results = []
    for t in trades:
        d = t["buy_time"].astimezone(_ET).date()
        key = (t["symbol"], d)
        if key not in bars_cache:
            try:
                resp = client.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=t["symbol"],
                    timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d, end=d + timedelta(days=1),
                ))
                bars_cache[key] = list(resp.data.get(t["symbol"], []))
            except Exception as e:
                print(f"  {t['symbol']} {d}: bar fetch failed ({e})")
                bars_cache[key] = []
        bars = bars_cache[key]
        if not bars:
            continue
        res = simulate_trade(bars, t, args.dwell, args.warmup_min, args.require_negative_gain,
                              args.vwap_buffer_pct)
        if res:
            results.append(res)

    fired = [r for r in results if r["rule_fired"]]
    not_fired = [r for r in results if not r["rule_fired"]]
    improved = [r for r in fired if r["delta_pct"] > 0.05]
    hurt = [r for r in fired if r["delta_pct"] < -0.05]
    neutral = [r for r in fired if abs(r["delta_pct"]) <= 0.05]

    print(f"Simulated {len(results)} trades (dwell={args.dwell} bar(s), warmup={args.warmup_min}min)")
    print(f"  Rule fired before actual exit: {len(fired)}/{len(results)}")
    print(f"  Never fired / fired after actual exit (no change): {len(not_fired)}")
    print(f"  -> improved outcome:  {len(improved)}  (avg delta {sum(r['delta_pct'] for r in improved)/len(improved):+.2f}%)" if improved else "  -> improved outcome:  0")
    print(f"  -> hurt outcome:      {len(hurt)}  (avg delta {sum(r['delta_pct'] for r in hurt)/len(hurt):+.2f}%)" if hurt else "  -> hurt outcome:      0")
    print(f"  -> ~neutral:          {len(neutral)}")
    total_delta = sum(r["delta_pct"] for r in fired)
    print(f"  Net P&L-% delta across all fired trades: {total_delta:+.2f}%")

    hurt_winners = [r for r in hurt if r["actual_pnl_pct"] > 0]
    print(f"\n  Of the 'hurt' trades, {len(hurt_winners)} were actual winners cut short by the rule "
          f"(the real risk of this exit).")

    print("\nWorst 'hurt' cases (rule fired but actual outcome was better):")
    for r in sorted(hurt, key=lambda r: r["delta_pct"])[:8]:
        print(f"  {r['symbol']:6s} {r['buy_time'].astimezone(_ET).date()}  "
              f"actual={r['actual_pnl_pct']:+.2f}%  hyp={r['hyp_pnl_pct']:+.2f}%  "
              f"delta={r['delta_pct']:+.2f}%")

    print("\nBest 'improved' cases (rule would have saved money):")
    for r in sorted(improved, key=lambda r: -r["delta_pct"])[:8]:
        print(f"  {r['symbol']:6s} {r['buy_time'].astimezone(_ET).date()}  "
              f"actual={r['actual_pnl_pct']:+.2f}%  hyp={r['hyp_pnl_pct']:+.2f}%  "
              f"delta={r['delta_pct']:+.2f}%")

    out_path = (_ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["provider", "symbol", "date", "actual_pnl_pct", "rule_fired",
                    "hyp_pnl_pct", "delta_pct", "hyp_exit_time"])
        for r in results:
            w.writerow([
                r["provider"], r["symbol"], r["buy_time"].astimezone(_ET).date(),
                r["actual_pnl_pct"], r["rule_fired"], r.get("hyp_pnl_pct"), r.get("delta_pct"),
                r["hyp_exit_time"].astimezone(_ET).strftime("%H:%M") if r.get("hyp_exit_time") else "",
            ])
    print(f"\nFull results written to {out_path}")

    if cache_path:
        import pickle
        with open(cache_path, "wb") as f:
            pickle.dump(bars_cache, f)


if __name__ == "__main__":
    main()
