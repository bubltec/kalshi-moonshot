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

## 4. The units: curve variance

Every entry, exit and stake reads how the market's own curve behaves, in units of its own variance. The typical minute is the root-mean-square one-second move times sqrt(60). The **cushion** is the distance from spot to strike in those typical moves. Thresholds are quantiles of the market's own last hours of readings, recomputed on a schedule; a reading outside its recent range is an **anomaly** state and the rule sits out. See CLAUDE.md rules 1-3 and `marketprofile.py`, `curvefeatures.py`, `curve.py`, `variants.py`.

## 5. Lanes and the buy chain

A **lane** is one strategy written as data (`data/strategies.toml`, untracked; shipped by `scripts/cloud_push.sh`; `docs/lanes.md` is generated from it by `scripts/lane_cards.py`). A lane names the series it watches, the price band, the minutes of the 15 it buys in, the stake, the open cap, its **buy module** (steps in order), its **exit module** and how the order is placed. A new rule is a step, never a lane setting that acts on its own.

Buy steps (`buychain.py`): `price`, `risk_score`, `risk_trend`, `price_trend`, `perp_worth`, `calm` (the anomaly check), `variety`, `vol_regime`, `cushion`, `spike`, `center_agree`, `same_side`, `cooldown`, `state_record` (rule 8: the state's own record paid), `curve_model`, `ladder`, and two shadow-only steps that never say no: `state_record_shadow`, `context_score`.

## 6. The exit module (sell chain)

Steps (`sellchain.py`; bodies are `ProfitWatcher._sell_<name>` in `watcher.py`): `never_ahead`, `under_even`, `profit_take`, `trail`, `long_hold` (a hold), `time_to_go`, `late_tight`, and shadow-only `shape`, `shape_timed`, `trail_timed`. The lane's **curve exit** (`curveexit.py`, `curverisk.py`, `curvevalue.py`) sells when the bid net of the fee is a set margin over the curve's chance; **shock** and **strike-cross** exits cover the Treasury lane (`quoteshock.py`). The `loss` step was removed on 2026-11-19 (user). The default order and each step's settings are in `sellchain.py`.

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

## 11. Open design work: the binary risk model

Named by the user on 2026-11-19. A 0-to-100 score (50 a toss-up, 0 a sure NO, 100 a sure YES; NO is 100 minus it) read at every point of every market from the spot's own curve history, cushion, time left and the value, refit hourly on the trailing hours so it moves with the market. It is not an adjustment to the price and not a price. A 50/50 bet with nothing from the curve is not a good bet: the fee makes it lose. Studies so far (3 days, 11-17 to 11-19, `scripts/risk_score_*.py`, `studies/results/2026-11-19-risk-score-*.txt`): linear readings carry no stable direction edge; the bottom of the score reliably flags bets to skip. For speed the evaluation works in log-odds (no exp on the tick), with per-market range readings from a background template, so it fits the 60 ms budget. The existing, older `riskscore.py` (Kalshi's price moved by the curve's readings) and `contextscore.py` are the predecessors.

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

- Which of the older modules (`llm.py`, `rag.py`, `estimators.py`, `runner.py`'s model-rated scan, `search.py`, the Polymarket modules, `jobs.py`) are still on a live path was not checked for this document. CLAUDE.md says the database job queue remains only for lanes on timers while the feed is down. A cleanup pass should mark each as live, fallback or removable.
- Which lanes are live right now is in the lane file and the admin page, not here; `docs/lanes.md` can be stale.
- Host sizes (2 CPU) and runner sizes (4 vCPU, 16 GB) are from the infra code and script headers, not read from the running machines.

## 14. Notes log (newest first; add a line for every change to the system)

- 2026-11-19: Rewrote this document as a full system description (purpose, capabilities, end-to-end flow, lanes, chains, where things run, data, lifecycle, glossary, gaps). Added the rule that heavy compute goes to the Fargate jobs runner (`cloud_job.sh`), after studies run through `cloud.sh run` loaded the live host. Named the binary risk model.
