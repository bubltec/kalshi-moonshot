# kalshi-moonshot: what it is, what it can do, how it works

**A living document. Every agent and person working in this repo reads it first and updates it in the same commit as any change to the system.** `CLAUDE.md` holds the rules for studies and for the buy/sell logic; this file holds the system design. If they disagree, fix the wrong one and say so in the notes log (section 12). A fact that was not checked is marked *unverified*.

---

## 1. In one paragraph

kalshi-moonshot is a bot that trades Kalshi event contracts, mainly the **15-minute crypto and commodity markets** ("will BTC be above the strike at 9:45?"). It listens to Kalshi's websocket and to price feeds, reads how each market's price curve is behaving in units of that market's own variance, decides in memory whether to buy a side (YES or NO), buys through one gate that enforces the risk limits, then watches every open position and sells by the lane's exit rules or holds to settlement. Everything it decides is logged, and every rule is judged by studies on the recorded tape before it is switched on. Live trading runs on one cloud host in Ohio; heavy analysis runs on a separate throwaway runner; the Mac holds the code, config and small analysis.

## 2. What it does, and what it does not

**It can:**
- Watch every open 15-minute market in the series it is configured for (11 crypto series, 8 commodity series, 4 Treasury-yield series; see `docs/lanes.md`), on a websocket, with each market on its own thread.
- Decide a buy in under 60 ms from in-memory data only (no database or REST call on the decision path).
- Place and manage orders: taker buys, resting post-only buys that re-quote as the price moves, resting and taker sells, with one risk gate (`risk.py`) that every order passes.
- Sell by exit modules: curve exit, profit take, trail, shock, strike-cross, time-to-go and others (section 6).
- Size a bet in dollars (`sizing.py`, fractional Kelly scaled to the balance) and keep a per-lane cap on open bets.
- Record everything: a tape of every quote and settlement index tick, every decision and its reasons, every bet, every fill, and (since 2026-11-17) all 15 risk readings of every open market every 30 s.
- Run any rule in **shadow** (log "would do X", score it against what followed) before it is live, and retire a rule whose score turns negative.
- Run on paper (observe mode) or live; the live switch is the user's.
- Be steered from an admin page (lane overrides within limits) and publish a public status page.
- Tell someone when it stops (watchdog alarms by email).

**It does not:**
- Predict outcomes better than Kalshi's price, as a goal. It looks for timing, cushion and discipline, not for beating the odds (user rule).
- Key any rule on a coin or commodity name. A market is described by what its curve is doing (CLAUDE.md rule 1).
- Hold state a restart cannot recover: orders, bets and fills are in Postgres and the exchange.

## 3. How a bet flows, end to end

```
 FEEDS                         MEMORY                         DECISIONS                    EXCHANGE / RECORD
 Kalshi websocket  ─────────►  ticks.py   (spot ticks)  ──┐
   (quotes, trades,            Store      (quotes, books, │   entrywatch.py: a thread per
    books, fills,              bet book, watchlist)       ├─► (lane, market): tick ─► buy chain ─► risk.py gate ─► order ─► Kalshi
    settlement indexes)        templates (per-market      │      (buychain.py steps)                                     │
 Kalshi perp reference           readings built in the    │                                                              ▼
 Hyperliquid / Pyth /            background)              │   watcher.py: a thread watching every ◄── fills ◄─── Kalshi websocket
 BiQuote (hints, cushion)                                 └─► open bet: tick ─► sell chain ─► sale order ─► Kalshi
                                                              (sellchain.py steps)
 All ticks and decisions are queued and written behind to Postgres (tape + main schemas).
 Background jobs rebuild templates, the state record, the curve model, history and account sync from Postgres into memory.
```

