# kalshi-moonshot

Scans Kalshi event contracts for long shots (cheap YES contracts that pay $1), asks a local
model for its own probability, and only bets when that estimate beats the market price by a
margin after fees. Every decision is logged so the model can be scored against real outcomes
before any money is at risk.

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

**Stakes follow the balance.** Nothing is a flat dollar amount. `sizing.py` stakes a fraction (`KELLY_FRACTION`,
default 25%) of full Kelly, `(chance - cost) / (1 - cost)` of equity, so a bigger edge or a better payout earns a
bigger bet and a smaller balance earns a smaller one. Two caps per lane keep the shape you described, many cheap
moonshot tickets next to steadier bets: one bet is at most 1% of equity in moonshot (3% in steady and commodity),
and all of a lane's open bets together at most 15% in moonshot (40% in the others). Equity is cash plus open
bets, so stakes compound as the balance grows. `MAX_USD_PER_TRADE` stays a hard ceiling, and `MAX_LOSS_PER_DAY` stops new buys once the day's realized losses (New York time, your hand trades included) reach it.

Without credentials, or in `observe`, the balance is a paper one: `PAPER_BANKROLL_USD` (default $100) plus what the
paper bets have returned, less what is still riding. A paper bet's size is stored on its decision, a market is
staked once, and results compound the same way a real balance would. With keys and `MODE=trade` it uses Kalshi's
available balance and portfolio value instead. `bankroll` shows the balance, open exposure per lane and each
lane's dollars staked and returned. A 5-cent moonshot at 1% of $100 buys about 18 contracts: $1 at risk, $17 back.

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
- **Feeds.** All free, keyless and real-time. Crypto comes from Kraken's public WebSocket v2 ticker
  (`krakenfeed.py`, the stream kraken-portfolio uses). Gold, silver, WTI, Brent, copper, natural gas, platinum
  and palladium come from Hyperliquid's "xyz" perpetuals (`hlfeed.py`): deep, around-the-clock markets with a
  live mid every few seconds. Both give 5-minute history for volatility and 1-minute candles for the offset
  below. A feed that goes quiet for two minutes is refused outright. Yahoo futures (10 minutes old) remain
  only as a fallback for series with no real-time feed, and their 15-minute series are not priced.
- **The offset to Kalshi's reference, measured.** A Kalshi 15-minute market publishes its exact reference
  twice: the strike is the reference at the window's open, and the settlement value is the reference at its
  close, which is the next window's strike. So every 15-minute boundary has a known reference. For each
  15-minute series the bot matches those boundaries to the feed's 1-minute candles and takes the median
  offset (refreshed every 10 minutes at scoping). Measured on 2026-10-19: gold -0.01% (spread 0.02%), silver
  -0.03% (0.01%), WTI +0.02% (0.02%), natural gas +0.03% (0.03%), crypto within 0.03%, and copper a steady
  +1.28% (0.01%), because Kalshi's copper reference differs from Hyperliquid's. All are smaller than a typical
  5-minute move (~0.1%), so a 15-minute market can be priced from the live feed. A series with too few settled
  windows is not guessed at.
- **Day trades only.** The lane takes markets closing within `COMMODITY_MAX_HOURS_TO_CLOSE` (default 2) hours:
  15-minute and hourly markets, and a daily one only in its last two hours. It does not hold positions
  overnight or for tomorrow's close.
- **Wait for the book.** A market is not bet until it has been open `COMMODITY_MIN_AGE_MINUTES` (default 20),
  and only when its bid-ask spread is at most `COMMODITY_MAX_SPREAD` (default 2 cents). Right after a market
  opens its book is thin and its quotes jump. 15-minute windows are exempt from the age rule because their
  entry window already waits (8.5-12 minutes in).
- **Stop-loss on 15-minute windows.** A 15-minute position always has under an hour left, so the stop-loss (sell
  when the bid is down 30%) applies from the moment it is bought. On history, 15-minute favorites bought 9-12 minutes
  in returned about -0.8% held to settlement and -3.8% with that stop. `STOP_LOSS_15M=false` leaves them to settle;
  hourly and daily markets keep their stop either way.
