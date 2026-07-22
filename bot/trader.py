import logging
import time
from typing import Optional, Tuple

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
    TrailingStopOrderRequest,
)

logger = logging.getLogger(__name__)


class Trader:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        self.client = TradingClient(api_key, api_secret, paper=paper)
        self.paper = paper

    def is_market_open(self) -> bool:
        try:
            return self.client.get_clock().is_open
        except Exception:
            return False

    def buy_stock(
        self,
        symbol: str,
        budget_usd: float,
        current_price: float,
    ) -> Tuple[Optional[Order], Optional[str]]:
        """
        Buy as many whole shares of *symbol* as *budget_usd* allows at
        *current_price*. Returns (order, None) on success or (None, error_str).
        """
        shares = int(budget_usd / current_price)
        if shares < 1:
            return None, (
                f"Price ${current_price:.4f} exceeds ${budget_usd:.0f} budget "
                f"(need at least ${current_price:.4f} for 1 share)"
            )

        limit_price = round(current_price * 1.005, 2)
        cost = shares * limit_price
        try:
            bp = float(self.client.get_account().buying_power)
            if bp < cost:
                return None, (
                    f"insufficient buying power (have ${bp:.2f}, need ${cost:.2f} "
                    f"for {shares} × ${limit_price:.2f})"
                )
        except Exception as e:
            logger.warning("Could not verify buying power before buy: %s", e)

        try:
            order = self.client.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=str(shares),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                    extended_hours=True,
                )
            )
            logger.info(
                "Buy submitted: %s x%d  limit=$%.4f  id=%s", symbol, shares, limit_price, order.id
            )
            return order, None
        except Exception as e:
            logger.error("Buy order failed for %s: %s", symbol, e)
            return None, str(e)

    def wait_for_fill(self, order_id: str, timeout: int = 60, poll: int = 5) -> Optional[Order]:
        """
        Poll order status until filled, cancelled, or *timeout* seconds elapse.
        Returns the filled Order or None.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                order = self.client.get_order_by_id(order_id)
                status = order.status.value
                if status == "filled":
                    logger.info(
                        "Order %s filled: %s shares @ $%s",
                        order_id, order.filled_qty, order.filled_avg_price,
                    )
                    return order
                if status in ("cancelled", "expired", "rejected"):
                    logger.warning("Order %s ended with status: %s", order_id, status)
                    return None
            except Exception as e:
                logger.error("Error polling order %s: %s", order_id, e)
            time.sleep(poll)

        logger.warning("Order %s did not fill within %ds", order_id, timeout)
        return None

    def submit_trailing_stop(
        self, symbol: str, qty: int, trail_percent: float
    ) -> Optional[Order]:
        """
        Place a GTC trailing-stop SELL order.
        *trail_percent*: distance from the high-water mark. Alpaca caps this at 25%.
        The order stays active across sessions until filled or manually cancelled.
        """
        # Alpaca hard limit
        if trail_percent > 25:
            logger.warning(
                "trail_percent %.0f%% exceeds Alpaca's 25%% maximum — capping at 25%%",
                trail_percent,
            )
            trail_percent = 25.0
        try:
            order = self.client.submit_order(
                TrailingStopOrderRequest(
                    symbol=symbol,
                    qty=str(qty),
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    trail_percent=trail_percent,
                )
            )
            logger.info(
                "Trailing stop submitted: %s x%d  trail=%.0f%%  id=%s",
                symbol, qty, trail_percent, order.id,
            )
            return order
        except Exception as e:
            logger.error("Trailing stop failed for %s: %s", symbol, e)
            return None

    def submit_stop_loss(
        self, symbol: str, qty: int, stop_price: float
    ) -> Optional[Order]:
        """
        Place a GTC stop-loss SELL order that rests on the broker at a fixed
        *stop_price*. Unlike the polled HARD_STOP_PCT check in the screener
        loops, this fires the instant the exchange prints a trade through the
        stop — no waiting for the next monitor cycle, which is what let fast
        drops slip a point or more past the intended percentage.
        """
        # Alpaca requires whole-cent increments at/above $1; sub-penny (up to
        # 4 decimals) is only accepted below $1. Rounding to 4 decimals
        # unconditionally produces prices like $1.178 that get rejected.
        tick_price = round(stop_price, 2) if stop_price >= 1 else round(stop_price, 4)
        try:
            order = self.client.submit_order(
                StopOrderRequest(
                    symbol=symbol,
                    qty=str(qty),
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    stop_price=tick_price,
                )
            )
            logger.info(
                "Stop-loss submitted: %s x%d  stop=$%.4f  id=%s",
                symbol, qty, tick_price, order.id,
            )
            return order
        except Exception as e:
            logger.error("Stop-loss failed for %s: %s", symbol, e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order_by_id(order_id)
            logger.info("Cancelled order %s", order_id)
            return True
        except Exception as e:
            logger.error("Cancel failed for %s: %s", order_id, e)
            return False

    def market_sell(self, symbol: str, qty: int) -> Optional[Order]:
        try:
            order = self.client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=str(qty),
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            logger.info("Market sell submitted: %s x%d  id=%s", symbol, qty, order.id)
            return order
        except Exception as e:
            logger.error("Market sell failed for %s: %s", symbol, e)
            return None

    def get_cash_balance(self) -> Optional[float]:
        try:
            return float(self.client.get_account().cash)
        except Exception as e:
            logger.error("Account cash fetch failed: %s", e)
            return None

    def get_order_status(self, order_id: str) -> Optional[str]:
        try:
            return self.client.get_order_by_id(order_id).status.value
        except Exception as e:
            logger.error("Status check failed for %s: %s", order_id, e)
            return None

    def get_position_qty(self, symbol: str) -> int:
        """Return currently held share qty for symbol, or 0 if no position exists."""
        try:
            pos = self.client.get_open_position(symbol)
            return int(float(pos.qty))
        except Exception:
            return 0

    def get_filled_avg_price(self, order_id: str) -> Optional[float]:
        try:
            order = self.client.get_order_by_id(order_id)
            return float(order.filled_avg_price) if order.filled_avg_price else None
        except Exception as e:
            logger.error("Fill-price lookup failed for %s: %s", order_id, e)
            return None
