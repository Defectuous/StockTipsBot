"""
Mid-cap most-active + MACD/RSI screener loop ($2–$20).

Every SCAN_INTERVAL_SECONDS the script:
  1. Fetches the top 100 most active stocks in the $2–$20 range
  2. Screens each for a bullish MACD + RSI bounce setup
  3. Buys any that pass, subject to per-stock cooldown and MAX_POSITIONS limit

Wallet:
  Each day the script reconciles its cash wallet against Alpaca's account cash.
  25% of the day's opening balance is held in reserve (never traded).
  Trade size = deployable_capital / MAX_POSITIONS, fixed for the day.
  Wallet grows or shrinks across days as PnL accumulates.

Config (env vars or .env):
  SCREENER_ID             wallet/provider identifier               default: MID
  STARTING_BALANCE        initial wallet balance (first run only)  default: 500
  MAX_POSITIONS           max concurrent open positions            default: 2
  RESERVE_PCT             % of day-start balance held in reserve   default: 25
  TRAILING_STOP_PERCENT   trailing-stop distance %                 default: 10
  BUY_COOLDOWN_SECONDS    min seconds between buys/stock           default: 86400
  SCAN_INTERVAL_SECONDS   seconds between full scans               default: 60
  ALPACA_PAPER            true / false                             default: true
  DISCORD_WEBHOOK_URL     webhook for buy/error alerts             optional
  PROFIT_LOCK_PCT         gain % to tighten trailing stop          default: 50
  TIGHT_STOP_PCT          tighter stop % after profit lock         default: 5
  RSI_EXIT_LEVEL          RSI level to exit on (declining)         default: 75
  MAX_HOLD_MINUTES        force-sell after this many min           default: 120
  START_TIME_ET           don't scan before this time ET           default: "" (off)
  STOP_BUY_TIME_ET        stop new buys after this time ET         default: "" (off)
  DUMP_TIME_ET            force-sell all at clock time ET          default: "" (off)
  HARD_STOP_PCT           hard stop loss % from entry              default: 0 (off)
  MAX_ENTRY_MOVE_PCT      skip buys already up > this %            default: 0 (off)
  MAX_ATR                 skip buys with ATR above this            default: 0 (off)
  MAX_RVOL                skip buys with RVOL above this           default: 0 (off)
  MID_MIN_PRICE           lower price bound                        default: 2.00
  MID_MAX_PRICE           upper price bound                        default: 20.00
"""
import logging
import os
import time
import warnings
from datetime import datetime, timedelta, timezone

import pytz
from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.database import (
    close_position,
    get_open_position_count,
    get_open_positions,
    get_wallet,
    init_db,
    init_wallet,
    is_ticker_on_cooldown,
    mark_stop_tightened,
    record_ticker_alert,
    reset_day_wallet,
    save_position,
    update_trailing_stop_order,
    update_wallet_cash,
)
from bot.discord_notify import send_alert, send_close, send_error
from bot.market_data import _rsi_series
from bot.most_active import get_most_active_penny_stocks
from bot.screener import _analyze
from bot.trader import Trader

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("mid.log"),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()

ALPACA_KEY      = os.getenv("MID_ALPACA_API_KEY")    or os.environ["ALPACA_API_KEY"]
ALPACA_SECRET   = os.getenv("MID_ALPACA_API_SECRET") or os.environ["ALPACA_API_SECRET"]
ALPACA_PAPER    = os.getenv("ALPACA_PAPER", "true").lower() == "true"
SCREENER_ID     = os.getenv("MID_SCREENER_ID",        "MID")
STARTING_BALANCE = float(os.getenv("MID_STARTING_BALANCE", "500"))
MAX_POSITIONS   = int(os.getenv("MID_MAX_POSITIONS",   "2"))
RESERVE_PCT     = float(os.getenv("MID_RESERVE_PCT",   "25"))
TRAIL_PCT       = float(os.getenv("TRAILING_STOP_PERCENT",  "10"))
COOLDOWN_SECS   = int(os.getenv("BUY_COOLDOWN_SECONDS",     "86400"))
SCAN_INTERVAL   = int(os.getenv("SCAN_INTERVAL_SECONDS",    "60"))
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL",          "")
PROFIT_LOCK_PCT = float(os.getenv("PROFIT_LOCK_PCT",        "50"))
TIGHT_STOP_PCT  = float(os.getenv("TIGHT_STOP_PCT",         "5"))
RSI_EXIT_LEVEL  = float(os.getenv("RSI_EXIT_LEVEL",         "75"))
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES",        "120"))
START_TIME_ET     = os.getenv("START_TIME_ET",               "")
STOP_BUY_TIME_ET  = os.getenv("STOP_BUY_TIME_ET",            "")
DUMP_TIME_ET      = os.getenv("DUMP_TIME_ET",                "")
HARD_STOP_PCT      = float(os.getenv("HARD_STOP_PCT",        "0"))
MAX_ENTRY_MOVE_PCT = float(os.getenv("MAX_ENTRY_MOVE_PCT",   "0"))
MAX_ATR            = float(os.getenv("MAX_ATR",              "0"))
MAX_RVOL           = float(os.getenv("MAX_RVOL",             "0"))
MIN_PRICE       = float(os.getenv("MID_MIN_PRICE",          "2.00"))
MAX_PRICE       = float(os.getenv("MID_MAX_PRICE",          "20.00"))

