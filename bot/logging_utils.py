"""Shared logging setup for the screener entry points."""
import logging
import os
import time
from logging.handlers import TimedRotatingFileHandler


class DailyFileHandler(TimedRotatingFileHandler):
    """
    Rotating file handler that names each day's log '<prefix>.mmddyyyy'
    (e.g. sml.log.08092026) instead of the base class's scheme of a static
    current-day filename that only gets a date suffix once rolled over.
    Rotation is checked on each emit() call (stdlib behavior), so the new
    day's file is opened lazily on the next log line after local midnight,
    not by a background timer.
    """

    def __init__(self, prefix: str, **kwargs):
        self._prefix = prefix
        super().__init__(self._dated_filename(), when="midnight", encoding="utf-8", **kwargs)

    def _dated_filename(self) -> str:
        return f"{self._prefix}.{time.strftime('%m%d%Y')}"

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        self.baseFilename = os.path.abspath(self._dated_filename())
        if not self.delay:
            self.stream = self._open()
        current_time = int(time.time())
        new_rollover_at = self.computeRollover(current_time)
        while new_rollover_at <= current_time:
            new_rollover_at += self.interval
        self.rolloverAt = new_rollover_at


def configure_logging(log_prefix: str) -> None:
    """logging.basicConfig with a console handler + a DailyFileHandler for log_prefix."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            DailyFileHandler(log_prefix),
        ],
    )
