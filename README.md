# kalshi-moonshot

Scans Kalshi event contracts for long shots (cheap YES contracts that pay $1), asks a local
model for its own probability, and only bets when that estimate beats the market price by a
margin after fees. Every decision is logged so the model can be scored against real outcomes
before any money is at risk.

Replaces the old Kraken desktop app (`../kraken-portfolio`, left untouched).

## The idea in one paragraph

A 5-cent contract that wins pays 20x. Long shots are overpriced on average, so buying them
blindly loses money. This only works if the model is better calibrated than the market, which
is unproven. So the project is built to **measure that first** (`report`) and trade second.

## Layout

| File | Job |
|---|---|
| `kalshi.py` | The only code that talks to Kalshi (signed requests, market data, IOC buy) |
| `lanes.py` | A lane is data: price band, hours, spread, bet rule, size, estimator name. Built from `.env`; `data/strategies.toml` changes or adds lanes |
| `strategy.py` | One generic filter driven by a lane spec, price-trend check, fee and edge math |
| `estimators.py` | The part a lane plugs in: `llm` (local model + RAG + news) or `ladder` (commodity math) |
| `commodities.py` | Prices gold/silver/oil/gas/copper threshold ladders: center from the ladder, width from realized volatility. Series list lives in `instruments.toml` |
| `calibration.py` | Weighs each estimate against our settled record: measured skill sets the weight, price-band hit rates replace a fixed trust |
| `rag.py` | Retrieval over our record: similar settled cases (same series first, never itself) plus the base rate for that series, category or lane |
| `search.py` | News headlines about a market's subject (Google News RSS, no key); odds headlines are dropped |
| `llm.py` | Ollama call that returns a probability. It is never shown the market price |
| `risk.py` | The one gate every order passes through |
| `jobs.py` | Persistent job queue in the same SQLite file: retry with backoff, expiry, resume after a stop |
| `worker.py` | The `run` loop: runs queued scan / evaluate / resolve jobs one at a time |
| `runner.py` | One scan pass; logs every decision; places a capped order only if allowed |
| `store.py` | SQLite (`data/moonshot.db`): decisions, orders, and a local search index of settled cases |
| `report.py` | Brier score of model vs market on settled markets |

## Lanes are data

Every lane below is a `LaneSpec` (`python -m kalshi_moonshot lanes` prints them with how the record is
currently weighing each one). Defaults come from `.env`. To change one or add one without touching code,
create `data/strategies.toml`:

```toml
[lane.moonshot]
min_edge = 0.08

[lane.metals]          # a new lane
estimator = "ladder"
price = [0.02, 0.30]
hours = [1, 48]
min_edge = 0.10
series = ["KXGOLDD", "KXSILVERD"]
needs_instrument = true
```

A typo is an error, not a silent default. Edits apply on the next scan.

**Odds, not labels.** A long shot is a price and a payout, whichever contract carries it: a 6-cent NO on a heavy
favorite pays the same 94 cents as a 6-cent YES. The moonshot and steady lanes therefore consider both
contracts (`MOONSHOT_SIDES`, `STEADY_SIDES`, default `yes,no`). Every number is kept in the contract's own terms:
entry price, chance it pays, edge, trend, stop-loss, and the report's win/loss.

**Guaranteed profit.** `arbs` looks for strike ladders where buying YES on a lower strike and NO on a higher one
costs under $1 after fees, which pays at least $1 whatever happens. On 2026-10-19 it found none across about
500 ladders and 60,000 markets. It is report-only: both legs must fill, and the orders here are
immediate-or-cancel, so one leg can fill alone. Sets that merely look exclusive (team spreads, partial outcome
lists) are not guaranteed and are deliberately not counted.

**The record has a vote.** After an estimator answers, `calibration.py` blends it with what our own settled
markets say: `p = w * estimate + (1 - w) * band hit rate`. `w` starts at the lane's `trust` and slides to the
estimator's measured skill (1 - model Brier / market Brier) as markets settle; the band rate starts at the ask
and moves only as outcomes arrive. With no record, the estimate passes through. `model_p` in the log stays the
raw estimate so `report` still scores the model honestly; `cal_p` is what the bet rule used.

**Commodities and crypto lane (`ladder`): steady, not a moonshot.** Kalshi's gold, silver, oil, gas, copper and
crypto markets are price-threshold ladders and 15-minute "up in the next 15 mins?" contracts. This lane buys the
side that is already close to decided, at 70-98 cents, for a small return that repeats. It can buy YES or NO
(`sides`), and it runs hourly, daily, weekly and 15-minute series (`instruments.toml`).

