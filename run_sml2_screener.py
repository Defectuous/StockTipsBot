"""
SML2 screener — real-time WebSocket position monitoring with REST scan loop.

Small-cap most-active + MACD/RSI screener ($0.50–$5.00 default range).
WebSocket upgrade of run_sml_screener.py — same strategy, async fill detection.

WebSocket improvements over run_sml_screener.py:

  TradingStream (WebSocket, instant)
    - Order fills fire via threading.Event — no 5-second poll loop
    - Trailing stop fills detected in real time — no per-position REST check each cycle

  StockDataStream (WebSocket, tick-by-tick)
    - Subscribes to live trade prices for every held symbol
    - monitor_positions reads from an in-memory price cache — zero REST calls per position
    - Positions are unsubscribed automatically on close to stay lean

  Position monitor now runs every MONITOR_INTERVAL_SECONDS (default 10s) instead of
  SCAN_INTERVAL_SECONDS, costing nothing extra because it uses cached prices.

Config (env vars or .env):
  SCREENER_ID             wallet/provider identifier               default: SML2
  STARTING_BALANCE        initial wallet balance (first run only)  default: 500
  MAX_POSITIONS           max concurrent open positions            default: 2
  RESERVE_PCT             % of day-start balance held in reserve   default: 25
  TRAILING_STOP_PERCENT   trailing-stop distance %                 default: 10
  BUY_COOLDOWN_SECONDS    min seconds between buys/stock           default: 86400
  SCAN_INTERVAL_SECONDS   seconds between full scans               default: 60
  MONITOR_INTERVAL_SECONDS secs between position checks            default: 10
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
"""
import asyncio
import logging
import os
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

import pytz
from dotenv import load_dotenv
from alpaca.data import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.stream import TradingStream

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
        logging.FileHandler("sml2.log"),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()

ALPACA_KEY       = os.getenv("SML2_ALPACA_API_KEY")   or os.getenv("SML_ALPACA_API_KEY")   or os.environ["ALPACA_API_KEY"]
ALPACA_SECRET    = os.getenv("SML2_ALPACA_API_SECRET") or os.getenv("SML_ALPACA_API_SECRET") or os.environ["ALPACA_API_SECRET"]
ALPACA_PAPER     = os.getenv("ALPACA_PAPER", "true").lower() == "true"
SCREENER_ID      = os.getenv("SCREENER_ID",            "SML2")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "500"))
MAX_POSITIONS    = int(os.getenv("MAX_POSITIONS",       "2"))
RESERVE_PCT      = float(os.getenv("RESERVE_PCT",       "25"))
TRAIL_PCT        = float(os.getenv("TRAILING_STOP_PERCENT",   "10"))
COOLDOWN_SECS    = int(os.getenv("BUY_COOLDOWN_SECONDS",      "86400"))
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL_SECONDS",     "60"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL_SECONDS",  "10"))
DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK_URL",           "")
PROFIT_LOCK_PCT  = float(os.getenv("PROFIT_LOCK_PCT",         "50"))
TIGHT_STOP_PCT   = float(os.getenv("TIGHT_STOP_PCT",          "5"))
RSI_EXIT_LEVEL   = float(os.getenv("RSI_EXIT_LEVEL",          "75"))
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES",          "120"))
START_TIME_ET    = os.getenv("START_TIME_ET",                 "")
STOP_BUY_TIME_ET = os.getenv("STOP_BUY_TIME_ET",             "")
DUMP_TIME_ET     = os.getenv("DUMP_TIME_ET",                  "")
HARD_STOP_PCT    = float(os.getenv("HARD_STOP_PCT",           "0"))
MAX_ENTRY_MOVE_PCT = float(os.getenv("MAX_ENTRY_MOVE_PCT",    "0"))
MAX_ATR          = float(os.getenv("MAX_ATR",                 "0"))
MAX_RVOL         = float(os.getenv("MAX_RVOL",                "0"))

_RSI_MIN = 50.0
_RSI_MAX = 65.0

