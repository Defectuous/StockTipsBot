# TODO

## From 2026-08-07 SML/SML2 daily review — reviewed 2026-08-09

- [ ] **OKLL-style winners may be getting capped by MAX_HOLD_MINUTES (90m).**
      Confirmed with Alpaca 5-min bars for 2026-08-07: SML exited OKLL at
      11:53 ET ($3.67, +11.4%), SML2 at 11:32 ET ($3.64, +10.1%), both on the
      90m cap. Price kept climbing after both exits, peaking ~$3.87 around
      15:20 ET (+5.5% past SML's exit) before giving most of it back into the
      close (~$3.66-3.75). So the cutoff did cost real upside on this
      instance, though a chunk of the extra move round-tripped by end of day.
      Still open: design a trailing-stop-tightening mechanism for positions
      past their gain checkpoints instead of (or alongside) the flat 90m
      cutoff. Worth checking a few more big-mover instances before building,
      since this one sample alone isn't a slam dunk.

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
