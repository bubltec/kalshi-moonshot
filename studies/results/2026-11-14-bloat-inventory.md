# Bloat inventory, 2026-11-14

Question: what does the bot carry that it does not use, and what in the live path makes no sense?
Read-only. Nothing was changed. Raw output: `studies/results/2026-11-14-bloat-inventory-raw.txt`. Scripts: `scripts/bloat_inventory/`.
"Running" means live (`LIVE_LANES`) or paper and switched on (`PAPER_LANE_NAMES`, `PAPER_LANES=false` switches the rest off).

## Headline counts

| What | Carried | Used by a running lane |
|---|---|---|
| Lanes the code knows | 21 | 14 (6 live, 8 paper) |
| Lanes that trade live today | 6 named | 1 in practice (`crypto_multiscale`; the 5 commodity lanes are idle for the weekend) |
| Lane settings (`LaneSpec` fields) | 298 | 104 |
| Per-bet exit state (`Target` fields in `watcher.py`) | 198 | not counted |
| Exit checks in the watcher (`_*_hit`) | 16 | 4 (the risk exit on live lanes; the curve exit and resting curve sale on paper lanes only) |
| Estimators | 3 kinds (`ladder`, `llm`, `llm_shape`) | 1 (`ladder`, all 14 running lanes) |
| Settings in the live lane's block | 86 | 61 (25 are inert: their master switch is off) |
| Modules in `src/kalshi_moonshot/` | 108, 30,533 lines | 93 reachable from the worker by import (imports are not gated on settings, so this overstates use) |
| Background threads the code can start | 33 named | not measured on the host |
| `.env` settings / `config.Settings` fields | 53 / 166 | not measured |
| Scripts / result files / test files | 484 / 238 / 164 | n/a |

## 1. Lanes

- **Live (6):** `crypto_multiscale`, `commodity_energy`, `commodity_wti`, `commodity_natgas`, `commodity_precious`, `commodity_industrial`.
- **Paper and running (8):** `crypto_multiscale_stretch`, `crypto_multiscale_underdog`, `crypto_multiscale_rest`, `crypto_multiscale_comeback`, `commodity_multiscale_rest`, `commodity_multiscale_kalshi`, `commodity_multiscale_tick`, `index_wave`.
- **Defined but off (7):** `treasury_favorite`, `commodity_multiscale`, `crypto_ladder_perp` (in the lane file); `moonshot`, `steady`, `commodity`, `stop_flip` (built into the code; the two `llm` lanes and the `llm_shape` lane are among these).
- **Named in `PAPER_LANE_NAMES` with no lane behind them (4):** `crypto_trend_paper`, `crypto_trend_scalp_paper`, `crypto_trend_scalp_loose`, `crypto_underdog_scalp`.
- The five commodity lanes carry 79 to 85 settings each and are near copies of one another; the seven `*_multiscale_*` paper lanes carry 62 to 89 each and are near copies of `crypto_multiscale` or `commodity_multiscale`. The lane file is 1,404 lines for 1,280 settings.

## 2. Settings

Of 298 lane settings:

- **104** are set to a non-default value by at least one running lane.
- **17** are set only by lanes that are off: `max_lag`, `shock_source`, `shock_units`, `shock_margin_units`, `cross_units`, `max_crossings`, `choppy_hours`, `choppy_series`, `choppy_left_max`, `choppy_price_min`, `choppy_skip`, `strike_through_seconds`, `strike_through_rearm`, `min_return`, `trend`, `min_trend` (and `enabled`, which is a real switch).
- **177** are set by no lane at all. Of those, **90 default to off**, so they are features no lane uses, and **87 default to a working value**, so they are constants nobody tunes (many belong to the 90: the knobs of a feature that is off).

Families among the unused settings (full list in the raw file): the record gate (`gate_*`), the nearest-neighbour and grid gates (`knn_*`, `grid_*`), stake scaling from the record (`scale_*`), the automatic variety and market skips (`auto_*`), the value exit (`value_exit*`), the wrong-side exit (`wrong_side_*`), the strike-through sale, the timed drop and bounce exits, the scalp, trail, spike, hover, unstable and cash-out exits, the tick buy (`tick_buy_*`), the entry-move, entry-bounce, entry-spike and entry-spot gates, the take-profit multiples.

Where the code that reads them sits (distinct unused settings read / used settings read):

