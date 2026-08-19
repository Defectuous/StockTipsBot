# Gap Stock Trading Strategies

Reference doc for the momentum/gap-stock day trading strategies used and evaluated in this project. Written to be self-contained for review outside this codebase. Source: `screener/strategy_selector.py`, `screener/signal_generator.py`, `screener/technical_levels.py`, and backtests in `scripts/real_backtest_45_pillars.py` / `scripts/three_more_strategies_backtest.py`.

Universe: small-cap stocks ($1–$20), gapping up pre-market on volume, $1–20 price band, float under ~20M shares (Ross Cameron / Warrior Trading style momentum trading).

---

## 1. The four core entry setups

### Gap and Go
Stock gaps up 10%+ pre-market on a news catalyst and continues higher after the open.
- **Entry:** just above the pre-market high (PM high + 0.1–0.2%), stop-limit order
- **Stop:** just below the pre-market low (PM low − 0.2%)
- **Target 1 / 2:** entry + 20% / entry + 50%
- **Works best:** gap ≥ 20%, float under 5M, RVOL ≥ 5x, fresh clear catalyst, tight pre-market consolidation near the high
- **Kills the setup:** gap fills immediately at open, volume dries up without testing PM high, multiple overhead resistance levels within 5%

### VWAP Bounce
Stock that already ran pulls back to VWAP or the 9 EMA (whichever price is actually respecting), finds support, bounces.
- **Entry:** just above the support level (level + 0.1%) on the bounce candle close
- **Stop:** below the actual shakeout low (the real dip that got reclaimed) — not a fixed % below the level
- **Target 1 / 2:** prior HOD / HOD + 3%
- **Requires a genuine dip-then-reclaim**, not just current proximity to the level — a bar closing *below* VWAP is not a bounce yet
- **Works best:** stock already up 10%+ on the day, orderly pullback (3–5 candles), volume lighter on the pullback than the initial move
- **Kills the setup:** stock approaching the level from below (resistance, not support), volume spike on the sell-off into the level

### HOD (High of Day) Breakout
Stock pauses just below its high of day, consolidates, breaks out on volume — a continuation setup.
- **Entry:** just above HOD (HOD + 0.1–0.2%), stop-limit
- **Stop:** below the nearest consolidation zone, else 2% below HOD
- **Target 1 / 2:** entry + 10% / entry + 20%
- **Works best:** stock already made a strong directional move, tight consolidation (1–2% range, 3–10 min), breakout volume 2x+ consolidation average, best between 9:30–11:00 AM
- **Kills the setup:** multiple failed breakout attempts at the same level, light-volume breakout, past 11:30 AM, price below the 9 EMA going into the attempt

### ABCD Pattern
Harmonic pullback: strong move up (A→B, the pole), pulls back to support (B→C), resumes to a D target equal to or extending the AB move.
- **Entry:** at/just above the D target (D + 0.2%)
- **Stop:** 1% below the D target
- **Target 1 / 2:** C level / B level (or B + AB distance)
- **Works best:** clean pole ≥10%, orderly pullback, BC retracement near 61.8% of AB (golden ratio)
- **Kills the setup:** BC retracement > 90% of AB, heavy volume on the BC leg, D target sitting in a known support zone

### Priority order (when multiple setups qualify)
1. gap_and_go — highest win rate at open, fresh news, tight float
2. hod_breakout — confirmed uptrend + volume
3. vwap_bounce — requires the stock to have already proven itself
4. abcd_pattern — only with a precise D target and clean BC ratio

**Caveat:** this priority order is only enforced when an LLM (Claude) makes the pick from candidate setups. The deterministic rule-based fallback (used whenever the LLM call fails or is disabled) just takes the highest rule score with no priority tiebreak — and in backtesting this let a lower-priority setup (vwap_bounce) dominate trade volume (77% of trades) despite being the weakest performer. If porting this system, either keep the LLM arbitration step live, or add an explicit priority tiebreak to the fallback path.

---

## 2. Pre-entry gates

### Catalyst hard-gate
A stock trading purely on a pump with **no identifiable news catalyst** (`catalyst_type == "none"`) is disqualified regardless of how strong the technical setup looks — forced to no-trade rather than sized down. Backtested: this gate blocked 73 of 149 high-scoring candidates (49%) in one 18-day sample — the single largest determinant of whether a candidate trades at all, bigger than any technical factor.
- **Caveat:** some genuine no-catalyst momentum trades are real and profitable (documented in outside trader narration research) — the more faithful version of real practitioner behavior is probably reduced size on no-catalyst setups, not a hard zero. Kept as a hard gate here because two real forward trades on this exact profile both lost.

