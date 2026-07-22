# Trade Config

Tracks the current screening/trading settings for each screener and when they last changed.
Source of truth is still `.env` + the defaults in each `run_*_screener.py` docstring — this file
is a snapshot for quick reference, not a replacement. Update the relevant table (and add a
changelog entry below) any time a setting changes.

**Last updated:** 2026-07-22

## Deployed screeners

| Screener | Service | Universe | Status |
|---|---|---|---|
| SML | `screener-sml.service` | $0.50–$5.00 | running (paper) |
| SML2 | `screener-sml2.service` | $0.50–$5.00 | running (paper) |
| MID | `screener-mid.service` | $2–$20 | running (paper) |
| SUPER | `screener-super.service` | $2–$50 | running (paper) |
| LIVE | *(no service file)* | $0.50–$5.00, real money | not deployed — see [memory: Live Trading Budget]|

## SML (`run_sml_screener.py`)

| Setting | Value | Source |
|---|---|---|
| MAX_POSITIONS | 2 | .env |
| BUY_AMOUNT budget | deployable_capital / 2 | wallet, 25% reserve |
| TRAILING_STOP_PERCENT | 10% | .env |
| HARD_STOP_PCT | 5% (polled + resting order) | .env |
| PROFIT_LOCK_PCT | 15% -> tighten stop | .env |
| TIGHT_STOP_PCT | 5% | .env |
| MAX_HOLD_MINUTES | 90 | .env |
| MIN_GAIN_AT_30M | -2.0% | default |
| MIN_GAIN_AT_60M | 0.0% | default |
| MAX_ENTRY_MOVE_PCT | skip if already up >8% | .env |
| MIN_CHANGE_PCT | 2.0% min daily gain to buy | .env |
| MIN_RVOL | 1.5x | .env |
| RSI_EXIT_LEVEL | 75 (falling) | .env |
| RSI entry band | none (no gate) | — |
| MACD fresh-crossover gate | none (no gate) | — |
| START_TIME_ET / STOP_BUY_TIME_ET / DUMP_TIME_ET | 09:30 / 11:45 / 12:00 | .env |
| BUY_COOLDOWN_SECONDS | 86400 (once/day/stock) | .env |

## SML2 (`run_sml2_screener.py`)

Same as SML plus entry gates added 2026-07-22 (commit `04e261f`):

| Setting | Value | Source |
|---|---|---|
| RSI_ENTRY_MIN / RSI_ENTRY_MAX | 60 / 70 | code default |
| REQUIRE_MACD_FRESH_CROSSOVER | true | code default |
| MONITOR_INTERVAL_SECONDS | 10s (WebSocket price cache) | code default |

All other settings (MAX_POSITIONS, stops, hold time, entry filters, timing) match SML above — both
read the same shared `.env` keys.

## MID (`run_mid_screener.py`)

| Setting | Value | Source |
|---|---|---|
| MID_MIN_PRICE / MID_MAX_PRICE | $2.00 / $20.00 | .env |
| MAX_POSITIONS | 2 (shared key with SML) | .env |
| Stops / hold / entry filters | same as SML table above | .env |

## SUPER (`run_super_screener.py`)

| Setting | Value | Source |
|---|---|---|
| SUPER_MIN_PRICE / SUPER_MAX_PRICE | $2.00 / $50.00 | .env |
| SUPER_MAX_POSITIONS | 999 (effectively unlimited) | .env |
| SUPER_MAX_BUY_AMOUNT | $1000/trade | .env |
| Stops / hold / entry filters | same as SML table above (shared keys) | .env |

## LIVE (`run_live_screener.py`) — not yet running

| Setting | Value | Source |
|---|---|---|
| LIVE_MAX_POSITIONS | 2 | .env |
| LIVE_STARTING_BALANCE | $500 | .env |
| LIVE_ALPACA_API_KEY/SECRET | unset | .env |
| Everything else | code defaults (LIVE_ has no other overrides in .env) | — |

Planned to go live in paper first per [memory: Live Trading Budget ($500)].

---

## Changelog

- **2026-07-22** — Initial baseline captured from `.env` + code defaults. Fixed a stop-loss
  rounding bug in `bot/trader.py` (`submit_stop_loss` now rounds to 2 decimals at/above $1,
  4 decimals below $1) — not a screening-setting change, but noted here since it affects
  hard-stop order placement on both SML and SML2.
