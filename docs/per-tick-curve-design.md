# Per-tick curve decision (design, 2026-11-12)

Status: design only. No code, no config, nothing live. Written from the user's note that (A) the Kalshi-curve shadow barely bets, where the old ladder lanes bet most rounds, and (B) the curve has a reading on every tick, so a decision that looks at a fixed top share of moments wastes it.

## What is wrong now

`commodity_multiscale_kalshi` (paper twin of the live lane, `spot_source = "kalshi"`) buys only when all of these hold: the curve model's reading is in its top 15% (`model_top = 0.15`), the price is 0.70 to 0.90, the entry is between minutes 4 and 15, the curve chance is within 0.05 of the ask (`buy_curve_edge_min = -0.05`), `min_edge` 0.015, spread at most 0.04, one bet per event. Result: 28 bets on 20 markets on 11-11 after 18:05 ET and 10 on 6 markets on 11-12, a few percent of the 8 series x 96 windows a day. The exit, by contrast, already reads the curve every tick (`watcher._curve_hit`: sell when the bid after the sell fee is at least P plus a margin, `curvevalue.read`). The entry does not use the same number the same way.

## The rule

One number, both directions, every tick: **P**, the curve's chance that the side settles at $1 (`curvevalue.read`, pessimistic of the 900 s and 180 s rates, in the market's own variance).

- gap(t) = P(t) - (ask(t) + entry fee), per watched side, on every quote tick from the socket (not only when a quote moves into the price band).
- BUY when the gap has been at or above the market's own cutoff for `persist` seconds. The cutoff is the 80th percentile of that side's own gap readings over its last 6 hours, floored at 0 (the fee is already in the gap). Recomputed hourly (rule 2). Stated before any result; not tuned.
- HOLD, and the existing curve exit sells when the bid after the fee is at least P plus its margin. Unchanged, so the exit and entry share P and differ only in direction.
- ANOMALY: a gap beyond the side's own 1st/99th percentile, or the curve's rate above 3x its own median, is logged as `anomaly`, never counted as a signal, and the rule sits out (rule 3).
- Every tick that would have bought logs "would buy at gap g (cutoff c)" with the curve's level, slope and curvature readings, and is scored against what followed (rule 5).

No model top-share, no hand-set price band for the decision itself, no coin or commodity name anywhere (rule 1). Sizing stays the lane's `fixed_stake` in phase 1; scaling the stake with the gap in its own units is phase 3.

## Phases, one change at a time

1. **Clean twin, only the decision differs.** Same series, price band 0.70-0.90, entry window 4-15 minutes, fill mode, stake, exit and spot as `commodity_multiscale_kalshi`; `model_buy` off; the tick rule above in its place; one bet per market side. New lane `commodity_multiscale_tick`, PAPER. Attribution is then clean against the existing twin and the live lane.
2. **Wider band** as its own clean twin of phase 1: the band follows the gap (any price where the gap clears), so cheap-side troughs and favorites are in. Only after phase 1 scores.
3. **Re-entry and stake by gap.** Allow a re-buy after a sale when the gap clears again (after that market's own recovery time), and scale the stake with the gap in its own units. This is the round trip the user trades by hand.

## Evidence before it ships to shadow (rules 4, 5, 7)

One question on the same bets, one stated rule (the 80th percentile, 5 s persistence, both fixed before looking): over the commodity windows since Monday 2026-11-09, how many windows have a tick that clears the cutoff, and on those bets (held to settlement, and with the lane's curve exit, fee on every leg) what is the net per dollar against what actually happened on the bets the live lane and the existing twin made? Report n, net dollars, bets beaten and lost, and the result by the curve shape readings (level, slope and curvature at 5 and 13 minutes), not by time or by asset. A shape that is negative is not claimed. Script `scripts/tick_gap_study.py`, result in `studies/results/` with the date.

## Where it touches the code

- `entrywatch.py`: today a hit needs a quote entering the lane's band and window; the tick rule needs every quote tick on watched markets for lanes that set it (`tick_buy = true`).
- `runner.review_lane` (near the `buy_curve_edge_min` check, `runner.py:714`): the gap test replaces `curvemodel.admits` for lanes with `tick_buy`.
- A small per-side gap history (in memory, hourly recompute) beside the curve reading; `curvevalue.read` is reused as is.
- New lane fields: `tick_buy`, `tick_buy_quantile = 0.8`, `tick_buy_hours = 6`, `tick_buy_persist = 5`. An old image refuses unknown lane fields, so the push needs `--build`, timed at xx:44:40 or xx:14:40 after a warm build.

## Decided

- Anomalies (2026-11-12, user: "option 1 is fine for now"): phase 1 sits them out and logs each one with its curve readings and what it would have paid. A later phase may bet them under their own rule once the log shows they pay. The phase 1 comparison stays clean because only the decision rule differs from the existing twin.

## Open choices for the user

- Start with phase 1 on the same band, or go straight to the wider band?
- 80th percentile as the starting cutoff is a stated guess; a lower one bets more often, a higher one less. It is not swept.
- Bar for going live: the scorecard's old "both halves" test is replaced by positive in each curve shape claimed (rule 4), over at least 30 settled bets and a week of live bets.