PROVIDER = f"{SCREENER_ID}_SCREENER"

_5MIN  = TimeFrame(5,  TimeFrameUnit.Minute)
_15MIN = TimeFrame(15, TimeFrameUnit.Minute)

# ── Shared streaming state ────────────────────────────────────────────────────

# Real-time price cache: updated by StockDataStream on every trade tick
_prices: Dict[str, float] = {}
_prices_lock = threading.Lock()

# Fill notification: main thread registers an Event; TradingStream sets it on fill
_fill_events: Dict[str, threading.Event] = {}
_fill_results: Dict[str, Any] = {}
_fill_lock = threading.Lock()

# Which symbols the StockDataStream is currently subscribed to
_subscribed: Set[str] = set()
_sub_lock = threading.Lock()

# Trailing stop order id → position info; used by TradingStream callback
# to close positions without a DB lookup on every fill event
_ts_to_pos: Dict[str, dict] = {}
_ts_lock = threading.Lock()

_data_stream: Optional[StockDataStream] = None
_trading_stream: Optional[TradingStream] = None
_trader: Optional[Trader] = None


def _passes(result) -> bool:
    rsi_ok  = result.rsi_trending_up and _RSI_MIN <= result.rsi <= _RSI_MAX
    macd_ok = result.macd_above_signal and result.histogram_expanding
    return rsi_ok and macd_ok


# ── Stream callbacks ──────────────────────────────────────────────────────────

async def _on_trade(data) -> None:
    """StockDataStream trade callback — caches the latest price for each symbol."""
    with _prices_lock:
        _prices[data.symbol] = float(data.price)


async def _on_trade_update(data) -> None:
    """
    TradingStream callback — fires instantly on every order state change.

    For fills: wakes any thread waiting in _wait_for_fill(), and if the
    filled order is a sell (trailing stop or manual sell), kicks off async
    position close on a worker thread so we don't block the event loop.
    """
    order = data.order
    oid   = str(order.id)
    event = data.event

    if event in ("fill", "partial_fill") and order.status.value == "filled":
        with _fill_lock:
            _fill_results[oid] = order
            ev = _fill_events.get(oid)
            if ev:
                ev.set()

        if order.side.value == "sell":
            fill_price = float(order.filled_avg_price or 0)
            threading.Thread(
                target=_close_position_from_stop,
                args=(oid, fill_price),
                daemon=True,
            ).start()

    elif event in ("cancelled", "expired", "rejected"):
        with _fill_lock:
            ev = _fill_events.get(oid)
            if ev:
                ev.set()


def _close_position_from_stop(ts_order_id: str, fill_price: float) -> None:
    """
    Called in a worker thread when TradingStream reports a sell fill.
    Looks up the position via in-memory registry (no DB query needed).
    """
    with _ts_lock:
        pos = _ts_to_pos.pop(ts_order_id, None)
    if pos is None:
        return  # not a trailing stop we're tracking (e.g. manual sell from monitor)

    pnl = (fill_price - pos["buy_price"]) * pos["shares"]
    close_position(pos["id"], fill_price, datetime.now(timezone.utc), pnl)
    update_wallet_cash(SCREENER_ID, fill_price * pos["shares"])
    _unsubscribe_prices([pos["symbol"]])

    logger.info(
        "  WS STOP  %s closed @ $%.4f  PnL=$%+.2f",
        pos["symbol"], fill_price, pnl,
    )
    if DISCORD_WEBHOOK:
        send_close(
            DISCORD_WEBHOOK, pos["symbol"], pos["buy_price"],
            fill_price, pos["shares"], pnl,
            paper=ALPACA_PAPER, reason="Trailing stop filled",
        )


# ── Subscription helpers ──────────────────────────────────────────────────────

def _subscribe_prices(symbols: list) -> None:
    if not symbols or _data_stream is None:
        return
    new_syms = []
    with _sub_lock:
        for s in symbols:
            if s not in _subscribed:
                _subscribed.add(s)
                new_syms.append(s)
    if new_syms:
        _data_stream.subscribe_trades(_on_trade, *new_syms)
        logger.info("Price stream subscribed: %s", new_syms)


