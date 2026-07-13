"""
Multi-day trading report — Discord version of the "July Trading Report" style
summary: combined stats, per-strategy breakdown, an equity-curve chart, and
a trade log, for one or more screeners over a date range.

Usage:
    python tools/report_range.py                                # this month, SML + SML2
    python tools/report_range.py 2026-07-01 2026-07-09           # specific range, SML + SML2
    python tools/report_range.py 2026-07-01 2026-07-09 SML,MID   # specific range + providers

Config (env or .env):
    DISCORD_WEBHOOK_URL   required
    ALPACA_PAPER          true/false  (shown in footer)
"""
import io
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytz
import requests
from dotenv import load_dotenv

from bot.database import _connect, get_wallet

load_dotenv()

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
ALPACA_PAPER    = os.getenv("ALPACA_PAPER", "true").lower() == "true"

_GREEN = 0x00FF7F
_RED   = 0xFF4444
_BLUE  = 0x5865F2

_SERIES_COLORS = ["#2a78d6", "#1baf7a", "#e3a13e", "#a366d9"]


def _post_with_file(webhook_url: str, payload: dict, filename: str, file_bytes: bytes):
    resp = requests.post(
        webhook_url,
        data={"payload_json": __import__("json").dumps(payload)},
        files={"file": (filename, file_bytes, "image/png")},
        timeout=20,
    )
    if resp.status_code not in (200, 204):
        print(f"Discord returned {resp.status_code}: {resp.text[:300]}")


def _post(webhook_url: str, payload: dict):
    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code not in (200, 204):
        print(f"Discord returned {resp.status_code}: {resp.text[:300]}")


