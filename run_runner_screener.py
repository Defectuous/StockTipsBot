"""
RUNNER screener — real-time WebSocket position monitoring with REST scan loop.

Catches early movers before they're extreme enough to rank in Alpaca's
top-100-actives/top-50-movers lists (which is how SML/SML2/MID/SUPER all
discover candidates). See todo.md's 2026-07-30 review: CYCU (+453%) and
GCTK (+78%) never appeared in any screener log until they were already up
100-500%, past every tier's entry cap — a structural gap in discovery, not
a tunable entry parameter.

Discovery is two-stage instead of Alpaca's pre-ranked lists:
  Stage A (once/trading day, cached)  — bot.most_active.get_tradable_asset_symbols():
                                         the full tradable US-equity universe
  Stage B (every scan cycle)          — bot.most_active.get_universe_snapshot():
                                         bulk-snapshot that universe, filtered
                                         to a wide static price band

Entry is velocity-based instead of SML2's RSI-bounce/MACD-confirmation setup
(bot.runner_screener._analyze): short-window (1-min bar) price velocity +
time-adjusted RVOL + a session-anchored (not full-day) VWAP sanity check —
meant to react while a move is still small, not after it's already extended.

Risk is tighter than SML/SML2 to match the higher-volatility setups this
tier is explicitly hunting: smaller position size (higher RESERVE_PCT),
tighter hard stop, shorter max hold, wider buy-order slippage buffer (see
bot.trader.Trader.buy_stock's slippage_pct) since fast movers blow through
a 0.5% limit buffer disproportionately often.

WebSocket architecture (fills, position monitoring, stop handling) is
reused verbatim from run_sml2_screener.py — see that file's docstring for
details of the streaming design.

Config (env vars or .env):
  SCREENER_ID                 wallet/provider identifier               default: RUNNER
  RUNNER_STARTING_BALANCE     initial wallet balance (first run only)   default: 500
  RUNNER_MAX_POSITIONS        max concurrent open positions             default: 2
  RUNNER_RESERVE_PCT          % of day-start balance held in reserve    default: 50
  RUNNER_TRAILING_STOP_PERCENT trailing-stop distance % (fallback only) default: 10
  RUNNER_HARD_STOP_PCT        hard stop loss % from entry (primary)     default: 5
                               submitted as a resting broker-side stop
                               order at entry, same as SML2
  RUNNER_MAX_HOLD_MINUTES     force-sell after this many minutes        default: 25
  RUNNER_SLIPPAGE_PCT         buy limit-price buffer above ask          default: 2.0
  RUNNER_MAX_BUY_AMOUNT       max $ per trade, regardless of wallet     default: 250.00
                               math (RESERVE_PCT/MAX_POSITIONS sizing
                               is still computed and the smaller of the
                               two wins)
  RUNNER_MIN_PRICE            Stage-A/B price band floor                default: 0.10
  RUNNER_MAX_PRICE            Stage-A/B price band ceiling              default: 10.00
  RUNNER_VELOCITY_MIN_PCT     min 1-min-bar price velocity to trigger   default: 3.0
  RUNNER_VELOCITY_LOOKBACK_MIN velocity measurement window (minutes)    default: 3
  RUNNER_MIN_RVOL             min time-adjusted RVOL to trigger         default: 3.0
  RUNNER_MAX_CANDIDATES       cap on Stage-B symbols screened per cycle default: 300
                               (top by volume) — keeps the per-cycle
                               bars fetch fast; on 2026-07-31 an
                               unbounded ~2900-symbol screen made each
                               scan take 55-100+s end to end, stale
                               enough to chase already-cresting spikes
  RUNNER_PRICE_STALENESS_SECONDS max age of a cached WS price before    default: 45
                               falling back to a REST snapshot — thin
                               microcaps can go quiet for minutes with
                               no print, which otherwise silently pins
                               gain_pct (and the hard-stop poll
                               fallback) near 0% while the real price
                               has already moved
  RUNNER_MAX_ENTRY_MOVE_PCT   skip buys already up > this %% on the day default: 15
                               (sanity ceiling only — unlike SML2 there
                               is deliberately no daily-change floor,
                               since gating on cumulative change is the
                               exact mechanism this tier replaces)
  BUY_COOLDOWN_SECONDS        min seconds between buys/stock             default: 86400
  SCAN_INTERVAL_SECONDS       seconds between full scans                 default: 60
  MONITOR_INTERVAL_SECONDS    secs between position checks               default: 10
  ALPACA_PAPER                true / false                               default: true
  DISCORD_WEBHOOK_URL         webhook for buy/error alerts               optional
  PROFIT_LOCK_PCT             gain %% to tighten trailing stop            default: 50
  TIGHT_STOP_PCT              tighter stop %% after profit lock           default: 5
  RSI_EXIT_LEVEL               RSI level to exit on (declining)          default: 75
  START_TIME_ET                don't scan before this time ET            default: "" (off)
  STOP_BUY_TIME_ET             stop new buys after this time ET          default: "" (off)
  DUMP_TIME_ET                 force-sell all at clock time ET           default: "" (off)
  MIN_GAIN_AT_30M              exit if gain%% below this by 30min held    default: -2.0
  MIN_GAIN_AT_60M              exit if gain%% below this by 60min held    default: 0.0
"""
import asyncio
import logging
import os
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

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
    update_hard_stop_order,
    update_trailing_stop_order,
    update_wallet_cash,
)
from bot.discord_notify import send_alert, send_close, send_error
from bot.market_data import _rsi_series, estimate_entry_indicators
from bot.most_active import get_tradable_asset_symbols, get_universe_snapshot
from bot.runner_screener import RVOL_MIN, VELOCITY_MIN_PCT, _analyze
from bot.trader import Trader

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("runner.log"),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()