| Module | Unused | Used |
|---|---|---|
| `watcher.py` | 97 | 43 |
| `runner.py` | 79 | 46 |
| `__main__.py` | 17 | 13 |
| `entrywatch.py` | 12 | 9 |
| `worker.py` | 9 | 8 |
| `scale.py` | 8 | 3 |
| `gate.py` | 7 | 1 |
| `knn.py` | 4 | 1 |
| `grid.py` | 2 | 0 |

Two thirds of what the watcher reads off a lane, and nearly two thirds of what the runner reads, belongs to features no running lane has on.

## 3. Exit checks

Sixteen `_*_hit` checks run in the watcher's chain on every quote. Which running lanes arm each:

| Exit | Running lanes that arm it |
|---|---|
| Risk exit (`curve_risk_sell`) | the 6 live lanes and `crypto_multiscale_stretch` |
| Profit take (`curve_take`), swing hold, stop wait | `crypto_multiscale`, `crypto_multiscale_stretch` |
| Squeeze | `crypto_multiscale_stretch` (paper) |
| Curve exit (`curve_exit >= 0`) | 3 paper lanes |
| Resting curve sale (`curve_rest`) | 4 paper lanes |
| Stop loss, timed drop, bounce, unstable, hover, strike-through, cross take, shock, wrong side, late drop, settlement guard, value exit | **none** |
| Take-profit multiples, cash-out, scalp, trail, spike, cash-if-up / drop / left | **none** |
| Burst exit | **none** (see 5.1) |

Twelve of the sixteen checks, and the six older profit rules, cannot fire on any running lane. On the live lanes the only exit is the risk exit.

## 4. Modules and feeds

Import reachability does not show bloat here: `__main__.py` (1,393 lines) and the worker import nearly everything whatever the settings say. By what the running lanes ask for, these serve nothing that runs:

| Area | Modules | Lines |
|---|---|---|
| Gates no lane sets | `gate.py`, `knn.py`, `grid.py`, `scale.py` | 1,058 |
| Language-model estimators (off: 3 lanes, none running) | `llm.py`, `shape.py`, `search.py`, part of `estimators.py`, the `ollama` service in `docker-compose.yml` | 453 + |
| Forecast lane (retired) | `forecast.py`, `forecast_fit.py` | 499 |
| Other venues | `polymarket.py`, `esports.py` | 544 |
| Command-line only (not the running bot) | `evidence.py`, `leadlag.py`, `audit.py`, `scorecard.py`, `patience.py`, `scalp.py`, `gap.py`, `target.py`, `misses.py` | 1,294 |
| Imported by tests or scripts only | `jobrunner.py`, `settings_sync.py`, `coinbasefeed.py`, `curvefeatures.py` | 505 |

External price feeds: the code carries Kraken, Coinbase, Finnhub, iTick, Pyth, BiQuote and Hyperliquid readers. The rule since 2026-11-12 is Kalshi's own feed for new work, and `crypto_multiscale` has `curve_risk_spot_weight = 0` (price only). In 291 log lines read from the host at 14:50 ET, 11 were the Finnhub stream failing and reconnecting and 10 were the iTick stream; the Polymarket socket and the esports job were also running. Whether any running lane still reads each feed was not traced here and needs a per-feed check before anything is removed (the commodity lanes do read a spot).

## 5. Logic in the live path that does nothing, or makes no sense

Each of these is on `crypto_multiscale`, the lane trading real money.

1. **25 of its 86 settings are inert.** Their master switch is off, so the value is never read:
   - curve exit off (`curve_exit = -1`): `curve_exit_persist`, `curve_exit_near`, `curve_exit_scales`, `curve_behind_last`, `curve_drop_units`, `curve_drop_seconds`, `curve_drop_margin_units`, `curve_drop_persist`
   - shock exit off: `shock_last`, `shock_persist`, `shock_memory`
   - burst exit off (`burst_exit` is false on every lane): `burst_ratio`, `burst_dollars`, `burst_favorite`, `burst_share`. Across the lane file there are 68 burst settings on lanes with `burst_exit` off: 68 lines that do nothing.
   - timed drop off (`drop_cents = 0`): `drop_seconds`, `drop_confirm`, `drop_stay`, `drop_window`
   - curve model off (`model_buy = false`): `model_top`, `model_features`
   - scope and review timers (the lane is event-driven since this morning): `scope_minutes`, `review_minutes`
   - stop loss off: `watch_loss_seconds`, `watch_loss_lift`
   A further 15 settings in its block only repeat the code's default.
