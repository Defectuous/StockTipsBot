"""
Fetch the most actively traded stocks using Alpaca data only.

Uses the most-actives screener endpoint (top 100 by volume) then enriches
each symbol with a snapshot to get price, daily change %, and yesterday's
volume for surge detection.
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from alpaca.data import StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MostActivesRequest, StockSnapshotRequest

logger = logging.getLogger(__name__)

_FETCH_TOP = 100

MIN_PRICE_PENNY  = 0.50
MAX_PRICE_PENNY  = 5.00

MIN_PRICE_SURGE  = 2.00
MAX_PRICE_SURGE  = 20.00
MIN_VOL_RATIO    = 1.5   # today's volume must be at least 1.5× yesterday's


@dataclass
class ActivePennyStock:
    symbol:      str
    price:       float
    change_pct:  Optional[float]
    volume:      float
    trade_count: float


@dataclass
class VolumeSurgeStock:
    symbol:      str
    price:       float
    change_pct:  Optional[float]
    volume:      float
    prev_volume: float
    volume_ratio: float
    trade_count: float


def _fetch_actives_and_snapshots(
    api_key: str,
    api_secret: str,
) -> Tuple[Dict[str, Tuple[float, float]], Dict]:
    """Return (volume_map, snapshots) for the top 100 most active stocks."""
    screener = ScreenerClient(api_key=api_key, secret_key=api_secret)
    data     = StockHistoricalDataClient(api_key, api_secret)

    try:
        actives = screener.get_most_actives(MostActivesRequest(top=_FETCH_TOP))
    except Exception as e:
        logger.error("Failed to fetch most actives: %s", e)
        return {}, {}

    symbols    = [s.symbol for s in actives.most_actives]
    volume_map = {s.symbol: (s.volume, s.trade_count) for s in actives.most_actives}

    try:
        snapshots = data.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols))
    except Exception as e:
        logger.error("Failed to fetch snapshots: %s", e)
        return volume_map, {}

    return volume_map, snapshots


def get_most_active_penny_stocks(
    api_key:    str,
    api_secret: str,
    min_price:  float = MIN_PRICE_PENNY,
    max_price:  float = MAX_PRICE_PENNY,
) -> List[ActivePennyStock]:
    """Return penny stocks from the top 100 most active, sorted by volume descending."""
    volume_map, snapshots = _fetch_actives_and_snapshots(api_key, api_secret)
    if not snapshots:
        return []

    results = []
    for symbol, (vol, trades) in volume_map.items():
        snap = snapshots.get(symbol)
        if not snap or not snap.daily_bar:
            continue

        price = snap.daily_bar.close
        if price < min_price or price >= max_price:
            continue

        change_pct = None
        if snap.previous_daily_bar and snap.previous_daily_bar.close:
            prev = snap.previous_daily_bar.close
            change_pct = round((price - prev) / prev * 100, 2)

        results.append(ActivePennyStock(
            symbol      = symbol,
            price       = price,
            change_pct  = change_pct,
            volume      = vol,
            trade_count = trades,
        ))

    results.sort(key=lambda s: s.volume, reverse=True)
    return results


def get_volume_surge_stocks(
    api_key:       str,
    api_secret:    str,
    min_price:     float = MIN_PRICE_SURGE,
    max_price:     float = MAX_PRICE_SURGE,
    min_vol_ratio: float = MIN_VOL_RATIO,
) -> List[VolumeSurgeStock]:
    """
    Return $2–$20 stocks from the top 100 most active whose today's volume
    is at least min_vol_ratio × yesterday's volume, sorted by ratio descending.
    """
    volume_map, snapshots = _fetch_actives_and_snapshots(api_key, api_secret)
    if not snapshots:
        return []

    results = []
    for symbol, (vol, trades) in volume_map.items():
        snap = snapshots.get(symbol)
        if not snap or not snap.daily_bar or not snap.previous_daily_bar:
            continue

        price      = snap.daily_bar.close
        prev_vol   = snap.previous_daily_bar.volume
        if price < min_price or price >= max_price:
            continue
        if not prev_vol:
            continue

        ratio = vol / prev_vol
        if ratio < min_vol_ratio:
            continue

        prev_close = snap.previous_daily_bar.close
        change_pct = None
        if prev_close:
            change_pct = round((price - prev_close) / prev_close * 100, 2)

        results.append(VolumeSurgeStock(
            symbol       = symbol,
            price        = price,
            change_pct   = change_pct,
            volume       = vol,
            prev_volume  = prev_vol,
            volume_ratio = round(ratio, 2),
            trade_count  = trades,
        ))

    results.sort(key=lambda s: s.volume_ratio, reverse=True)
    return results
