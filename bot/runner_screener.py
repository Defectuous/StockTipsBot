"""
Screen an arbitrary candidate universe for early-runner momentum: a
short-window price velocity spike confirmed by elevated volume, instead of
the RSI-bounce/MACD-confirmation setup SML/SML2 use. Meant to catch a move
while it's still small (reacting within 1-2% of the move), not after it's
already a top mover — see todo.md's 2026-07-30 review (CYCU/GCTK never
appeared in any screener log until they were already up 100-500%).

  1-minute bars  — price velocity: % change over a short trailing window
  15-minute bars — time-adjusted RVOL (today's volume vs. the historical
                   average for the same time-of-day window)
  Short session-anchored VWAP (last ~60 min of 1-min bars) — sanity filter;
  a full-day VWAP is nearly meaningless for a name that sat flat for hours
  before spiking.

Results are sorted by velocity descending — the fastest-moving names first.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional

from bot.market_data import _price_velocity_pct, _rvol_time_adjusted, _vwap

logger = logging.getLogger(__name__)

VELOCITY_MIN_PCT      = 3.0   # trailing-window % price move required to trigger
VELOCITY_LOOKBACK_MIN = 3     # minutes back to measure velocity over
RVOL_MIN               = 3.0  # time-adjusted RVOL floor
VWAP_WINDOW_MIN         = 60  # session-anchored VWAP window, not full-day


@dataclass
class RunnerScreenedStock:
    symbol:            str
    price:              float
    change_pct:         Optional[float]
    velocity:            Optional[float]
    rvol:                Optional[float]
    vwap:                Optional[float]
    above_vwap:          bool
    last_bar_bullish:    bool

    @property
    def passes(self) -> bool:
        velocity_ok = self.velocity is not None and self.velocity >= VELOCITY_MIN_PCT
        rvol_ok     = self.rvol is not None and self.rvol >= RVOL_MIN
        # Live data from 2026-07-31 showed the velocity trigger frequently
        # firing on a spike that was already reversing (confirmed by
        # Alpaca's hard-stop rejections showing the market already several
        # % below the fill price within ~1 second of entry). Requiring the
        # most recent 1-min bar to still be green is a cheap check against
        # buying a move that's already topped.
        return velocity_ok and rvol_ok and self.above_vwap and self.last_bar_bullish


def _analyze(
    symbol:     str,
    bars1:      list,
    bars15:     list,
    price:      float,
    change_pct: Optional[float],
    now_et,
    velocity_lookback_min: int = VELOCITY_LOOKBACK_MIN,
) -> Optional["RunnerScreenedStock"]:
    """
    bars1 should cover at least VWAP_WINDOW_MIN minutes so the
    session-anchored VWAP has enough data; velocity only needs
    velocity_lookback_min + 1.

    bars15 should cover several prior trading days for RVOL.
    """
    if len(bars1) < velocity_lookback_min + 1:
        logger.debug("%s: only %d 1-min bars — skipping", symbol, len(bars1))
        return None

    velocity = _price_velocity_pct(bars1, lookback=velocity_lookback_min)
    rvol     = _rvol_time_adjusted(bars15, now_et)
    vwap_val = _vwap(bars1[-VWAP_WINDOW_MIN:])
    last_bar = bars1[-1]

    return RunnerScreenedStock(
        symbol            = symbol,
        price             = price,
        change_pct        = change_pct,
        velocity          = velocity,
        rvol              = rvol,
        vwap              = vwap_val,
        above_vwap        = (price > vwap_val) if vwap_val is not None else False,
        last_bar_bullish  = last_bar.close >= last_bar.open,
    )
