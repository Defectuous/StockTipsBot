import logging
from dataclasses import dataclass
from typing import List

from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MarketMoversRequest

logger = logging.getLogger(__name__)

MIN_PRICE = 2.0
MAX_PRICE = 25.0
MAX_RESULTS = 10
# Request more than we need so price and change-pct filters have room to work
_FETCH_TOP = 50


@dataclass
class TrendingStock:
    symbol: str
    price: float
    change_pct: float
    change_usd: float


def get_trending(api_key: str, api_secret: str, min_price: float = MIN_PRICE, max_price: float = MAX_PRICE, max_change_pct: Optional[float] = None) -> List[TrendingStock]:
    """
    Return up to MAX_RESULTS gainers priced below *max_price*, sorted by
    percent change descending.  May return fewer if not enough qualify.
    """
    client = ScreenerClient(api_key=api_key, secret_key=api_secret)

    try:
        movers = client.get_market_movers(
            MarketMoversRequest(top=_FETCH_TOP, market_type="stocks")
        )
    except Exception as e:
        logger.error("Failed to fetch market movers: %s", e)
        return []

    gainers = getattr(movers, "gainers", []) or []

    results = []
    for mover in gainers:
        price = float(mover.price or 0)
        pct = float(mover.percent_change or 0)
        if price < min_price or price >= max_price or pct <= 0:
            continue
        if max_change_pct is not None and pct >= max_change_pct:
            continue
        results.append(TrendingStock(
            symbol=mover.symbol,
            price=price,
            change_pct=round(pct, 2),
            change_usd=round(float(mover.change or 0), 4),
        ))

    results.sort(key=lambda s: s.change_pct, reverse=True)
    return results[:MAX_RESULTS]
