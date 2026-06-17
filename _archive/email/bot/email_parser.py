import re
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Each entry: (provider_name, compiled_regex_that_captures_ticker_in_group_1)
#
# STT format example:
#   STT:
#   Genius Group Ltd (GNS)
#
# BULLSEYE format example:
#   BULLSEYE (Ndaq: TDTH)
#
# Add new providers here as a new tuple.
_PATTERNS = [
    (
        "STT",
        re.compile(
            r"STT\s*:\s*[\r\n]+[^\r\n(]*\(\s*([A-Z]{1,5})\s*\)",
            re.IGNORECASE,
        ),
    ),
    (
        "BULLSEYE",
        re.compile(
            r"BULLSEYE\b[^\r\n]*\b(?:Ndaq|NASDAQ|NYSE|OTC|OTCBB|Amex)\s*:\s*([A-Z]{1,5})\b",
            re.IGNORECASE,
        ),
    ),
]


def parse_email(body: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (provider, ticker) extracted from a Google Voice forwarded SMS body.
    Returns (None, None) when no known pattern matches.
    """
    if not body:
        return None, None

    for provider, pattern in _PATTERNS:
        match = pattern.search(body)
        if match:
            ticker = match.group(1).upper().strip()
            logger.info("Parsed  provider=%s  ticker=%s", provider, ticker)
            return provider, ticker

    logger.debug("No provider pattern matched in email body")
    return None, None
