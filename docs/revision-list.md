# Revision list

Things found while simplifying the bot that still need revising. Started 2026-11-18 (user: "keep a list of these items so we can continue revising and improving"). Add to it as items are found; move an item to Done with its commit when it lands.

## Open

### Model ideas written as lane options
1. **Commodity buy model.** The commodity lanes gate buys with `min_cushion_z`, `max_vol_rel`, `buy_curve_edge_min`, `entry_elapsed` (user: "these are model concepts, they shouldn't be lane options"; "they are also antiquated, lets revise them in a new model for commodities"). Proposed: a buy module the lanes point at: `price`, then `risk_score`, then a new `perp_worth` step (Kalshi's perp chance against the ask). On paper beside the live lanes first.
2. **Volatility as an anomaly state.** `max_vol_rel = 1.6` is a fixed number per lane. A market far outside its own normal is the anomaly state of rule 3: handle it once in the model.
3. **Settings that gate nothing.** On the commodity lanes: `entry_trend_seconds` (no bar set), `entry_spike_view`, `entry_spike_seconds` (notes only). On every 15-minute lane: `min_age_minutes` (not applied to 15-minute markets at all). Remove the lines, then the options if no lane needs them.

### Lane file size
4. **Shared blocks for the rest of a lane.** `crypto_risk_score` is 35 lines after the buy and exit modules. The remainder repeats across lanes: markets (series, sides, hours, spread), order mechanics (`buy_slip`, `buy_max_move`, ...), stake limits. Same treatment as the modules.

### Buy logic
5. **The favourites lane's worth check is mostly its own hit rate.** On `crypto_risk_score` the ladder step reads the record-adjusted chance (weight on the ladder's estimate about 0.07, the rest the price band's past hit rate). Decide whether the step should read the ladder's own number.
6. **`crypto_low_swing` first bets.** Four paper bets, four losses (2026-11-18 08:26 to 08:40): three were the same side on three coins in one minute with three and a half minutes left. It has no same-side cap and no limit on how late it buys.
7. **Live path for a held market.** On paper a second bet on a held market is logged unstaked; that block is paper only. What live does there is untraced. Trace it, with a test, before any chain lane goes live.

### Feeds
8. **The outside feed is still read on commodities** in `curvemodel.py`, `staterecord.py`, `hotcheck.py`, several readers in `variants.py`, and the ladder's own instruments (`hl:` symbols). Crypto reads Kalshi throughout. Audit each against the 2026-11-12 rule (Kalshi's feed going forward).

### Studies owed
9. Rerun `scripts/commodity_risk_step_on_live_bets.py` on readings taken after the risk score's feed fix is deployed.
10. Rerun the ladder-against-market study on rows after 2026-11-17 22:47 ET (before that, crypto `model_p` is mostly a stale price).
11. Rerun `scripts/risk_trend_shadow_score.py` after a day or more of tags; score the line sale's "would sell" records.
12. Score the live commodity lanes' Kalshi perp check (live since 2026-11-18 09:43 ET) on the bets that follow.

### Records and tooling
13. **Unstaked repeat rows inflate counts.** A paper lane's "right side" counts must use rows with a stake only. Two rows from the night of 11-17 still carry another side's sale.
14. **Host access.** Every read of the host goes through AWS sign-in, which expires. An MCP server behind the admin site's login (as the GREED project has) would let Claude read decisions and tape without it.
15. **Look rate after deploy.** The look rate is now 5 s for every lane; three paper lanes had 20 to 30 s. Check the host's load after the deploy.

## Done

- 2026-11-18 `5747b19`: a paper lane's own price was handed to model lanes as the model's chance.
- 2026-11-18 `ec129e7`: a sale is written on the side that was sold only.
- 2026-11-18 `b59a89b`: the gap buy rule and its lane `crypto_risk_gap` removed.
- 2026-11-18 `5c3ebcd`: exit modules (`exit_module`, a profit part and a loss part).
- 2026-11-18 `b8df717`: the recorder reads only what it records by; a new lane field cannot stop the tape.
- 2026-11-18 `213dc1a`: the risk score reads Kalshi's perp on commodities (not deployed at the time of writing).
- 2026-11-18: the lane switch `entry_socket` removed (the socket is how two-level lanes run); the look rate is one global setting, `ENTRY_MIN_GAP_SECONDS` (not deployed at the time of writing).