- **Center and width.** The price's center comes from a probit fit over the event's liquid strikes. Near expiry a
  ladder is mostly decided strikes and cannot be fitted, so the feed-to-ladder basis seen on a recent fit of the
  same instrument is reused with extra width. The width is realized volatility over the time left, plus the
  *age of the feed's last price* (a stale price hides a move), plus the basis uncertainty.
- **Model risk.** No probability is claimed closer than `COMMODITY_TAIL_FLOOR` (0.5%) to 0 or 1, and volatility is
  scaled by `COMMODITY_VOL_MULT` (1.25) for fat tails.
- **Feeds.** Crypto is real-time (Yahoo). Yahoo's commodity futures are about 10 minutes old, which the width
  accounts for, but a real-time commodity source would tighten near-expiry prices a lot. Kalshi settles its
  15-minute commodity contracts on Pyth prices; Pyth's free endpoints returned 401/404 when tried.
- **What to expect.** A decided strike usually sells at 95-98 cents, so the margin is 1-4 cents against a loss of
  the whole stake on a miss. That only pays at a very high hit rate, which is why the record decides: `report`
  scores the lane by side, and calibration shrinks the estimate toward the observed hit rate as markets settle.
  Run it in `observe` until the hit rate per price band is clearly above the price.
- **NO orders.** Buying NO is sent as a sell-YES order at `1 - price`, and exiting as a `reduce_only` buy-YES, per
  Kalshi's order docs. That path is covered by tests but has not been exercised against the live exchange.

**Two levels.** A lane with `review_minutes` set runs two loops instead of riding the 30-minute all-markets scan:

- **Scoping (slow, `scope_minutes`):** fetches only the lane's own series (about 13 requests, not 28,000 markets)
  and saves the events worth watching to a watchlist in the database.
- **Reviewing (fast, `review_minutes`):** re-reads only the watched events, re-prices them, drops finished
  ones, and queues an evaluation for any market with an edge. A per-lane cooldown stops it re-logging the same
  unchanged view every pass.

Jobs carry a `priority`, so the fast lane's work runs ahead of a backlog of slow model calls. The commodity lane
uses this by default: scope every 10 min (new 15-minute markets keep appearing), review every 2 min.
Any lane can opt in from `strategies.toml` by setting `review_minutes` plus `series` (or `needs_instrument`).

## Lane notes

- **moonshot**: cheap contracts (`MIN_PRICE`-`MAX_PRICE`) with a small chance of a large payout.
- **steady**: likely outcomes (ask 0.66-0.75, about +31% to +48% after the fee) closing within
  72 hours whose ask has risen over the last 6 hours and is not rolling over. The rising price is the signal. The model only blocks a buy when it is more than `STEADY_MAX_LAG` (0.15) under that ask, so a 0.55 on a 0.66 favorite can still be taken and a 0.25 on a 0.70 favorite is skipped. Small trades (`STEADY_USD_PER_TRADE`, default $2). Scored separately from moonshot.

Before each estimate the model also gets recent news headlines on the subject (`SEARCH_ENABLED`): last 3 days, newest first, with age on each line. Fresh specific facts (a protest today, a speech, a poll) can justify moving off the base rate; a week-old theme piece is not a reason to treat a long shot as likely. Sports is skipped by default (`SKIP_CATEGORIES=Sports`). Markets that resolve on a live number (a price, a total, a count, an average) are skipped too (`SKIP_NUMERIC=true`): the model cannot see the current value, so its odds there are guesses. Kalshi marks these with `strike_type` greater/less/between; team-win (`structured`) and date-based (`custom`) markets are kept.

Two more guards, both from a paper “win” that was also a paper bet on the other side of the same live game:

- **One side per game.** Mutually exclusive events (Kalshi’s `mutually_exclusive` flag) can contribute at most one market. A later scan will not bet the other team.
- **Don’t buy a crashing price.** A moonshot whose ask has fallen (`MOONSHOT_MAX_DROP`, default 2 cents over the lookback, or on the last candle) is treated as a live favorite dying, not a long shot.
- **Stop-loss.** If we already have a bet, there is under `STOP_LOSS_HOURS` (1h) left, and the bid is down `STOP_LOSS_DROP` (30%) or more from what we paid, the bot sells IOC at the bid (`reduce_only`, so it cannot open a short). In `observe` this is a paper sell. `report` then scores that bet on the exit price, not on settlement. No bid means it cannot exit and it holds. Checks run every `STOP_LOSS_CHECK_MINUTES` (5) while something is in that window, not only on the 30-minute scan. The kill switch does not block a stop-loss sell.