2. **The profit take assumes a buy-back the lane does not allow.** The take's bar is low early "since we can always buy back", but `cooldown_minutes = 3` stops the lane deciding on that market for three minutes after any decision. On the 13:30 XRP market it sold at 13:20:25 and was locked out until 13:23.
3. **The line sale's profit branch cannot fire on most of what the lane buys.** `curve_risk_min_profit = 0.15` only applies when the bid is under 0.5 and the bet is in profit, which needs a buy under 0.5. The lane's price band is 0.30 to 0.88. This is the gap the profit take was built for; the old branch is now redundant for buys above 0.5.
4. **Three holds and one override on the same loss.** A losing bet passes the hour view, the swing hold, the stop and its wait of half the time left. The squeeze (paper) then overrides two of those. Rules added the same day pull in opposite directions; none has a forward score yet.
5. **A sale decided at one price filled at another.** The 13:51:07 XRP profit take was decided at a bid of 0.91 and filled at 0.884, 2.6 cents a contract (about $0.90). Not investigated.
6. **The entry trend check reads through restart holes.** It needs the side's price 60 s back and its moves over the 2 minutes before. After a restart that window spans about 60 s with no quotes. Decisions per 15 minutes in the four restart windows today: 1, 4, 2, 1; in the neighbouring windows: 7, 6, 5, 5. Cause not proven.
7. **The lane can buy with almost no time left.** `entry_elapsed = [3, 15]` allows a buy up to the close. The two stretched paper buys today came 3:00 and 2:03 before the close, on sides priced 0.42 and 0.43 with the lane's own chance near 0.49, and both lost. The live lane has the same window.
8. **The live lane has not bet since 13:49 ET** (2 to 5 evaluations per window since, all passed). Not a bug by itself, but worth a look alongside 6 and the 0.10 edge bar.
9. **Your manual trades were booked wrong.** The three hand plays on the 13:15 BTC market are one row: 120 NO at 0.32 for $41.76. The fills were 90 NO, then 30 YES, then 50 YES. The bot's books show a loss on a market where the account made $37.05.

## 6. What could go, ranked by code removed against risk

Line counts are estimates from the module sizes and the share of each file that reads unused settings. Nothing here is used by a live or paper-running lane, except where a decision is named.

| # | Removal | Rough size | Needs your decision first |
|---|---|---|---|
| 1 | The 12 exit checks no running lane arms, the 6 old profit rules, and their `Target` and `LaneSpec` fields | 600 to 900 lines in `watcher.py`, about 80 settings | Are any of the off lanes coming back (`treasury_favorite`, `commodity_multiscale`, `crypto_ladder_perp`)? |
| 2 | The gates no lane sets: `gate.py`, `knn.py`, `grid.py`, `scale.py`, the `auto_*` skips, and their calls in `runner.py` | about 1,060 lines plus runner branches, about 40 settings | None found |
| 3 | The language-model estimators and the `ollama` service | about 450 lines, 3 built-in lanes | Confirm `moonshot`, `steady` and the shape lane are retired |
| 4 | The forecast lane's code | about 500 lines | Confirm retired |
| 5 | Polymarket and esports | about 545 lines, two sockets or jobs running on the host | Confirm they are not wanted |
| 6 | The 25 inert settings on `crypto_multiscale` and the same on its copies (68 burst lines alone) | about 250 lines of the lane file | None: they are not read |
| 7 | The 4 dead names in `PAPER_LANE_NAMES`, and the 3 off lanes in the lane file | about 220 lines of the lane file | As 1 |
| 8 | Paper lanes that have run their week | 62 to 89 settings each | Which of the 8 paper lanes are still wanted |
| 9 | External feeds no running lane reads | 100 to 270 lines each | A per-feed check, and the commodity lanes' needs |
| 10 | Command-line-only modules and the 484 scripts | about 1,300 lines of modules | Which reports you still run |

After 1 and 2, the buy path and the sell path of a lane would each be one short chain, and the lane file could be written as one base lane plus the few settings each copy changes.

## Not covered

- Which `.env` switches and which of the 33 threads are live on the host.
- A line-by-line read of `runner.evaluate`'s gates against the running lanes (the counts above are by setting, not by branch).
- Bugs outside the live lane's path.
- No file was deleted or edited, and no claim here was checked by removing code and running the tests.
