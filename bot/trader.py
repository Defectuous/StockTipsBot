import logging
import time
from typing import Optional, Tuple

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest

logger = logging.getLogger(__name__)


class Trader:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        self.client = TradingClient(api_key, api_secret, paper=paper)
        self.paper = paper

    def is_market_open(self) -> bool:
        try:
            return self.client.get_clock().is_open
        except Exception as e:
            logger.error("Clock check failed: %s", e)
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

        try:
            order = self.client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=str(shares),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            logger.info(
                "Buy submitted: %s x%d @ ~$%.4f  id=%s", symbol, shares, current_price, order.id
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
        *trail_percent*: distance from the high-water mark (e.g. 50 = 50 %).
        The order stays active across sessions until filled or manually cancelled.
        """
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

    def get_order_status(self, order_id: str) -> Optional[str]:
        try:
            return self.client.get_order_by_id(order_id).status.value
        except Exception as e:
            logger.error("Status check failed for %s: %s", order_id, e)
            return None

    def get_filled_avg_price(self, order_id: str) -> Optional[float]:
        try:
            order = self.client.get_order_by_id(order_id)
            return float(order.filled_avg_price) if order.filled_avg_price else None
        except Exception as e:
            logger.error("Fill-price lookup failed for %s: %s", order_id, e)
            return None