PROVIDER = f"{SCREENER_ID}_SCREENER"

_5MIN  = TimeFrame(5,  TimeFrameUnit.Minute)
_15MIN = TimeFrame(15, TimeFrameUnit.Minute)


def _compute_buy_amount(screener_id: str) -> float:
    wallet = get_wallet(screener_id)
    if not wallet:
        return 0.0
    deployable = wallet["day_start_balance"] * (1 - RESERVE_PCT / 100)
    return deployable / MAX_POSITIONS


def _log_wallet(screener_id: str) -> None:
    wallet = get_wallet(screener_id)
    if not wallet:
        return
    reserve    = wallet["day_start_balance"] * RESERVE_PCT / 100
    deployable = wallet["day_start_balance"] - reserve
    available  = wallet["current_balance"] - reserve
    logger.info(
        "Wallet [%s]  total=$%.2f  reserve=$%.2f  deployable=$%.2f  available=$%.2f",
        screener_id,
        wallet["current_balance"],
        reserve,
        deployable,
        max(available, 0),
    )


def _maybe_reset_day(
    trader: Trader, screener_id: str, last_day: list
) -> None:
    today_et = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    if last_day[0] == today_et:
        return

    wallet       = get_wallet(screener_id)
    alpaca_cash  = trader.get_cash_balance()
    db_balance   = wallet["current_balance"] if wallet else STARTING_BALANCE

    if alpaca_cash is not None:
        logger.info(
            "Day reset [%s]: DB cash=$%.2f  Alpaca cash=$%.2f  → using Alpaca",
            screener_id, db_balance, alpaca_cash,
        )
        reconciled = alpaca_cash
    else:
        logger.warning(
            "Day reset [%s]: Alpaca cash unavailable, keeping DB value $%.2f",
            screener_id, db_balance,
        )
        reconciled = db_balance

    reset_day_wallet(screener_id, today_et, reconciled)
    last_day[0] = today_et
    _log_wallet(screener_id)


