"""
TradingStream subclass that adds exponential backoff to reconnects.

alpaca-py's TradingStream._run_forever() retries on any failure -- including
a flat-out auth rejection -- after only a 10ms sleep. Against a server that
also rate-limits (HTTP 429) reconnect floods, that turns one lost handshake
into an unbounded storm: SML and SML2 share one Alpaca API key, and when they
raced for the account's single trading-stream slot on 2026-08-24, the loser
logged 200k+ reconnect attempts over 12 hours, which appears to have gotten
the shared key rate-limited across REST/screener endpoints too. Overriding
_run_forever (same control flow, capped exponential backoff + jitter) makes
a lost race or any other transient rejection degrade to slow retries instead.
"""
import asyncio
import logging
import random
from typing import Callable, Optional

from alpaca.trading.stream import TradingStream as _AlpacaTradingStream

logger = logging.getLogger(__name__)


class ResilientTradingStream(_AlpacaTradingStream):
    _BASE_BACKOFF = 1.0
    _MAX_BACKOFF = 60.0
    _ALERT_AFTER_FAILURES = 5  # roughly 1+2+4+8+16 = 31s of failures before alerting

    def __init__(self, *args, on_persistent_failure: Optional[Callable[[str], None]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_persistent_failure = on_persistent_failure
        self._consecutive_failures = 0
        self._alerted = False

    async def _run_forever(self):
        self._loop = asyncio.get_running_loop()
        while not self._trade_updates_handler:
            if not self._stop_stream_queue.empty():
                self._stop_stream_queue.get(timeout=1)
                return
            await asyncio.sleep(0.1)
        logger.info("started trading stream")
        self._should_run = True
        self._running = False
        backoff = self._BASE_BACKOFF

        while True:
            if not self._should_run:
                logger.info("Trading stream stopped")
                return
            try:
                if not self._running:
                    logger.info("starting trading websocket connection")
                    await self._start_ws()
                    self._running = True
                    if self._consecutive_failures:
                        logger.info(
                            "trading stream recovered after %d failed attempts",
                            self._consecutive_failures,
                        )
                    backoff = self._BASE_BACKOFF
                    self._consecutive_failures = 0
                    self._alerted = False
                await self._consume()
            except Exception as e:
                await self.close()
                self._running = False
                self._consecutive_failures += 1
                sleep_for = backoff * (0.8 + 0.4 * random.random())
                logger.warning(
                    "trading stream error (attempt %d), backing off %.1fs: %s",
                    self._consecutive_failures, sleep_for, e,
                )
                if (
                    self._on_persistent_failure
                    and not self._alerted
                    and self._consecutive_failures >= self._ALERT_AFTER_FAILURES
                ):
                    self._alerted = True
                    try:
                        self._on_persistent_failure(
                            f"Trading stream has failed {self._consecutive_failures} times in a "
                            f"row (latest: {e}); backing off up to {self._MAX_BACKOFF:.0f}s between retries."
                        )
                    except Exception:
                        logger.exception("on_persistent_failure callback raised")
                await asyncio.sleep(sleep_for)
                backoff = min(backoff * 2, self._MAX_BACKOFF)
