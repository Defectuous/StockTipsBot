"""Analyze entry variables across winners vs losers."""
import sqlite3
from pathlib import Path

conn = sqlite3.connect(str(Path(__file__).parent / "stockbot.db"))
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT symbol, provider, buy_price, sell_price,
           (sell_price - buy_price) / buy_price * 100 AS gain_pct,
           ROUND((JULIANDAY(sell_time) - JULIANDAY(buy_time)) * 1440, 1) AS hold_minutes,
           rsi_at_entry, atr_at_entry, change_pct_at_entry,
           macd_crossover_fresh, rvol_at_entry, pnl
    FROM positions
    WHERE status = 'closed'
      AND sell_price IS NOT NULL
    ORDER BY gain_pct DESC
""").fetchall()
conn.close()

trades = [dict(r) for r in rows]

def bucket(gain):
    if gain >= 8:   return "BIG WIN  (>=8%)"
    if gain >= 3:   return "WIN      (3-8%)"
    if gain >= 0:   return "SCRATCH  (0-3%)"
    if gain >= -5:  return "LOSS     (0 to -5%)"
    return             "BIG LOSS (<-5%)"

# ── Per-bucket averages ────────────────────────────────────────────────────────
buckets = {}
for t in trades:
    b = bucket(t["gain_pct"])
    buckets.setdefault(b, []).append(t)

print("=" * 72)
print(f"{'BUCKET':<22} {'N':>4}  {'RSI':>6}  {'ATR':>7}  {'chg%':>7}  {'RVOL':>7}  {'cross':>5}")
print("-" * 72)

order = ["BIG WIN  (>=8%)", "WIN      (3-8%)", "SCRATCH  (0-3%)", "LOSS     (0 to -5%)", "BIG LOSS (<-5%)"]
for b in order:
    grp = buckets.get(b, [])
    if not grp:
        continue
    def avg(key):
        vals = [t[key] for t in grp if t[key] is not None]
        return sum(vals)/len(vals) if vals else None
    def pct(key, val):
        vals = [t[key] for t in grp if t[key] is not None]
        return sum(1 for v in vals if v == val) / len(vals) * 100 if vals else 0
    rsi  = avg("rsi_at_entry")
    atr  = avg("atr_at_entry")
    chg  = avg("change_pct_at_entry")
    rvol = avg("rvol_at_entry")
    cross= pct("macd_crossover_fresh", 1)
    print(f"{b:<22} {len(grp):>4}  {rsi or 0:>6.1f}  {atr or 0:>7.4f}  {chg or 0:>7.1f}  {rvol or 0:>7.3f}  {cross:>4.0f}%")

# ── Top 10 winners detail ──────────────────────────────────────────────────────
print()
print("=" * 90)
print("TOP 15 WINNERS")
print(f"{'SYM':<6} {'gain%':>6}  {'hold':>5}m  {'RSI':>5}  {'ATR':>7}  {'chg%':>7}  {'RVOL':>7}  {'cross':>5}  {'provider'}")
print("-" * 90)
for t in trades[:15]:
    print(f"{t['symbol']:<6} {t['gain_pct']:>+6.2f}%  {t['hold_minutes'] or 0:>5.0f}m"
          f"  {t['rsi_at_entry'] or 0:>5.1f}"
          f"  {t['atr_at_entry'] or 0:>7.4f}"
          f"  {t['change_pct_at_entry'] or 0:>7.1f}%"
          f"  {t['rvol_at_entry'] or 0:>7.3f}"
          f"  {'yes' if t['macd_crossover_fresh'] else 'no ':>5}"
          f"  {t['provider']}")

# ── Bottom 10 losers detail ────────────────────────────────────────────────────
print()
print("=" * 90)
print("BOTTOM 15 LOSERS")
print(f"{'SYM':<6} {'gain%':>6}  {'hold':>5}m  {'RSI':>5}  {'ATR':>7}  {'chg%':>7}  {'RVOL':>7}  {'cross':>5}  {'provider'}")
print("-" * 90)
for t in sorted(trades, key=lambda x: x["gain_pct"])[:15]:
    print(f"{t['symbol']:<6} {t['gain_pct']:>+6.2f}%  {t['hold_minutes'] or 0:>5.0f}m"
          f"  {t['rsi_at_entry'] or 0:>5.1f}"
          f"  {t['atr_at_entry'] or 0:>7.4f}"
          f"  {t['change_pct_at_entry'] or 0:>7.1f}%"
          f"  {t['rvol_at_entry'] or 0:>7.3f}"
          f"  {'yes' if t['macd_crossover_fresh'] else 'no ':>5}"
          f"  {t['provider']}")

# ── change_pct thresholds ──────────────────────────────────────────────────────
print()
print("=" * 72)
print("WIN RATE BY change_pct_at_entry BUCKET")
print(f"{'chg% range':<20} {'N':>4}  {'win%':>5}  {'avg gain':>8}  {'avg loss':>8}")
print("-" * 72)

def chg_bucket(v):
    if v is None:  return None
    if v < 0:      return "negative (down day)"
    if v < 5:      return "0-5%"
    if v < 15:     return "5-15%"
    if v < 30:     return "15-30%"
    if v < 60:     return "30-60%"
    return             ">60% (pumped)"

cbuckets = {}
for t in trades:
    b = chg_bucket(t["change_pct_at_entry"])
    if b:
        cbuckets.setdefault(b, []).append(t)

for b in ["negative (down day)", "0-5%", "5-15%", "15-30%", "30-60%", ">60% (pumped)"]:
    grp = cbuckets.get(b, [])
    if not grp: continue
    wins   = [t for t in grp if t["gain_pct"] >= 0]
    losses = [t for t in grp if t["gain_pct"] < 0]
    avg_w  = sum(t["gain_pct"] for t in wins)   / len(wins)   if wins   else 0
    avg_l  = sum(t["gain_pct"] for t in losses) / len(losses) if losses else 0
    print(f"{b:<20} {len(grp):>4}  {len(wins)/len(grp)*100:>4.0f}%  {avg_w:>+8.2f}%  {avg_l:>+8.2f}%")

# ── RVOL thresholds ────────────────────────────────────────────────────────────
print()
print("=" * 72)
print("WIN RATE BY rvol_at_entry BUCKET")
print(f"{'RVOL range':<20} {'N':>4}  {'win%':>5}  {'avg gain':>8}  {'avg loss':>8}")
print("-" * 72)

def rvol_bucket(v):
    if v is None: return None
    if v < 0.1:   return "<0.1x  (cold)"
    if v < 0.5:   return "0.1-0.5x"
    if v < 1.0:   return "0.5-1.0x"
    if v < 3.0:   return "1-3x"
    if v < 10.0:  return "3-10x"
    return            ">10x   (extreme)"

rbuckets = {}
for t in trades:
    b = rvol_bucket(t["rvol_at_entry"])
    if b:
        rbuckets.setdefault(b, []).append(t)

for b in ["<0.1x  (cold)", "0.1-0.5x", "0.5-1.0x", "1-3x", "3-10x", ">10x   (extreme)"]:
    grp = rbuckets.get(b, [])
    if not grp: continue
    wins   = [t for t in grp if t["gain_pct"] >= 0]
    losses = [t for t in grp if t["gain_pct"] < 0]
    avg_w  = sum(t["gain_pct"] for t in wins)   / len(wins)   if wins   else 0
    avg_l  = sum(t["gain_pct"] for t in losses) / len(losses) if losses else 0
    print(f"{b:<20} {len(grp):>4}  {len(wins)/len(grp)*100:>4.0f}%  {avg_w:>+8.2f}%  {avg_l:>+8.2f}%")

# ── RSI buckets ────────────────────────────────────────────────────────────────
print()
print("=" * 72)
print("WIN RATE BY rsi_at_entry BUCKET")
print(f"{'RSI range':<20} {'N':>4}  {'win%':>5}  {'avg gain':>8}  {'avg loss':>8}")
print("-" * 72)

def rsi_bucket(v):
    if v is None: return None
    if v < 50:    return "<50  (weak)"
    if v < 55:    return "50-55"
    if v < 60:    return "55-60"
    if v < 65:    return "60-65"
    if v < 70:    return "65-70"
    return            ">=70 (hot)"

rsi_bkts = {}
for t in trades:
    b = rsi_bucket(t["rsi_at_entry"])
    if b:
        rsi_bkts.setdefault(b, []).append(t)

for b in ["<50  (weak)", "50-55", "55-60", "60-65", "65-70", ">=70 (hot)"]:
    grp = rsi_bkts.get(b, [])
    if not grp: continue
    wins   = [t for t in grp if t["gain_pct"] >= 0]
    losses = [t for t in grp if t["gain_pct"] < 0]
    avg_w  = sum(t["gain_pct"] for t in wins)   / len(wins)   if wins   else 0
    avg_l  = sum(t["gain_pct"] for t in losses) / len(losses) if losses else 0
    print(f"{b:<20} {len(grp):>4}  {len(wins)/len(grp)*100:>4.0f}%  {avg_w:>+8.2f}%  {avg_l:>+8.2f}%")
