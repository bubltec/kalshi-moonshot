# Live path review, 2026-11-14 evening

Question (user: "resume our cleanup and code review for fixes and efficiency improvements"): reading the code the live lane (`crypto_multiscale`) runs on a buy and a sale, what is wrong or wasteful?
Read-only. Nothing changed. Each item names the file and line it was read at (HEAD 47918fd) and, where there is one, the live bet that shows it. Items marked UNVERIFIED were not traced to the end.

## Bugs and logic that does not do what is intended

1. **A profit take chases the bid down like a crash exit.** `runner.py:2130-2160`, `EXIT_RETRY_SLIP = (0.02, 0.05, 0.10)` at `runner.py:2020`.
   The first sell is an immediate-or-cancel at the bid less the slip. If any contracts are left, the loop sells the rest 2, then 5, then 10 cents under a fresh read of the bid. The loop is skipped only when the reason starts with `take-profit`; the new profit take's reason starts with `curve risk exit (profit take)`, so it gets the crash ladder. `watcher.py` (the `cut = why.startswith((... "curve risk exit"))` line) also lets it sell under cost.
   Evidence: XRP NO 13:51:07, decided at 0.91, average fill 0.884 (2.6 cents a contract, about $0.90 on 34 contracts).
   Fix: a profit sale's floor is the take's own bar (or the decided bid less the slip): no ladder, and never under cost.

2. **The entry trend check has no upper limit.** `runner.py:904-906`: the bet passes when the side's 60-second move is over `entry_trend_z` (0.0 on the live lane) typical moves; nothing stops a move of 5 or 10.
   Evidence: BTC NO 18:28:09, bought at 0.77 after the side had jumped about 60 cents in the minute before; lost $27.28. A move that size is the anomaly state of rule 3 (no rule assumed to hold), and the buy's nudge already treats over 3 typical moves as one (`lanes.ANOMALY_Z`).
   Fix: the same cap on the trend check.

3. **The lane buys until the last second.** `data/strategies.toml`, `entry_elapsed = [3, 15.0]`. The BTC bet above was bought 111 seconds before the close; both stretched paper buys of the afternoon were bought with 2 to 3 minutes left. With the two-gate exit the stop's wait late in a market is a second or two, so a late buy has no exit worth the name.
   Not a code bug: a setting. Named here because every exit study of the day ran into it.

4. **The hour's width correction sits on the floor of its grid.** `widthfit.GRID` starts at 0.5; both fits seen so far (17:16, 18:31) returned exactly 0.500, so the true best value may be lower and the fit cannot say. The user's call was to keep it adapting; the note is only that 0.5 is a wall, not a reading.

5. **An asset-name split inside the buy.** `runner.py` (the `cls = "crypto" if variants.crypto_index(shot.series) else "commodity" ...` line in `evaluate`): the automatic variety skip keys its ledger on crypto against commodity. Rule 1 says no rule may be keyed on an asset. It only runs under `skip_varieties_auto`, which no running lane sets, so it goes with the unused gates.

## Waste

6. **Three paper lanes do 92% of the buy evaluations.** Log of 51 minutes after the 18:31 restart: `crypto_multiscale_underdog` 201, `_rest` 169, `_comeback` 153, the live lane 24. They share the three buy workers with the live lane.

7. **Every evaluation rereads every open bet.** `runner.py`, `open_now = sum(1 for r in store.open_bets() ...)` for `max_open`, and `entrywatch.lane_full` does the same before it. Two scans of the open bets per evaluation, about 560 evaluations in 51 minutes. Cheap each time; it is in the hot path for nothing while `max_open` is 30 and the lane holds one or two.

8. **The window offsets are still read over REST.** `commodities.learn_windows` calls `client.settled_markets(series)` for every 15-minute series every 10 minutes (`WINDOW_REFRESH = 600`). The settlement value of each window already arrives on the lifecycle stream. This is one of the REST reads the user asked to be rid of.

9. **Shadow rules that have retired themselves keep running.** At every start the log prints "retire it" for the choppy-state sell+flip rule (-93.13 over 92 signals in 7 days) and two accumulator variants (-0.87 per $1 over 33 signals). Rule 6: a rule whose score turns negative stops being proposed. They are scored and logged on every bet.

10. **Start-up.** Already being worked on in the parent session (two-stage reload, the open windows read by name): 29 requests refused for rate limits in the first 30 seconds, the first scope 41 seconds, the reload 104 seconds.

## Checked and found sound

- `watcher._risk_decide` after the swing hold's removal: the order is the profit take, the hour view, the max loss (with the squeeze's nudge), the line, then the wait. No path sells a bet in profit except the take; no path sells in the last 8 seconds.
- `lanes.nudge`: zero when either share is zero, never over the cap.
- `curverisk.stop_wait`: zero when the lane's share is zero (a lane with no wait still sells at once).
- `manual.bot_buying`: only a live lane's buy job marks its market, so the user's own trades stay visible on paper-only markets.

## Not reviewed

`entrywatch._on_quote` and the review queue, the resting buy (`_place_floor_bid`, `_place_resting`), `commodities.price_event` above the width, anything the commodity lanes run that the crypto lane does not. UNVERIFIED: whether item 7's scans show up in the "database" time of the slow-job lines.
