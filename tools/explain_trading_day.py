"""
explain_trading_day.py — Explain why a screener did or didn't trade on a given day.

Parses the screener log(s) synced into pi_data/ (each log file holds a single
day's run, overwritten daily) and tallies why candidates were rejected —
RSI band, MACD-not-fresh, RVOL, move-% too small/large, cooldown, already up
past the daily-gain limit — plus buy-cutoff / position-cap / cash-shortage
events. Cross-checks actual fills against positions in pi_data/stockbot.db
(source of truth for what filled, since a "BUY" log line doesn't guarantee
the order got filled).

Usage:
    python tools/explain_trading_day.py                  # today, sml + sml2
    python tools/explain_trading_day.py --providers sml sml2 mid super
    python tools/explain_trading_day.py --date 2026-07-22
    python tools/explain_trading_day.py --dir pi_data
"""
import argparse
import re
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent

SKIP_RE  = re.compile(r"^\d\d:\d\d:\d\d\s+INFO\s+SKIP\s+(\S+)\s+—\s+(.*)$")
SCAN_RE  = re.compile(r"^\d\d:\d\d:\d\d\s+INFO\s+\[(\d\d:\d\d:\d\d)\]\s+Scanned (\d+) stocks -> (\d+) passing")
BUY_RE   = re.compile(r"^\d\d:\d\d:\d\d\s+INFO\s+BUY\s+(\S+)")
FILLED_RE = re.compile(r"^\d\d:\d\d:\d\d\s+INFO\s+FILLED\s+(\S+)\s+.*=\s*\$([\d.]+)")
SOLD_RE  = re.compile(r"^\d\d:\d\d:\d\d\s+INFO\s+SOLD\s+(\S+)\s+@\s+\$([\d.]+)\s+PnL=\$([-+\d.]+)\s+\((.*)\)")
CUTOFF_RE = re.compile(r"Past buy cutoff (\S+) ET")
CAP_RE    = re.compile(r"Position cap reached \((\d+/\d+)\)")
CASH_RE   = re.compile(r"Insufficient deployable cash: available=\$([\d.]+)\s+needed=\$([\d.]+)")
ERROR_RE  = re.compile(r"^\d\d:\d\d:\d\d\s+ERROR\s+(.*)$")


def _reason_bucket(reason: str) -> str:
    """Collapse a SKIP reason string to a stable category, stripping live values."""
    if "RSI" in reason and "band" in reason:
        return "RSI outside entry band"
    if "MACD crossover not fresh" in reason:
        return "MACD crossover not fresh"
    if "RVOL" in reason:
        return "RVOL below minimum"
    if "cooldown" in reason:
        return "symbol on cooldown"
    if "already up" in reason:
        return "already up past daily-gain limit"
    if "change" in reason and "min" in reason:
        return "price move below minimum %"
    if "VWAP" in reason:
        return "VWAP filter"
    return reason


def parse_log(path: Path) -> dict:
    scans = 0
    max_passing = 0
    skip_reasons: Counter = Counter()
    skip_symbols: Counter = Counter()
    buys, fills, sells = [], [], []
    cutoff_hit = None
    cap_hit = None
    cash_shortage = None
    stop_errors = []

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if m := SCAN_RE.match(line):
            scans += 1
            max_passing = max(max_passing, int(m.group(3)))
            continue
        if m := SKIP_RE.match(line):
            skip_symbols[m.group(1)] += 1
            skip_reasons[_reason_bucket(m.group(2))] += 1
            continue
        if m := BUY_RE.match(line):
            buys.append(m.group(1))
            continue
        if m := FILLED_RE.match(line):
            fills.append((m.group(1), float(m.group(2))))
            continue
        if m := SOLD_RE.match(line):
            sells.append((m.group(1), float(m.group(2)), float(m.group(3)), m.group(4)))
            continue
        if m := CUTOFF_RE.search(line):
            if cutoff_hit is None:
                cutoff_hit = m.group(1)
            continue
        if m := CAP_RE.search(line):
            cap_hit = m.group(1)
            continue
        if m := CASH_RE.search(line):
            cash_shortage = (m.group(1), m.group(2))
            continue
        if m := ERROR_RE.match(line):
            if "Stop-loss failed" in m.group(1):
                stop_errors.append(m.group(1))

    return dict(
        scans=scans, max_passing=max_passing,
        skip_reasons=skip_reasons, skip_symbols=skip_symbols,
        buys=buys, fills=fills, sells=sells,
        cutoff_hit=cutoff_hit, cap_hit=cap_hit,
        cash_shortage=cash_shortage, stop_errors=stop_errors,
    )