def monitor_positions(
    trader: Trader,
    data_client: StockHistoricalDataClient,
    provider: str,
    screener_id: str,
) -> None:
    positions = [dict(r) for r in get_open_positions(provider)]
    if not positions:
        return

    symbols = list({p["symbol"] for p in positions})
    now     = datetime.now(pytz.UTC)

    try:
        snaps = data_client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbols)
        )
    except Exception as e:
        logger.warning("Monitor snapshot failed: %s", e)
        snaps = {}

    try:
        bars5 = data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=_5MIN,
            start=now - timedelta(minutes=120),
            end=now,
        )).data
    except Exception as e:
        logger.warning("Monitor bars failed: %s", e)
        bars5 = {}

    for pos in positions:
        sym            = pos["symbol"]
        pos_id         = pos["id"]
        buy_price      = pos["buy_price"]
        shares         = pos["shares"]
        stop_order_id  = pos.get("trailing_stop_order_id")
        stop_tightened = pos.get("stop_tightened", 0)

        # ── 1. DB sync: check if trailing stop was already filled on Alpaca ──
        if stop_order_id:
            try:
                stop_order = trader.client.get_order_by_id(stop_order_id)
                if stop_order.status.value == "filled":
                    fill_price = float(stop_order.filled_avg_price)
                    pnl        = (fill_price - buy_price) * shares
                    close_position(pos_id, fill_price, datetime.now(timezone.utc), pnl)
                    update_wallet_cash(screener_id, fill_price * shares)
                    logger.info("  SYNC  %s closed @ $%.4f  PnL=$%+.2f", sym, fill_price, pnl)
                    if DISCORD_WEBHOOK:
                        send_close(DISCORD_WEBHOOK, sym, buy_price, fill_price, shares, pnl,
                                   paper=ALPACA_PAPER, reason="Trailing stop filled")
                    continue
            except Exception as e:
                logger.debug("Stop order check failed for %s: %s", sym, e)

        snap = snaps.get(sym)
        if not snap:
            continue
        current_price = (
            snap.latest_trade.price if snap.latest_trade
            else snap.daily_bar.close if snap.daily_bar
            else None
        )
        if current_price is None:
            continue

        gain_pct = (current_price - buy_price) / buy_price * 100

        # ── 2. Hard stop loss ─────────────────────────────────────────────────
        if HARD_STOP_PCT > 0 and gain_pct <= -HARD_STOP_PCT:
            logger.info("  HARD STOP %s  gain=%.2f%%  (limit=%.0f%%)", sym, gain_pct, HARD_STOP_PCT)
            if stop_order_id:
                trader.cancel_order(stop_order_id)
            sell = trader.market_sell(sym, shares)
            if sell:
                filled = trader.wait_for_fill(str(sell.id), timeout=30)
                if filled:
                    fp  = float(filled.filled_avg_price)
                    pnl = (fp - buy_price) * shares
                    close_position(pos_id, fp, datetime.now(timezone.utc), pnl)
                    update_wallet_cash(screener_id, fp * shares)
                    logger.info("  SOLD  %s @ $%.4f  PnL=$%+.2f  (hard stop)", sym, fp, pnl)
                    if DISCORD_WEBHOOK:
                        send_close(DISCORD_WEBHOOK, sym, buy_price, fp, shares, pnl,
                                   paper=ALPACA_PAPER, reason=f"Hard stop -{HARD_STOP_PCT:.0f}%")
            continue

        # ── 3. Time exit ──────────────────────────────────────────────────────
        buy_dt   = datetime.fromisoformat(pos["buy_time"])
        held_min = (now - buy_dt).total_seconds() / 60
        if held_min >= MAX_HOLD_MINUTES:
            logger.info("  TIME EXIT %s  held %.0fm  gain=%+.1f%%", sym, held_min, gain_pct)
            if stop_order_id:
                trader.cancel_order(stop_order_id)
            sell = trader.market_sell(sym, shares)
            if sell:
                filled = trader.wait_for_fill(str(sell.id), timeout=30)
                if filled:
                    fp  = float(filled.filled_avg_price)
                    pnl = (fp - buy_price) * shares
                    close_position(pos_id, fp, datetime.now(timezone.utc), pnl)
                    update_wallet_cash(screener_id, fp * shares)
                    logger.info("  SOLD  %s @ $%.4f  PnL=$%+.2f  (time exit)", sym, fp, pnl)
                    if DISCORD_WEBHOOK:
                        send_close(DISCORD_WEBHOOK, sym, buy_price, fp, shares, pnl,
                                   paper=ALPACA_PAPER, reason="Max hold time exit")
            continue

        # ── 4. Dump time ──────────────────────────────────────────────────────
        if DUMP_TIME_ET:
            now_et    = now.astimezone(pytz.timezone("America/New_York"))
            dump_h, dump_m = map(int, DUMP_TIME_ET.split(":"))
            if (now_et.hour, now_et.minute) >= (dump_h, dump_m):
                logger.info("  DUMP EXIT %s  %s ET  gain=%+.1f%%", sym, DUMP_TIME_ET, gain_pct)
                if stop_order_id:
                    trader.cancel_order(stop_order_id)
                sell = trader.market_sell(sym, shares)
                if sell:
                    filled = trader.wait_for_fill(str(sell.id), timeout=30)
                    if filled:
                        fp  = float(filled.filled_avg_price)
                        pnl = (fp - buy_price) * shares
                        close_position(pos_id, fp, datetime.now(timezone.utc), pnl)
                        update_wallet_cash(screener_id, fp * shares)
                        logger.info("  SOLD  %s @ $%.4f  PnL=$%+.2f  (dump time)",
                                    sym, fp, pnl)
                        if DISCORD_WEBHOOK:
                            send_close(DISCORD_WEBHOOK, sym, buy_price, fp, shares, pnl,
                                       paper=ALPACA_PAPER, reason=f"Dump time {DUMP_TIME_ET} ET")
                continue

        # ── 5. RSI exit ───────────────────────────────────────────────────────
        sym_bars = list(bars5.get(sym, []))
        if len(sym_bars) >= 20:
            closes   = [b.close for b in sym_bars]
            rsi_vals = [r for r in _rsi_series(closes) if r is not None]
            if len(rsi_vals) >= 4:
                rsi         = rsi_vals[-1]
                rsi_falling = rsi_vals[-1] < rsi_vals[-3]
                if rsi > RSI_EXIT_LEVEL and rsi_falling:
                    logger.info("  RSI EXIT %s  RSI=%.1f (falling)  gain=%+.1f%%",
                                sym, rsi, gain_pct)
                    if stop_order_id:
                        trader.cancel_order(stop_order_id)
                    sell = trader.market_sell(sym, shares)
                    if sell:
                        filled = trader.wait_for_fill(str(sell.id), timeout=30)
                        if filled:
                            fp  = float(filled.filled_avg_price)
                            pnl = (fp - buy_price) * shares
                            close_position(pos_id, fp, datetime.now(timezone.utc), pnl)
                            update_wallet_cash(screener_id, fp * shares)
                            logger.info("  SOLD  %s @ $%.4f  PnL=$%+.2f  (RSI exit)",
                                        sym, fp, pnl)
                            if DISCORD_WEBHOOK:
                                send_close(DISCORD_WEBHOOK, sym, buy_price, fp, shares, pnl,
                                           paper=ALPACA_PAPER, reason="RSI overbought exit")
                    continue

        # ── 6. Profit lock ────────────────────────────────────────────────────
        if not stop_tightened and gain_pct >= PROFIT_LOCK_PCT:
            logger.info("  LOCK  %s  +%.1f%% -> tightening stop %.0f%% -> %.0f%%",
                        sym, gain_pct, TRAIL_PCT, TIGHT_STOP_PCT)
            cancelled = trader.cancel_order(stop_order_id) if stop_order_id else True
            if cancelled:
                new_stop = trader.submit_trailing_stop(sym, shares, TIGHT_STOP_PCT)
                if new_stop:
                    mark_stop_tightened(pos_id, str(new_stop.id))
                    logger.info("  STOP  %s tightened to %.0f%%  id=%s",
                                sym, TIGHT_STOP_PCT, new_stop.id)
            else:
                logger.warning("  LOCK  %s — cancel failed, skipping new stop to avoid duplicates", sym)