ALPACA_KEY       = os.getenv("RUNNER_ALPACA_API_KEY")    or os.environ["ALPACA_API_KEY"]
ALPACA_SECRET    = os.getenv("RUNNER_ALPACA_API_SECRET") or os.environ["ALPACA_API_SECRET"]
ALPACA_PAPER     = os.getenv("ALPACA_PAPER", "true").lower() == "true"
SCREENER_ID      = os.getenv("SCREENER_ID",              "RUNNER")
STARTING_BALANCE = float(os.getenv("RUNNER_STARTING_BALANCE", "500"))
MAX_POSITIONS    = int(os.getenv("RUNNER_MAX_POSITIONS",       "2"))
RESERVE_PCT      = float(os.getenv("RUNNER_RESERVE_PCT",       "50"))
TRAIL_PCT        = float(os.getenv("RUNNER_TRAILING_STOP_PERCENT", "10"))
MAX_BUY_AMOUNT   = float(os.getenv("RUNNER_MAX_BUY_AMOUNT",    "250.00"))
COOLDOWN_SECS    = int(os.getenv("BUY_COOLDOWN_SECONDS",       "86400"))
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL_SECONDS",      "60"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL_SECONDS",   "10"))
DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK_URL",            "")
PROFIT_LOCK_PCT  = float(os.getenv("PROFIT_LOCK_PCT",          "50"))
TIGHT_STOP_PCT   = float(os.getenv("TIGHT_STOP_PCT",           "5"))
RSI_EXIT_LEVEL   = float(os.getenv("RSI_EXIT_LEVEL",           "75"))
MAX_HOLD_MINUTES = int(os.getenv("RUNNER_MAX_HOLD_MINUTES",    "25"))
START_TIME_ET    = os.getenv("START_TIME_ET",                  "")
STOP_BUY_TIME_ET = os.getenv("STOP_BUY_TIME_ET",               "")
DUMP_TIME_ET     = os.getenv("DUMP_TIME_ET",                   "")
HARD_STOP_PCT    = float(os.getenv("RUNNER_HARD_STOP_PCT",     "5"))
SLIPPAGE_PCT     = float(os.getenv("RUNNER_SLIPPAGE_PCT",      "2.0"))
MIN_PRICE        = float(os.getenv("RUNNER_MIN_PRICE",         "0.10"))
MAX_PRICE        = float(os.getenv("RUNNER_MAX_PRICE",         "10.00"))
VELOCITY_MIN     = float(os.getenv("RUNNER_VELOCITY_MIN_PCT",  str(VELOCITY_MIN_PCT)))
VELOCITY_LOOKBACK_MIN = int(os.getenv("RUNNER_VELOCITY_LOOKBACK_MIN", "3"))
MIN_RVOL         = float(os.getenv("RUNNER_MIN_RVOL",          str(RVOL_MIN)))
MAX_ENTRY_MOVE_PCT = float(os.getenv("RUNNER_MAX_ENTRY_MOVE_PCT", "15"))
MAX_CANDIDATES   = int(os.getenv("RUNNER_MAX_CANDIDATES",      "300"))
PRICE_STALENESS_SECONDS = float(os.getenv("RUNNER_PRICE_STALENESS_SECONDS", "45"))
MIN_GAIN_AT_30M  = float(os.getenv("MIN_GAIN_AT_30M",           "-2.0"))
MIN_GAIN_AT_60M  = float(os.getenv("MIN_GAIN_AT_60M",            "0.0"))