## Progress notes

**2026-10-18, retrieval (RAG) over our own record.** Settled decisions are indexed in
`data/moonshot.db` with SQLite full-text search (FTS5, BM25 ranking). Before each estimate the
bot retrieves similar settled questions and shows the model the title, its old probability, and how it resolved. Prices are never stored in the index.

- What it is: keyword retrieval in one relational file. Not embeddings, not a graph database.
- Limits: it matches words, not meaning, and only about 17 rows were indexed. It cannot learn much yet.
- Next step, once a few hundred markets have settled: Ollama embeddings (`nomic-embed-text`) with `sqlite-vec`, still one file. A graph only makes sense if relationships between markets matter.
- Hourly review: a `review` job reads the same scorecard as `report`. It does not turn a lane off. A category with 20+ settled markets and a model Brier at least 0.10 worse than the market is added to `SKIP_CATEGORIES`, except Sports, which stays in the scan. Moonshot's minimum edge goes up, in small steps, only after 10 losing bets. It never turns trading on or raises a risk cap. Changes land in `data/tuning.json`, which overrides `.env`; delete that file to undo them. `REVIEW_ENABLED=false` stops it.
- v5 steady: the lane stays on. A rising favorite is taken unless the model is more than 0.15 under the ask.
- v4 news: search the last 3 days, rank newest first, label age; the prompt treats only fresh specific facts as leverage (the Antifa 0.65 came from a 12-day-old theme piece).
- v3 filters: at most one market per mutually exclusive event; moonshot skips a falling ask; stop-loss sells (paper in observe) when under 1h is left and the bid is down 30% or more.
- Job queue and rate limits: `run` is now a persistent queue (`jobs` table). A scan queues one `evaluate` job per shortlisted market, plus the next scan and a `resolve`. Stopping the bot loses nothing: finished jobs stay finished and interrupted ones resume on start (a restart still scans right away). A failed job retries with backoff (30s, 60s, ...) up to `JOB_MAX_ATTEMPTS`, then is parked as `dead` (see `jobs`). A model outage is retried, not logged as a pass. Evaluations expire after one scan interval rather than run on stale prices, and each re-reads the market's current price first. A job that dies mid-order is never re-run automatically (check Kalshi, then clear it), and orders carry a stable id per job so a resend cannot buy twice. The Kalshi client now paces itself (`KALSHI_MAX_RPS`), asks for 1000 markets per page (`KALSHI_PAGE_SIZE`), honors `Retry-After`, adds jitter, and retries server errors and timeouts on reads only.
- Strategy versions: every decision is tagged with the logic that made it (`STRATEGY_VERSION` in `store.py`; bump it when filters, prompt, or model logic change). Rows from before versions existed are `v1`. `EXCLUDE_VERSIONS` (default `v1`) keeps chosen versions out of `report` and out of the similar-case search; `report` lists how many decisions each version has. `prune` deletes excluded versions later, except tickers we hold orders on.
- Scoring fix: the bot re-scores a market every scan, so `report` now counts each market once. The first "17 settled" was about 9 markets. Early result: the model lost to the market on short-dated sports totals, which is why sports is skipped.

## Safety model

- `MODE=observe` (default) never places orders.
- `KALSHI_ENV=demo` (default) is Kalshi's play-money environment.
- Real orders need **both** `KALSHI_ENV=prod` and `ALLOW_REAL_MONEY=true`.
- Per-trade cap, per-day cap, max open positions, no duplicate positions.
- A buy is skipped when less than `MIN_ORDER_USD` (default $0.25) is offered at that price, so a one-contract fill of a few cents does not go through.
- Orders are immediate-or-cancel: nothing is ever left resting on the book.
- `python -m kalshi_moonshot kill` (or `touch data/KILL`) stops all orders at once.

## Run it

```bash
cp .env.example .env            # then edit
mkdir -p .secrets              # put your Kalshi private key here as kalshi.key

# Ollama on the Mac host (uses the Apple GPU); a container cannot.
# The bot calls the Ollama server on every estimate, so it must be running, not just installed.
brew install ollama
brew services start ollama      # runs in the background and restarts at login
# Keep the Mac awake while ollama is running (otherwise Docker and the bot sleep too).
# Installed as a login LaunchAgent: ~/.local/bin/ollama-awake.sh
# Check: pmset -g assertions | grep caffeinate
ollama pull qwen3:8b            # downloads the weights once; fits in 16 GB
curl -s localhost:11434/api/tags   # should list qwen3:8b; an empty list means the pull did not finish

docker compose up -d --build    # starts the scan loop
# After that, edits to .env apply on the next scan. Rebuild only when the code changes.
docker compose logs -f bot
docker compose exec bot python -m kalshi_moonshot doctor
docker compose exec bot python -m kalshi_moonshot report
```