def scan_and_trade(
    trader: Trader,
    data_client: StockHistoricalDataClient,
    provider: str,
    screener_id: str,
) -> None:
    now    = datetime.now(pytz.UTC)
    now_et = now.astimezone(pytz.timezone("America/New_York"))
    ts     = now_et.strftime("%H:%M:%S")
    hm     = (now_et.hour, now_et.minute)

    if START_TIME_ET:
        sh, sm = map(int, START_TIME_ET.split(":"))
        if hm < (sh, sm):
            logger.info("[%s] Before start time %s ET — waiting", ts, START_TIME_ET)
            return

    if STOP_BUY_TIME_ET:
        bh, bm = map(int, STOP_BUY_TIME_ET.split(":"))
        if hm >= (bh, bm):
            logger.info("[%s] Past buy cutoff %s ET — no new buys", ts, STOP_BUY_TIME_ET)
            return

    # ── Position cap check ────────────────────────────────────────────────────
    open_count = get_open_position_count(provider)
    if open_count >= MAX_POSITIONS:
        logger.info("[%s] Position cap reached (%d/%d) — no new buys", ts, open_count, MAX_POSITIONS)
        return

    # ── Wallet check ──────────────────────────────────────────────────────────
    wallet = get_wallet(screener_id)
    if not wallet:
        logger.error("No wallet found for %s — skipping scan", screener_id)
        return

    buy_amount = _compute_buy_amount(screener_id)
    reserve    = wallet["day_start_balance"] * RESERVE_PCT / 100
    available  = wallet["current_balance"] - reserve

    if available < buy_amount:
        logger.info(
            "[%s] Insufficient deployable cash: available=$%.2f  needed=$%.2f",
            ts, available, buy_amount,
        )
        return

    # ── 1. Most active stocks in price range ─────────────────────────────────
    actives = get_most_active_penny_stocks(ALPACA_KEY, ALPACA_SECRET,
                                           min_price=MIN_PRICE, max_price=MAX_PRICE)
    if not actives:
        logger.info("[%s] No most-active data returned.", ts)
        return

    symbols    = [s.symbol for s in actives]
    price_map  = {s.symbol: (s.price, None) for s in actives}
    volume_map = {s.symbol: s.volume for s in actives}

    # ── 2. Snapshots ──────────────────────────────────────────────────────────
    prev_vol_map: dict = {}
    try:
        snaps = data_client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbols)
        )
        for sym, snap in snaps.items():
            if snap and snap.daily_bar and snap.previous_daily_bar:
                price = snap.daily_bar.close
                prev  = snap.previous_daily_bar.close
                chg   = round((price - prev) / prev * 100, 2) if prev else 0.0
                price_map[sym] = (price, chg)
                if snap.previous_daily_bar.volume:
                    prev_vol_map[sym] = snap.previous_daily_bar.volume
    except Exception as e:
        logger.warning("Snapshot fetch failed: %s", e)

    # ── 3. Bars ───────────────────────────────────────────────────────────────
    try:
        bars5 = data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=_5MIN,
            start=now - timedelta(minutes=120),
            end=now,
        )).data
    except Exception as e:
        logger.warning("5-min bar fetch failed: %s", e)
        bars5 = {}

    try:
        bars15 = data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=_15MIN,
            start=now - timedelta(days=3),
            end=now,
        )).data
    except Exception as e:
        logger.warning("15-min bar fetch failed: %s", e)
        bars15 = {}

    # ── 4. Screen ─────────────────────────────────────────────────────────────
    passing = []
    for sym in symbols:
        price, chg = price_map[sym]
        if chg is None:
            chg = 0.0
        result = _analyze(
            sym,
            list(bars5.get(sym,  [])),
            list(bars15.get(sym, [])),
            price,
            chg,
        )
        if result and result.passes:
            passing.append(result)

    logger.info(
        "[%s] Scanned %d stocks ($%.0f–$%.0f) → %d passing MACD+RSI  positions=%d/%d  buy=$%.2f",
        ts, len(symbols), MIN_PRICE, MAX_PRICE, len(passing), open_count, MAX_POSITIONS, buy_amount,
    )

    if not passing:
        return

    # ── 5. Buy ────────────────────────────────────────────────────────────────
    for stock in passing:
        if get_open_position_count(provider) >= MAX_POSITIONS:
            break

        wallet    = get_wallet(screener_id)
        available = wallet["current_balance"] - wallet["day_start_balance"] * RESERVE_PCT / 100
        if available < buy_amount:
            logger.info("  SKIP  — depleted available cash after buying")
            break

        sym = stock.symbol

        if is_ticker_on_cooldown(sym, COOLDOWN_SECS):
            logger.info("  SKIP %s — cooldown", sym)
            continue

        if MAX_ENTRY_MOVE_PCT > 0 and stock.change_pct > MAX_ENTRY_MOVE_PCT:
            logger.info("  SKIP %s — already up %.1f%% today (limit %.0f%%)",
                        sym, stock.change_pct, MAX_ENTRY_MOVE_PCT)
            continue

        if MAX_ATR > 0 and stock.atr and stock.atr > MAX_ATR:
            logger.info("  SKIP %s — ATR %.4f exceeds limit %.4f",
                        sym, stock.atr, MAX_ATR)
            continue

        today_vol = volume_map.get(sym, 0)
        prev_vol  = prev_vol_map.get(sym)
        rvol_now  = (today_vol / prev_vol) if prev_vol else None
        if MAX_RVOL > 0 and rvol_now and rvol_now > MAX_RVOL:
            logger.info("  SKIP %s — RVOL %.1fx exceeds limit %.0fx",
                        sym, rvol_now, MAX_RVOL)
            continue

        logger.info("  BUY  %s  $%.4f  RSI=%.1f  chg=%+.2f%%  budget=$%.2f",
                    sym, stock.price, stock.rsi, stock.change_pct, buy_amount)

        order, err = trader.buy_stock(sym, buy_amount, stock.price)
        if err:
            logger.error("  Buy failed for %s: %s", sym, err)
            if DISCORD_WEBHOOK and "insufficient buying power" not in err:
                send_error(DISCORD_WEBHOOK, f"Buy failed for **{sym}**: {err}")
            if "insufficient buying power" in err:
                break
            continue

        filled = trader.wait_for_fill(str(order.id), timeout=180)
        if not filled:
            logger.error("  %s order did not fill within 60s", sym)
            continue

        fill_price = float(filled.filled_avg_price)
        fill_qty   = int(float(filled.filled_qty))
        cost       = fill_price * fill_qty
        logger.info("  FILLED %s  %d × $%.4f = $%.2f", sym, fill_qty, fill_price, cost)

        update_wallet_cash(screener_id, -cost)
        _log_wallet(screener_id)

        pos_id = save_position(
            symbol               = sym,
            provider             = provider,
            shares               = fill_qty,
            buy_price            = fill_price,
            buy_time             = datetime.now(timezone.utc),
            buy_order_id         = str(filled.id),
            rsi_at_entry         = stock.rsi,
            atr_at_entry         = stock.atr,
            change_pct_at_entry  = stock.change_pct,
            macd_crossover_fresh = stock.macd_crossover,
            rvol_at_entry        = round(rvol_now, 3) if rvol_now else None,
        )

        ts_order = trader.submit_trailing_stop(sym, fill_qty, TRAIL_PCT)
        if ts_order:
            update_trailing_stop_order(pos_id, str(ts_order.id))
            logger.info("  STOP  %s  trail=%.0f%%  id=%s", sym, TRAIL_PCT, ts_order.id)
        else:
            logger.warning("  Trailing stop failed for %s — set manually on Alpaca", sym)

        if DISCORD_WEBHOOK:
            send_alert(
                webhook_url    = DISCORD_WEBHOOK,
                symbol         = sym,
                provider       = provider,
                price          = fill_price,
                rsi            = stock.rsi,
                volume         = int(volume_map.get(sym, 0)),
                momentum       = stock.change_pct,
                shares_bought  = fill_qty,
                total_cost     = cost,
                paper          = ALPACA_PAPER,
            )

        record_ticker_alert(sym)