### Reclaim gate (for pullback/bounce entries)
Requires an actual dip-below-then-close-back-above the support level within a lookback window, not just current proximity to it. Also derives the stop from the real shakeout low (lowest price during the dip) rather than a fixed percentage below the level — three independent instances of stops getting noise-triggered by a fixed-% stop that had no relationship to where price actually bottomed motivated this change.

---

## 3. Exit mechanics

**Trailing stop, not a fixed take-profit.** On entry fill, a resting take-profit order is effectively replaced almost immediately by a trailing stop sized at the original entry-to-stop distance, trailing the highest price since entry. In practice this means take-profit targets rarely execute (in one sample, 144 of 172 real exits were trailing-stop fills, only 2 were target fills) — the position rides until it gives back (entry_price − stop_price) dollars/share off its peak, or the session force-closes.

**Backtest note on trigger granularity:** a trailing stop shouldn't trigger off an intrabar wick in a 1-min-bar simulation — validated against real trade data, an intrabar-low trigger stopped a position out a full 12 minutes too early on a same-minute wick; switching to a bar-**close**-crossing-the-trailing-level trigger matched the real exit timing almost exactly. If backtesting this kind of system on bar data, close-based triggers are the more faithful approximation.

**Session force-close:** flatten all open positions at a fixed cutoff (this project uses 12:00 PM ET). Outside research suggests trading's actual edge degrades earlier — see time-of-day filter below.

---

## 4. Candidate filters (backtest-validated, not implemented in source strategy)

These were derived by replaying the four setups above against real historical minute bars and testing filters against the resulting trades. None are hard rules above — they're empirically-supported additions worth testing forward before adopting.

| Filter | Rule | Backtest result | Confidence |
|---|---|---|---|
| **Price floor** | Entry price ≥ $2 | 42 trades, 38.1% win rate, +1.09% avg P&L vs. unfiltered 33.9%/+0.13% | Strong |
| **Time cutoff** | Entry before 10:00 AM (local market time) | Before: 35.3% wr, +0.40% avg. After: 27.3% wr, −1.16% avg | Strong |
| **Price + time combined** | Both of the above | 35 trades, 40.0% win rate, +1.49% avg P&L (~6.5x baseline total) | Strong |
| **Fast MACD confirmation** | MACD(5,13,9) line > signal line on the entry bar | Bullish: 44 trades, 38.6% wr, +0.97% avg. Bearish: 18 trades, 22.2% wr, **−1.93% avg** | Strong |
| **Float rotation** | (cumulative volume ÷ float) at entry ≥ 2x | ≥2x: 53.8% wr, +3.26% avg (best bucket). <0.5x: 28.9% wr, −0.81% avg (worst, most common) | Strong, but counterintuitive — see note below |
| **ATR-sized stop** | Actual stop distance between 1x and 3x ATR(14)% | 1-3x ATR: 37.0% wr, +0.19% avg vs. <1x ATR (too tight): −0.24% avg | Weak |

**Notes:**
- Standard-period MACD (12,26,9), the daily-chart default, showed **no edge** in this backtest — bearish readings actually had a slightly higher win rate than bullish. Only the faster (5,13,9) variant (recommended in short-term/small-cap day-trading guides) carried a real signal. Don't assume MACD works without checking which period you're using.
- Float rotation result is the opposite of the common "high rotation = exhaustion" framing. Likely explanation: rotation was measured only at the moment a setup was *already* confirmed (post-reclaim/breakout) — by then, high rotation reflects proven sustained demand rather than a spent move. This may not generalize to measuring rotation as a standalone pre-entry screen.
- Best combo found: fast-MACD-bullish + rotation ≥0.5x → 19 trades, 47.4% win rate, +2.68% avg P&L/trade (best average of anything tested). Filters are not strictly additive — stacking the price/time filter with the MACD filter performed slightly *worse* than the price/time filter alone, because some good trades cleared one gate but not the other.
- Also directly explains why "5/5 pillars passing" underperformed "4/5" in this system: the 5th pillar (float) is easiest to max out on the cheapest micro-caps, and 5/5 candidates traded under $2 more than twice as often as 4/5 candidates (56% vs 28%) — the perfect score was disproportionately finding the riskiest setups, not better ones.

## 5. Methodology caveats (read before reusing any of the above)

- Backtest sample: 149 candidates across 18 trading days, 62 of which produced a simulated trade. Per-bucket sample sizes above range from 8 to 44 — directional leads, not statistically settled edges.
- "Total P&L" figures cited in source research are unweighted sums of independent per-trade % returns, not a compounded/risk-weighted equity curve.
- The backtest used a deterministic rule-based strategy pick (no live LLM call) for cost and reproducibility — see the priority-order caveat in section 1.
- Entry fills at the exact computed entry price with no slippage modeled; exits use the close-based trailing-stop model described in section 3.
