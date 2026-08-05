# Trade Config

Tracks the current screening/trading settings for each screener and when they last changed.
Source of truth is still `.env` + the defaults in each `run_*_screener.py` docstring — this file
is a snapshot for quick reference, not a replacement. Update the relevant table (and add a
changelog entry below) any time a setting changes.

**Last updated:** 2026-08-04 (MID/SUPER archived; RVOL ceiling + ATR-based risk sizing added to SML/SML2/RUNNER; RUNNER section added)

## Deployed screeners

| Screener | Service | Universe | Status |
|---|---|---|---|
| SML | `screener-sml.service` | $0.50–$5.00 | running (paper) |
| SML2 | `screener-sml2.service` | $0.50–$5.00 | running (paper) |
| RUNNER | `screener-runner.service` | $0.10–$10.00, wide-universe | running (paper) |
| MID | `_archive/mid/screener-mid.service` | $2–$20 | **archived 2026-08-04** — see below |
| SUPER | `_archive/super/screener-super.service` | $2–$50 | **archived 2026-08-04** — see below |
| LIVE | *(no service file)* | $0.50–$5.00, real money | not deployed — see [memory: Live Trading Budget]|

**MID/SUPER archived 2026-08-04:** never actively tuned (see [memory: SML/SML2 Focus] — user is
hyper-focused on SML/SML2), and MID's shared-key inheritance (e.g. picking up `MAX_RVOL`/
`MAX_ENTRY_MOVE_PCT` changes meant for SML/SML2) was pure incidental collateral, not a deliberate
config. `run_mid_screener.py`/`run_super_screener.py` and their `.service` files moved to
`_archive/mid/` and `_archive/super/` respectively (git history preserved via `git mv`). **This
only archives the code in this repo** — the actual `screener-mid`/`screener-super` systemd
services on the Pi are still running until manually stopped there:
`sudo systemctl stop screener-mid screener-super && sudo systemctl disable screener-mid screener-super`,
after pulling this change (note: once pulled, their `ExecStart` path no longer exists, so leaving
them running risks a `Restart=on-failure` crash-loop until they're stopped/disabled). Historical
MID/SUPER trade data in `stockbot.db` is untouched — `tools/explain_trading_day.py`,
`tools/sync_db.py`, etc. still support `--providers mid super` for looking back at old trades.

## SML (`run_sml_screener.py`)

| Setting | Value | Source |
|---|---|---|
| MAX_POSITIONS | 2 | .env |
| BUY_AMOUNT budget | deployable_capital / 2, then risk-sized (see ATR row below) | wallet, 25% reserve |
| TRAILING_STOP_PERCENT | 10% (fallback only, see ATR row) | .env |
| HARD_STOP_PCT | 5% base risk anchor (polled + resting order) | .env |
| ATR-based stop/sizing | stop = 2x ATR, bounded 2-10%; position sized so $ risk stays ~constant instead of $ exposure — added 2026-08-04 | `.env` (`ATR_STOP_MULT`/`ATR_MIN_STOP_PCT`/`ATR_MAX_STOP_PCT`, shared with SML2/RUNNER) |
| PROFIT_LOCK_PCT | 15% -> tighten stop | .env |
| TIGHT_STOP_PCT | 5% | .env |
| MAX_HOLD_MINUTES | 90 | .env |
| MIN_GAIN_AT_30M | -2.0% | default |
| MIN_GAIN_AT_60M | 0.0% | default |
| MAX_ENTRY_MOVE_PCT | skip if already up >15% | `.env` (`SML_MAX_ENTRY_MOVE_PCT` override, raised from shared 10% on 2026-08-04) |
| MIN_CHANGE_PCT | 2.0% min daily gain to buy | .env |
| MIN_RVOL | 1.5x | .env |
| MAX_RVOL | skip if RVOL >10x (likely already exhausted) | `.env` (shared key, added 2026-08-04 — see note below) |
| EXCLUDE_SYMBOLS | MSTU, TZA, HTZ (leveraged/decay ETPs + chronic losers) | `.env` (added 2026-08-01; only SML/SML2 implement this check — MID/SUPER/RUNNER don't) |
| RSI_EXIT_LEVEL | 75 (falling) | .env |
| RSI entry band | none (no gate) | — |
| MACD fresh-crossover gate | none (no gate) | — |
| START_TIME_ET / STOP_BUY_TIME_ET / DUMP_TIME_ET | 09:45 / off / 15:30 | `.env` (`SML_STOP_BUY_TIME_ET=off`, `SML_DUMP_TIME_ET=15:30` overrides, 2026-07-29) |
| BUY_COOLDOWN_SECONDS | 86400 (once/day/stock, **per-screener** as of 2026-07-23) | .env |

Note: `MAX_ENTRY_MOVE_PCT` now has a per-screener override for SML too (`SML_MAX_ENTRY_MOVE_PCT`,
added 2026-08-04) — the shared `MAX_ENTRY_MOVE_PCT=10` key now only affects MID and SUPER, which
have no override. `MAX_RVOL` is a shared key with **no** SML-specific override, and MID/SUPER's
code already reads it too, so it also applies there (confirmed 2026-08-04). `EXCLUDE_SYMBOLS` is
also a shared key with no override, but MID/SUPER/RUNNER don't implement the check in code at all
— it only affects SML/SML2 in practice.

## SML2 (`run_sml2_screener.py`)

Same as SML plus entry gates added 2026-07-22 (commit `04e261f`):

| Setting | Value | Source |
|---|---|---|
| RSI_ENTRY_MIN / RSI_ENTRY_MAX | 55 / 70 | `.env` (min lowered from 60, 2026-07-25) |
| MACD_MIN_BARS_ABOVE_SIGNAL | 3 (requires a **confirmed**, not fresh, crossover) | code default, `SML2_MACD_MIN_BARS_ABOVE_SIGNAL` override available — changed from requiring freshness 2026-07-29 (commit `095165e`): live data through that date showed fresh-crossover trades averaging -$1.75 (27% win, n=48) vs +$1.32 (46% win, n=13) for confirmed ones |
| ATR-based stop/sizing | same as SML row above — added 2026-08-04 | `.env` (shared `ATR_STOP_MULT`/`ATR_MIN_STOP_PCT`/`ATR_MAX_STOP_PCT`) |
| MAX_RVOL | skip if RVOL >10x | `.env` (shared key, added 2026-08-04) |
| MONITOR_INTERVAL_SECONDS | 10s (WebSocket price cache) | code default |
| MAX_ENTRY_MOVE_PCT | skip if already up >15% | `.env` (`SML2_MAX_ENTRY_MOVE_PCT` override, 2026-07-23) |

All other settings (MAX_POSITIONS, stops, hold time, entry filters, timing) match SML above — both
read the same shared `.env` keys, and are run as parallel A/B test variants, not redundant bots —
see [memory: SML/SML2 Focus].

## MID and SUPER — archived 2026-08-04 (`_archive/mid/`, `_archive/super/`)

Not actively tuned or reviewed (see [memory: SML/SML2 Focus]), moved out of the active root so
they stop showing up in day-to-day work. Last known config before archiving, for reference only —
not being kept current:

| Setting | MID | SUPER |
|---|---|---|
| Price band | $2.00–$20.00 | $2.00–$50.00 |
| MAX_POSITIONS | 2 (shared key with SML) | 999 (effectively unlimited, `SUPER_MAX_POSITIONS`) |
| Max buy amount | — | $1000/trade (`SUPER_MAX_BUY_AMOUNT`) |
| MAX_RVOL | 10x — inherited via shared key when it was added 2026-08-04 (not deliberately tuned) | same |
| ATR-based stop/sizing | never implemented — still flat %/$ sizing | same |
| Stops / hold / entry filters | otherwise matched SML's table above (shared `.env` keys) | same |

## RUNNER (`run_runner_screener.py`)

Structurally different from SML/SML2/MID/SUPER — scans the full tradable-asset universe (not
Alpaca's most-actives/movers list) and enters on short-window price velocity + RVOL, not RSI/MACD.

| Setting | Value | Source |
|---|---|---|
| MAX_POSITIONS | 2 (own key, `RUNNER_MAX_POSITIONS`) | code default |
| RESERVE_PCT | 50% | code default (`RUNNER_RESERVE_PCT`) — note: combined with the buy-sizing formula, this leaves RUNNER effectively running ~1 concurrent position most of the time even though the cap is 2; known, left as-is for now |
| BUY_AMOUNT budget | min(deployable/2, $250), then risk-sized (see ATR row) | code default (`RUNNER_MAX_BUY_AMOUNT`) |
| HARD_STOP_PCT | 5% base risk anchor (resting order, primary control) | code default (`RUNNER_HARD_STOP_PCT`) |
| ATR-based stop/sizing | stop = 2x ATR, bounded 2-10%; same formula as SML/SML2 — added 2026-08-04 (RUNNER didn't compute ATR before this) | `.env` (shared `ATR_STOP_MULT`/`ATR_MIN_STOP_PCT`/`ATR_MAX_STOP_PCT`) |
| TRAILING_STOP_PERCENT | 10% (fallback only, if hard stop submit fails — now falls back to the ATR stop_pct instead, not the looser 10%) | `RUNNER_TRAILING_STOP_PERCENT` |
| MAX_HOLD_MINUTES | 45 | `.env` (`RUNNER_MAX_HOLD_MINUTES`, raised from 25 on 2026-08-04 — every 7/31 trade timed out near 0% gain, 25m didn't give a 3-min velocity spike enough room to resolve) |
| FILL_TIMEOUT_SECONDS | 360s | `.env` (`RUNNER_FILL_TIMEOUT_SECONDS`, raised from 180 on 2026-08-04 — COOT/MTEX buys got cancelled unfilled at 180s on fast-moving illiquid microcaps) |
| VELOCITY_MIN_PCT / lookback | 3.0% over 3 min | code default |
| MIN_RVOL | 3.0x | code default (`RUNNER_MIN_RVOL`) |
| MAX_RVOL | skip if RVOL >10x (likely already exhausted) | `.env` (`RUNNER_MAX_RVOL`, added 2026-08-04) |
| MAX_ENTRY_MOVE_PCT | skip if already up >15% (sanity ceiling only — no daily-change floor, unlike SML/SML2) | code default |
| MAX_CANDIDATES | top 1000 by volume, screened per cycle for velocity+RVOL | `.env` (`RUNNER_MAX_CANDIDATES`, raised from 300 on 2026-08-03 — the 300 cap was excluding low-float RVOL breakout names, causing zero buy signals for a full session) |
| PRICE_STALENESS_SECONDS | 45s (WS price cache max age before REST snapshot fallback) | code default |
| MIN_PRICE / MAX_PRICE | $0.10 / $10.00 | code default |
| START_TIME_ET / STOP_BUY_TIME_ET / DUMP_TIME_ET | 09:45 / 11:45 / 12:00 | shared `.env` keys |
| BUY_COOLDOWN_SECONDS | 86400 (shared key) | .env |

RUNNER also has `tools/log_checkpoint_shadow.py` (added 2026-08-04, scheduled daily 5pm ET via
Windows Task Scheduler) tracking what its 30m/60m checkpoint exits would have done if held
longer — see `reports/checkpoint_shadow_log.csv` for accumulating evidence before any change to
`MIN_GAIN_AT_30M`/`MIN_GAIN_AT_60M` for RUNNER.

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

- **2026-08-04** — Archived MID and SUPER: moved `run_mid_screener.py`/`screener-mid.service` to
  `_archive/mid/` and `run_super_screener.py`/`screener-super.service` to `_archive/super/` (via
  `git mv`, history preserved). Neither has been actively tuned or reviewed since this project's
  focus narrowed to SML/SML2 ([memory: SML/SML2 Focus]), and MID/SUPER were passively inheriting
  shared-key changes meant for SML/SML2 (e.g. today's `MAX_RVOL` addition) with no deliberate
  review of the effect. This only archives the code in the repo — the live `screener-mid`/
  `screener-super` systemd services on the Pi need to be stopped/disabled there manually, since
  this session has no remote access to that host. Historical MID/SUPER trade data in `stockbot.db`
  is unaffected and still queryable via the existing multi-provider tools.

- **2026-08-04** — Added `tools/log_checkpoint_shadow.py`, scheduled daily at 5:00pm ET via a
  Windows Task Scheduler job (`StockTipsBot-CheckpointShadowLog`) on the dev machine. For every
  RUNNER trade closed by the 30m/60m checkpoint rule, pulls the full session's bars and logs what
  the gain would have been at the `RUNNER_MAX_HOLD_MINUTES` mark, plus session peak/trough/close,
  to `reports/checkpoint_shadow_log.csv`. Built after manually checking three 2026-08-04 RUNNER
  losers (SCKT, FTRK, ANNA) against their full-day price paths — mixed result (FTRK would've been
  +7.4% if held longer, ANNA would've been worse, SCKT didn't turn positive for 5+ hours) — not
  enough signal from one day to change the checkpoint thresholds, so this exists to accumulate
  evidence before touching them.

- **2026-08-04** — Added a shared `MAX_RVOL=10` ceiling (SML, SML2, RUNNER — RUNNER didn't support
  the concept before, added via `bot/runner_screener.py` + `RUNNER_MAX_RVOL`). Cross-tabbing win
  rate against `rvol_at_entry` across 103 closed SML/SML2/RUNNER trades showed a monotonic decline
  — 34% win rate at RVOL <0.5x down to 8% at RVOL >10x (avg loss -3.11%) — consistent with the
  fuller 343-trade history across all screener generations. Extreme RVOL reads as exhaustion
  (buying into an already-blown-out spike), not confirmation, contrary to the filters' original
  assumption. `MAX_RVOL` is a shared key with no per-screener override — MID and SUPER already had
  code support for it, so this also silently tightened their filters (not deliberately tuned for
  them, see [memory: SML/SML2 Focus] — MID/SUPER aren't being actively tested right now).

- **2026-08-04** — Replaced fixed-% stops and fixed-$ position sizing with ATR-based risk sizing
  in SML, SML2, and RUNNER (`_size_by_risk()`, same formula in all three files). Stop distance is
  now `ATR_STOP_MULT x ATR` (default 2.0x), bounded to [`ATR_MIN_STOP_PCT`, `ATR_MAX_STOP_PCT`]
  (default 2-10%), and share count is set so dollar risk (stop distance x shares) stays roughly
  constant at what the old fixed budget x `HARD_STOP_PCT` risked — a tight/low-ATR stock now gets
  a bigger position, a wide/volatile one gets a smaller one, instead of every trade risking a
  different amount purely because stop distance varied while dollar exposure didn't. Falls back to
  the old flat behavior when ATR is unavailable (e.g. too few bars right after open). RUNNER never
  computed ATR before this — added via `bot/market_data._atr()` on its 1-min bars. Also fixed
  SML/SML2's hard-stop-submit-fails fallback to use the same ATR `stop_pct` instead of the looser
  default `TRAIL_PCT` (matches the fix already applied to RUNNER on 2026-08-03/04) — the fallback
  exists because price can gap through a fixed stop price between fill and submission on thin,
  fast-moving microcaps, and the fallback was silently doubling the intended risk when it fired.
  MID and SUPER were **not** touched — they still use flat %/$ sizing.

- **2026-08-04** — Raised SML's `MAX_ENTRY_MOVE_PCT` to 15% via a new `SML_MAX_ENTRY_MOVE_PCT`
  override (was inheriting the shared 10%), matching SML2. Prompted by 2026-08-03 candidates that
  got rejected at 10% and then kept running (e.g. AUTL +14.6%, TE +6.9%, GMEX +6.2%, all skipped
  for "already up too much"). Caveat recorded same day: a broader win-rate-by-`change_pct_at_entry`
  analysis across historical trades showed the *base rate* for chasing already-extended moves is
  actually poor (49% win on down days declining to 18% at 30-60% up, with losses averaging -10%
  vs +7.25% for wins in that bucket) — this raise was based on one day's near-misses, which is
  anecdote, not base rate. Not reverted, but flagged as unsettled — watch results before loosening
  further.

- **2026-08-03** — Raised `RUNNER_MAX_CANDIDATES` from 300 to 1000. The 300 cap (added 2026-07-31
  to fix 55-100s scan cycles from screening the full ~2900-symbol band) was capping to the top 300
  symbols **by raw volume** before checking velocity/RVOL — on 2026-08-03 this produced `0 passing
  velocity+RVOL` on every single scan cycle for the entire 09:45-11:45 session, because RUNNER's
  edge is catching low-float names whose *relative* volume spikes 4-35x their own average without
  necessarily cracking the top 300 by absolute volume. Also raised `RUNNER_MAX_HOLD_MINUTES` from
  25 to 45 (every 2026-07-31 RUNNER trade timed out near 0% gain — 25m didn't give a 3-min velocity
  spike room to resolve) and `RUNNER_FILL_TIMEOUT_SECONDS` from 180 to 360 (COOT/MTEX buys got
  cancelled unfilled at 180s on fast-moving illiquid microcaps). Also fixed RUNNER's hard-stop
  fallback to use `HARD_STOP_PCT` (tight) instead of the looser `TRAIL_PCT` when the hard stop
  submit fails — same class of bug later found and fixed in SML/SML2 on 2026-08-04 (see above).

- **2026-08-01** — Added `EXCLUDE_SYMBOLS=MSTU,TZA,HTZ` — never buy these regardless of signal.
  MSTU/TZA are leveraged/decay ETPs (structurally bad for a buy-and-hold-for-minutes strategy),
  HTZ a chronic loser (0/3 win, -$22.90). Verified 2026-08-04: only SML and SML2's code actually
  reads this key — MID, SUPER, and RUNNER have no `EXCLUDE_SYMBOLS` check at all, so it does
  **not** apply to them despite being a shared `.env` variable name.

- **2026-07-25** — Lowered SML2's `RSI_ENTRY_MIN` from 60 to 55 via `.env` (was
  previously only a code default, not env-exposed). Reviewing 2026-07-24's SML2 log
  showed NVD hovering RSI 55.9–59.9 continuously for 90 minutes (10:03–11:33 ET),
  repeatedly missing the 60 floor by a point or two while also needing a simultaneous
  fresh MACD cross — the single largest cluster of near-miss rejections that day (46
  skips). BATL showed the same pattern earlier (RSI 54.5–57.9). `RSI_ENTRY_MIN`/`_MAX`
  are read only by `run_sml2_screener.py`, so no shared-key collision risk with
  SML/MID/SUPER — no per-screener-prefixed override needed, unlike `MAX_ENTRY_MOVE_PCT`.
  `RSI_ENTRY_MAX` stays at 70 (code default). This is an `.env`-only change (the code
  already read `RSI_ENTRY_MIN` generically) — needs the `.env` update + a
  `screener-sml2.service` restart on the Pi, no code deploy required.

- **2026-07-23** — Fixed the buy cooldown (`BUY_COOLDOWN_SECONDS`) to be scoped per
  screener instead of global. `ticker_alerts` (the table backing `is_ticker_on_cooldown`
  / `record_ticker_alert` in `bot/database.py`) had no `provider` column, so a buy by any
  one screener put that symbol on cooldown for *all* screeners for 24h. Discovered while
  reviewing 2026-07-23's no-trade day for SML2: SML bought BATL at 09:43 ET, which then
  showed up as `SKIP BATL — cooldown` in SML2's log from 09:51 on, even though SML2 never
  traded it — SML2 lost access to the day's best-qualifying candidate (RSI 63.6, RVOL 1.7x,
  fresh MACD cross) purely because a different bot got there first. Added a `provider`
  column to `ticker_alerts` (migrated automatically via `init_db()`, existing rows keep
  `provider=''` and simply age out — no cooldown carries over from before the fix) and
  threaded `PROVIDER` through both functions and all five screener entry points (SML, SML2,
  MID, SUPER, LIVE). Each screener's cooldown is now independent, matching the intent of
  running SML/SML2 as separate A/B variants ([memory: SML/SML2 Focus]). Needs a `git pull`
  and a service restart on the Pi to take effect there — this fix only lives in the repo so far.

- **2026-07-23** — Raised shared `MAX_ENTRY_MOVE_PCT` from 8% to 10% on the Pi's `.env`
  (applied there first, local `.env` updated to match). Since this key has no per-screener
  override except SML2, it affects SML, MID, and SUPER together — SML2 stays independent
  at 15% via `SML2_MAX_ENTRY_MOVE_PCT`.

- **2026-07-23** — Raised SML2's `MAX_ENTRY_MOVE_PCT` from 8% to 15%. On 2026-07-22 SML2
  (and SML) skipped every buy all day; several repeat-offender tickers (LICN, AIRJ, MWC,
  SNTG) were consistently 15-25% up and got filtered out at the 8% cap. Added a
  `SML2_MAX_ENTRY_MOVE_PCT` env override (same pattern as `SML2_ALPACA_API_KEY`) in
  `run_sml2_screener.py` so this only affects SML2 — SML/MID/SUPER stay at the shared 8%
  via `MAX_ENTRY_MOVE_PCT`.

- **2026-07-22** — Initial baseline captured from `.env` + code defaults. Fixed a stop-loss
  rounding bug in `bot/trader.py` (`submit_stop_loss` now rounds to 2 decimals at/above $1,
  4 decimals below $1) — not a screening-setting change, but noted here since it affects
  hard-stop order placement on both SML and SML2.