## Changing settings and re-running

Edit `.env`. No rebuild is needed; rebuild only when the code changes.

| I want to | Command |
|---|---|
| Run one scan now with the current `.env` and print each market, probability, and edge | `docker compose exec bot python -m kalshi_moonshot scan` |
| Restart the loop so it scans immediately (it scans on start, then sleeps) | `docker compose restart bot` |
| Check for markets that have settled | `docker compose exec bot python -m kalshi_moonshot resolve` |
| See the scorecards (all lanes, each lane, each category), minus excluded versions | `docker compose exec bot python -m kalshi_moonshot report` |
| List open real buys with the model probability, edge, and headlines from the fill | `docker compose exec bot python -m kalshi_moonshot show-buys` |
| Same, but include every version | `docker compose exec bot python -m kalshi_moonshot report --all-versions` |
| Show each lane's parameters and how the record is weighing it | `docker compose exec bot python -m kalshi_moonshot lanes` |
| Run the hourly review now (tighten a setting only when the scorecard is clear) | `docker compose exec bot python -m kalshi_moonshot review` |
| See the job queue (pending, done, dead, with errors) | `docker compose exec bot python -m kalshi_moonshot jobs` |
| Requeue dead jobs (never ones that were mid-order) | `docker compose exec bot python -m kalshi_moonshot jobs retry` |
| Fill in the category of old decisions from Kalshi (dry run; add `--yes` to write, backup first) | `docker compose exec bot python -m kalshi_moonshot backfill` |
| Scan open strike ladders for guaranteed-profit pairs (read-only) | `docker compose exec bot python -m kalshi_moonshot arbs` |
| Show what deleting excluded versions would remove (dry run) | `docker compose exec bot python -m kalshi_moonshot prune` |
| Delete them (makes a backup copy of the database first) | `docker compose exec bot python -m kalshi_moonshot prune --yes` |

The background loop re-reads `.env` at the start of each scan (every `SCAN_INTERVAL_MINUTES`).
One-off commands (`scan`, `report`, `show-buys`, `doctor`, `resolve`) re-read it every time they run.

What a re-run does and does not change:

- `scan` shows the effect of new settings right away, on the markets that are open now.
- `report` only changes as markets settle, because a market needs an outcome to be scored.
- `report` scores each market on the model's **first** estimate. Changing the model, the prompt,
  or the news search does not re-score markets already rated. To compare two setups fairly,
  note the date you changed something and judge only decisions made after it.

Without Docker: `make install && python -m kalshi_moonshot doctor` (Python 3.12 or 3.13).
`make check` runs ruff, mypy and pytest, the same as CI. Dependencies are locked in `uv.lock`; the Docker image
installs `requirements.lock` with hashes (`uv export --no-dev --no-emit-project -o requirements.lock` to refresh).

## Going from observing to trading

1. Run `observe` for a few weeks. `report` needs 30+ settled decisions to mean anything.
2. If `model_beats_market` is false or the bet return is negative, **stop**. There is no edge.
3. If it is positive, set `MODE=trade` on demo, then tiny caps on prod.

## Runs only while this machine is on

Docker on a laptop stops when it sleeps, which also stops the bot. The same compose file
moves unchanged to a small VPS or an always-on Mac mini (use
`docker compose --profile ollama up -d` on Linux).

On this Mac, a LaunchAgent keeps the machine from idle-sleeping while the `ollama` process
is running (`~/.local/bin/ollama-awake.sh`, loaded from
`~/Library/LaunchAgents/com.user.ollama-awake.plist`). It starts at login and picks up a
new Ollama pid after a restart. Check it with `pmset -g assertions | grep caffeinate`.

- The screen can still turn off. That is fine.
- On battery, system sleep is not blocked. Plug in.
- Closing the lid can still sleep a MacBook. Test before leaving it overnight.
- To undo: `launchctl bootout gui/$(id -u)/com.user.ollama-awake` then delete the plist
  and the script above.

## Tests

`make test` (signing, fees and sizing, both lanes' filters and trend, mutex and falling-ask
guards, stop-loss, risk gate, news parsing, prompt, retrieval, model parsing, scoring,
database migration).