- **Minimum return.** A win must pay at least `COMMODITY_MIN_RETURN` (10%) on the stake after the fee, which
  caps the entry at about 90 cents. A 96-cent entry makes about 4% if it wins and loses everything if it
  misses, so it is not taken however likely it looks.
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

## Watching for profit, live

A 15-minute contract can move 20 cents in half a minute, so the bot also watches open bets on Kalshi's live socket
(`watcher.py`). A lane opts in with any of `take_profit_x` (sell when the bid reaches that multiple of the price paid),
`take_profit_at` (sell at an absolute bid), `trail_activate` + `trail_drop` (a peak-crash exit: once the bid has been that fraction above the price paid, sell when it has stayed that many dollars below its peak for `trail_confirm_seconds`, default 10; a bid counts toward the peak only after it has held `trail_hold_seconds`, default 3, so a one-tick spike is not a peak and a dip of a second or two is ignored) or `spike_cents` + `spike_seconds` (sell, while in profit, when the bid has jumped
that much within that time). Each quote that arrives is checked; on a hit the bot re-reads the market, confirms the bid is
still above the price paid, and sells through the same exit code as the stop-loss: a paper sale for a paper lane, an
immediate-or-cancel reduce-only order for a live one. No default lane sets these, so nothing live changes until you add them
to a lane in `data/strategies.toml`; `late_ticket` (paper) uses `take_profit_x = 3` and a 10-cent spike. A change to these
fields needs a restart, because the watcher starts with the bot. A backstop pass also checks take-profit bets once a minute.

`late_ticket_trail` (paper) runs beside `late_ticket` with only a peak-crash exit (up 10%, then a 4-cent fall from the peak), to compare it with the 3x/spike exit on the same kind of bet. The peak is tracked by the live watcher only, so it starts again from the price paid if the bot restarts.

What the history says about it: on 3 days of quote ticks, a peak-crash exit (up 10%, then a 2-10 cent fall) returned -7% to -8% on favorites against -3.7% for holding, and -17% to -19% on cheap tickets against -3.8%, so the live commodity lane is left without one. A spike exit beat holding on cheap tickets by about 10 points on minute data but still lost money.

Two paper lanes can compare exits on the same markets: bookkeeping (open bets, exits, "already holding") is per lane, so
`late_ticket` and `late_ticket_trail` may both hold one market and each exits on its own rule. A main-scan lane with
`shares_markets = true` (like `steady_raw`, the no-model control for `steady`) is offered the markets another lane took
instead of the leftovers, which is what lets a control be compared with the lane it shadows.

## Currencies, indices and other new markets

