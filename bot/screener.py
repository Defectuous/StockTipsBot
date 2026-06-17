"""
Screen the live trending-movers list for stocks with bullish momentum setups.

Each candidate is evaluated across two timeframes:

  5-minute bars  — RSI bounce: RSI has pulled back to the 50–65 zone and is
                   now recovering upward (not overbought, still has room)
  15-minute bars — MACD: line is above signal with an expanding histogram
                   (confirms momentum building, not fading)

ATR (Average True Range) is computed from 5-min bars and shown as volatility
context — it is not a pass/fail filter.

Results are sorted by RSI ascending — lowest RSI first means most room left
before hitting overbought territory.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

import pytz
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.market_data import _atr, _macd_analysis, _rsi_series
from bot.trending import TrendingStock, get_trending

logger = logging.getLogger(__name__)

_5MIN  = TimeFrame(5,  TimeFrameUnit.Minute)
_15MIN = TimeFrame(15, TimeFrameUnit.Minute)

_5MIN_WINDOW_MINUTES  = 120   # 2 hours → ~24 bars
_15MIN_WINDOW_DAYS    = 3     # 3 calendar days → ~50 15-min bars (need ≥35 for MACD)

_RSI_MIN     = 50.0   # below this = momentum lost
_RSI_MAX     = 75.0   # above this = overbought


@dataclass
class ScreenedStock:
    symbol:              str
    price:               float
    change_pct:          float
    rsi:                 float
    rsi_trending_up:     bool
    macd_above_signal:   bool
    macd_crossover:      bool
    histogram_expanding: bool
    atr:                 Optional[float]

    @property
    def passes(self) -> bool:
        rsi_ok   = self.rsi_trending_up and _RSI_MIN <= self.rsi <= _RSI_MAX
        macd_ok  = self.macd_above_signal and self.histogram_expanding
        return rsi_ok and macd_ok


def _analyze(
    symbol:     str,
    bars_5m:    list,
    bars_15m:   list,
    price:      float,
    change_pct: float,
) -> Optional["ScreenedStock"]:
    if len(bars_5m) < 20:
        logger.debug("%s: only %d 5-min bars — skipping", symbol, len(bars_5m))
        return None

    closes_5m  = [b.close for b in bars_5m]
    rsi_vals   = [r for r in _rsi_series(closes_5m) if r is not None]
    if len(rsi_vals) < 4:
        return None

    rsi            = rsi_vals[-1]
    rsi_trending_up = rsi_vals[-1] > rsi_vals[-3]

    closes_15m = [b.close for b in bars_15m]
    macd       = _macd_analysis(closes_15m)
    atr_val    = _atr(bars_5m)

    return ScreenedStock(
        symbol              = symbol,
        price               = price,
        change_pct          = change_pct,
        rsi                 = rsi,
        rsi_trending_up     = rsi_trending_up,
        macd_above_signal   = macd["above_signal"]        if macd else False,
        macd_crossover      = macd["crossover"]           if macd else False,
        histogram_expanding = macd["histogram_expanding"] if macd else False,
        atr                 = atr_val,
    )


def screen_trending(api_key: str, api_secret: str) -> List[ScreenedStock]:
    """Return trending stocks ($2–$25) showing a live MACD + RSI bounce setup."""
    trending: List[TrendingStock] = get_trending(api_key, api_secret)
    if not trending:
        logger.info("No trending stocks to screen.")
        return []

    symbols   = [s.symbol for s in trending]
    price_map = {s.symbol: (s.price, s.change_pct) for s in trending}
    now       = datetime.now(pytz.UTC)
    client    = StockHistoricalDataClient(api_key, api_secret)

    try:
        bars_5m = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=_5MIN,
            start=now - timedelta(minutes=_5MIN_WINDOW_MINUTES),
            end=now,
        )).data
    except Exception as e:
        logger.error("5-min bar fetch failed: %s", e)
        bars_5m = {}

    try:
        bars_15m = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=_15MIN,
            start=now - timedelta(days=_15MIN_WINDOW_DAYS),
            end=now,
        )).data
    except Exception as e:
        logger.error("15-min bar fetch failed: %s", e)
        bars_15m = {}

    candidates = []
    for symbol in symbols:
        price, change_pct = price_map[symbol]
        stock = _analyze(
            symbol,
            list(bars_5m.get(symbol,  [])),
            list(bars_15m.get(symbol, [])),
            price,
            change_pct,
        )
        if stock and stock.passes:
            candidates.append(stock)

    candidates.sort(key=lambda s: s.rsi)
    return candidates
