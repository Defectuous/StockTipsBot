"""
Daily SML / SML2 report — renders a day's screener scan activity and trade
outcomes as a sortable HTML dashboard.

Sources:
  - {dir}/{provider}.log   SKIP/BUY/FILLED/SOLD/Scanned lines, parsed with
    the same regexes tools/explain_trading_day.py uses. These logs are
    overwritten daily by the running screener process, so --date only
    matches the log content if --dir points at an archived/synced copy
    taken before the next day's run started (e.g. pi_data/, synced once a
    day). The default --dir is pi_data since that's where this project's
    screeners actually run and sync from.
  - {dir}/stockbot.db      positions table — source of truth for what
    actually filled, closed, and its P&L. Always correctly date-filtered
    regardless of --dir.

Usage:
    python daily_report.py                      # today (ET), pi_data/, opens in browser
    python daily_report.py --date 2026-08-07
    python daily_report.py --dir .               # use root logs/db instead of pi_data/
    python daily_report.py --providers sml sml2
    python daily_report.py --no-open
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import webbrowser
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from pathlib import Path

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.explain_trading_day import parse_log, resolve_log_path

ET = pytz.timezone("America/New_York")
ROOT = Path(__file__).resolve().parent
LOGS_DIR = ROOT / "logs"

PROVIDER_MAP = {"sml": "SML_SCREENER", "sml2": "SML2_SCREENER"}


def _today() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _fmt_et(iso: str | None) -> str | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).strftime("%H:%M")


def _hold_minutes(buy_time: str, sell_time: str | None) -> float | None:
    buy_dt = datetime.fromisoformat(buy_time)
    if buy_dt.tzinfo is None:
        buy_dt = buy_dt.replace(tzinfo=timezone.utc)
    if sell_time:
        end_dt = datetime.fromisoformat(sell_time)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)
    return round((end_dt - buy_dt).total_seconds() / 60, 1)


def _fetch_positions(db_path: Path, provider: str, trade_date: str) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Synced/archived DB snapshots (e.g. pi_data/) can predate a schema
    # migration on the live DB — select only columns that actually exist so
    # an older snapshot still renders, just with those fields blank.
    have_cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
    optional = ["rsi_at_entry", "atr_at_entry", "change_pct_at_entry", "macd_crossover_fresh",
                "rvol_at_entry", "vwap_z_at_entry", "stop_pct_at_entry"]
    select_cols = ["symbol", "shares", "buy_price", "buy_time", "sell_price", "sell_time", "pnl", "status"]
    select_cols += [c for c in optional if c in have_cols]
    rows = conn.execute(
        f"""
        SELECT {', '.join(select_cols)}
        FROM positions
        WHERE provider = ? AND (date(buy_time) = ? OR date(sell_time) = ?)
        ORDER BY buy_time
        """,
        (provider, trade_date, trade_date),
    ).fetchall()
    conn.close()
    return [{**dict.fromkeys(optional), **dict(r)} for r in rows]


def _build_trades(provider_key: str, positions: list[dict], sells_log: list[tuple]) -> list[dict]:
    """Attach the log-line exit reason to each closed DB position, matching
    by symbol in log order — SOLD is logged immediately after close_position()
    in the same call, so log order lines up with ascending sell_time."""
    reasons_by_symbol: dict[str, deque] = defaultdict(deque)
    for sym, _price, _pnl, reason in sells_log:
        reasons_by_symbol[sym].append(reason)

    out = []
    for p in positions:
        shares, buy_price, pnl = p["shares"], p["buy_price"], p["pnl"]
        pnl_pct = (pnl / (buy_price * shares) * 100) if (pnl is not None and buy_price and shares) else None
        exit_reason = None
        if p["status"] == "closed" and reasons_by_symbol[p["symbol"]]:
            exit_reason = reasons_by_symbol[p["symbol"]].popleft()
        out.append({
            "provider": provider_key.upper(),
            "symbol": p["symbol"],
            "shares": shares,
            "buy_price": buy_price,
            "buy_time": _fmt_et(p["buy_time"]),
            "sell_price": p["sell_price"],
            "sell_time": _fmt_et(p["sell_time"]),
            "status": p["status"],
            "pnl": round(pnl, 2) if pnl is not None else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "hold_minutes": _hold_minutes(p["buy_time"], p["sell_time"]),
            "exit_reason": exit_reason,
            "rsi": p["rsi_at_entry"],
            "atr": p["atr_at_entry"],
            "change_pct": p["change_pct_at_entry"],
            "macd_fresh": bool(p["macd_crossover_fresh"]) if p["macd_crossover_fresh"] is not None else None,
            "rvol": p["rvol_at_entry"],
            "vwap_z": p["vwap_z_at_entry"],
            "stop_pct": p["stop_pct_at_entry"],
            # Flag: exited purely because the clock ran out while still
            # solidly positive — see todo.md, MAX_HOLD_MINUTES may be
            # capping winners that would've kept running.
            "cut_short": exit_reason == "Max hold time exit" and (pnl_pct or 0) > 5,
        })
    return out


def _provider_summary(key: str, log_data: dict | None, trades: list[dict]) -> dict:
    closed = [t for t in trades if t["status"] == "closed"]
    wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
    losses = sum(1 for t in closed if (t["pnl"] or 0) <= 0)
    net_pnl = sum(t["pnl"] or 0 for t in closed)
    return {
        "key": key.upper(),
        "log_found": log_data is not None,
        "scans": log_data["scans"] if log_data else 0,
        "max_passing": log_data["max_passing"] if log_data else 0,
        "skip_total": sum(log_data["skip_reasons"].values()) if log_data else 0,
        "skip_reasons": log_data["skip_reasons"].most_common(8) if log_data else [],
        "skip_symbols": log_data["skip_symbols"].most_common(8) if log_data else [],
        "buys_attempted": len(log_data["buys"]) if log_data else 0,
        "fills": len(log_data["fills"]) if log_data else 0,
        "cash_shortage": log_data["cash_shortage"] if log_data else None,
        "cap_hit": log_data["cap_hit"] if log_data else None,
        "cutoff_hit": log_data["cutoff_hit"] if log_data else None,
        "stop_errors": len(log_data["stop_errors"]) if log_data else 0,
        "trades_n": len(trades),
        "closed_n": len(closed),
        "open_n": len(trades) - len(closed),
        "wins": wins,
        "losses": losses,
        "net_pnl": round(net_pnl, 2),
    }


def _skip_bars_html(rows: list[tuple]) -> str:
    if not rows:
        return '<p class="empty-note">No SKIP lines logged.</p>'
    max_n = max(n for _, n in rows) or 1
    items = "".join(
        f'<div class="bar-row"><span class="bar-label">{label}</span>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{n / max_n * 100:.0f}%"></div></div>'
        f'<span class="bar-n">{n}</span></div>'
        for label, n in rows
    )
    return f'<div class="bar-list">{items}</div>'


def _flags_html(summary: dict) -> str:
    flags = []
    if summary["cash_shortage"]:
        avail, needed = summary["cash_shortage"]
        flags.append(f'<span class="flag-pill bad">Cash-limited (avail ${avail}, needed ${needed})</span>')
    if summary["cap_hit"]:
        flags.append(f'<span class="flag-pill">Position cap hit ({summary["cap_hit"]})</span>')
    if summary["cutoff_hit"]:
        flags.append(f'<span class="flag-pill">Buy cutoff {summary["cutoff_hit"]} ET</span>')
    if summary["stop_errors"]:
        flags.append(f'<span class="flag-pill bad">{summary["stop_errors"]} stop-loss placement error(s)</span>')
    if not summary["log_found"]:
        flags.append('<span class="flag-pill bad">No log file found for this day</span>')
    return "".join(flags)


def _cut_short_callout(trades: list[dict]) -> str:
    flagged = [t for t in trades if t["cut_short"]]
    if not flagged:
        return ""
    names = ", ".join(f'<strong>{t["symbol"]}</strong> ({t["provider"]}, {t["pnl_pct"]:+.1f}%)' for t in flagged)
    return f"""
  <div class="callout">
    <span class="tag">Time-exit flag</span>
    <span>{names} were closed only because MAX_HOLD_MINUTES elapsed, while still up more than 5% — the position may have kept running past the cutoff. See todo.md for a trailing-stop-based alternative to the flat time exit.</span>
  </div>"""


def build_report(trade_date: str | None = None, data_dir: str = "pi_data",
                  providers: list[str] | None = None) -> str:
    trade_date = trade_date or _today()
    providers = providers or ["sml", "sml2"]

    base = ROOT / data_dir if not Path(data_dir).is_absolute() else Path(data_dir)
    db_path = base / "stockbot.db"

    provider_summaries = []
    all_trades: list[dict] = []
    for key in providers:
        provider = PROVIDER_MAP.get(key, f"{key.upper()}_SCREENER")
        log_path = resolve_log_path(base, key, date.fromisoformat(trade_date))
        log_data = parse_log(log_path) if log_path.exists() else None
        positions = _fetch_positions(db_path, provider, trade_date)
        trades = _build_trades(key, positions, log_data["sells"] if log_data else [])
        all_trades.extend(trades)
        provider_summaries.append(_provider_summary(key, log_data, trades))

    html = _render(trade_date, provider_summaries, all_trades)

    LOGS_DIR.mkdir(exist_ok=True)
    out_path = LOGS_DIR / f"report_{trade_date}.html"
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


def _render(trade_date: str, summaries: list[dict], trades: list[dict]) -> str:
    date_display = datetime.strptime(trade_date, "%Y-%m-%d").strftime("%b %#d, %Y") \
        if os.name == "nt" else datetime.strptime(trade_date, "%Y-%m-%d").strftime("%b %-d, %Y")

    total_scans = sum(s["scans"] for s in summaries)
    total_closed = sum(s["closed_n"] for s in summaries)
    total_wins = sum(s["wins"] for s in summaries)
    total_losses = sum(s["losses"] for s in summaries)
    total_net_pnl = round(sum(s["net_pnl"] for s in summaries), 2)
    total_open = sum(s["open_n"] for s in summaries)

    provider_cards = "".join(f"""
    <div class="provider-card">
      <div class="provider-head">
        <h3>{s['key']}</h3>
        <div class="flags">{_flags_html(s)}</div>
      </div>
      <div class="mini-stats">
        <div class="mini-stat"><div class="n">{s['scans']}</div><div class="l">Scans</div></div>
        <div class="mini-stat"><div class="n">{s['skip_total']}</div><div class="l">Skips</div></div>
        <div class="mini-stat"><div class="n">{s['buys_attempted']}</div><div class="l">Buys</div></div>
        <div class="mini-stat"><div class="n">{s['fills']}</div><div class="l">Fills</div></div>
        <div class="mini-stat"><div class="n">{s['closed_n']}</div><div class="l">Closed</div></div>
        <div class="mini-stat"><div class="n {'pos' if s['net_pnl'] >= 0 else 'neg'}">${s['net_pnl']:+.2f}</div><div class="l">Net PnL</div></div>
      </div>
      <div class="skip-cols">
        <div>
          <h4>Skip reasons</h4>
          {_skip_bars_html(s['skip_reasons'])}
        </div>
        <div>
          <h4>Most-skipped symbols</h4>
          {_skip_bars_html(s['skip_symbols'])}
        </div>
      </div>
    </div>""" for s in summaries)

    callouts = _cut_short_callout(trades)

    html = _TEMPLATE
    html = html.replace("__DATE_DISPLAY__", date_display)
    html = html.replace("__DATE_ISO__", trade_date)
    html = html.replace("__SCANS_N__", str(total_scans))
    html = html.replace("__TRADES_N__", str(total_closed + total_open))
    html = html.replace("__CLOSED_N__", str(total_closed))
    html = html.replace("__OPEN_N__", str(total_open))
    html = html.replace(
        "__RECORD_NOTE__",
        f'<div class="stat-note">{total_wins}W-{total_losses}L</div>' if total_closed else "",
    )
    html = html.replace("__NET_PNL__", f"{total_net_pnl:+.2f}")
    html = html.replace("__NET_PNL_CLASS__", "pos" if total_net_pnl >= 0 else "neg")
    html = html.replace("__PROVIDER_CARDS__", provider_cards)
    html = html.replace("__CALLOUTS__", callouts)
    html = html.replace("__DATA_JSON__", json.dumps(trades, default=str))
    return html


_TEMPLATE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SML / SML2 — __DATE_ISO__</title>
<style>
:root{
  --bg:#10141c; --surface:#171d29; --surface-2:#1f2733; --border:#2a3341;
  --text:#e8ecf3; --text-dim:#8b96a8; --text-faint:#5c6577;
  --accent:#d9a441; --accent-soft:rgba(217,164,65,.14); --accent-strong:#f0bd5f;
  --good:#4fae8a; --good-soft:rgba(79,174,138,.14);
  --bad:#c85c5c; --bad-soft:rgba(200,92,92,.14);
  --shadow: 0 8px 24px rgba(0,0,0,.35);
  --mono: ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --sans: ui-sans-serif, -apple-system, "Segoe UI", Inter, Roboto, Helvetica, Arial, sans-serif;
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#eef0f4; --surface:#ffffff; --surface-2:#f5f6f9; --border:#dde1e8;
    --text:#1a2029; --text-dim:#5b6472; --text-faint:#8891a0;
    --accent:#a97417; --accent-soft:rgba(169,116,23,.10); --accent-strong:#8a5e10;
    --good:#217a58; --good-soft:rgba(33,122,88,.10);
    --bad:#a83636; --bad-soft:rgba(168,54,54,.10);
    --shadow: 0 8px 24px rgba(30,35,50,.08);
  }
}
:root[data-theme="dark"]{
  --bg:#10141c; --surface:#171d29; --surface-2:#1f2733; --border:#2a3341;
  --text:#e8ecf3; --text-dim:#8b96a8; --text-faint:#5c6577;
  --accent:#d9a441; --accent-soft:rgba(217,164,65,.14); --accent-strong:#f0bd5f;
  --good:#4fae8a; --good-soft:rgba(79,174,138,.14);
  --bad:#c85c5c; --bad-soft:rgba(200,92,92,.14);
  --shadow: 0 8px 24px rgba(0,0,0,.35);
}
:root[data-theme="light"]{
  --bg:#eef0f4; --surface:#ffffff; --surface-2:#f5f6f9; --border:#dde1e8;
  --text:#1a2029; --text-dim:#5b6472; --text-faint:#8891a0;
  --accent:#a97417; --accent-soft:rgba(169,116,23,.10); --accent-strong:#8a5e10;
  --good:#217a58; --good-soft:rgba(33,122,88,.10);
  --bad:#a83636; --bad-soft:rgba(168,54,54,.10);
  --shadow: 0 8px 24px rgba(30,35,50,.08);
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{
  background:var(--bg); color:var(--text); font-family:var(--sans);
  padding:2.5rem 1.5rem 5rem; min-height:100vh;
}
.wrap{max-width:1180px;margin:0 auto;}
header.page{
  display:flex; justify-content:space-between; align-items:flex-end; gap:1.5rem;
  flex-wrap:wrap; margin-bottom:1.75rem; padding-bottom:1.5rem;
  border-bottom:1px solid var(--border);
}
.eyebrow{
  font-family:var(--mono); font-size:.72rem; letter-spacing:.14em; text-transform:uppercase;
  color:var(--accent); margin:0 0 .4rem;
}
h1{ font-size:1.85rem; margin:0; font-weight:700; letter-spacing:-.01em; text-wrap:balance; }
h1 .date{color:var(--text-dim); font-weight:500;}
.subtitle{color:var(--text-dim); font-size:.92rem; margin:.4rem 0 0; max-width:60ch;}
.stats{display:flex; gap:.75rem; flex-wrap:wrap;}
.stat{
  background:var(--surface); border:1px solid var(--border); border-radius:10px;
  padding:.65rem 1rem; min-width:92px;
}
.stat .n{
  font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:1.4rem;
  font-weight:600; line-height:1.1; color:var(--text);
}
.stat .n.pos{color:var(--good);} .stat .n.neg{color:var(--bad);}
.stat .l{font-size:.68rem; color:var(--text-dim); text-transform:uppercase; letter-spacing:.08em; margin-top:.2rem;}
.stat .stat-note{font-size:.66rem; color:var(--text-dim); margin-top:.15rem;}
.callout{
  display:flex; gap:.7rem; align-items:flex-start;
  background:var(--accent-soft); border:1px solid var(--accent); border-radius:10px;
  padding:.85rem 1rem; margin-bottom:1.1rem; font-size:.85rem; color:var(--text);
}
.callout .tag{
  font-family:var(--mono); font-size:.68rem; letter-spacing:.06em; text-transform:uppercase;
  color:var(--accent-strong); font-weight:700; flex-shrink:0; padding-top:.1rem;
}
.providers{display:grid; grid-template-columns:1fr 1fr; gap:1.1rem; margin-bottom:1.75rem;}
@media (max-width:820px){ .providers{grid-template-columns:1fr;} }
.provider-card{
  background:var(--surface); border:1px solid var(--border); border-radius:12px;
  padding:1.1rem 1.2rem; box-shadow:var(--shadow);
}
.provider-head{display:flex; justify-content:space-between; align-items:center; gap:.6rem; flex-wrap:wrap; margin-bottom:.8rem;}
.provider-head h3{margin:0; font-size:1.05rem; font-family:var(--mono); letter-spacing:.02em;}
.flags{display:flex; gap:.4rem; flex-wrap:wrap;}
.flag-pill{
  font-family:var(--mono); font-size:.66rem; text-transform:uppercase; letter-spacing:.04em;
  background:var(--surface-2); border:1px solid var(--border); color:var(--text-dim);
  padding:.2rem .5rem; border-radius:999px; white-space:nowrap;
}
.flag-pill.bad{background:var(--bad-soft); border-color:var(--bad); color:var(--bad);}
.mini-stats{display:grid; grid-template-columns:repeat(6,1fr); gap:.5rem; margin-bottom:1rem;}
.mini-stat{background:var(--surface-2); border-radius:8px; padding:.5rem .3rem; text-align:center;}
.mini-stat .n{font-family:var(--mono); font-variant-numeric:tabular-nums; font-weight:700; font-size:1rem;}
.mini-stat .n.pos{color:var(--good);} .mini-stat .n.neg{color:var(--bad);}
.mini-stat .l{font-size:.6rem; color:var(--text-dim); text-transform:uppercase; letter-spacing:.05em; margin-top:.15rem;}
.skip-cols{display:grid; grid-template-columns:1fr 1fr; gap:1rem;}
.skip-cols h4{margin:0 0 .4rem; font-size:.68rem; text-transform:uppercase; letter-spacing:.07em; color:var(--text-faint); font-weight:700;}
.empty-note{font-size:.78rem; color:var(--text-faint); margin:0;}
.bar-list{display:flex; flex-direction:column; gap:.32rem;}
.bar-row{display:grid; grid-template-columns:1fr 5fr auto; align-items:center; gap:.5rem; font-size:.74rem;}
.bar-label{color:var(--text-dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
.bar-track{height:6px; background:var(--surface-2); border-radius:4px; overflow:hidden;}
.bar-fill{height:100%; background:var(--accent); border-radius:4px;}
.bar-n{font-family:var(--mono); color:var(--text-faint); text-align:right;}
.controls{
  display:flex; justify-content:space-between; align-items:center; gap:1rem;
  margin-bottom:.85rem; flex-wrap:wrap;
}
.filters{display:flex; gap:.4rem; flex-wrap:wrap;}
.filters button{
  font-family:var(--sans); font-size:.78rem; font-weight:600; color:var(--text-dim);
  background:var(--surface); border:1px solid var(--border); border-radius:999px;
  padding:.38rem .85rem; cursor:pointer; transition:all .15s ease;
}
.filters button:hover{color:var(--text); border-color:var(--text-faint);}
.filters button.active{ background:var(--accent-soft); border-color:var(--accent); color:var(--accent-strong); }
.hint{font-size:.76rem; color:var(--text-faint); font-family:var(--mono);}
.table-scroll{
  overflow-x:auto; border:1px solid var(--border); border-radius:12px;
  background:var(--surface); box-shadow:var(--shadow);
}
table{border-collapse:collapse; width:100%; min-width:820px;}
thead th{
  position:sticky; top:0; background:var(--surface-2); z-index:1;
  text-align:left; font-size:.68rem; text-transform:uppercase; letter-spacing:.07em;
  color:var(--text-dim); font-weight:600; padding:.7rem .85rem;
  border-bottom:1px solid var(--border); cursor:pointer; white-space:nowrap;
  user-select:none;
}
thead th:hover{color:var(--text);}
thead th.num{text-align:right;}
thead th .arrow{opacity:0; margin-left:.25rem; font-size:.7em;}
thead th.sorted .arrow{opacity:1; color:var(--accent);}
tbody tr.row{ border-bottom:1px solid var(--border); cursor:pointer; transition:background .12s ease; }
tbody tr.row:hover{background:var(--surface-2);}
tbody tr.row.expanded{background:var(--surface-2);}
tbody tr.row:last-child{border-bottom:none;}
td{padding:.62rem .85rem; font-size:.86rem; vertical-align:middle;}
td.num{ text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; }
td.sym{font-family:var(--mono); font-weight:700; letter-spacing:.02em;}
td.sym .flag{color:var(--accent); margin-left:.35rem; font-size:.75em;}
.chg{font-family:var(--mono); font-variant-numeric:tabular-nums;}
.chg.pos{color:var(--good);} .chg.neg{color:var(--bad);}
.pill{
  display:inline-flex; align-items:center; gap:.3rem; font-family:var(--mono);
  font-size:.68rem; text-transform:uppercase; letter-spacing:.05em; font-weight:700;
  padding:.22rem .55rem; border-radius:999px; white-space:nowrap;
}
.pill.open{background:var(--accent-soft); color:var(--accent-strong);}
.pill.win{background:var(--good-soft); color:var(--good);}
.pill.loss{background:var(--bad-soft); color:var(--bad);}
tr.detail-row td{padding:0; border-bottom:1px solid var(--border);}
.detail{
  display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:1.1rem; padding:1.1rem 1.4rem 1.3rem; background:var(--bg);
}
.detail h4{ margin:0 0 .5rem; font-size:.68rem; text-transform:uppercase; letter-spacing:.08em; color:var(--text-faint); font-weight:700; }
.detail .row2{display:flex; justify-content:space-between; padding:.22rem 0; gap:.6rem; font-size:.82rem;}
.detail .row2 span:first-child{color:var(--text-dim);}
.detail .row2 span:last-child{font-family:var(--mono); font-variant-numeric:tabular-nums; text-align:right;}
footer{ margin-top:2rem; font-size:.75rem; color:var(--text-faint); font-family:var(--mono); text-align:center; }
.empty{ padding:3rem 1rem; text-align:center; color:var(--text-dim); font-size:.9rem; }
</style>
</head>
<body>
<div class="wrap">
  <header class="page">
    <div>
      <p class="eyebrow">SML / SML2 — Momentum Screeners</p>
      <h1>Daily Report <span class="date">· __DATE_DISPLAY__</span></h1>
      <p class="subtitle">Scan/skip activity from the screener logs, trade outcomes from stockbot.db. Click a trade row for entry detail.</p>
    </div>
    <div class="stats">
      <div class="stat"><div class="n">__SCANS_N__</div><div class="l">Scans</div></div>
      <div class="stat"><div class="n">__TRADES_N__</div><div class="l">Positions</div>__RECORD_NOTE__</div>
      <div class="stat"><div class="n">__CLOSED_N__</div><div class="l">Closed</div></div>
      <div class="stat"><div class="n">__OPEN_N__</div><div class="l">Open</div></div>
      <div class="stat"><div class="n __NET_PNL_CLASS__">$__NET_PNL__</div><div class="l">Net PnL</div></div>
    </div>
  </header>
  __CALLOUTS__
  <div class="providers">__PROVIDER_CARDS__</div>
  <div class="controls">
    <div class="filters" id="filters">
      <button data-f="all" class="active">All</button>
      <button data-f="SML">SML</button>
      <button data-f="SML2">SML2</button>
      <button data-f="open">Open</button>
      <button data-f="win">Wins</button>
      <button data-f="loss">Losses</button>
    </div>
    <span class="hint">click column header to sort · click row to expand</span>
  </div>
  <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th data-k="provider">Screener<span class="arrow">▾</span></th>
          <th data-k="symbol">Symbol<span class="arrow">▾</span></th>
          <th data-k="buy_price" class="num">Buy<span class="arrow">▾</span></th>
          <th data-k="sell_price" class="num">Sell<span class="arrow">▾</span></th>
          <th data-k="hold_minutes" class="num">Hold (m)<span class="arrow">▾</span></th>
          <th data-k="pnl" class="num">PnL $<span class="arrow">▾</span></th>
          <th data-k="pnl_pct" class="num">PnL %<span class="arrow">▾</span></th>
          <th data-k="exit_reason">Exit reason</th>
          <th data-k="status">Status</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <footer>__DATE_ISO__ · sml.log / sml2.log (SKIP/BUY/FILLED/SOLD) &amp; stockbot.db (positions)</footer>
</div>
<script>
const DATA = __DATA_JSON__;

const fmtUSD = v => v==null ? "—" : "$" + Number(v).toFixed(4);
const fmtMoney = v => v==null ? "—" : (v>=0?"+":"") + "$" + Number(v).toFixed(2);
const fmtPct = v => v==null ? "—" : (v>0?"+":"") + Number(v).toFixed(1) + "%";
const fmtNum = (v, d=2) => v==null ? "—" : Number(v).toFixed(d);

function statusPill(d){
  if(d.status==="open") return `<span class="pill open">Open</span>`;
  const win = (d.pnl||0) > 0;
  return `<span class="pill ${win?'win':'loss'}">${win?'Win':'Loss'}</span>`;
}

let sortKey = "hold_minutes", sortDir = -1, activeFilter = "all";

function filtered(){
  return DATA.filter(d=>{
    if(activeFilter==="SML"||activeFilter==="SML2") return d.provider===activeFilter;
    if(activeFilter==="open") return d.status==="open";
    if(activeFilter==="win") return d.status==="closed" && (d.pnl||0) > 0;
    if(activeFilter==="loss") return d.status==="closed" && (d.pnl||0) <= 0;
    return true;
  });
}

function render(){
  let rows = filtered().slice();
  rows.sort((a,b)=>{
    let av=a[sortKey], bv=b[sortKey];
    if(sortKey==="provider"||sortKey==="symbol"||sortKey==="exit_reason"||sortKey==="status"){
      av=av||""; bv=bv||""; return sortDir*av.localeCompare(bv);
    }
    av = av==null ? -Infinity : av; bv = bv==null ? -Infinity : bv;
    return sortDir*(av-bv);
  });

  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  if(rows.length===0){
    tbody.innerHTML = `<tr><td colspan="9" class="empty">No trades match this filter.</td></tr>`;
    return;
  }
  rows.forEach(d=>{
    const tr = document.createElement("tr");
    tr.className = "row";
    tr.innerHTML = `
      <td>${d.provider}</td>
      <td class="sym">${d.symbol}${d.cut_short?'<span class="flag" title="Cut short by MAX_HOLD_MINUTES while still up >5%">⏱</span>':''}</td>
      <td class="num">${fmtUSD(d.buy_price)}</td>
      <td class="num">${fmtUSD(d.sell_price)}</td>
      <td class="num">${fmtNum(d.hold_minutes, 0)}</td>
      <td class="num chg ${(d.pnl||0)>=0?'pos':'neg'}">${fmtMoney(d.pnl)}</td>
      <td class="num chg ${(d.pnl_pct||0)>=0?'pos':'neg'}">${fmtPct(d.pnl_pct)}</td>
      <td>${d.exit_reason || (d.status==='open' ? '—' : '—')}</td>
      <td>${statusPill(d)}</td>
    `;
    tr.addEventListener("click", ()=> toggleDetail(tr, d));
    tbody.appendChild(tr);
  });
}

function toggleDetail(tr, d){
  const next = tr.nextElementSibling;
  if(next && next.classList.contains("detail-row")){
    next.remove();
    tr.classList.remove("expanded");
    return;
  }
  document.querySelectorAll("tr.detail-row").forEach(r=>r.remove());
  document.querySelectorAll("tr.row.expanded").forEach(r=>r.classList.remove("expanded"));
  tr.classList.add("expanded");

  const detailTr = document.createElement("tr");
  detailTr.className = "detail-row";
  detailTr.innerHTML = `<td colspan="9"><div class="detail">
    <div>
      <h4>Entry signal</h4>
      <div class="row2"><span>RSI</span><span>${fmtNum(d.rsi, 1)}</span></div>
      <div class="row2"><span>Change %</span><span>${fmtPct(d.change_pct)}</span></div>
      <div class="row2"><span>RVOL</span><span>${d.rvol!=null ? fmtNum(d.rvol,1)+'×' : '—'}</span></div>
      <div class="row2"><span>ATR</span><span>${fmtNum(d.atr, 4)}</span></div>
      <div class="row2"><span>VWAP z-score</span><span>${d.vwap_z!=null ? fmtNum(d.vwap_z,2) : '—'}</span></div>
      <div class="row2"><span>MACD fresh</span><span>${d.macd_fresh==null ? '—' : (d.macd_fresh ? 'Yes' : 'No')}</span></div>
    </div>
    <div>
      <h4>Timing</h4>
      <div class="row2"><span>Buy time (ET)</span><span>${d.buy_time || '—'}</span></div>
      <div class="row2"><span>Sell time (ET)</span><span>${d.sell_time || '—'}</span></div>
      <div class="row2"><span>Shares</span><span>${d.shares}</span></div>
      <div class="row2"><span>Stop % (ATR-sized)</span><span>${d.stop_pct!=null ? fmtNum(d.stop_pct,1)+'%' : '—'}</span></div>
    </div>
  </div></td>`;
  tr.after(detailTr);
}

document.querySelectorAll("#filters button").forEach(btn=>{
  btn.addEventListener("click", ()=>{
    document.querySelectorAll("#filters button").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    activeFilter = btn.dataset.f;
    render();
  });
});
document.querySelectorAll("thead th").forEach(th=>{
  th.addEventListener("click", ()=>{
    const k = th.dataset.k;
    if(sortKey===k) sortDir *= -1; else { sortKey=k; sortDir = -1; }
    document.querySelectorAll("thead th").forEach(t=>t.classList.remove("sorted"));
    th.classList.add("sorted");
    th.querySelector(".arrow").textContent = sortDir===1 ? "▴" : "▾";
    render();
  });
});
document.querySelector('thead th[data-k="hold_minutes"]').classList.add("sorted");
render();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a day's SML/SML2 screener activity as an HTML dashboard.")
    parser.add_argument("--date", help="YYYY-MM-DD (defaults to today, ET)")
    parser.add_argument("--dir", default="pi_data", help="directory holding {provider}.log + stockbot.db (default: pi_data)")
    parser.add_argument("--providers", nargs="+", default=["sml", "sml2"], help="which screeners to include (default: sml sml2)")
    parser.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    args = parser.parse_args()

    path = build_report(args.date, args.dir, args.providers)
    print(f"Report written to {path}")
    if not args.no_open:
        webbrowser.open(f"file://{os.path.abspath(path)}")


if __name__ == "__main__":
    main()