PROVIDER = f"{SCREENER_ID}_SCREENER"

_1MIN  = TimeFrame.Minute
_15MIN = TimeFrame(15, TimeFrameUnit.Minute)

# ── Shared streaming state ────────────────────────────────────────────────────

# Real-time price cache: updated by StockDataStream on every trade tick.
# _price_updated_at tracks when each entry was last refreshed — thin
# microcaps from the wide RUNNER universe can go minutes without a print,
# so a cached price is only trustworthy if it's recent (see monitor_positions).
_prices: Dict[str, float] = {}
_price_updated_at: Dict[str, float] = {}
_prices_lock = threading.Lock()

# Fill notification: main thread registers an Event; TradingStream sets it on fill
_fill_events: Dict[str, threading.Event] = {}
_fill_results: Dict[str, Any] = {}
_fill_lock = threading.Lock()

# Which symbols the StockDataStream is currently subscribed to
_subscribed: Set[str] = set()
_sub_lock = threading.Lock()

# Stop order id (trailing OR hard stop-loss) → position info; used by
# TradingStream callback to close positions without a DB lookup on every
# fill event. Both of a position's resting orders point at the same dict
# (via "trailing_id"/"hard_id") so whichever fills first can cancel the other.
_ts_to_pos: Dict[str, dict] = {}
_ts_lock = threading.Lock()

_data_stream: Optional[StockDataStream] = None
_trading_stream: Optional[TradingStream] = None
_trader: Optional[Trader] = None

# Stage-A daily universe cache
_universe_cache: Dict[str, Any] = {"date": None, "symbols": []}


def _get_daily_universe(now_et: datetime) -> List[str]:
    """Stage A — refresh the full tradable-asset universe once per trading day."""
    today = now_et.strftime("%Y-%m-%d")
    if _universe_cache["date"] != today:
        symbols = get_tradable_asset_symbols(ALPACA_KEY, ALPACA_SECRET)
        _universe_cache["date"]    = today
        _universe_cache["symbols"] = symbols
        logger.info("Universe refresh: %d tradable symbols for %s", len(symbols), today)
    return _universe_cache["symbols"]


def _register_stops(
    pos_id: int, sym: str, buy_price: float, shares: int,
    trailing_id: Optional[str], hard_id: Optional[str],
) -> None:
    """Register a position's resting stop order(s) for TradingStream lookup."""
    info = {
        "id": pos_id, "symbol": sym, "buy_price": buy_price, "shares": shares,
        "trailing_id": trailing_id, "hard_id": hard_id,
    }
    with _ts_lock:
        if trailing_id:
            _ts_to_pos[trailing_id] = info
        if hard_id:
            _ts_to_pos[hard_id] = info


