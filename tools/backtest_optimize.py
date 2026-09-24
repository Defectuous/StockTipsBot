"""
backtest_optimize.py -- Coordinate-ascent search over SML2 exit and entry
settings on the live-fidelity backtest (backtest_stop_floor.LIVE_SML2: no
lookahead, realistic stop fills, live MACD/RVOL/start-time gates).

Overfitting guard: the sample is small (~40-50 resim entries), so a change is
only accepted if it raises total $ AND does not lower $ in either half of the
sample (before / on-or-after SPLIT_DATE, the SML2 $500 reset). Each round
tries every option of every knob against the current best and takes the
single best accepted move; stops when nothing is accepted.

$ uses the same sizing model as backtest_stop_floor.py (risk-sized, capped at
CASH_CAP). Win rate is reported but not optimized -- a higher win rate that
loses more money isn't better.

Usage:
    python tools/backtest_optimize.py --days 110
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
import backtest_stop_floor as sf  # noqa: E402

load_dotenv()
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
SPLIT_DATE = "2026-08-25"

# knob -> {option label: overrides}. The first option is not special; the
# starting point is LIVE_SML2 (current live config) whatever its label.
EXIT_KNOBS = {
    "trail": {
        "off":            dict(ATR_TRAIL_ACTIVATE_PCT=0.0, ATR_TRAIL_BE_FLOOR=False),
        "2.5%/1.5xATR":   dict(ATR_TRAIL_ACTIVATE_PCT=2.5, ATR_TRAIL_MULT=1.5, ATR_TRAIL_BE_FLOOR=False),
        "2.5%/1.5x+BE":   dict(ATR_TRAIL_ACTIVATE_PCT=2.5, ATR_TRAIL_MULT=1.5, ATR_TRAIL_BE_FLOOR=True),
        "2.5%/1.0x+BE":   dict(ATR_TRAIL_ACTIVATE_PCT=2.5, ATR_TRAIL_MULT=1.0, ATR_TRAIL_BE_FLOOR=True),
        "4%/1.5x+BE":     dict(ATR_TRAIL_ACTIVATE_PCT=4.0, ATR_TRAIL_MULT=1.5, ATR_TRAIL_BE_FLOOR=True),
        "5%/1.0x+BE":     dict(ATR_TRAIL_ACTIVATE_PCT=5.0, ATR_TRAIL_MULT=1.0, ATR_TRAIL_BE_FLOOR=True),
        "8%/1.5x+BE":     dict(ATR_TRAIL_ACTIVATE_PCT=8.0, ATR_TRAIL_MULT=1.5, ATR_TRAIL_BE_FLOOR=True),
    },
    "checkpoints": {
        "30m-5/60m-3": dict(MIN_GAIN_AT_30M=-5.0, MIN_GAIN_AT_60M=-3.0),
        "30m-3/60m-1": dict(MIN_GAIN_AT_30M=-3.0, MIN_GAIN_AT_60M=-1.0),
        "30m-2/60m0":  dict(MIN_GAIN_AT_30M=-2.0, MIN_GAIN_AT_60M=0.0),
    },
    "max_hold": {"90m": dict(MAX_HOLD_MINUTES=90), "120m": dict(MAX_HOLD_MINUTES=120),
                 "180m": dict(MAX_HOLD_MINUTES=180)},
    "dump": {"12:00": dict(DUMP_TIME_ET="12:00"), "13:30": dict(DUMP_TIME_ET="13:30"),
             "15:30": dict(DUMP_TIME_ET="15:30")},
    "rsi_exit_gate": {"0%": dict(RSI_EXIT_MIN_GAIN_PCT=0.0), "5%": dict(RSI_EXIT_MIN_GAIN_PCT=5.0)},
    "vwap_reclaim": {"off": dict(VWAP_RECLAIM_EXIT_DWELL=0), "dwell 9": dict(VWAP_RECLAIM_EXIT_DWELL=9),
                     "dwell 15": dict(VWAP_RECLAIM_EXIT_DWELL=15)},
}
ENTRY_KNOBS = {
    "max_rvol": {f"{v}x": dict(MAX_RVOL=float(v)) for v in (10, 6, 5, 4, 3)},
    "min_rvol": {"1.5x": dict(MIN_RVOL=1.5), "2.0x": dict(MIN_RVOL=2.0)},
    "max_entry_move": {f"{v}%": dict(MAX_ENTRY_MOVE_PCT=float(v)) for v in (17, 13, 10)},
    "start": {"09:45": dict(ENTRY_START_TIME_ET="09:45"), "10:00": dict(ENTRY_START_TIME_ET="10:00")},
    "stop_buy": {"11:45": dict(STOP_BUY_TIME_ET="11:45"), "11:00": dict(STOP_BUY_TIME_ET="11:00")},
    "min_price": {"off": dict(MIN_ENTRY_PRICE=0.0), "$1.00": dict(MIN_ENTRY_PRICE=1.0),
                  "$1.50": dict(MIN_ENTRY_PRICE=1.5)},
}
ENTRY_KEYS = {k for opts in ENTRY_KNOBS.values() for o in opts.values() for k in o}


class Evaluator:
    def __init__(self, by_day, client):
        self.pairs = [(d, s) for d in sorted(by_day) for s in sorted(by_day[d])]
        self.data = {p: bt.fetch_symbol_data(client, p[1], p[0]) for p in self.pairs}
        self._entries = {}

    def _apply(self, cfg):
        saved = {k: getattr(bt, k) for k in cfg}
        for k, v in cfg.items():
            setattr(bt, k, v)
        return saved

    def entries(self, cfg):
        key = tuple(sorted((k, v) for k, v in cfg.items() if k in ENTRY_KEYS))
        if key not in self._entries:
            saved = self._apply(cfg)
            try:
                out = []
                for p in self.pairs:
                    if self.data[p]:
                        e = bt.simulate_entry_rsi_macd(p[1], self.data[p], p[0])
                        if e:
                            out.append((p, e))
                self._entries[key] = out
            finally:
                self._apply(saved)
        return self._entries[key]

    def run(self, cfg):
        entries = self.entries(cfg)
        saved = self._apply(cfg)
        try:
            rows = []
            for (d, sym), e in entries:
                data = self.data[(d, sym)]
                ex = bt.simulate_exit(data["bars1"], data["bars5"], e["entry_time"],
                                      e["entry_price"], e.get("atr"))
                rows.append(dict(date=d, symbol=sym, **e, **ex))
            return rows
        finally:
            self._apply(saved)


def score(rows):
    def usd(rs):
        return sum(sf.dollars(r, 2.0) for r in rs)
    early = [r for r in rows if str(r["date"]) < SPLIT_DATE]
    late = [r for r in rows if str(r["date"]) >= SPLIT_DATE]
    n = len(rows)
    return dict(n=n, usd=usd(rows), early=usd(early), late=usd(late),
                n_early=len(early), n_late=len(late),
                win=100 * sum(r["gain_pct"] >= 0 for r in rows) / n if n else 0.0,
                avg=sum(r["gain_pct"] for r in rows) / n if n else 0.0)


def fmt(s):
    return (f"n={s['n']:<3} win={s['win']:5.1f}%  avg={s['avg']:+5.2f}%  $={s['usd']:+8.2f}  "
            f"[pre-reset {s['n_early']}: {s['early']:+7.2f} | post-reset {s['n_late']}: {s['late']:+7.2f}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=110)
    ap.add_argument("--providers", nargs="+", default=["sml", "sml2"])
    ap.add_argument("--db", default="pi_data/stockbot.db")
    args = ap.parse_args()

    client = StockHistoricalDataClient(os.getenv("SML_ALPACA_API_KEY"), os.getenv("SML_ALPACA_API_SECRET"))
    by_day = bt.find_traded_symbols_by_day(_ROOT / args.db, args.providers, args.days)
    ev = Evaluator(by_day, client)
    print(f"Universe: {len(by_day)} days, {len(ev.pairs)} (day, symbol) pairs\n")

    cfg = dict(sf.LIVE_SML2, ATR_TRAIL_BE_FLOOR=False, MIN_RVOL=1.5, MAX_RVOL=10.0,
               STOP_BUY_TIME_ET="11:45", MIN_ENTRY_PRICE=0.0)
    best = score(ev.run(cfg))
    print(f"CURRENT LIVE SML2   {fmt(best)}\n")
    chosen = []
    knobs = {**EXIT_KNOBS, **ENTRY_KNOBS}
    for rnd in range(1, 8):
        moves = []
        print(f"-- round {rnd}")
        for knob, opts in knobs.items():
            for label, ov in opts.items():
                trial = {**cfg, **ov}
                if trial == cfg:
                    continue
                s = score(ev.run(trial))
                ok = (s["usd"] > best["usd"] + 1.0 and s["early"] >= best["early"] - 0.01
                      and s["late"] >= best["late"] - 0.01)
                print(f"   {'✓' if ok else ' '} {knob:<14} {label:<14} {fmt(s)}")
                if ok:
                    moves.append((s["usd"], knob, label, ov, s))
        if not moves:
            print("   no accepted move -- done\n")
            break
        _, knob, label, ov, s = max(moves, key=lambda m: m[0])
        cfg = {**cfg, **ov}
        best = s
        chosen.append(f"{knob}={label}")
        print(f"   => take {knob}={label}   {fmt(s)}\n")

    print("FINAL:", ", ".join(chosen) or "(no change)")
    print(f"       {fmt(best)}")
    final_rows = ev.run(cfg)
    print("\nFinal-config trades:")
    for r in sorted(final_rows, key=lambda r: (r["date"], r["symbol"])):
        print(f"  {r['date']} {r['symbol']:<5} {r['entry_time']:%H:%M} ${r['entry_price']:.3f}  "
              f"{r['gain_pct']:+6.1f}%  {r['reason']} ({r['held_min']}m)")


if __name__ == "__main__":
    main()