In words:
1. **Feeds.** `kalshiws.py` holds one authenticated websocket (quotes, trades, order books, the account's fills, settlement indexes). Perp and commodity feeds (`perpref.py`, `hlfeed.py`, `pythfeed.py`, `biquotefeed.py`) add a real-number cushion for each market. The socket reader only stores the tick and wakes threads.
2. **Entry.** `entrywatch.py` runs a thread per (lane, event). A tick for a watched market runs the lane's **buy module**: the named steps of `buychain.py`, in the lane's own order, until one says no. A side that passes every step is a buy.
3. **The gate.** The order goes through `risk.py`, the only place orders are placed: kill file, real-money guard, no duplicate position, no second side of an exclusive event, open-position cap, per-trade cap, daily loss limit. A refusal is logged with its reason.
4. **The order.** The lane says how: a taker buy at the ask, or a resting post-only bid that `restbuys.py` keeps up to date on the socket's quotes and fills.
5. **Watching.** `watcher.py` follows every open bet on the socket. Each tick runs the lane's **exit module**: the named steps of `sellchain.py`, in the lane's order. A step answers sell, hold, or nothing (the next step is asked).
6. **Settlement.** A bet not sold settles at the close. Kalshi settles these markets on the average of the settlement index over the last 60 s (`settlement.py` computes the last minute exactly).
7. **Learning.** Settled bets and the tape feed studies, the state record (`staterecord.py`), the curve model (`curvemodel.py`) and per-market profiles (`marketprofile.py`), which come back into memory as thresholds from the market's own recent history.

### The processes and threads (from the code, 2026-11-19)

Two processes run on the host, both from the same image:

- **`python -m kalshi_moonshot record`** (`cmd_record`): the **tape recorder**, a process of its own so a bot rebuild leaves no gap in the tape. It writes Kalshi's market, settlement-index and extra-series sockets and the perp reference to the `tape` schema and does nothing else (thread `tape`, `tape-refresh`).
- **`python -m kalshi_moonshot run`** (`cmd_run`, `Worker.run_forever` in `worker.py`): the **bot**, with `TAPE_ROLE=memory` (it keeps the same streams in memory only). Its threads:

| Job | Threads (names in the code) | Modules |
|---|---|---|
| Sockets and feeds | `kalshi-ws`, `perpref-ws`, `hyperliquid-ws`, `pyth-stream`, `biquote`, `futflow-ws`, `perp-bars`, `polymarket-ws` | `kalshiws.py`, `perpref.py`, `hlfeed.py`, `pythfeed.py`, `biquotefeed.py`, `futflow.py`, `kalshifeed.py`, `polyws.py` |
| Buy decisions (one thread per lane and event) | `entry-<lane>-<event>`, `entry-review` | `entrywatch.py` |
| Sell decisions and position watching | the watcher, `memory-reload`, `refit-models`, `variety-ledger` | `watcher.py` |
| Resting buys and orders | on the socket's quotes and fills | `restbuys.py`, `orderkeys.py` (`order-keys-seed`) |
| In-memory store and write-behind to Postgres | `bet-book`, `decision-writer`, `write-behind`, `store-memo` | `store.py` |
| Warm-up and market discovery | `memory-fill`, `discovery`, `discovery-hot`, `market-events`, `watch-new` | `worker.py`, `marketdata.py` |
| Background jobs that build data | `maintenance`, `tape-refresh`, `regime-refresh`, `forecast-refit`, `calibration`, `risk-record`, `settlement-edge`, `control`, `archive`, `backup`, `sitepub` | `worker.py`, `riskrecord.py`, `settlement_edge.py`, `control.py`, `archive.py`, `backup.py`, `sitepub.py` |
| Watchdog and diagnostics | `heartbeat`, `req-stats` | `heartbeat.py`, `reqstats.py` |
| Fallback (feed down or entry thread dead) | `buy-job-<i>`, `live-lane-<lane>`, the job queue | `worker.py`, `jobs.py` |

## 4. The units: curve variance

Every entry, exit and stake reads how the market's own curve behaves, in units of its own variance. The typical minute is the root-mean-square one-second move times sqrt(60). The **cushion** is the distance from spot to strike in those typical moves. Thresholds are quantiles of the market's own last hours of readings, recomputed on a schedule; a reading outside its recent range is an **anomaly** state and the rule sits out. See CLAUDE.md rules 1-3 and `marketprofile.py`, `curvefeatures.py`, `curve.py`, `variants.py`.

## 5. Lanes and the buy chain

A **lane** is one strategy written as data (`data/strategies.toml`, untracked; shipped by `scripts/cloud_push.sh`; `docs/lanes.md` is generated from it by `scripts/lane_cards.py`). A lane names the series it watches, the price band, the minutes of the 15 it buys in, the stake, the open cap, its **buy module** (steps in order), its **exit module** and how the order is placed. A new rule is a step, never a lane setting that acts on its own.

Buy steps (`buychain.py`): `price`, `risk_score`, `risk_trend`, `price_trend`, `perp_worth`, `calm` (the anomaly check), `variety`, `vol_regime`, `cushion`, `spike`, `center_agree`, `same_side`, `cooldown`, `state_record` (rule 8: the state's own record paid), `curve_model`, `ladder`, and two shadow-only steps that never say no: `state_record_shadow`, `context_score`, `binary_risk_shadow`, and `binary_risk` (the binary risk model decides the buy). `late_swing` (2026-11-20: the late swing lane's buy, see section 11b).

## 6. The exit module (sell chain)

Steps (`sellchain.py`; bodies are `ProfitWatcher._sell_<name>` in `watcher.py`): `never_ahead`, `under_even`, `profit_take`, `trail`, `long_hold` (a hold), `time_to_go`, `late_tight`, and `swing_take` (the late swing lane's tiered target) and shadow-only `shape`, `shape_timed`, `trail_timed`. The lane's **curve exit** (`curveexit.py`, `curverisk.py`, `curvevalue.py`) sells when the bid net of the fee is a set margin over the curve's chance; **shock** and **strike-cross** exits cover the Treasury lane (`quoteshock.py`). The `loss` step was removed on 2026-11-19 (user). The default order and each step's settings are in `sellchain.py`.

## 7. Where everything runs

| Place | What runs there | How you reach it |
|---|---|---|
| **Bot host** (EC2, us-east-2, 2 CPU, no open ports, shared with live trading) | the live bot (`python -m kalshi_moonshot run`), the tape recorder (`... record`, its own container), Postgres 18 (schemas `main` and `tape`), the risk-reading recorder (`riskrecord.py`, a thread in the bot) | AWS Systems Manager only: `scripts/cloud.sh` |
| **Jobs runner** (Fargate, 4 vCPU, 16 GB, on demand) | one heavy script at a time: studies, refits, walk-forwards. Restores the newest nightly backup into its own throwaway Postgres, runs the script, writes output to the archive bucket, ends. Never touches the live host or its database | `scripts/cloud_job.sh` (stack `KalshiMoonshotJobs`, `infra/jobs_stack.py`, entry point `jobrunner.py`) |
| **Archive** (S3 + Athena) | old tape as Parquet, nightly `pg_dump` backups, job outputs under `jobs/<id>/` | `infra/archive_stack.py`, `archive.py`, `backup.py` |
| **Watchdog** (CloudWatch) | emails when the host's status checks fail or the bot's heartbeat stops | `infra/watchdog_stack.py`, `heartbeat.py`, `docs/watchdog.md` |
| **Site** (S3 + CloudFront) | public page and admin page, `kalshi-moonshot.bubbletech.io` | `scripts/publish_site.sh`, `infra/site_stack.py`, `sitepub.py`, `control.py`, `overrides.py` |
| **The Mac** | code, config (`data/strategies.toml`), small analysis, a local demo-credential bot. Not live | `make`, `python -m kalshi_moonshot` |

Infrastructure is code in `infra/` (CDK; one self-contained stack per concern, so deploying one cannot replace the live host: the bot stack's template has drifted and a newer machine image would replace the instance; do not `cdk deploy` it casually).

### Where work belongs

| Work | Where |
|---|---|
| Put code or lanes live | `scripts/cloud_push.sh` (`--build` for new code, started just after a quarter-hour close; see CLAUDE.md) |
| A quick read: counts, a short query, logs | `scripts/cloud.sh sql`, `psql`, `grep`, `status` |
| **Any study, refit or walk-forward, or any script that loops over the tape or the readings** | **`scripts/cloud_job.sh SCRIPT.py` (the Fargate runner). Never `cloud.sh run`** |
| A review of the last few hours | `cloud.sh sql` or a short niced script; the runner's data is up to a day old |

`cloud.sh run` executes inside the live bot's container. On 2026-11-19 long studies run that way held the 2-CPU host at a load average of about 11 and left stray processes in the bot's container. Never leave a long job on the host and never run a broad `pkill` there (a pattern can match the bot's own Python). The runner sends this Mac's `src/` (`CODE=0` for the image as built) and `data/strategies.toml`, and copies results to `./job-results/<id>/`.

## 8. Data

- **Postgres on the host** is the only live copy. Query the Docker Postgres, not `data/moonshot.db` (stale). Key tables: `windows` (each market's result and strike), `risk_readings` (all 15 readings per open market about every 30 s, whole market, with `age`; since 2026-11-17), decisions and bets (`store.py`), and the `tape` schema (`quotes`, `flow_1s`, `index_ticks`, `feed_ticks`, `fut_1s`, `fut_ticks`, `index_fast`).
- **In memory** (all live logic): the quote store, books, bet book, watchlist (`Store`, `ticks.py`), per-market templates. A tick reads nothing else. `memguard.py` raises in tests on any SELECT or Kalshi GET inside a decision.
- **Backups and archive** are in the archive bucket; the runner restores from them.
- **Evidence** is scripts in `scripts/`, results in `studies/results/` (dated), the question and the answer in the commit message.

## 9. How a rule becomes live (the study lifecycle)

1. A study asks one question on the same bets, fee on every leg, and reports n, net dollars, beat/lost counts, and the result by curve shape (CLAUDE.md rule 4). Studies run on the jobs runner.
2. A rule that passes goes to **shadow**: it logs "would do X" with its curve reading and is scored against what followed, for at least a week of live bets.
3. The user flips it live. Breaking fixes (a missing exit, a rule that did not apply) skip the week and go live at once, with evidence in the commit (rule 5).
4. Every rule keeps a rolling score and retires if it turns negative (rule 6).

## 10. Architecture rules (summary; the full text is in CLAUDE.md)

Entry and exit run on the websocket, not on timers. Work is branched into threads and queues that talk through shared memory; the database is written behind them. A tick's buy decision and sell decision each take under 60 ms (`steptime` measures it), each market on its own thread. No database call in buy or sell logic. Buying and selling happen only in modules. The image has no numpy: `src/` is stdlib only. `tests/test_architecture_rules.py` fails the build on a timer or a table read in the decision paths.

**Price feeds: Kalshi's own only** (user, 2026-11-11: "kalshi data only (with kalshi perpetuals to fill real spot pricing compared to strike)"; restated 2026-11-20: "everything is supposed to use kalshi's perpetuals, we were supposed to remove biQuote, and any other feeds"). The spot a curve reading, a cushion or a study uses is a crypto coin's Kalshi settlement index, or, for a commodity, Kalshi's perp reference (`perpref.py`, `KP_*`). BiQuote, Hyperliquid, Pyth and the other outside feeds are not inputs to any rule or study; they stay at most as hints until removed. **Status (verified 2026-11-20 from the code): not yet removed.** `biquotefeed.py` is still imported by `variants.py` (`spot_index` falls back to it), `tape.py`, `marketprofile.py` and `watcher.py`, and the studies written before this date read BiQuote for commodities (`early_swing_targets.py`, `early_swing_misses_and_signal.py`, `early_swing_by_feed.py`; the perp version is `early_swing_by_feed_perp.py`). Removing the outside feeds is open work. A new study or step reads the Kalshi index or the perp reference.

## 11. Open design work: the binary risk model

Named by the user on 2026-11-19, redefined 2026-11-20: **a buy-or-not score** at any point on the curve of any market with history. It does not estimate the chance of YES and is never compared with Kalshi's price ("we can't beat Kalshi's price and should not try to"). For each side (YES or NO) at each 30 s point it predicts the profit of buying that side now, net of fee, under every exit a hand trader has (held to settlement, or sold 1, 2, 3, 5 minutes later), from the settlement index's own curve in the market's typical minutes (cushion, bounce, path efficiency, range, hour position, recent outcomes) and the price paid (the value). Rules from the user: an ask of 0.45-0.55 is never a bet (score forced to 0); low and high prices are read separately (a low price is always risky, but its conditions can be good); the goal is profit, either direction, any time frame; built fresh from the tape, none of `riskscore.py`'s readings; refit hourly on the trailing hours so it moves with the market. Built in `scripts/binary_risk_model.py` (runs on the Fargate runner).

First result (`studies/results/2026-11-20-binary-risk-model-buy-score.txt`, 11-09 to 11-19, 8,357 markets, 205-208 test hours): every ask band loses on average (hold -2% to -6%, round trips -6% to -20%), 0.45-0.55 loses -5% held and -9% to -10% as round trips. The price alone picks nothing better than a random point. With the curve, picks beat a random point in the same band by 3-9 points when held to settlement (0.55-0.65: +3% vs -6%; 0.65-0.80: +1% vs -6%; 0.35-0.45: +5% vs -3%, 24 h window, top 5%), but about break-even in dollars, and every early-sale exit still loses. Under 0.20 the picks lose. Not yet a profit. By curve shape (`2026-11-20-binary-risk-model-by-shape.txt`): over 0.55 the lift is positive in every third of every reading but dollars are break-even; under 0.45 cheap picks lose when the cushion is low and win in choppy paths (+18%, ~2 standard errors, 217 picks): a lead.

Feature variants (`2026-11-20-binary-risk-model-variants.txt`, twelve sets fixed before the run, chosen on the first half of the test hours and confirmed on the second): price alone has no lift; adding the cushion, shape and bounce adds little; **the hour's position (h_move, h_pos, h_strike) is the first family whose lift is positive in both halves** (V4: +3.2 and +6.7 points at the top 5%), and recent outcomes keep it steady (V5: +6.2 and +6.1, net +2.1% and +1.7%, the most stable set); more readings (multi-scale slopes, crossings, interactions) lift the first half and fall back in the second, the best-of-twelve effect, so they are not kept. Working model: V5.

**Shadow step built (2026-11-20):** `binaryrisk.py` and the buy step `binary_risk_shadow` (never says no; its note is "would buy" or "would not buy" with the predicted net and readings). Reads memory only (the settlement index's tick history, the strike, the model file, and a recent-outcomes table a background job refreshes each minute); the model is refit hourly by its own low-priority process on the trailing 24 h (`python -m kalshi_moonshot.binaryrisk`, started by the watcher's refit thread when a lane names the step). Two paper lanes in `data/strategies.toml` (2026-11-20; neither in `LIVE_LANES`; pushed) test the model with the same buy model and different amounts of lane around it (user: `crypto_risk_score` is the "full featured" lane; the bare lane has no other buy logic and no exit): `crypto_binary_risk` buys by the one-step module `binary_only` (the binary risk model alone, price band 0.20-0.98) and has no exit module, so it holds to settlement and, by design (user: "i like the accumulation concept"), keeps adding to a side it holds at the lane's own 3-minute spacing; `crypto_risk_score` buys by the module `risk_first` (the price band 0.60-0.85, the binary risk model, the re-buy cooldown and a state-record note) and sells by its `risk_exit` module, so it buys, sells and buys again. No one-bet-per-side limit on either (user: "do not limit to one buy"). The old risk score is no longer used by any lane. Measured on the runner (2026-11-20): the refit reads the last 24 h, builds 13,236 training sides and fits in about 25 seconds, so an hourly niced refit on the host is light; the bars were +0.218 (top 5%) and +0.164 (top 10%) net per $1. Crypto series only for now (their settlement index is in memory). Scoring it needs a week of live bets (CLAUDE.md rule 5).

**Concepts to harvest from the three unused modules** (investigated 2026-11-19; concepts only, none of the code is used directly):
- `confidence.py` (shadow, 2026-11-06): the same idea as the binary risk model, a logistic on logit(mid) plus curve features refit daily. Its study (`studies/results/2026-11-06-confidence-study.txt`): log loss 0.3016 against the market's 0.3027, but buys where it beat the ask by 3-6 cents were +3.9% (533 buys) and +5.3% (201 buys) after the fee, positive in both halves of three test days; not established. Curve readings the binary model does not have yet: bounce off the 5-minute high and low (`up5`, `dn5`), 5-minute path efficiency (`eff5`), the 15-minute range and the spot's place in it (`amp15`, `pos15`), the hour's move and the spot's and the strike's place in the hour's range (`h_move`, `h_pos`, `h_strike`).
- `curvefeatures.py` (a library of 106 named readings, `studies/results/2026-11-12-feature-library.txt`): each reading at ten scales (1 to 180 minutes, Fibonacci), in the window's own typical move: level, slope, curvature, volatility rate against the 55-minute rate, range, path efficiency, distance under the high and over the low, strike crossings and time since the last one, bounce, plus the contract's own momentum and taker-flow imbalance and spikes. The study scored round trips (the bid change one to three minutes later), not settlement: the strongest readings were the contract's own bid momentum (`mom_1m`..`mom_3m`) and the 5 and 8 minute slope, yet every bucket still lost (all moments -19.7% a round trip; the best bucket about -8%). Lesson for the binary model: it can aim at the round trip a hand trader takes, not only at the settlement, and it has not read taker flow at all.
- `regime.py` (the recent favorite hit rate, from settled windows only): the hit rate of the last hours' favorites was 88-92% on mornings and 74-80% on the afternoons after. A recent-outcomes reading that follows the day's drift. The binary model's sign flipped between days because the day's drift (11-19 favored NO) moved the whole market, and it has no reading for that. A trailing YES rate over the last settled windows (past only, in memory, from the windows table) is the cheapest missing input.

The older `riskscore.py` (Kalshi's price moved by the curve's readings) and `contextscore.py` are the predecessors.

### 11b. The late swing lane (2026-11-20, paper, not yet pushed)

User: in the last 2:00 down to 0:45 of a 15-minute market, a low bet on a cheap (0.10-0.30) or dear (0.70-0.90) side, YES or NO, when the settlement index has been jumpy and has crossed the strike in the last five minutes, sold for the most it can make. A bet cannot be placed in retrospect, so the **tier** (100%, 70%, 50%, 40%, 30%, 20% of the lane's stake) falls with the time left: the window is six equal slices, the first slice the biggest bet. Built (`lateswing.py`): buy step `late_swing` (the window, the low-or-high ask, at least `late_swing_min_crosses` strike crossings in five minutes and the last minute's move rate at least the five-minute rate, all from the market's own curve), the stake scaled by the tier in `runner.evaluate`, and sell step `swing_take`: sold once the bid net of the fee gives the tier's share of the room the buy left (tier 100% asks the whole room, so it rides to settlement). Example lane `late_swing` and modules in `strategies.example.toml`; fields `late_swing_*` in `lanes.py`. The tier shares and the target shares are the user's list used both ways, a first setting, not fitted. Paper first (CLAUDE.md rule 5); the study that scores each tier on its own bets comes after a week of them.

## 12. Glossary

- **Lane**: one strategy as data. **Module**: an ordered list of steps (buy module, exit module).
- **Step**: one named rule in a chain. **Chain**: the steps a lane asks, in its own order.
- **Cushion**: spot's distance to the strike in the market's typical moves. **Typical minute**: RMS one-second move times sqrt(60).
- **State record**: the recorded net per dollar of a curve state at a price band; a lane buys the states that paid.
- **Shadow**: logs what a rule would do and scores it; never acts. **Observe mode / paper**: bets that are logged but not sent.
- **Tape**: the recorded quotes and index ticks of every open market.
- **Anomaly**: a reading outside its market's recent range; the rule sits out.
- **Settlement index**: the price Kalshi settles on (averaged over the last 60 s of the market).

## 13. Known gaps (unverified)

- **Older modules, checked by imports only (2026-11-19).** An import is not proof a module runs. By import from the bot's entry point, `confidence.py`, `curvefeatures.py` and `regime.py` are imported by no bot code (only scripts, tests or a word in a comment): candidates for removal, each with a study or test that would go with it. `settings_sync.py` is a command run as a module (`python -m kalshi_moonshot.settings_sync`) and `jobrunner.py` is the runner's entry point; both are live by design. Everything else is imported by the bot. The model-rated path is still constructed at start (`cmd_run` builds an Ollama client, a news search and the commodity pricer for `Worker`; `estimators.py`, `runner.py`, `llm.py`, `search.py`, `rag.py` and the Polymarket paper thread `polymarket-paper` hang off it). Whether a lane still uses it live is decided by the lane file, not the code: unverified. Removing code that serves only a retired rule is CLAUDE.md rule 8's call, and needs the lane file checked first.
- Which lanes are live right now is in the lane file and the admin page, not here; `docs/lanes.md` can be stale.
- Host sizes (2 CPU) and runner sizes (4 vCPU, 16 GB) are from the infra code and script headers, not read from the running machines.

## 14. Notes log (newest first; add a line for every change to the system)

- 2026-11-20: `binaryrisk.py` reads a commodity on Kalshi's perp reference (`curve_index`, `open_strike`: the strike is the perp's value at the window's open), and training rows include commodity windows. On the perp the model shows no commodity lift (studies/results/2026-11-20-binary-risk-commodity-perp.txt), so the paper lane block in `docs/commodity_binary_risk.lane.toml` is not in `strategies.toml`. Branch jjosef/early-swing-e7d4b9, not merged.
- 2026-11-20: Final shape of the two binary risk paper lanes: the bare `crypto_binary_risk` (no other buy logic, no exit, accumulates) and the full-featured `crypto_risk_score` (binary risk model plus its gates, cooldown and exits). A one-bet-per-side limit was tried for 90 minutes and taken out at the user's word. Binary risk model on the live commodity lanes' 570 real bets and its commodity picks: no edge (study `scripts/binary_risk_commodity.py`).
- 2026-11-20: Wrote down the price-feed rule in section 10 (Kalshi's settlement index and perp reference only; BiQuote and the other outside feeds to be removed) and its status: still in the code, and three early-swing studies read BiQuote for commodities. Found while checking the binary risk model: the live step reads only a crypto settlement index, so it scores no commodity today.
- 2026-11-20: The binary risk model's predicted net is capped at what the bet can pay, (1 - ask - fee) / ask, in the bar and in the live step (the first paper bets showed NO bought at 0.98 on a predicted +0.19 per $1). Study `studies/results/2026-11-20-binary-risk-model-cap.txt`: top 5% lift +7.9 and +6.1 points on the two halves (plain +6.2 and +6.1), picks over 0.80 fall from 659 to 79. A model file without the `capped` flag is refit at once. The step's note now shows the side's own cushion and hour place.
- 2026-11-20: Added the late swing lane: buy step `late_swing`, sell step `swing_take`, `lateswing.py`, lane fields `late_swing_*` and an example lane (paper, tiers by time left; section 11b). Needs a push with `--build`.
- 2026-11-20: Fixed the binary risk model's refit trigger: the watcher started it only for lanes naming `binary_risk_shadow`, so the two paper lanes on `binary_risk` never got a model or the recent-outcomes table and refused every buy. `binaryrisk.wanted` now covers both steps (test added). Needs a push.
- 2026-11-20: `cloud_push.sh --build` now always tags and pushes the image as `:recorder` too, so the tape recorder (a permanent part of the system) moves with every push and there is no `--recorder` flag or missing-tag check (user: "it just push[es] the same image"). A push restarts the recorder with the bot: a few seconds without a tape write.
- 2026-11-20: Added the deciding buy step `binary_risk`; in the lane file, `crypto_risk_score` now buys by the one-step module `binary_only` and keeps its `risk_exit`, and the paper lane `crypto_binary_risk` has the same buy and no exit module; not yet pushed.
- 2026-11-20: Binary risk model by curve shape and twelve feature variants; working set V5 (price, cushion, shape, bounce, the hour, recent outcomes).
- 2026-11-20: Redefined the binary risk model as a buy-or-not score (profit of buying a side now, any exit), built fresh from the tape; first result in section 11.
- 2026-11-19: Investigated `confidence.py`, `curvefeatures.py` and `regime.py` (concepts for the binary risk model, section 11).
- 2026-11-19: Added the process and thread map (read from `cmd_run`, `cmd_record` and every named thread) and an import check of the older modules (three unimported, the model-rated path still constructed at start).

- 2026-11-19: Rewrote this document as a full system description (purpose, capabilities, end-to-end flow, lanes, chains, where things run, data, lifecycle, glossary, gaps). Added the rule that heavy compute goes to the Fargate jobs runner (`cloud_job.sh`), after studies run through `cloud.sh run` loaded the live host. Named the binary risk model.
