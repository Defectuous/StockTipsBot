# TODO

## From 2026-08-17 daily review (no trades today)

- [x] **RVOL bug — fully root-caused and fixed 2026-08-17 (not a feed-lag
      issue after all).** `bars15` was fetched with
      `start = now - timedelta(days=3)`, pinned to *today's exact clock
      time*, not midnight. That has enough slack on a normal weekday, but
      **2026-08-17 is a Monday**: `now - 3 days` lands on Friday at the
      identical clock time, so Friday's early-session bars (market open
      through that time) are structurally never inside the fetch window.
      `avg_prior` comes back `0` → `_rvol_time_adjusted` returns `None` →
      logged as a fake `0.0x`, all day, every cycle. **Recurs every Monday
      and after any holiday**, not just 08-17 — this wasn't a one-off.
        - **Confirmed with live replay, not just theory:** re-ran the exact
          calc for real 08-17 skip events with the fixed window. NBIZ went
          from `None` to real values (1.76x at 10:07 ET, 1.24x at 15:43
          ET); KEEL similarly (1.06x, 1.17x).
        - **NBIZ genuinely would have been bought around 10:07 ET today.**
          All 28 of its SML skips were RVOL-only — it had already cleared
          every other filter (exclude list, cooldown, daily-gain cap, ATR,
          VWAP, change%, MACD-freshness) each time, with RVOL=1.76x > the
          1.5x min as the very last gate, falsely rejected. KEEL's real
          RVOL was genuinely under 1.5x both times checked — that one
          wasn't a false rejection.
        - **Fix applied** in both `run_sml_screener.py` and
          `run_sml2_screener.py`: lookback now anchors to midnight 7
          calendar days back instead of "now minus 3 days," which survives
          any single weekend or one-day holiday. `python -m py_compile`
          clean on both.
        - Kept the DEBUG branch-reason logging and `None`-vs-`0.0x` log-line
          fix from the first pass — still useful as a tripwire if this class
          of bug resurfaces (e.g. around a multi-day holiday the 7-day
          window doesn't cover).

- [x] **Manually reconcile the stuck EOSE position (#120) in `stockbot.db`.**
      Fixed local `pi_data/stockbot.db` copy directly (2026-08-17): marked
      closed, sell_price=4.347692, sell_time=2026-08-12T13:32:43Z,
      pnl=+$6.84 (matches the values already present in the stale root
      `stockbot.db` dev copy, position id 447, which had this closed
      correctly). **Still needs to be run on the live Pi's `stockbot.db`
      to actually stop the retry loop** — no SSH/network access to the Pi
      from this machine, so couldn't apply it there directly:
      `sqlite3 stockbot.db "UPDATE positions SET status='closed', sell_price=4.347692, sell_time='2026-08-12T13:32:43.066339+00:00', pnl=6.839976 WHERE id=120 AND symbol='EOSE' AND status='open';"`
      Bigger fix (retry-without-dedup + no reconciliation pass against
      Alpaca, see [[project_eose_stuck_position_bug]]) still open, can wait.

- [ ] **Watch CONL tomorrow.** Rejected 5x in SML2 on MACD-crossover
      freshness (`1-2 bars < 3 min`) early in the 08-17 session — a real
      setup building, just hadn't matured past the freshness gate yet. Also
      hit by the RVOL 0.0x bug above later in the day, so its true RVOL is
      unknown. If it shows up again, worth a manual look once the RVOL fix
      lands.

## From 2026-08-14 daily review

- [x] **Extend SML2's trading day past 11:45 ET — engineering done, exact
      cutoff value is a decision for you, not picked automatically.**
      Added `SML2_STOP_BUY_TIME_ET` override in `run_sml2_screener.py`
      (2026-08-17), mirroring SML's existing `SML_STOP_BUY_TIME_ET` pattern
      exactly (`"off"` disables it, shared `STOP_BUY_TIME_ET` untouched so
      MID/SUPER aren't affected). Not yet set in `.env` — no cutoff change
      has gone live.
        - **Evidence pulled (full history, not just a sample):** every SML
          buy after 11:45 ET across the whole DB — only 7 trades total
          since tracking began: OPEN (7/30, $0), BBAI (8/4, -$1.04), CONL
          (8/7, -$1.52), DXF (8/11, -$3.04), EOSE (8/11, +$6.84), OPEN
          (8/13, $0), HUMA (8/14, +$12.52). **Net +$13.76, 2/7 winners.**
          BBAI and DXF were both near-dump-time entries with no room to
          develop — exactly the pattern [[project_hard_stop_bug_fix]]'s
          sibling fix (`SML_MIN_MINUTES_TO_DUMP`, commit `746a756`) already
          guards against, and **SML2 already has the equivalent
          `SML2_MIN_MINUTES_TO_DUMP` support** (confirmed in code), so that
          specific failure mode should be covered whenever the cutoff is
          extended. Net-of-those-two: +$17.84 across the remaining 5.
        - **Recommendation, not applied:** data leans positive but n=7 is
          thin. Given SML runs with its cutoff fully off
          (`SML_STOP_BUY_TIME_ET=off`) and the two bots are meant to be an
          A/B pair on the same underlying strategy (see
          [[project_focus_sml]]), the simplest matching move would be
          `SML2_STOP_BUY_TIME_ET=off` rather than picking a specific
          halfway time like 13:00 — but that's your call on how much
          you're comfortable extending, and DUMP_TIME_ET should stay in
          place either way as the real backstop. Let me know the value and
          I'll set it.

- [x] **ZCMD loss 2026-08-14 — all three questions resolved 2026-08-17.**
        - **Post-exit price action, checked:** pulled 5-min bars for the
          rest of 08-14. Price never recovered back toward the $1.11 entry
          — peaked around $1.08 mid-afternoon (13:00-13:25 ET) and drifted
          $1.00-1.07 the rest of the day. **VWAP-reclaim earned its keep
          here** — not a false positive like the OKLL case, no further
          exit-side edge to find on this trade.
        - **RVOL=5.6x cross-check, done:** bucketed win-rate/avg-pnl by
          RVOL across all 117 closed trades with data. The 5-7x bucket
          (n=8) came back 12.5% win / -$3.49 avg — close to the >10x
          bucket's 9.1%/-$4.50, notably worse than the 1-3x buckets around
          it. **Suggestive that the exhaustion pattern extends down to 5x,
          not just >10x** — but n=8 is too thin to act on alone. Flagging,
          not changing `MAX_RVOL` yet; worth re-checking once more 5-7x
          trades accumulate.
        - **ZCMD track record, resolved:** 0/4 wins across both 08-07 and
          08-14, both SML and SML2, total -$18.69. Added to
          `EXCLUDE_SYMBOLS` in `.env` (2026-08-17) alongside MSTU/TZA/HTZ —
          same bar, same treatment.

## From 2026-08-07 SML/SML2 daily review — reviewed 2026-08-09

- [x] **OKLL-style winners capped by MAX_HOLD_MINUTES (90m) — checked 4 more
      instances (2026-08-17), confirms the pattern but also the risk of
      just raising the cap.** Full history has only 5 trades ever
      time-exited near 90min with >5% gain: OKLL x2 (already documented),
      BATL (07-23, +6.5%), SPHL (07-07, +5.3%), INMB (06-25, +5.1%). Pulled
      15-min bars for all three new ones past their exit:
        - **BATL**: ran to +12% intraday (vs +6.5% exit) by ~14:00 ET, gave
          most back to ~+8% by close.
        - **SPHL**: kept climbing for *hours* — still up ~+15% by 15:00 ET
          (vs +5.3% exit) — then **violently round-tripped in the final
          15-min bar**, closing near session lows. Would've turned a good
          hold into a much worse exit if held to the literal close.
        - **INMB**: chopped flat/down for ~2.5hrs, then a genuine second
          leg to +13% around 14:15-14:30 ET, faded back to ~+7% by close.
        - **Conclusion: real upside is being left on the table (4/5
          instances kept running well past the 90m cap), but "just raise
          MAX_HOLD_MINUTES" is not a safe fix on its own** — SPHL shows
          exactly the failure mode a flat longer cap would walk into
          (holding into a violent close-time reversal). This supports the
          original idea better than a flat extension: a
          trailing-stop-tightening / partial-profit-lock mechanism for
          positions still green well past 90min, so extra upside gets
          captured incrementally instead of all-or-nothing. **Still not
          built — this is a new exit-strategy feature (design + backtest
          against these 5 + future instances), sizable enough to warrant
          its own planning session rather than bolting on quickly.**

- [x] **SML "insufficient deployable cash" on 2026-08-07** (need
      $408.52/have $326.81, need $163.36/have $151.56, 19+ skip events).
      Root cause: ATR-based risk sizing (added 2026-08-04) could demand up to
      2.5x a slot's flat budget on a tight-stop/low-ATR pick — enough to
      exceed the entire 2-slot deployable pool — and the code skipped the
      trade outright instead of taking a smaller affordable position.
      **Already fixed** in commit `54f1e90` (2026-08-06, pushed to
      `origin/master`): sizing now caps to `available` cash and takes the
      largest position that still respects the ATR stop, across
      SML/SML2/RUNNER. The 2026-08-07 log still shows the old skip message
      format, meaning the Pi was running stale code that day — **action
      item: confirm the Pi has pulled `54f1e90`+ and restarted the
      screener-sml/screener-sml2/screener-runner services.**

- [x] **SML2 scan cadence much sparser than SML** (305 scan cycles vs 2,022
      for SML on the same day). Confirmed intentional, not a bug: SML got a
      dedicated `SML_STOP_BUY_TIME_ET=off` override in commit `1bef777`
      (2026-07-29) so it scans/trades all day; SML2 still uses the shared
      default `STOP_BUY_TIME_ET=11:45`, so its full scans stop there. Commit
      message confirms this was deliberate.

- [ ] (minor) SML logged 13 DNS resolution errors on Alpaca's most-actives/
      movers endpoints, all outside the 9:52-11:53 ET trading window
      (off-hours universe-refresh calls). Didn't affect any trade. Low
      priority — just noting in case it recurs or starts overlapping market
      hours.

## Deferred (not from today, carried from memory)

- [x] Modify `daily_report.py` to work with this project — rewritten 2026-08-08 to
      read sml.log/sml2.log (SKIP/BUY/FILLED/SOLD) + stockbot.db positions instead
      of the old pillar/catalyst JSON schema that never matched this codebase.

## Logging — 2026-08-09

- [x] **Cut logs at midnight instead of growing forever / getting overwritten
      daily.** Added `bot/logging_utils.py:DailyFileHandler` (subclasses
      `TimedRotatingFileHandler`) — active log file is named
      `<prefix>.mmddyyyy` (e.g. `sml.log.08092026`) from the first line of
      the day; rotation is checked on each log call, so the new file opens
      lazily on the next logging event after local midnight, not via a
      background timer. Wired into SML, SML2, RUNNER, and LIVE (all
      non-archived screeners) via `configure_logging()`. Updated
      `tools/explain_trading_day.py` (new `resolve_log_path()`),
      `daily_report.py`, `tools/pull_day_candidates.py`, and
      `tools/log_checkpoint_shadow.py` to resolve the dated file for a given
      report date, falling back to the old flat name for pre-rotation logs.
      `.gitignore` patterns updated to `*.log*` so rotated files stay
      ignored. Tested: forced a simulated midnight rollover, confirmed
      yesterday's file is left intact and a new dated file opens; ran
      `explain_trading_day.py` and `daily_report.py` against real
      `pi_data/` logs to confirm the flat-file fallback still works.
      **Action item: the external process that syncs Pi logs into
      `pi_data/` needs to pick up the new dated filenames (e.g. a glob
      instead of one fixed name) for historical-day lookups to work — it
      isn't part of this repo, so wasn't changed here.**

## ZCMD loss review / early-exit research — 2026-08-09

- [x] **Why did SML/SML2 both lose on ZCMD (2026-08-07)?** Both bought a
      failed second bounce (price had already made a lower high vs. its
      $1.19 pre-entry peak) and both were correctly cut by the existing
      30-min checkpoint exit (-2.2%/-2.6%) rather than riding to the wider
      hard stop — ZCMD kept sliding to ~$1.09-1.10 for the rest of the day,
      so the checkpoint saved roughly half the loss vs. the counterfactual.
      Conclusion: the exit side worked fine here; the entry is the weak link.

- [x] **Checked "bought on a lower-high second bounce" as a general pattern**
      across the 10 largest SML/SML2 losers vs. the 9 largest winners
      (distance of entry price below the pre-entry intraday high). **Result:
      not predictive** — winners buy just as far below the day's high as
      losers do (e.g. HIVE -3.9%, INMB -4.9% off-high, both winners). This
      is just the normal shape of an RSI-pullback entry, not a red flag.
      **Don't build a filter on this** — no edge found.

- [x] **Built `tools/vwap_reclaim_shadow.py`** — shadow-tests a candidate
      early-exit rule ("exit if price closes back below VWAP") against all
      103 closed SML/SML2 trades in `stockbot.db`, comparing hypothetical
      vs. actual P&L. Not yet committed/pushed (working tree only).
        - Naive version (any VWAP dip, 2-bar confirm): fires on 70/103
          trades, way too trigger-happy — would have gutted real winners
          incl. **OKLL (+9.94% -> -3.50%)**, BYAH, HIVE, BATL, OPEN.
        - Gating on "must also be underwater vs. entry price" alone still
          cut OKLL short (it genuinely dipped red+below-VWAP before its
          big run).
        - **Best tuned version found: require 8 consecutive 1-min closes
          both below VWAP AND below entry price** before firing. Result:
          fires on 42/103, 26 improved (avg +2.2%), only 13 hurt (avg
          -1.0%, all near-flat trades, nothing like OKLL), just 1 real
          winner nicked (+0.37% -> -1.5%). Net delta +43.9%, though ~26pts
          of that is one outlier (TNMG, a stuck multi-day position from an
          orphaned-fill bug, not a normal same-day fade) — net is still
          positive (~+18% across the other 41 affected trades) excluding it.

  - [x] Committed `tools/vwap_reclaim_shadow.py` to git (`47e5fd3`).

- [x] **Swept dwell and a new %-based VWAP-buffer variant** (added
      `--vwap-buffer-pct` to the tool) to see if either beats the dwell=8
      baseline. All runs use `--require-negative-gain`, cached 1-min bars
      in `reports/_vwap_bar_cache.pkl`.
        - **dwell=9 (no buffer) is strictly better than dwell=8**: fired
          41/103 (vs 42), only 10 hurt (vs 13), net delta **+51.58%** (vs
          +43.86%), and ex-TNMG-outlier net **+24.51%** (vs +17.93%) —
          same single winner nicked either way. **New best-tuned
          candidate: dwell=9.**
        - dwell=5/6/7 (below 8) all reintroduce winner-cutting (4-5
          winners cut vs. 1 at dwell=8/9) and worse hurt-trade averages —
          confirms lower dwell is a strict downgrade, don't go below 8.
        - dwell=10/12 (above 9) trend back down (+44.69%, +40.54%) — 9 is
          a local peak, not just "higher is better."
        - **%-based VWAP buffer variant does not help.** Tried
          0.2%/0.3%/0.5% buffers at both dwell=8 and dwell=9 — all reduce
          net delta vs. the unbuffered version at the same dwell (e.g.
          dwell=9+0.3% buffer: net +40.25%, ex-TNMG +13.18%, both worse
          than dwell=9 unbuffered). The buffer trims some hurt trades but
          trims improved trades by more. **Don't add a VWAP buffer.**

- [x] **Wired the tuned rule into `monitor_positions()` for both SML and
      SML2** (2026-08-11) — new `vwap_reclaim_exit_price()` helper in
      `bot/market_data.py`, shared by both screeners. Fetches market-open-
      anchored 1-min bars per monitor cycle (only when
      `VWAP_RECLAIM_DWELL_MIN > 0`, so it costs nothing when disabled), and
      checks each open position for `VWAP_RECLAIM_DWELL_MIN` (default 9)
      consecutive 1-min closes below both the running VWAP and entry price,
      ignoring the first `VWAP_RECLAIM_WARMUP_MIN` (default 3) minutes
      post-entry. Sits as a new step ahead of the 30/60-min checkpoints in
      both `monitor_positions()` functions (step 3 in SML, step 2 in SML2 —
      SML2 already runs hard-stop first). require-negative-gain and no
      buffer are hardcoded (not exposed as env vars) since the sweep found
      no case where either helped. Unit-tested the helper against synthetic
      bar sequences (sustained dip fires, still-green never fires, dip
      shorter than dwell doesn't fire, alternating dip/recover resets the
      streak) — all passed. Both screener modules import clean.

  **Caveat carried into live use:** n=103 trades but SML/SML2 mostly trade
  the *same* symbol the same day, so this isn't 103 independent samples —
  and dwell=9 was picked by looking at these same trades (in-sample).
  **Action item: watch the first 1-2 weeks of live `VWAP RECLAIM EXIT` log
  lines closely** (which symbols, how often, whether any look like a winner
  cut short) before trusting the tuned dwell fully — can be disabled per-
  screener any time via `VWAP_RECLAIM_DWELL_MIN=0`.
  - [ ] `reports/vwap_reclaim_shadow.csv` has the full per-trade results
        (and `reports/vwap_reclaim_dwell{5,6,7,8,9,10,12}.csv` /
        `vwap_reclaim_d{8,9}_b{0.2,0.3,0.5}.csv` for the sweep) if you
        want to eyeball the "hurt"/"improved" lists yourself first.

## Code review of the VWAP-reclaim live wire-in (`cb24063`) — 2026-08-12

- [x] **`vwap_reclaim_exit_price()` can complete its dwell=9 streak on the
      current, still-forming 1-min bar, not a settled close.** Fixed
      2026-08-17: added an optional `now` param — when passed, drops the
      trailing bar if its minute hasn't fully elapsed yet
      (`bars1[-1].timestamp + timedelta(minutes=1) > now`), before building
      the VWAP series. Both call sites (`run_sml_screener.py`,
      `run_sml2_screener.py`) now pass `now=now` (already in scope in
      `monitor_positions()`). Left the param optional/defaulted to `None`
      so `tools/vwap_reclaim_shadow.py`'s historical-bar shadow-testing
      stays unaffected. Verified with synthetic bars: settled streak still
      fires, in-progress last bar gets dropped (streak short by one, no
      fire), no-`now` call still fires (back-compat) — see conversation for
      the ad-hoc script. `python -m py_compile` clean on all three files.

- [x] **Unguarded naive-vs-aware datetime comparison could abort a whole
      monitor cycle.** Fixed 2026-08-17: both screeners now normalize
      `buy_dt` right after `datetime.fromisoformat(pos["buy_time"])` —
      `if buy_dt.tzinfo is None: buy_dt = buy_dt.replace(tzinfo=timezone.utc)`
      — same pattern `tools/backfill_entry_stats.py` already used. Confirmed
      the later `held_min = (now - buy_dt)` subtraction reuses this same
      normalized variable in both files, so one fix covers both exposures.

- [ ] (minor, efficiency) **VWAP-reclaim doubles uncached REST bar-fetches
      per monitor cycle.** `run_sml_screener.py:301-315` /
      `run_sml2_screener.py:602-614` — `bars1` (session-anchored 1-min, for
      VWAP-reclaim) is fetched separately from the existing `bars5` (for
      RSI) every 30-60s cycle, and re-pulls the *entire* growing session
      from 09:30 every time instead of caching and appending deltas —
      payload grows to ~390 bars/symbol/cycle by day's end. A failed fetch
      falls back to `bars1={}` with only a warning log, silently disabling
      the exit for that cycle with no alert distinct from "condition not
      met." Worth weighing given the added load per open position per
      cycle, but not a correctness bug.

- [x] (minor, cleanup) Dead clause in the warmup filter —
      `bot/market_data.py:217`: `b.timestamp < buy_time` is always subsumed
      by `b.timestamp < warmup_cutoff` for the non-negative
      `VWAP_RECLAIM_WARMUP_MIN` this is actually configured with. Harmless
      today; only matters if `warmup_min` were ever misconfigured negative.
      **Ruled out as a real issue:** checked whether the shadow-test sweep
      that picked dwell=9 used a different `--require-negative-gain`
      setting than what's hardcoded live — both always require underwater
      vs. entry, so no mismatch there.