def _fetch_closed(provider: str, start: date, end: date) -> list:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT symbol, shares, buy_price, sell_price, pnl, sell_time
               FROM positions
               WHERE status = 'closed'
                 AND provider = ?
                 AND date(sell_time) BETWEEN ? AND ?
               ORDER BY sell_time""",
            (provider, start.isoformat(), end.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def _build_equity_chart(series: dict, starting_balance: dict) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 3.2), dpi=150)
    fig.patch.set_facecolor("#0d0d0d")
    ax.set_facecolor("#0d0d0d")

    for i, (provider, trades) in enumerate(series.items()):
        start_bal = starting_balance.get(provider, 500.0)
        equity = [start_bal]
        for t in trades:
            equity.append(equity[-1] + (t["pnl"] or 0))
        color = _SERIES_COLORS[i % len(_SERIES_COLORS)]
        ax.plot(range(len(equity)), equity, color=color, linewidth=2, label=provider)
        ax.scatter([len(equity) - 1], [equity[-1]], color=color, s=25, zorder=5)

    ax.axhline(500.0, color="#555", linestyle="--", linewidth=1)
    ax.set_xlabel("Trade sequence", color="#aaa", fontsize=9)
    ax.set_ylabel("Equity ($)", color="#aaa", fontsize=9)
    ax.tick_params(colors="#aaa", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333")
    ax.grid(True, color="#2c2c2a", linewidth=0.5)
    legend = ax.legend(facecolor="#1a1a19", edgecolor="#333", labelcolor="#eee", fontsize=8)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _provider_stats(trades: list, start_bal: float) -> dict:
    pnl = sum(t["pnl"] or 0 for t in trades)
    wins = sum(1 for t in trades if (t["pnl"] or 0) >= 0)
    losses = sum(1 for t in trades if (t["pnl"] or 0) < 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0
    equity = start_bal + pnl
    ret_pct = (pnl / start_bal * 100) if start_bal else 0.0
    return {
        "pnl": pnl, "wins": wins, "losses": losses, "win_rate": win_rate,
        "equity": equity, "ret_pct": ret_pct, "count": len(trades),
    }


def _send_summary(start: date, end: date, providers: list, series: dict, stats: dict, image_name: str):
    mode_tag = "[PAPER] " if ALPACA_PAPER else ""
    total_pnl = sum(s["pnl"] for s in stats.values())
    total_trades = sum(s["count"] for s in stats.values())
    total_wins = sum(s["wins"] for s in stats.values())
    total_start = sum(s["start_bal"] for s in stats.values())
    combined_ret = (total_pnl / total_start * 100) if total_start else 0.0
    combined_win_rate = (total_wins / total_trades * 100) if total_trades else 0.0

    fields = [
        {"name": "Combined P&L",      "value": f"${total_pnl:+.2f}",              "inline": True},
        {"name": "Combined Return",   "value": f"{combined_ret:+.2f}%",           "inline": True},
        {"name": "Total Trades",      "value": str(total_trades),                 "inline": True},
        {"name": "Combined Win Rate", "value": f"{combined_win_rate:.1f}% ({total_wins}W / {total_trades - total_wins}L)", "inline": True},
    ]
    for provider in providers:
        s = stats[provider]
        fields.append({
            "name": provider,
            "value": (
                f"P&L: ${s['pnl']:+.2f} ({s['ret_pct']:+.2f}%)\n"
                f"Equity: ${s['equity']:.2f}\n"
                f"Win rate: {s['win_rate']:.1f}% ({s['wins']}W / {s['losses']}L)"
            ),
            "inline": True,
        })

    embed = {
        "title": f"{mode_tag}Trading Report — {start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}",
        "color": _GREEN if total_pnl >= 0 else _RED,
        "fields": fields,
        "image": {"url": f"attachment://{image_name}"},
        "footer": {"text": f"StockTipsBot | {'Paper' if ALPACA_PAPER else 'Live'} Trading"},
    }
    return embed


def _send_trade_log(provider: str, trades: list, color: int):
    lines = []
    for t in trades:
        d = datetime.fromisoformat(t["sell_time"]).strftime("%b %d")
        pnl_str = f"${t['pnl']:+.2f}" if t["pnl"] is not None else "n/a"
        lines.append(f"`{d}` `{t['symbol']:<6}` buy ${t['buy_price']:.4f} sell ${t['sell_price']:.4f} **{pnl_str}**")

    embed = {
        "title": f"{provider} — Trade Log ({len(trades)})",
        "description": "\n".join(lines) if lines else "_No trades._",
        "color": color,
    }
    _post(DISCORD_WEBHOOK, {"embeds": [embed]})


def main():
    if not DISCORD_WEBHOOK:
        print("DISCORD_WEBHOOK_URL not set — nothing to send.")
        return

    today = datetime.now(pytz.timezone("America/New_York")).date()

    if len(sys.argv) > 1:
        start = date.fromisoformat(sys.argv[1])
    else:
        start = today.replace(day=1)

    end = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else today

    if len(sys.argv) > 3:
        providers = [f"{p.strip().upper()}_SCREENER" for p in sys.argv[3].split(",")]
    else:
        providers = ["SML_SCREENER", "SML2_SCREENER"]

    print(f"Reporting {start} to {end} for {', '.join(providers)}...")

    series = {}
    stats = {}
    starting_balance = {}
    for provider in providers:
        trades = _fetch_closed(provider, start, end)
        series[provider] = trades
        screener_id = provider.replace("_SCREENER", "")
        wallet = get_wallet(screener_id)
        start_bal = wallet["initial_balance"] if wallet else 500.0
        starting_balance[provider] = start_bal
        s = _provider_stats(trades, start_bal)
        s["start_bal"] = start_bal
        stats[provider] = s

    if all(s["count"] == 0 for s in stats.values()):
        print("No trades found for that range.")
        _post(DISCORD_WEBHOOK, {"embeds": [{
            "title":       f"Trading Report — {start} to {end}",
            "description": "_No trades in this range._",
            "color":       _BLUE,
            "footer":      {"text": "StockTipsBot"},
        }]})
        return

    image_name = "equity_curve.png"
    chart_bytes = _build_equity_chart(series, starting_balance)
    summary_embed = _send_summary(start, end, providers, series, stats, image_name)

    print("Posting summary + equity curve...")
    _post_with_file(DISCORD_WEBHOOK, {"embeds": [summary_embed]}, image_name, chart_bytes)

    for i, provider in enumerate(providers):
        if series[provider]:
            print(f"Posting trade log for {provider}...")
            _send_trade_log(provider, series[provider], _GREEN if stats[provider]["pnl"] >= 0 else _RED)

    print("Done.")


if __name__ == "__main__":
    main()
