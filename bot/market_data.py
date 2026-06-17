import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import pytz
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)


def _ema_series(values: List[float], period: int) -> List[Optional[float]]:
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    result[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1.0 - k)
        result[i] = ema
    return result


def _rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    result: List[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _to_rsi(ag: float, al: float) -> float:
        return 100.0 if al == 0 else round(100 - (100 / (1 + ag / al)), 2)

    result[period] = _to_rsi(avg_gain, avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i + 1] = _to_rsi(avg_gain, avg_loss)
    return result


def _macd_analysis(closes: List[float]) -> Optional[dict]:
    """MACD crossover analysis on the provided close series."""
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)

    macd_vals: List[float] = [
        e12 - e26
        for e12, e26 in zip(ema12, ema26)
        if e12 is not None and e26 is not None
    ]
    if len(macd_vals) < 9:
        return None

    sig_series = _ema_series(macd_vals, 9)
    pairs = [(m, s) for m, s in zip(macd_vals, sig_series) if s is not None]
    if len(pairs) < 3:
        return None

    macd, signal   = pairs[-1]
    pm,   ps       = pairs[-2]
    histogram      = macd - signal
    prev_histogram = pm - ps

    # Crossover: above signal now, was below within last 10 bars
    was_below = any(m < s for m, s in pairs[-10:-1])
    crossover = (macd > signal) and was_below
    histogram_expanding = histogram > 0 and histogram > prev_histogram

    return {
        "macd":                round(macd,      4),
        "signal":              round(signal,    4),
        "histogram":           round(histogram, 4),
        "crossover":           crossover,
        "above_signal":        macd > signal,
        "histogram_expanding": histogram_expanding,
    }


def _atr(bars: list, period: int = 14) -> Optional[float]:
    """Average True Range over the last *period* bars."""
    if len(bars) < period + 1:
        return None
    trs = [
        max(bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low  - bars[i - 1].close))
        for i in range(1, len(bars))
    ]
    return round(sum(trs[-period:]) / period, 4)


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
        bars = list(resp[symbol])
    except KeyError:
        logger.warning("No historical bars returned for %s", symbol)
    except Exception as e:
        logger.warning("Bars unavailable for %s: %s", symbol, e)

    # Latest bar for current price snapshot
    latest_bar = None
    try:
        req = StockLatestBarRequest(symbol_or_symbols=symbol)
        resp = client.get_stock_latest_bar(req)
        latest_bar = resp[symbol]
    except KeyError:
        logger.warning("No latest bar returned for %s", symbol)
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
        "volume":   int(bar.volume),
        "rsi":      rsi,
        "momentum": momentum,
        "bars":     bars,
    }