def _unsubscribe_prices(symbols: list) -> None:
    if not symbols or _data_stream is None:
        return
    to_drop = []
    with _sub_lock:
        for s in symbols:
            if s in _subscribed:
                _subscribed.discard(s)
                to_drop.append(s)
    if to_drop:
        _data_stream.unsubscribe_trades(*to_drop)
        logger.info("Price stream unsubscribed: %s", to_drop)


# ── Fill waiter ───────────────────────────────────────────────────────────────

def _wait_for_fill(order_id: str, timeout: int = 60) -> Optional[Any]:
    """
    Block until TradingStream fires a fill event for order_id, or timeout elapses.
    Falls back to a single REST check if the stream event was missed.
    Replaces Trader.wait_for_fill()'s 5-second poll loop.
    """
    event = threading.Event()
    with _fill_lock:
        # Check if fill already arrived before we registered (race condition guard)
        if order_id in _fill_results:
            return _fill_results.pop(order_id)
        _fill_events[order_id] = event

    event.wait(timeout=timeout)

    with _fill_lock:
        _fill_events.pop(order_id, None)
        result = _fill_results.pop(order_id, None)

    if result is not None:
        return result

    # Stream may have missed the event during a brief reconnect — REST fallback
    if _trader:
        try:
            order = _trader.client.get_order_by_id(order_id)
            if order.status.value == "filled":
                logger.debug("Order %s confirmed via REST fallback", order_id)
                return order
        except Exception as e:
            logger.error("REST fill check failed for %s: %s", order_id, e)

    logger.warning("Order %s did not fill within %ds", order_id, timeout)
    return None


# ── Stream thread ─────────────────────────────────────────────────────────────

def _start_streams(api_key: str, api_secret: str, paper: bool) -> None:
    """
    Initialise both WebSocket streams and run them in a dedicated asyncio
    event loop on a daemon thread.  Returns after the streams have had a
    moment to connect so that subscribe calls issued right after work.
    """
    global _data_stream, _trading_stream

    _data_stream    = StockDataStream(api_key, api_secret)
    _trading_stream = TradingStream(api_key, api_secret, paper=paper)
    _trading_stream.subscribe_trade_updates(_on_trade_update)

    def _thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(asyncio.gather(
                _trading_stream.run(),
                _data_stream.run(),
            ))
        except Exception as e:
            logger.error("Stream thread error: %s", e, exc_info=True)

    threading.Thread(target=_thread, name="alpaca-streams", daemon=True).start()
    time.sleep(2)   # give streams time to authenticate and connect


# ── Wallet helpers ────────────────────────────────────────────────────────────

def _compute_buy_amount() -> float:
    wallet = get_wallet(SCREENER_ID)
    if not wallet:
        return 0.0
    deployable = wallet["day_start_balance"] * (1 - RESERVE_PCT / 100)
    return deployable / MAX_POSITIONS


def _log_wallet() -> None:
    wallet = get_wallet(SCREENER_ID)
    if not wallet:
        return
    reserve    = wallet["day_start_balance"] * RESERVE_PCT / 100
    deployable = wallet["day_start_balance"] - reserve
    available  = wallet["current_balance"] - reserve
    logger.info(
        "Wallet [%s]  total=$%.2f  reserve=$%.2f  deployable=$%.2f  available=$%.2f",
        SCREENER_ID, wallet["current_balance"], reserve, deployable, max(available, 0),
    )


