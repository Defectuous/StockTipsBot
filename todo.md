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
