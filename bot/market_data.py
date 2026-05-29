import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import pytz
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI from a list of close prices (oldest first)."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def get_stock_data(symbol: str, api_key: str, api_secret: str) -> Optional[Dict]:
    """
    Fetch recent market data for *symbol* via Alpaca.
    Returns a dict with keys: symbol, price, open, high, low, close,
    volume, rsi, momentum, bars — or None on complete failure.
    """
    client = StockHistoricalDataClient(api_key, api_secret)

    # Fetch last ~30 minutes of 1-minute bars for RSI / momentum
    end = datetime.now(pytz.UTC)
    start = end - timedelta(minutes=35)

    bars = []
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
        )
        resp = client.get_stock_bars(req)
        bars = list(resp.get(symbol, []))
    except Exception as e:
        logger.warning("Bars unavailable for %s: %s", symbol, e)

    # Latest bar for current price snapshot
    latest_bar = None
    try:
        req = StockLatestBarRequest(symbol_or_symbols=symbol)
        resp = client.get_stock_latest_bar(req)
        latest_bar = resp.get(symbol)
    except Exception as e:
        logger.warning("Latest bar unavailable for %s: %s", symbol, e)

    if not latest_bar and not bars:
        logger.error("No market data at all for %s", symbol)
        return None

    # Use latest bar if available; fall back to last historical bar
    bar = latest_bar or bars[-1]

    closes = [b.close for b in bars]
    rsi = _rsi(closes) if len(closes) >= 15 else None

    momentum = None
    if len(closes) >= 10:
        momentum = round((closes[-1] - closes[-10]) / closes[-10] * 100, 2)

    return {
        "symbol":   symbol,
        "price":    bar.close,
        "open":     bar.open,
        "high":     bar.high,
        "low":      bar.low,
        "close":    bar.close,
        "volume":   bar.volume,
        "rsi":      rsi,
        "momentum": momentum,
        "bars":     bars,
    }
