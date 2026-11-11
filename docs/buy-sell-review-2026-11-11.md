# Buy and sell logic: what each rule reads (first pass, 2026-11-11)

Asked by the user after the copper loss of 06:30 ET: the external feeds were meant as extra judgement, not as what the logic runs on. This is an inventory of where each price reading comes from, what the studies of 11-10 and 11-11 say about it, and the order to change it in. It covers the three lanes that are enabled today. It is a first pass from a search of the code, not a line-by-line review.

## The three kinds of reading

| Kind | What it is | Where it comes from |
|---|---|---|
| Kalshi contract | The contract's own bid, ask, trades | Kalshi socket (`ticker`, `trade`), tape tables `quotes`, `flow_1s` |
| Kalshi underlying | Kalshi's own price for the thing the market is about | Crypto: the CF Benchmarks indexes on Kalshi's socket (`BRTI`, `*USD_RTI`), which are the settlement source. New on 11-11: the reference price on Kalshi's perps (`KP_*`, `perpref.py`), record only |
| External | Another provider's price for the same thing | BiQuote (`BQ_*`, every commodity reading), Hyperliquid perps (`hl:` trades, the perp-lead rule), Finnhub and iTick (recorded, not read by a rule), Pyth (off, not entitled) |

Every spot reading in the code resolves the series to an index through one expression, `crypto_index(series) or biquote_index(series)` (`variants.py`, `curvemodel.py`, `hotcheck.py`, `marketprofile.py`, `staterecord.py`, `watcher.py`). So crypto lanes already run on a Kalshi underlying, and commodity lanes run entirely on the external BiQuote price.

## Live rules and what they read

| Lane | Rule | Reading | Kind |
|---|---|---|---|
| crypto_multiscale | Buy: curve model (`model_buy`, `curvemodel.py`) | Index path at several scales against the strike | Kalshi underlying |
| crypto_multiscale | Buy: hot-market gate (`hotcheck.py`) | Index history, last day | Kalshi underlying |
| crypto_multiscale | Buy: curve chance within 0.05 of the ask (`buy_curve_edge_min`) | Index cushion and 900 s rate | Kalshi underlying |
| crypto_multiscale | Buy: flat-trend skip (`buy_flat_trend_pct`) | Five coins' 60-minute index change | Kalshi underlying |
| crypto_multiscale | Sell: curve sale (`curve_exit`) | Index cushion, bid | Kalshi underlying + contract |
| commodity_multiscale | Buy: curve model | BiQuote path against BiQuote's own window-open price | External |
| commodity_multiscale | Buy: hot-market gate | BiQuote history | External |
| commodity_multiscale | Buy: curve chance within 0.05 of the ask | BiQuote cushion and rate | External |
| commodity_multiscale | Buy: from minute 4 | The clock | Structural |
| commodity_multiscale | Sell: curve sale | BiQuote cushion, bid | External + contract |
| treasury_favorite | Buy: price band and window | Contract price | Kalshi contract |
| treasury_favorite | Sell: quote shock and cross | Contract mid | Kalshi contract |
| all | Tags and regime readings (`mtype`, `[vol]`, variety, state record) | The same index per series | As the lane's index |

Off today but still in the code and reading the same indexes: hour-drift and perp-lead gates (underdog), trajectory, gap and time-to-go sales, shock, cross, wrong-side, late-drop, value exit.

## What the studies say (files in `studies/results/`)

1. **The contract's own price predicts the result better than our feed's chance** at every time left, on both kinds of feed; a walk-forward blend puts 0 to 0.3 of its weight on our feed and scores the same as the price alone (`2026-11-11-implied-value.txt`).
2. **The external commodity feed is on the wrong side of the result in 5.4% of windows**: copper 13%, natural gas 10.4%, the rest 2 to 5%; 32% when its own move is under 2 bp (`2026-11-11-feed-agreement.txt`).
3. **Kalshi's perp reference equalled the 15-minute settlement value for gold, silver, platinum and palladium** at the one boundary sampled; copper and the energy contracts are a different instrument with a level offset (`2026-11-11-perp-reference-probe.txt`).
4. **Curve chance minus price does not sort buys across the tape** (`2026-11-11-tape-edge-check.txt`); on the lanes' own bets it looked good on six days, most of it one day (`2026-11-11-multiscale-buy-filters.txt`).
5. **The curve model's score does not rank the bets it takes** on either lane (`2026-11-11-multiscale-buy.txt`).
6. **When the buy happens matters**: commodity bets before minute 4 lose; on the tape, mid-priced buys from minute 8 on are positive and earlier ones negative.
7. **No exit tested beats holding by much**; the curve sale is about level, trajectory and time-to-go lost when they first ran live (`2026-11-10-*.txt`, overnight review 11-11).

## Order of change proposed

1. **One resolver for "the spot of this series"**, replacing the repeated `crypto_index(...) or biquote_index(...)`, so the source is chosen in one place and logged with every reading.
2. **Metals to the Kalshi perp reference** once a day of `KP_*` ticks confirms the match seen at one boundary; copper and energy only after their offset to the settlement value is measured over many windows.
3. **The contract-implied cushion as a reading in its own right** (`Phi^-1(mid) * sqrt(seconds left / 60)` typical minutes, Kalshi data only): the cushion for markets with no Kalshi underlying (treasury), and the check on any spot reading. Where the two disagree, follow the contract or sit out; never act on the spot alone.
4. **External feeds become judgement only**: lead signals (does the perp or BiQuote move first), never the cushion a rule acts on.
5. **Re-study the buy on the new readings**: the curve model's features, the time-of-buy reading, and the edge gate, which item 4 above says is probably noise.
6. **Market facts from the socket, not the REST API** (user, 2026-11-11: the REST calls cause hang-ups and blocks). The `market_lifecycle_v2` channel sends each new window's target (`metadata_updated`, `floor_strike`, 14 to 19 s after the open in the probe of 11-10) and each closed window's result (`determined`), for every series we follow, about one message a second for the whole exchange. First the strike, into the same in-memory store the REST listing fills (`ticks.Strikes`), with the REST listing kept as the fallback; then the result; then slow the 20 s listing to a safety check. Kalshi's perp reference prices are read over REST today (`perpref.py`); the perps websocket is the same move for that feed once our key can connect to it.
7. **Remove code that serves only retired rules** (rule 8 in CLAUDE.md) once the lanes that used it are confirmed gone.

Each step is a study or a shadow first unless the user calls it a fix.