Kalshi lists 15-minute markets beyond crypto and commodities: EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, the S&P 500 and
Nasdaq-100, Brent and more. The five currency pairs are `live = true` in `instruments.toml` (the indices and Brent are `live = false`, which means a live lane prices
and logs them and paper-trades them, but never sends an order for them until you change that line). The `fx_ticket` lane buys flat-$1 cheap
tickets in the five pairs only, sells on a spike or a held peak-crash, and goes live only when `LIVE_LANES` in `.env` names it. Currencies read Hyperliquid's EUR,
GBP and JPY perpetuals (Kraken's spot FX for AUD and CAD); the indices read the S&P 500 and Nasdaq-100 perps. The offset between
each feed and Kalshi's reference is learned from settled windows, like gold. Run `history` and `profile --series KXEURUSD15M`
to see how a new market behaves before trusting it, and flip `live` only on a series with a record. Not yet covered:
Dow (no feed), interest rates (no feed), and the extra crypto series (ADA, BCH, BNB, HYPE, NEAR, TON, ZEC).

Kalshi keeps a separate cash balance per exchange shard: crypto and commodities are on shard 2, most other categories (currencies
included) on shard 0, and an API order can spend only the cash on its own market's shard. The website moves the money for you;
the API does not. `shardmove` calls Kalshi's intra-exchange transfer for you (dry run by default, `--yes` to send, capped by
`--max-usd`). When a refused order is a balance problem on the market's own shard, the bot says so and pauses orders there.

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
- Per-trade cap, a daily loss limit (`MAX_LOSS_PER_DAY`: realized losses only, wins offset them, spending does not count), max open positions, no duplicate positions.
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
| See whether 15-minute markets were mispriced, from the paper log of every window | `docker compose exec bot python -m kalshi_moonshot shadow` |
| Show the balance, open exposure per lane, and dollars staked and returned | `docker compose exec bot python -m kalshi_moonshot bankroll` |
| Show each lane's parameters and how the record is weighing it | `docker compose exec bot python -m kalshi_moonshot lanes` |
| Run the hourly review now (tighten a setting only when the scorecard is clear) | `docker compose exec bot python -m kalshi_moonshot review` |
| See the job queue (pending, done, dead, with errors) | `docker compose exec bot python -m kalshi_moonshot jobs` |
| Requeue dead jobs (never ones that were mid-order) | `docker compose exec bot python -m kalshi_moonshot jobs retry` |
| Fill in the category of old decisions from Kalshi (dry run; add `--yes` to write, backup first) | `docker compose exec bot python -m kalshi_moonshot backfill` |
| See why a market that passed a lane's filter never got a decision (our chance, the edge, the bar it missed) | `docker compose exec bot python -m kalshi_moonshot screened --ticker KXBTCD` |
| See what your larger wins and losses have in common (the bot also rewrites `data/patterns.md` every 6 hours) | `docker compose exec bot python -m kalshi_moonshot patterns` |
| Replay finished bets as if each had waited for a slightly better price (read-only, needs the tape) | `docker compose exec bot python -m kalshi_moonshot patience` |
| See a lane's 15-minute results by how far into the window each bet was placed | `docker compose exec bot python -m kalshi_moonshot bymin --lane late_ticket` |
| Move cash between your own exchange shards, e.g. 2 -> 0 so currency orders can be funded (dry run unless `--yes`) | `docker compose exec bot python -m kalshi_moonshot shardmove --usd 15` |
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

## Going live one lane at a time

`MODE=trade` alone means every lane trades. To trade only some, name them:

```bash
MODE=trade
LIVE_LANES=commodity      # commodity also covers crypto (BTC, ETH, SOL, XRP, DOGE) and gold
```

The other lanes keep paper-trading on the paper balance: still sized, logged and scored, never sent. A lane that
is live is sized from your Kalshi balance; a paper lane never touches it. `LIVE_LANES=none` trades nothing.
`python -m kalshi_moonshot lanes` shows `LIVE` or `paper` beside each lane, and the bot logs the live lanes when
it starts. Stop-loss follows the same rule: a paper lane's stop is only recorded.

15-minute windows have their own switch: with `MODE=trade` and the commodity lane live, they still
paper-trade unless `COMMODITY_15M_LIVE=true`. Hourly and daily markets in the lane trade for real; the 15-minute
ones are sized, logged and scored on the paper balance until you flip it. `lanes` shows `(15m: paper)` or
`(15m: LIVE)`.

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

## How the 15-minute contracts move, from Kalshi's own history

Kalshi keeps 1-minute candles (bid, ask, last price, volume, open interest) for every market, settled ones
included. `history` downloads them for each 15-minute series' recent settled windows into the database (safe to
re-run; it only fetches windows it lacks) and `profile` reports, from about four days and 2,400 windows:

- the **path** through a window: how far from a toss-up the contract is at each minute, how much is already
  decided (a side at 90%+) and the spread, which is about 1 cent throughout, so getting in costs little;
- **when a window is effectively over**: by minute 10, 39% are at 90%+ and 53% have a side at 85%+ that goes on
  to win; by minute 13, 75%;
- **favorites** priced 70-90% at a given minute: how often they still lose (18% at minute 10, against an average
  price of 80 cents, so priced about fairly) and how far their price swings against a holder first (median 9
  cents; the worst tenth lose almost everything);
- the same **by hour of the day** (New York time): reversals, how much the contract moves, spread and volume.
  Each hour has only about 25-50 favorites per four days, so differences between hours are noisy until more
  days are stored.

The bot also keeps this up to date on its own: a low-priority `history` job (it only runs when no trading job is
waiting, and one run takes roughly 15 seconds) fetches newly settled windows hourly, 30 at a time, coming back in two minutes while a
backlog remains, and drops anything older than 30 days (`HISTORY_ENABLED`, `HISTORY_INTERVAL_MINUTES`,
`HISTORY_BATCH`, `HISTORY_KEEP_DAYS`). The first runs catch up on the last couple of days.