def main():
    mode = "PAPER" if ALPACA_PAPER else "LIVE"
    logger.info("=" * 60)
    logger.info("MID screener starting  [%s]  ($%.0f–$%.0f)", SCREENER_ID, MIN_PRICE, MAX_PRICE)
    logger.info("Mode: %s | MaxPos: %d | Reserve: %.0f%% | Stop: %.0f%% | Lock: +%.0f%%->%.0f%% | RSI exit: %.0f",
                mode, MAX_POSITIONS, RESERVE_PCT, TRAIL_PCT, PROFIT_LOCK_PCT, TIGHT_STOP_PCT, RSI_EXIT_LEVEL)
    logger.info("Cooldown: %ds | Interval: %ds", COOLDOWN_SECS, SCAN_INTERVAL)
    if START_TIME_ET or STOP_BUY_TIME_ET or DUMP_TIME_ET:
        logger.info("Window: start=%s  stop_buy=%s  dump=%s ET",
                    START_TIME_ET or "off", STOP_BUY_TIME_ET or "off", DUMP_TIME_ET or "off")
    logger.info("=" * 60)

    init_db()
    trader      = Trader(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
    data_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)

    init_wallet(SCREENER_ID, STARTING_BALANCE)

    last_day = [None]
    _maybe_reset_day(trader, SCREENER_ID, last_day)

    while True:
        try:
            _maybe_reset_day(trader, SCREENER_ID, last_day)
            monitor_positions(trader, data_client, PROVIDER, SCREENER_ID)
            scan_and_trade(trader, data_client, PROVIDER, SCREENER_ID)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break
        except Exception as e:
            logger.error("Unexpected error in scan loop: %s", e, exc_info=True)

        try:
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break


if __name__ == "__main__":
    main()