def fetch_db_trades(db_path: Path, provider: str, report_date: date) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT symbol, shares, buy_price, sell_price, pnl, status, buy_time, sell_time
           FROM positions
           WHERE provider = ?
             AND (date(buy_time) = ? OR date(sell_time) = ?)
           ORDER BY buy_time""",
        (provider, report_date.isoformat(), report_date.isoformat()),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def print_report(name: str, log_data: dict | None, db_trades: list[dict]):
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print("=" * 60)

    if db_trades:
        total_pnl = sum(t["pnl"] or 0 for t in db_trades if t["status"] == "closed")
        for t in db_trades:
            if t["status"] == "closed":
                print(f"  TRADE  {t['symbol']:<6} {t['shares']:>4}sh  "
                      f"buy ${t['buy_price']:.4f} -> sell ${t['sell_price']:.4f}  "
                      f"PnL ${t['pnl']:+.2f}")
            else:
                print(f"  OPEN   {t['symbol']:<6} {t['shares']:>4}sh  buy ${t['buy_price']:.4f}")
        print(f"  -> {len(db_trades)} position(s), net closed PnL ${total_pnl:+.2f}")
    else:
        print("  No trades in DB for this date.")

    if log_data is None:
        print("  (no log file found)")
        return

    d = log_data
    print(f"\n  Scanned {d['scans']} cycles, up to {d['max_passing']} candidates passing MACD+RSI at once.")

    if d["skip_reasons"]:
        print("  Rejection reasons (candidate-cycle count):")
        for reason, count in d["skip_reasons"].most_common():
            print(f"    - {reason}: {count}")
        print("  Most-rejected symbols:")
        for sym, count in d["skip_symbols"].most_common(5):
            print(f"    - {sym}: skipped {count}x")
    else:
        print("  No SKIP lines logged.")

    if d["cash_shortage"]:
        avail, needed = d["cash_shortage"]
        print(f"  Hit insufficient deployable cash (available=${avail}, needed=${needed}) at some point.")

    if d["stop_errors"]:
        print(f"  {len(d['stop_errors'])} stop-loss placement error(s) (likely held_for_orders race after fill).")

    if d["cap_hit"]:
        print(f"  Position cap reached: {d['cap_hit']}.")
    if d["cutoff_hit"]:
        print(f"  Buy cutoff reached: {d['cutoff_hit']} ET — no new buys after.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(),
                         help="Report date, YYYY-MM-DD (default: today)")
    parser.add_argument("--providers", nargs="+", default=["sml", "sml2"],
                         help="Which screeners to check (default: sml sml2)")
    parser.add_argument("--dir", default="pi_data",
                         help="Directory holding synced logs + db (default: pi_data)")
    args = parser.parse_args()

    data_dir = (_ROOT / args.dir) if not Path(args.dir).is_absolute() else Path(args.dir)
    db_path = data_dir / "stockbot.db"

    provider_map = {
        "sml": "SML_SCREENER", "sml2": "SML2_SCREENER",
        "mid": "MID_SCREENER", "super": "SUPER_SCREENER", "live": "LIVE_SCREENER",
    }

    print(f"Trading day report for {args.date}  (source: {data_dir})")

    for p in args.providers:
        log_path = data_dir / f"{p}.log"
        log_data = parse_log(log_path) if log_path.exists() else None
        provider = provider_map.get(p, p.upper() + "_SCREENER")
        db_trades = fetch_db_trades(db_path, provider, args.date)
        print_report(p.upper(), log_data, db_trades)

    print()


if __name__ == "__main__":
    main()