```bash
docker compose exec bot python -m kalshi_moonshot history     # download now (resumable; add --windows 400 for more)
docker compose exec bot python -m kalshi_moonshot profile     # pooled; --series KXBTC15M,KXGOLD15M to narrow
docker compose exec bot python -m kalshi_moonshot daily       # profit and loss of the bets actually taken, per day
```

## Lead-lag: does the quote trail the price it follows?

Minute candles cannot show whether a Kalshi quote reacts to a move in the underlying instantly or a few seconds
late. So the bot records a **tape** (`data/tape.db`, kept 3 days, read-only): every change of each open
15-minute market's quote from Kalshi's authenticated WebSocket, next to the price it follows. For crypto that is
the settlement index itself, which Kalshi streams (BRTI for bitcoin, `ETHUSD_RTI`, ...) about once a second with its
trailing 60-second average, the quantity those markets compare. For commodities, which settle elsewhere, it is the
Hyperliquid price. After a few hours:

```bash
docker compose exec bot python -m kalshi_moonshot leadlag     # --series KXBTC15M,KXGOLD15M to narrow
```

It reports, per series, the lag at which the quote follows the feed (a positive peak means the quote trails),
how far the quote keeps drifting after a sharp feed move, and a trade test (buy the side the move favors after a
2-second reaction time, sell back after 5 to 60 seconds, fees and spread included) beside a control of the same
trade at random moments. Only a gap between the two is an edge. A first nine-minute sample: crypto quotes follow
the index within about a second, and a round trip costs 7 to 8 cents per contract in fees and spread, so an edge
would have to beat that. `TAPE_ENABLED` and `TAPE_KEEP_DAYS` control it.

## Your own wins and losses: `sync` and `mytrades`

The bot only knew its own orders. `sync` pulls everything Kalshi holds for the account (fills and settlements,
read-only) into the local database, so trades made by hand in the app, cash-outs included, sit next to the bot's.
It is safe to re-run: rows are keyed by Kalshi's ids and each run asks only for what is new. The bot also does it
every 15 minutes as a low-priority job (`ACCOUNT_SYNC_ENABLED`, `ACCOUNT_SYNC_MINUTES`). A first sync looks up each market's open and close time,
a few dozen per run, so the full history fills in over a couple of runs.

`mytrades` rebuilds each trade from the raw fills (buy, then either a sale or settlement; closing fills match the
oldest opening ones first; profit after every fee) and labels it `bot`, `manual` or `mixed` by whether the order ids
are ones the bot placed. A trade whose fills disagree with Kalshi's settlement (usually a position opened before
the history window) is left out rather than guessed.

```bash
docker compose exec bot python -m kalshi_moonshot sync --full            # first time: everything Kalshi has
docker compose exec bot python -m kalshi_moonshot mytrades               # --source manual|bot|mixed|all, --series-suffix 15M, --days 30
```

It prints totals, results by series, cash-outs against held-to-settlement, 15-minute results by entry minute and by
entry price, and the multiples you sold at. A first read (Aug-Oct 2026, 15-minute markets bought by hand, 31
trades): every cash-out won (median 3.9x, held about 7 minutes), every loss was a trade held to settlement, and
the total was about break-even (-1.9%). Treat it as a prompt, not a result: 31 trades is far too few to separate
skill from luck, and you only see the cash-outs you chose to make.

### Paper test of a resting sell: `target`

`target` re-scores the bot's own finished 15-minute bets (paper and live) as if a sell had been resting at 1.5x, 2x
or 3x the price paid, using the stored minute candles' best bid. It places nothing and changes nothing; it prints
what the bets made as played, what they would have made with the sell, and how many would have sold. The same test
on 18 of your hand-placed 15-minute trades (2026-10-20) gave -10.7% as played, -1.5% with a 2x sell, -9.5% with 3x:
a sell at 2x helped there, but with 18 trades that is noise until the bot's own bets build up a record.

```bash
docker compose exec bot python -m kalshi_moonshot target                 # --mult 2 to pick one, --lane late_ticket for one lane
```