def _maybe_reset_day(trader: Trader, last_day: list) -> None:
    today_et = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    if last_day[0] == today_et:
        return

    wallet      = get_wallet(SCREENER_ID)
    alpaca_cash = trader.get_cash_balance()
    db_balance  = wallet["current_balance"] if wallet else STARTING_BALANCE

    if alpaca_cash is not None:
        logger.info("Day reset: DB=$%.2f  Alpaca=$%.2f -> using Alpaca", db_balance, alpaca_cash)
        reconciled = alpaca_cash
    else:
        logger.warning("Day reset: Alpaca cash unavailable, keeping DB $%.2f", db_balance)
        reconciled = db_balance

    reset_day_wallet(SCREENER_ID, today_et, reconciled)
    last_day[0] = today_et
    _log_wallet()


# ── Orphan position check ─────────────────────────────────────────────────────

def _check_untracked_positions(trader: Trader) -> None:
    """Detect Alpaca positions not in the DB and auto-register them."""
    try:
        alpaca_all = trader.client.get_all_positions()  # type: ignore[union-attr]
        alpaca_map = {p.symbol: p for p in alpaca_all}  # type: ignore[union-attr]
    except Exception as e:
        logger.warning("Orphan check: could not fetch Alpaca positions: %s", e)
        return

    db_syms = {p["symbol"] for p in get_open_positions(PROVIDER)}
    orphans = set(alpaca_map) - db_syms
    if not orphans:
        return

    logger.warning("UNTRACKED Alpaca positions (not in DB): %s", sorted(orphans))

    for sym in sorted(orphans):
        ap        = alpaca_map[sym]
        buy_price = float(ap.avg_entry_price)  # type: ignore[union-attr]
        shares    = int(float(ap.qty))          # type: ignore[union-attr]

        buy_order_id = f"recovered_{sym}_{int(time.time())}"
        buy_time: datetime = datetime.now(timezone.utc)
        try:
            filled = trader.client.get_orders(filter=GetOrdersRequest(  # type: ignore[union-attr]
                status=QueryOrderStatus.CLOSED, symbols=[sym], limit=20,
            ))
            buy_orders = [
                o for o in filled  # type: ignore[union-attr]
                if o.side == OrderSide.BUY and o.filled_at is not None
            ]
            if buy_orders:
                buy_orders.sort(key=lambda o: o.filled_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
                bo           = buy_orders[0]
                buy_order_id = str(bo.id)
                buy_time     = bo.filled_at or buy_time
        except Exception as e:
            logger.warning("Orphan %s: buy order lookup failed (%s) — using placeholder", sym, e)

        try:
            pos_id = save_position(sym, PROVIDER, shares, buy_price, buy_time, buy_order_id)
        except Exception as e:
            logger.error("Orphan %s: DB insert failed: %s", sym, e)
            continue

        ts_id = None
        try:
            open_orders = trader.client.get_orders(filter=GetOrdersRequest(  # type: ignore[union-attr]
                status=QueryOrderStatus.OPEN, symbols=[sym],
            ))
            for o in open_orders:  # type: ignore[union-attr]
                if o.type == OrderType.TRAILING_STOP:
                    ts_id = str(o.id)
                    break
        except Exception as e:
            logger.warning("Orphan %s: trailing stop lookup failed: %s", sym, e)

        if not ts_id:
            # No protective stop found on the broker (e.g. the fill was missed
            # during a network blip) — submit one now instead of leaving the
            # position unprotected until another exit rule happens to catch it.
            new_stop = trader.submit_trailing_stop(sym, shares, TRAIL_PCT)
            if new_stop:
                ts_id = str(new_stop.id)
                logger.info("Orphan %s: no stop found — submitted new one  id=%s", sym, ts_id)
            else:
                logger.warning("Orphan %s: registered in DB (pos_id=%d) but no trailing stop found and submit failed — set one manually", sym, pos_id)

        if ts_id:
            update_trailing_stop_order(pos_id, ts_id)
            with _ts_lock:
                _ts_to_pos[ts_id] = {
                    "id":        pos_id,
                    "symbol":    sym,
                    "buy_price": buy_price,
                    "shares":    shares,
                }
            logger.info("Orphan %s: registered  pos_id=%d  ts=%s", sym, pos_id, ts_id)

        _subscribe_prices([sym])


# ── Position monitoring ───────────────────────────────────────────────────────

def monitor_positions(trader: Trader, data_client: StockHistoricalDataClient) -> None:
    """
    Check every open position for exit conditions.

    Current price comes from the WebSocket price cache (_prices) — no REST
    call per symbol.  Bars for the RSI exit are fetched once per cycle in a
    single batched request.  Trailing stop fills are handled by TradingStream
    and do NOT require a check here.
    """
    positions = [dict(r) for r in get_open_positions(PROVIDER)]
    if not positions:
        return

    now     = datetime.now(pytz.UTC)
    now_et  = now.astimezone(pytz.timezone("America/New_York"))
    symbols = list({p["symbol"] for p in positions})

    # Ensure we're subscribed to price ticks for all held symbols
    _subscribe_prices(symbols)

    # Single batched bar fetch for RSI exit (all symbols at once)
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

        # Price from WebSocket cache — updated on every trade tick
        with _prices_lock:
            current_price = _prices.get(sym)

        if current_price is None:
            # Not yet in cache (stream just subscribed); fall back to snapshot
            try:
                snap = data_client.get_stock_snapshot(
                    StockSnapshotRequest(symbol_or_symbols=[sym])
                ).get(sym)
                if snap and snap.latest_trade:
                    current_price = float(snap.latest_trade.price)
            except Exception:
                pass

        if current_price is None:
            continue

        gain_pct = (current_price - buy_price) / buy_price * 100

        def _sell_and_close(reason: str, timeout: int = 30) -> None:
            if stop_order_id:
                trader.cancel_order(stop_order_id)
                with _ts_lock:
                    _ts_to_pos.pop(stop_order_id, None)

            # Verify what the broker actually holds before selling — the DB's
            # share count can drift from the real position (e.g. a trailing
            # stop fills partially, or a buy fill is misreported). Without this
            # check a stuck mismatch retries forever every monitor cycle.
            actual_qty = trader.get_position_qty(sym)
            if actual_qty <= 0:
                logger.warning(
                    "  %s — DB shows %d shares open but broker holds 0; "
                    "closing DB record at last known price (%s)",
                    sym, shares, reason,
                )
                pnl = (current_price - buy_price) * shares
                close_position(pos_id, current_price, datetime.now(timezone.utc), pnl)
                update_wallet_cash(SCREENER_ID, current_price * shares)
                _unsubscribe_prices([sym])
                return

            sell_qty = min(shares, actual_qty)
            if sell_qty < shares:
                logger.warning(
                    "  %s — DB shows %d shares but broker holds %d; selling %d",
                    sym, shares, actual_qty, sell_qty,
                )
            sell = trader.market_sell(sym, sell_qty)
            if not sell:
                return
            filled = _wait_for_fill(str(sell.id), timeout=timeout)
            if not filled:
                return
            fp  = float(filled.filled_avg_price)
            pnl = (fp - buy_price) * sell_qty
            close_position(pos_id, fp, datetime.now(timezone.utc), pnl)
            update_wallet_cash(SCREENER_ID, fp * sell_qty)
            _unsubscribe_prices([sym])
            logger.info("  SOLD  %s @ $%.4f  PnL=$%+.2f  (%s)", sym, fp, pnl, reason)
            if DISCORD_WEBHOOK:
                send_close(DISCORD_WEBHOOK, sym, buy_price, fp, sell_qty, pnl,
                           paper=ALPACA_PAPER, reason=reason)

        # ── 1. Hard stop ─────────────────────────────────────────────────────
        if HARD_STOP_PCT > 0 and gain_pct <= -HARD_STOP_PCT:
            logger.info("  HARD STOP  %s  gain=%.2f%%", sym, gain_pct)
            _sell_and_close(f"Hard stop -{HARD_STOP_PCT:.0f}%")
            continue

        # ── 2. Time exit ──────────────────────────────────────────────────────
        buy_dt   = datetime.fromisoformat(pos["buy_time"])
        held_min = (now - buy_dt).total_seconds() / 60
        if held_min >= MAX_HOLD_MINUTES:
            logger.info("  TIME EXIT  %s  held %.0fm  gain=%+.1f%%", sym, held_min, gain_pct)
            _sell_and_close("Max hold time exit")
            continue

        # ── 3. Dump time ──────────────────────────────────────────────────────
        if DUMP_TIME_ET:
            dump_h, dump_m = map(int, DUMP_TIME_ET.split(":"))
            if (now_et.hour, now_et.minute) >= (dump_h, dump_m):
                logger.info("  DUMP EXIT  %s  %s ET  gain=%+.1f%%", sym, DUMP_TIME_ET, gain_pct)
                _sell_and_close(f"Dump time {DUMP_TIME_ET} ET")
                continue

        # ── 4. RSI exit ───────────────────────────────────────────────────────
        sym_bars = list(bars5.get(sym, []))
        if len(sym_bars) >= 20:
            closes   = [b.close for b in sym_bars]
            rsi_vals = [r for r in _rsi_series(closes) if r is not None]
            if len(rsi_vals) >= 4:
                rsi         = rsi_vals[-1]
                rsi_falling = rsi_vals[-1] < rsi_vals[-3]
                if rsi > RSI_EXIT_LEVEL and rsi_falling:
                    logger.info(
                        "  RSI EXIT  %s  RSI=%.1f (falling)  gain=%+.1f%%",
                        sym, rsi, gain_pct,
                    )
                    _sell_and_close("RSI overbought exit")
                    continue

        # ── 5. Profit lock ────────────────────────────────────────────────────
        if not stop_tightened and gain_pct >= PROFIT_LOCK_PCT:
            logger.info(
                "  LOCK  %s  +%.1f%% -> tightening stop %.0f%% -> %.0f%%",
                sym, gain_pct, TRAIL_PCT, TIGHT_STOP_PCT,
            )
            cancelled = trader.cancel_order(stop_order_id) if stop_order_id else True
            if cancelled:
                new_stop = trader.submit_trailing_stop(sym, shares, TIGHT_STOP_PCT)
                if new_stop:
                    new_id = str(new_stop.id)
                    mark_stop_tightened(pos_id, new_id)
                    with _ts_lock:
                        _ts_to_pos.pop(stop_order_id, None)
                        _ts_to_pos[new_id] = {
                            "id":        pos_id,
                            "symbol":    sym,
                            "buy_price": buy_price,
                            "shares":    shares,
                        }
                    logger.info("  STOP  %s tightened to %.0f%%  id=%s",
                                sym, TIGHT_STOP_PCT, new_id)
            else:
                logger.warning("  LOCK  %s — cancel failed, keeping original stop", sym)


# ── Scan and trade ────────────────────────────────────────────────────────────

def scan_and_trade(trader: Trader, data_client: StockHistoricalDataClient) -> None:
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

    open_count = get_open_position_count(PROVIDER)
    if open_count >= MAX_POSITIONS:
        logger.info("[%s] Position cap (%d/%d) — no new buys", ts, open_count, MAX_POSITIONS)
        return

    wallet = get_wallet(SCREENER_ID)
    if not wallet:
        return

    buy_amount = _compute_buy_amount()
    reserve    = wallet["day_start_balance"] * RESERVE_PCT / 100
    available  = wallet["current_balance"] - reserve

    if available < buy_amount:
        logger.info("[%s] Insufficient cash: $%.2f available, $%.2f needed", ts, available, buy_amount)
        return

    # ── 1. Most active penny stocks ───────────────────────────────────────────
    actives = get_most_active_penny_stocks(ALPACA_KEY, ALPACA_SECRET)
    if not actives:
        logger.info("[%s] No most-active data returned", ts)
        return

    symbols    = [s.symbol for s in actives]
    price_map  = {s.symbol: (s.price, None) for s in actives}
    volume_map = {s.symbol: s.volume for s in actives}

    # ── 2. Snapshots ──────────────────────────────────────────────────────────
    prev_vol_map: dict = {}
    try:
        snaps = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols))
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
        result = _analyze(
            sym,
            list(bars5.get(sym,  [])),
            list(bars15.get(sym, [])),
            price,
            chg or 0.0,
        )
        if result and _passes(result):
            passing.append(result)

    logger.info(
        "[%s] Scanned %d stocks -> %d passing MACD+RSI  pos=%d/%d  buy=$%.2f",
        ts, len(symbols), len(passing), open_count, MAX_POSITIONS, buy_amount,
    )

    if not passing:
        return

    # ── 5. Buy ────────────────────────────────────────────────────────────────
    for stock in passing:
        if get_open_position_count(PROVIDER) >= MAX_POSITIONS:
            break

        wallet    = get_wallet(SCREENER_ID)
        available = wallet["current_balance"] - wallet["day_start_balance"] * RESERVE_PCT / 100
        if available < buy_amount:
            logger.info("  SKIP — depleted available cash after buying")
            break

        sym = stock.symbol

        if is_ticker_on_cooldown(sym, COOLDOWN_SECS):
            logger.info("  SKIP  %s — cooldown", sym)
            continue

        if MAX_ENTRY_MOVE_PCT > 0 and stock.change_pct > MAX_ENTRY_MOVE_PCT:
            logger.info("  SKIP  %s — already up %.1f%% (limit %.0f%%)",
                        sym, stock.change_pct, MAX_ENTRY_MOVE_PCT)
            continue

        if MAX_ATR > 0 and stock.atr and stock.atr > MAX_ATR:
            logger.info("  SKIP  %s — ATR %.4f > %.4f", sym, stock.atr, MAX_ATR)
            continue

        today_vol = volume_map.get(sym, 0)
        prev_vol  = prev_vol_map.get(sym)
        rvol_now  = (today_vol / prev_vol) if prev_vol else None
        if MAX_RVOL > 0 and rvol_now and rvol_now > MAX_RVOL:
            logger.info("  SKIP  %s — RVOL %.1fx > %.0fx", sym, rvol_now, MAX_RVOL)
            continue

        logger.info("  BUY   %s  $%.4f  RSI=%.1f  chg=%+.2f%%  budget=$%.2f",
                    sym, stock.price, stock.rsi, stock.change_pct, buy_amount)

        order, err = trader.buy_stock(sym, buy_amount, stock.price)
        if err:
            logger.error("  Buy failed for %s: %s", sym, err)
            if DISCORD_WEBHOOK and "insufficient buying power" not in err:
                send_error(DISCORD_WEBHOOK, f"Buy failed for **{sym}**: {err}")
            if "insufficient buying power" in err:
                break
            continue

        # Stream-based fill wait — no poll loop
        filled = _wait_for_fill(str(order.id), timeout=180)
        if not filled:
            logger.error("  %s order did not fill within 3 minutes -- cancelling", sym)
            if _trader:
                _trader.cancel_order(str(order.id))
            continue

        fill_price = float(filled.filled_avg_price)
        fill_qty   = int(float(filled.filled_qty))
        cost       = fill_price * fill_qty
        logger.info("  FILLED %s  %d × $%.4f = $%.2f", sym, fill_qty, fill_price, cost)

        update_wallet_cash(SCREENER_ID, -cost)
        _log_wallet()

        pos_id = save_position(
            symbol               = sym,
            provider             = PROVIDER,
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
            ts_id = str(ts_order.id)
            update_trailing_stop_order(pos_id, ts_id)
            # Register in memory so TradingStream callback can close without a DB lookup
            with _ts_lock:
                _ts_to_pos[ts_id] = {
                    "id":        pos_id,
                    "symbol":    sym,
                    "buy_price": fill_price,
                    "shares":    fill_qty,
                }
            logger.info("  STOP  %s  trail=%.0f%%  id=%s", sym, TRAIL_PCT, ts_id)
        else:
            logger.warning("  Trailing stop failed for %s — set manually on Alpaca", sym)

        # Start receiving real-time price ticks for this position
        _subscribe_prices([sym])

        if DISCORD_WEBHOOK:
            send_alert(
                webhook_url    = DISCORD_WEBHOOK,
                symbol         = sym,
                provider       = PROVIDER,
                price          = fill_price,
                rsi            = stock.rsi,
                volume         = int(volume_map.get(sym, 0)),
                momentum       = stock.change_pct,
                shares_bought  = fill_qty,
                total_cost     = cost,
                paper          = ALPACA_PAPER,
            )

        record_ticker_alert(sym)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _trader
    mode = "PAPER" if ALPACA_PAPER else "LIVE"
    logger.info("=" * 60)
    logger.info("SML2 screener starting  [%s]", SCREENER_ID)
    logger.info(
        "Mode: %s | MaxPos: %d | Reserve: %.0f%% | Stop: %.0f%% | "
        "Lock: +%.0f%%->%.0f%% | RSI exit: %.0f",
        mode, MAX_POSITIONS, RESERVE_PCT, TRAIL_PCT,
        PROFIT_LOCK_PCT, TIGHT_STOP_PCT, RSI_EXIT_LEVEL,
    )
    logger.info(
        "Scan: %ds | Monitor: %ds | Cooldown: %ds",
        SCAN_INTERVAL, MONITOR_INTERVAL, COOLDOWN_SECS,
    )
    if START_TIME_ET or STOP_BUY_TIME_ET or DUMP_TIME_ET:
        logger.info(
            "Window: start=%s  stop_buy=%s  dump=%s ET",
            START_TIME_ET or "off", STOP_BUY_TIME_ET or "off", DUMP_TIME_ET or "off",
        )
    logger.info("=" * 60)

    init_db()
    _trader     = Trader(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
    data_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)

    init_wallet(SCREENER_ID, STARTING_BALANCE)

    # ── Start WebSocket streams ───────────────────────────────────────────────
    logger.info("Connecting to Alpaca WebSocket streams…")
    _start_streams(ALPACA_KEY, ALPACA_SECRET, ALPACA_PAPER)
    logger.info("Streams connected.")

    # ── Restore state from any positions open before this run ────────────────
    existing = list(get_open_positions(PROVIDER))
    if existing:
        syms = list({p["symbol"] for p in existing})
        _subscribe_prices(syms)

        # Re-register trailing stops in memory for TradingStream callbacks
        with _ts_lock:
            for p in existing:
                ts_id = p["trailing_stop_order_id"]
                if ts_id:
                    _ts_to_pos[ts_id] = {
                        "id":        p["id"],
                        "symbol":    p["symbol"],
                        "buy_price": p["buy_price"],
                        "shares":    p["shares"],
                    }

        # Check for trailing stop fills that happened while we were offline
        with _ts_lock:
            snapshot = dict(_ts_to_pos)
        for ts_id, pos_info in snapshot.items():
            try:
                order = _trader.client.get_order_by_id(ts_id)
                if order.status.value == "filled":
                    logger.info(
                        "Startup: trailing stop %s was filled during downtime — closing %s",
                        ts_id, pos_info["symbol"],
                    )
                    _close_position_from_stop(ts_id, float(order.filled_avg_price))
            except Exception as e:
                logger.debug("Startup stop check for %s: %s", ts_id, e)

        logger.info("Restored %d open position(s): %s", len(existing), syms)

    last_day  = [None]
    last_scan = 0.0
    _maybe_reset_day(_trader, last_day)

    logger.info("Running — Ctrl+C to stop")
    try:
        while True:
            _maybe_reset_day(_trader, last_day)
            monitor_positions(_trader, data_client)

            now = time.monotonic()
            if now - last_scan >= SCAN_INTERVAL:
                _check_untracked_positions(_trader)
                scan_and_trade(_trader, data_client)
                last_scan = now

            time.sleep(MONITOR_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Stopped by user.")


if __name__ == "__main__":
    main()
