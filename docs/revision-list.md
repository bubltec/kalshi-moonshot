# Revision list

Things found while simplifying the bot that still need revising. Started 2026-11-18 (user: "keep a list of these items so we can continue revising and improving"). Add to it as items are found; move an item to Done with its commit when it lands.

## Open

### Model ideas written as lane options
1. **Commodity buy model: on paper, not judged.** Built 2026-11-18 (see Done): module `commodity_model` (`price`, `risk_score`, `perp_worth`, `calm`) on the paper lane `commodity_model`. Still open: score it against the five live lanes on the same markets after a week of bets; the live lanes still carry `min_cushion_z`, `max_vol_rel`, `buy_curve_edge_min` and `entry_elapsed` as lane options until the user moves them. The risk score's 25% level is unproven on commodities.
2. **What the paper lane leaves out, to decide.** `commodity_model` has none of the live lanes' `skip_varieties`, `max_spot_crosses = 4`, `require_spot_center_agree`, `min_cushion_z`. It keeps `entry_elapsed = [7, 14]` as a lane gate (a fixed number; a model concept by the user's list). It asks no model (`estimator = "market"`), so its `model_p` is the ask.
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
16. Score the `calm` step: how often it says anomaly, and what the markets it sat out did (its reading is in every decision's note and in the screen log).

### Records and tooling
13. **Unstaked repeat rows inflate counts.** A paper lane's "right side" counts must use rows with a stake only. Two rows from the night of 11-17 still carry another side's sale.
14. **Host access.** Every read of the host goes through AWS sign-in, which expires. An MCP server behind the admin site's login (as the GREED project has) would let Claude read decisions and tape without it.
17. **The live commodity lanes' exit is written twice.** `[exit.commodity_exit]` is a copy of `commodity_energy`'s exit settings (checked equal on all 74 exit settings, 2026-11-18). Point the five live lanes at the module so there is one copy; until then a change to one must be made to the other.
18. **A test fails on its own.** `tests/test_history.py::test_the_bot_schedules_history_at_a_priority_below_trading` (3 windows, expects 5) fails on `9a82fb3`, before the commodity model.
15. **Look rate after deploy.** The look rate is now 5 s for every lane; three paper lanes had 20 to 30 s. Check the host's load after the deploy.
16. **A test that depends on the time of day.** `tests/test_history.py::test_the_bot_schedules_history_at_a_priority_below_trading` passed at 10:05 ET and failed from about 10:40 ET on 2026-11-18 on the same code (it also fails on the commit before, with no data folder). Find what it reads from the clock and pin it.

### Sell logic
19. **Sale rules outside the sell chain.** The sell chain (`sellchain.py`) covers the risk exit's decision only. `watcher.ProfitWatcher.on_event` still asks every other sale rule in one fixed `or` chain on each quote (the stop, the fast crash, the drop, the bounce, the spike take, unstable, hover, then the risk exit, strike-through, the cross take, the shock, wrong side, the late drop, the settlement guard), and a second one on each spot tick (shock, wrong side, the value exit, the curve exit, the risk exit). No lane can order or leave out those; most are off on every current lane. Decide which are still used, remove the rest (CLAUDE.md rule 8), and bring what remains into the chain.
20. **The `loss` step is four rules in one.** The max loss, the line, their wait and the early hold share one wait and one latch (`t.risk_since`), so they were moved as one step. Splitting them needs the wait made a thing of its own first.
21. **A chain lane's shadow and held records do not name the step.** The note is on a sale's reason only. The rows in `data/risk_shadow.jsonl` (would-sell, held) carry `kind` for some rules and nothing for the profit take and the line.
22. **The two paper exit modules now have `steps`**, so `crypto_risk_score` and `crypto_low_swing` report `sell_chain` in their settings: each lane's version changes at the next deploy. Neither has a `loss` step: `round_trip` (`profit_take`, `trail`) by design, `risk_exit` (`profit_take`) at the user's word on 2026-11-18 (item 23). On both a loser rides to settlement.
23. **A time-based loss sale, to build.** User, 2026-11-18, on the line sale: "maybe it should just disappear. the hard line doesn't work, it needs to be time based so it doesn't cash out on a low spike when there's still 3+ mins left". The `loss` step is out of `[exit.risk_exit]` (paper lane `crypto_risk_score`) until then; the live commodity lanes still have the line. What it must be: a wait or a line that slides with the time left (through `curverisk.stop_wait` or the like), never a cut-off at fixed seconds: "3+ mins" is an example of the ratio, not a limit. One case to hold it to: the platinum bet of 11-17 06:39 was sold by the line with 9 s left and went on to win, so "late" alone does not make the sale right. Evidence so far, from quick reads of the host by the parent session and not a saved study (unverified): since Tue 2026-11-17 the line sale sold 27 bets on the live commodity and crypto lanes, 5 went on to win (sold at 0.05 to 0.27 with 9 s to 273 s left: platinum 11-17 06:39, Brent 11-17 07:23, BTC 11-17 18:37, BTC 11-18 01:52, HYPE 11-18 03:19), about $14 lost against holding on commodities and $24 on crypto. Owed first: that count as a script in `scripts/` with its result in `studies/results/`, on the same bets, by curve shape (rule 4).

## Done

- 2026-11-18 `5747b19`: a paper lane's own price was handed to model lanes as the model's chance.
- 2026-11-18 `ec129e7`: a sale is written on the side that was sold only.
- 2026-11-18 `b59a89b`: the gap buy rule and its lane `crypto_risk_gap` removed.
- 2026-11-18 `5c3ebcd`: exit modules (`exit_module`, a profit part and a loss part).
- 2026-11-18 `b8df717`: the recorder reads only what it records by; a new lane field cannot stop the tape.
- 2026-11-18 `213dc1a`: the risk score reads Kalshi's perp on commodities (not deployed at the time of writing).
- 2026-11-18: the lane switch `entry_socket` removed (the socket is how two-level lanes run); the look rate is one global setting, `ENTRY_MIN_GAP_SECONDS` (not deployed at the time of writing).
- 2026-11-18 `c55fc97`: buy steps `perp_worth` (the Kalshi perp check as a step) and `calm` (the anomaly state of rule 3 in place of a fixed `max_vol_rel`); module `commodity_model`, exit module `commodity_exit` and paper lane `commodity_model` in the lane file (not deployed at the time of writing; needs `--build` and the lane in `PAPER_LANE_NAMES`).
- 2026-11-18: the sell chain. The risk exit's decision as named steps (`never_ahead`, `under_even`, `profit_take`, `trail`, `long_hold`, `loss`) a lane asks in its own order: lane field `sell_chain`, or `steps` in its exit module (`sellchain.py`; the step bodies are `ProfitWatcher._sell_<name>`, moved out of `_risk_decide`). No chain = the old order and the old reasons; a chain lane's sale reason ends with the chain and the step that sold. Not deployed at the time of writing: the lane file now names `steps` in two exit modules, so it needs `--build` (an old image refuses it).