## Testing a buy/sell idea: `evidence`

Write the idea as a rule and get the same report every time. `src/kalshi_moonshot/rules.toml` ships five
(follow or fade a price slope, sell when it turns, a 2x/3x resting sell on the cheap side, buy a dip); copy it to
`data/rules.toml` to change them. A rule says which contract to buy (YES, NO or either), the price band, the
minutes of the window, a trigger (`slope`, `dip` or `always`) and an exit (`hold`, `slope_turn`, `trail`,
`target`, `stop`, `time`). Any list inside `entry` or `exit` is a grid and every combination is tried.

```bash
docker compose exec bot python -m kalshi_moonshot evidence                      # all rules
docker compose exec bot python -m kalshi_moonshot evidence --rule follow_the_slope --series KXBTC15M
```

The report tunes on the earliest 60% of windows and judges on the later 40%, beside a baseline that trades the same
side, band, minutes and exit with no trigger (so you see what the signal adds, not what trading costs), gives a 90%
interval for the held-out return by resampling whole 15-minute boundaries, and says how many settings were tried.
Verdicts: SUPPORTED (positive on both and the held-out interval is above zero), PROMISING (positive but the interval
includes zero), NOT PROFITABLE (beats its baseline but loses after costs), NOT SUPPORTED. On the 2,532 stored windows
today every rule is NOT PROFITABLE or NOT SUPPORTED: following a 5-minute slope of 4 cents a minute or more is the
closest (about +4% held out, interval -0.4% to +8%), and it is worth rerunning as the history grows.

## Checking an idea against history

`python scripts/replay_15m.py` replays recent settled 15-minute windows: at the 10-minute mark it asks what the
data-driven model would have done, using only what was knowable then (earlier windows' offsets, the prior three
hours of volatility), and prints the return of each rule after fees plus the model's calibration against the
market's. It places no orders. Run it before moving any rule from paper to live. Result on 2026-10-19 (704
windows, about 20 hours): buying favorites at 70-90 cents blindly returned -2.6%; buying cheap underdogs lost
15-30% (the model overstates tails); the model-and-market-agree favorites (edge of 1.5 cents or more at 70-90
cents) returned +7.5% on only 18 bets, far too few to trust.

**Cheap-side bets with an early sell** (buy whichever side costs 12-30 cents, sell at 2x or 3x). On 2,400
settled windows, split by time (find patterns in the first 60%, check them on the later 40%), with sells simulated
as resting orders that fill when a minute's best bid reaches the target: 40% of entries reach 2x and 23% reach 3x,
yet the return after fees was -21% (2x) and -15% (3x) on the later data. Round-trip fees alone are about 13% of
the stake at 2x, and selling at a multiple caps the upside of the contracts that would have gone to $1. The hour's
trend (steady climb or slide against a spike) did not help consistently, except that buying right after a spike
was worse in both periods (about -27% to -32%). Nothing tested reached a third of entries winning at settlement
(`strategies.example.toml` lane `fifteen` keeps score on paper). An edge would have to come from information the
price lacks, such as the live feed moving before the Kalshi quote does.

**Slope monitoring** (enter when a contract's price is climbing or falling at some rate, exit when the slope turns
or after a trailing drop). 54 rules (slope window 2, 3 or 5 minutes; minimum slope 1, 2 or 4 cents a minute;
follow the move or fade it; three exits) on 2,532 windows, tuned on the earliest 60% and checked on the later
40% (`scripts/slope_study.py`): none was profitable on the tuning windows, 2 of 54 were positive held out
(+0.6%, +3.3%, about what chance gives), and the median held-out return was -8.3%. Two things in it are real:
following the slope beats fading it (-5.8% against -10.6% on average) and beats random entries (-6.4%), and
holding to settlement beats slope-based or trailing exits (-5.4% against -9.5% and -9.8%). So the slope has some
information, about enough to cancel the cost of trading but not to profit from it. More windows arrive every
day, so rerun it as the history grows.

## Tests

`make test` (signing, fees and sizing, both lanes' filters and trend, mutex and falling-ask
guards, stop-loss, risk gate, news parsing, prompt, retrieval, model parsing, scoring,
database migration).