def _cancel_and_unregister_stops(trailing_id: Optional[str], hard_id: Optional[str]) -> None:
    """Cancel both resting stop orders (if present) and drop them from the registry."""
    for oid in (trailing_id, hard_id):
        if not oid:
            continue
        if _trader:
            _trader.cancel_order(oid)
        with _ts_lock:
            _ts_to_pos.pop(oid, None)


# ── Stream callbacks ──────────────────────────────────────────────────────────

async def _on_trade(data) -> None:
    """StockDataStream trade callback — caches the latest price for each symbol."""
    with _prices_lock:
        _prices[data.symbol] = float(data.price)
        _price_updated_at[data.symbol] = time.monotonic()


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


def _close_position_from_stop(order_id: str, fill_price: float) -> None:
    """
    Called in a worker thread when TradingStream reports a sell fill on
    either the trailing stop or the hard stop-loss. Looks up the position
    via in-memory registry (no DB query needed) and cancels the sibling
    order so it doesn't sit resting against a position that's already gone.
    """
    with _ts_lock:
        pos = _ts_to_pos.pop(order_id, None)
        if pos is None:
            return  # not a stop order we're tracking (e.g. manual sell from monitor)
        sibling_id = pos["hard_id"] if order_id == pos.get("trailing_id") else pos["trailing_id"]
        if sibling_id:
            _ts_to_pos.pop(sibling_id, None)
    if sibling_id and _trader:
        _trader.cancel_order(sibling_id)

    reason = "Hard stop filled" if order_id == pos.get("hard_id") else "Trailing stop filled"
    pnl = (fill_price - pos["buy_price"]) * pos["shares"]
    close_position(pos["id"], fill_price, datetime.now(timezone.utc), pnl)
    update_wallet_cash(SCREENER_ID, fill_price * pos["shares"])
    _unsubscribe_prices([pos["symbol"]])

    logger.info(
        "  WS STOP  %s closed @ $%.4f  PnL=$%+.2f  (%s)",
        pos["symbol"], fill_price, pnl, reason,
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
        with _prices_lock:
            for s in to_drop:
                _prices.pop(s, None)
                _price_updated_at.pop(s, None)
        logger.info("Price stream unsubscribed: %s", to_drop)


# ── Fill waiter ───────────────────────────────────────────────────────────────

def _wait_for_fill(order_id: str, timeout: int = 60) -> Optional[Any]:
    """
    Block until TradingStream fires a fill event for order_id, or timeout elapses.
    Falls back to a single REST check if the stream event was missed.
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
    return min(deployable / MAX_POSITIONS, MAX_BUY_AMOUNT)


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

def _check_untracked_positions(trader: Trader, data_client: StockHistoricalDataClient) -> None:
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

        # Orphan recovery has no access to the original scan-time context, so
        # change_pct/rvol would otherwise be recorded as NULL, which makes
        # these trades impossible to audit later. Best-effort reconstruct
        # them from historical bars around buy_time instead.
        stats = estimate_entry_indicators(data_client, sym, buy_price, buy_time)
        if all(v is None for v in stats.values()):
            logger.warning("Orphan %s: could not reconstruct any entry indicators", sym)

        try:
            pos_id = save_position(
                sym, PROVIDER, shares, buy_price, buy_time, buy_order_id,
                change_pct_at_entry=stats["change_pct"], rvol_at_entry=stats["rvol"],
            )
        except Exception as e:
            logger.error("Orphan %s: DB insert failed: %s", sym, e)
            continue

        ts_id = None
        hs_id = None
        try:
            open_orders = trader.client.get_orders(filter=GetOrdersRequest(  # type: ignore[union-attr]
                status=QueryOrderStatus.OPEN, symbols=[sym],
            ))
            for o in open_orders:  # type: ignore[union-attr]
                if o.type == OrderType.TRAILING_STOP:
                    ts_id = str(o.id)
                elif o.type == OrderType.STOP:
                    hs_id = str(o.id)
        except Exception as e:
            logger.warning("Orphan %s: stop order lookup failed: %s", sym, e)

        if not ts_id and not hs_id:
            # No protective stop found on the broker (e.g. the fill was missed
            # during a network blip) — submit one now instead of leaving the
            # position unprotected until another exit rule happens to catch it.
            # Only one stop type can rest on the shares at once (see entry
            # logic), so prefer the hard stop when enabled, else trailing.
            if HARD_STOP_PCT > 0:
                hard_stop_price = buy_price * (1 - HARD_STOP_PCT / 100)
                new_hs = trader.submit_stop_loss(sym, shares, hard_stop_price)
                if new_hs:
                    hs_id = str(new_hs.id)
                    logger.info("Orphan %s: no resting stop found — submitted hard stop  id=%s", sym, hs_id)
                else:
                    logger.warning("Orphan %s: hard stop submit failed — trying trailing stop", sym)
            if not hs_id:
                new_stop = trader.submit_trailing_stop(sym, shares, TRAIL_PCT)
                if new_stop:
                    ts_id = str(new_stop.id)
                    logger.info("Orphan %s: no resting stop found — submitted trailing stop  id=%s", sym, ts_id)
                else:
                    logger.warning("Orphan %s: registered in DB (pos_id=%d) but no stop order found and submit failed — set one manually", sym, pos_id)

        if ts_id:
            update_trailing_stop_order(pos_id, ts_id)
        if hs_id:
            update_hard_stop_order(pos_id, hs_id)
        if ts_id or hs_id:
            _register_stops(pos_id, sym, buy_price, shares, ts_id, hs_id)
            logger.info("Orphan %s: registered  pos_id=%d  ts=%s  hs=%s", sym, pos_id, ts_id, hs_id)

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
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=now - timedelta(minutes=120),
            end=now,
        )).data
    except Exception as e:
        logger.warning("Monitor bars failed: %s", e)
        bars5 = {}

    for pos in positions:
        sym                = pos["symbol"]
        pos_id             = pos["id"]
        buy_price          = pos["buy_price"]
        shares             = pos["shares"]
        stop_order_id      = pos.get("trailing_stop_order_id")
        hard_stop_order_id = pos.get("hard_stop_order_id")
        stop_tightened     = pos.get("stop_tightened", 0)

        # Price from WebSocket cache — updated on every trade tick. Thin
        # microcaps can go quiet for minutes with no print at all, which
        # would otherwise leave gain_pct (and the hard-stop poll fallback
        # below) silently pinned near the entry price — so a cache entry
        # older than PRICE_STALENESS_SECONDS is treated the same as missing.
        with _prices_lock:
            current_price = _prices.get(sym)
            updated_at    = _price_updated_at.get(sym)
        stale = updated_at is None or (time.monotonic() - updated_at) > PRICE_STALENESS_SECONDS

        if current_price is None or stale:
            try:
                snap = data_client.get_stock_snapshot(
                    StockSnapshotRequest(symbol_or_symbols=[sym])
                ).get(sym)
                if snap and snap.latest_trade:
                    current_price = float(snap.latest_trade.price)
                    with _prices_lock:
                        _prices[sym] = current_price
                        _price_updated_at[sym] = time.monotonic()
                    if stale and updated_at is not None:
                        logger.info(
                            "  %s — price cache stale (%.0fs old), refreshed via snapshot -> $%.4f",
                            sym, time.monotonic() - updated_at, current_price,
                        )
            except Exception:
                pass

        if current_price is None:
            continue

        gain_pct = (current_price - buy_price) / buy_price * 100

        def _sell_and_close(reason: str, timeout: int = 30) -> None:
            _cancel_and_unregister_stops(stop_order_id, hard_stop_order_id)

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

        # ── 1. Hard stop (poll fallback — the resting broker order from entry
        #      normally catches this instantly via a TradingStream fill event;
        #      this only fires if that order was never placed or is missing) ──
        if HARD_STOP_PCT > 0 and gain_pct <= -HARD_STOP_PCT:
            logger.info("  HARD STOP  %s  gain=%.2f%%", sym, gain_pct)
            _sell_and_close(f"Hard stop -{HARD_STOP_PCT:.0f}%")
            continue

        # ── 2. Time exit — graduated checkpoints at 30/60min tighten the bar,
        #      MAX_HOLD_MINUTES is the unconditional final cutoff. ──────────
        buy_dt   = datetime.fromisoformat(pos["buy_time"])
        held_min = (now - buy_dt).total_seconds() / 60
        if held_min >= MAX_HOLD_MINUTES:
            logger.info("  TIME EXIT  %s  held %.0fm  gain=%+.1f%%  (max hold)", sym, held_min, gain_pct)
            _sell_and_close("Max hold time exit")
            continue
        elif held_min >= 60 and gain_pct < MIN_GAIN_AT_60M:
            logger.info("  TIME EXIT  %s  held %.0fm  gain=%+.1f%% < %.1f%% required by 60m",
                        sym, held_min, gain_pct, MIN_GAIN_AT_60M)
            _sell_and_close("60-min checkpoint exit")
            continue
        elif held_min >= 30 and gain_pct < MIN_GAIN_AT_30M:
            logger.info("  TIME EXIT  %s  held %.0fm  gain=%+.1f%% < %.1f%% required by 30m",
                        sym, held_min, gain_pct, MIN_GAIN_AT_30M)
            _sell_and_close("30-min checkpoint exit")
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
            # Whichever stop type is currently resting (trailing or hard —
            # they're mutually exclusive, see entry logic) gets cancelled and
            # replaced with a tight trailing stop to lock in gains.
            active_stop_id = stop_order_id or hard_stop_order_id
            logger.info(
                "  LOCK  %s  +%.1f%% -> tightening stop -> %.0f%% trailing",
                sym, gain_pct, TIGHT_STOP_PCT,
            )
            cancelled = trader.cancel_order(active_stop_id) if active_stop_id else True
            if cancelled:
                new_stop = trader.submit_trailing_stop(sym, shares, TIGHT_STOP_PCT)
                if new_stop:
                    new_id = str(new_stop.id)
                    mark_stop_tightened(pos_id, new_id)
                    if hard_stop_order_id:
                        update_hard_stop_order(pos_id, None)
                    with _ts_lock:
                        _ts_to_pos.pop(active_stop_id, None)
                    _register_stops(pos_id, sym, buy_price, shares, new_id, None)
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

    # ── 1. Universe — Stage A (cached daily) + Stage B (per-cycle snapshot) ───
    universe_symbols = _get_daily_universe(now_et)
    if not universe_symbols:
        logger.info("[%s] No universe symbols available", ts)
        return

    actives = get_universe_snapshot(ALPACA_KEY, ALPACA_SECRET, universe_symbols, MIN_PRICE, MAX_PRICE)
    if not actives:
        logger.info("[%s] No candidates in $%.2f-$%.2f band", ts, MIN_PRICE, MAX_PRICE)
        return

    band_count = len(actives)
    # actives is already sorted by volume descending (see get_universe_snapshot).
    # Pulling 1-min+15-min bars for the full band (2000-3000+ symbols on a
    # typical day) is what made scan_and_trade take 55-100+ seconds end to
    # end on 2026-07-31 — long enough that entries were acting on signals a
    # minute or more stale. Capping to the top MAX_CANDIDATES by volume
    # keeps the bars fetch fast while still favoring the names most likely
    # to be worth screening.
    if MAX_CANDIDATES > 0 and len(actives) > MAX_CANDIDATES:
        actives = actives[:MAX_CANDIDATES]

    symbols    = [s.symbol for s in actives]
    price_map  = {s.symbol: (s.price, s.change_pct) for s in actives}
    volume_map = {s.symbol: s.volume for s in actives}

    # ── 2. Bars — 1-min (velocity + session VWAP), 15-min (RVOL) ──────────────
    try:
        bars1 = data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=_1MIN,
            start=now - timedelta(minutes=90),
            end=now,
        )).data
    except Exception as e:
        logger.warning("1-min bar fetch failed: %s", e)
        bars1 = {}

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

    # ── 3. Screen ─────────────────────────────────────────────────────────────
    passing = []
    for sym in symbols:
        price, chg = price_map[sym]
        result = _analyze(
            sym,
            list(bars1.get(sym,  [])),
            list(bars15.get(sym, [])),
            price,
            chg,
            now_et,
            velocity_lookback_min=VELOCITY_LOOKBACK_MIN,
        )
        if result and result.passes:
            passing.append(result)

    passing.sort(key=lambda s: s.velocity or 0.0, reverse=True)

    logger.info(
        "[%s] Universe %d -> %d in band -> %d screened (top by volume) -> %d passing velocity+RVOL  "
        "pos=%d/%d  buy=$%.2f",
        ts, len(universe_symbols), band_count, len(symbols), len(passing), open_count, MAX_POSITIONS, buy_amount,
    )

    if not passing:
        return

    # ── 4. Buy ────────────────────────────────────────────────────────────────
    for stock in passing:
        if get_open_position_count(PROVIDER) >= MAX_POSITIONS:
            break

        wallet    = get_wallet(SCREENER_ID)
        available = wallet["current_balance"] - wallet["day_start_balance"] * RESERVE_PCT / 100
        if available < buy_amount:
            logger.info("  SKIP — depleted available cash after buying")
            break

        sym = stock.symbol

        if is_ticker_on_cooldown(sym, COOLDOWN_SECS, PROVIDER):
            logger.info("  SKIP  %s — cooldown", sym)
            continue

        if MAX_ENTRY_MOVE_PCT > 0 and stock.change_pct is not None and stock.change_pct > MAX_ENTRY_MOVE_PCT:
            logger.info("  SKIP  %s — already up %.1f%% on the day (limit %.0f%%)",
                        sym, stock.change_pct, MAX_ENTRY_MOVE_PCT)
            continue

        if stock.velocity is None or stock.velocity < VELOCITY_MIN:
            logger.info("  SKIP  %s — velocity %.2f%% < %.1f%% min",
                        sym, stock.velocity or 0.0, VELOCITY_MIN)
            continue

        if stock.rvol is None or stock.rvol < MIN_RVOL:
            logger.info("  SKIP  %s — RVOL %.1fx < %.1fx min", sym, stock.rvol or 0.0, MIN_RVOL)
            continue

        logger.info(
            "  BUY   %s  $%.4f  velocity=%+.2f%% (min %.1f%%)  RVOL=%.1fx (min %.1fx)  "
            "VWAP=%s  budget=$%.2f",
            sym, stock.price, stock.velocity or 0.0, VELOCITY_MIN, stock.rvol or 0.0,
            MIN_RVOL, "ok" if stock.above_vwap else "fail", buy_amount,
        )

        order, err = trader.buy_stock(sym, buy_amount, stock.price, slippage_pct=SLIPPAGE_PCT)
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
            change_pct_at_entry  = stock.change_pct,
            rvol_at_entry        = round(stock.rvol, 3) if stock.rvol else None,
        )

        # Alpaca reserves the full share qty against the first resting sell
        # order it accepts, so a trailing stop and a hard stop-loss can never
        # both rest at once on the same shares — the second submit always
        # fails with "insufficient qty available for order". Prefer the hard
        # stop (tighter, fixed-price, fires the instant price trades through
        # it) since HARD_STOP_PCT is RUNNER's primary risk control, falling
        # back to the trailing stop only if the hard-stop submit itself fails.
        ts_id = None
        hs_id = None
        if HARD_STOP_PCT > 0:
            hard_stop_price = fill_price * (1 - HARD_STOP_PCT / 100)
            hs_order = trader.submit_stop_loss(sym, fill_qty, hard_stop_price)
            if hs_order:
                hs_id = str(hs_order.id)
                update_hard_stop_order(pos_id, hs_id)
                logger.info("  STOP  %s  hard=-%.0f%% ($%.4f)  id=%s",
                            sym, HARD_STOP_PCT, hard_stop_price, hs_id)
            else:
                logger.warning("  Hard stop-loss failed for %s — falling back to trailing stop", sym)
                ts_order = trader.submit_trailing_stop(sym, fill_qty, TRAIL_PCT)
                if ts_order:
                    ts_id = str(ts_order.id)
                    update_trailing_stop_order(pos_id, ts_id)
                    logger.info("  STOP  %s  trail=%.0f%%  id=%s", sym, TRAIL_PCT, ts_id)
                else:
                    logger.warning("  Trailing stop failed for %s — set manually on Alpaca", sym)
        else:
            ts_order = trader.submit_trailing_stop(sym, fill_qty, TRAIL_PCT)
            if ts_order:
                ts_id = str(ts_order.id)
                update_trailing_stop_order(pos_id, ts_id)
                logger.info("  STOP  %s  trail=%.0f%%  id=%s", sym, TRAIL_PCT, ts_id)
            else:
                logger.warning("  Trailing stop failed for %s — set manually on Alpaca", sym)

        # Register in memory so TradingStream callback can close without a DB lookup
        if ts_id or hs_id:
            _register_stops(pos_id, sym, fill_price, fill_qty, ts_id, hs_id)

        # Start receiving real-time price ticks for this position
        _subscribe_prices([sym])

        if DISCORD_WEBHOOK:
            send_alert(
                webhook_url    = DISCORD_WEBHOOK,
                symbol         = sym,
                provider       = PROVIDER,
                price          = fill_price,
                rsi            = None,
                volume         = int(volume_map.get(sym, 0)),
                momentum       = stock.change_pct,
                shares_bought  = fill_qty,
                total_cost     = cost,
                paper          = ALPACA_PAPER,
            )

        record_ticker_alert(sym, PROVIDER)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _trader
    mode = "PAPER" if ALPACA_PAPER else "LIVE"
    logger.info("=" * 60)
    logger.info("RUNNER screener starting  [%s]", SCREENER_ID)
    logger.info(
        "Mode: %s | MaxPos: %d | Reserve: %.0f%% | MaxBuy: $%.2f | HardStop: %.0f%% | "
        "MaxHold: %dm | Slippage: %.1f%%",
        mode, MAX_POSITIONS, RESERVE_PCT, MAX_BUY_AMOUNT, HARD_STOP_PCT, MAX_HOLD_MINUTES, SLIPPAGE_PCT,
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
    logger.info(
        "Universe band: $%.2f-$%.2f (top %d by volume) | Entry filters: VELOCITY>=%.1f%% (%dmin)  "
        "RVOL>=%.1fx  MAX_ENTRY_MOVE_PCT=%.0f%%  PriceStaleness=%.0fs",
        MIN_PRICE, MAX_PRICE, MAX_CANDIDATES, VELOCITY_MIN, VELOCITY_LOOKBACK_MIN, MIN_RVOL,
        MAX_ENTRY_MOVE_PCT, PRICE_STALENESS_SECONDS,
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

        # Re-register both stop orders in memory for TradingStream callbacks
        for p in existing:
            ts_id = p["trailing_stop_order_id"]
            hs_id = p["hard_stop_order_id"]
            if ts_id or hs_id:
                _register_stops(p["id"], p["symbol"], p["buy_price"], p["shares"], ts_id, hs_id)

        # Check for stop fills that happened while we were offline
        with _ts_lock:
            snapshot = dict(_ts_to_pos)
        already_closed: Set[int] = set()
        for oid, pos_info in snapshot.items():
            if pos_info["id"] in already_closed:
                continue
            try:
                order = _trader.client.get_order_by_id(oid)
                if order.status.value == "filled":
                    logger.info(
                        "Startup: stop order %s was filled during downtime — closing %s",
                        oid, pos_info["symbol"],
                    )
                    _close_position_from_stop(oid, float(order.filled_avg_price))
                    already_closed.add(pos_info["id"])
            except Exception as e:
                logger.debug("Startup stop check for %s: %s", oid, e)

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
                _check_untracked_positions(_trader, data_client)
                scan_and_trade(_trader, data_client)
                last_scan = now

            time.sleep(MONITOR_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Stopped by user.")


if __name__ == "__main__":
    main()
