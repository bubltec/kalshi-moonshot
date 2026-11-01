# kalshi-moonshot

A bot that trades [Kalshi](https://kalshi.com) event contracts. A **lane** is one strategy, written as data. The process scans markets, logs every decision, and places an order only when the lane asks for one and the risk gate allows it. The default mode records what it would have done and sends nothing.

The book that trades for real is a small set of 15-minute markets: favorites in commodities and crypto, and a cheap-side burst in crypto and energy. Everything else in the repo is a paper lane, a study, or a retired idea. Lane settings live in `data/strategies.toml`, which is not in git. Which of those lanes may send orders is `LIVE_LANES` in `.env`.

## Get started

You need Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/), and Docker if you want the bot and Postgres rather than a one-off command. A Kalshi API key is only required once you want signed calls. Create one at [kalshi.com/account/profile](https://kalshi.com/account/profile). Use [demo.kalshi.co](https://demo.kalshi.co) for the play-money environment.

```bash
git clone git@github.com:bubltec/kalshi-moonshot.git
cd kalshi-moonshot
cp .env.example .env
mkdir -p .secrets
# put the Kalshi private key at .secrets/kalshi.key
# leave MODE=observe and ALLOW_REAL_MONEY=false until you mean to trade
make install
python -m kalshi_moonshot doctor
```

`doctor` prints the environment, whether any lane would send a real order, whether public market data answers, and whether the local model is reachable. Ollama is optional. The live lanes do not wait on it.

### Run it on this machine

The paper bot uses `.env.local` and its own database, so it cannot spend the live account:

```bash
cp .env.example .env.local
docker compose -p moonshot-local -f docker-compose.yml -f docker-compose.local.yml up -d --build
docker compose -p moonshot-local exec bot python -m kalshi_moonshot doctor
docker compose -p moonshot-local logs -f bot
```

Postgres is on `127.0.0.1:5433` (`moonshot` / `moonshot`, database `moonshot`). The bot's tables are in the `moonshot` schema. The tape (quotes, trades, index ticks) is in the `tape` schema. Tests use SQLite and do not need this database.

`.env` on a machine that already trades is the **live** file (`MODE=trade`). `docker compose up` without the local override reads that file. Do not start it while the Ohio bot is trading the same account.

Without Docker, `python -m kalshi_moonshot scan` runs one pass against whatever `.env` points at. `python -m kalshi_moonshot run` is the loop the container runs.

### Stop orders

```bash
python -m kalshi_moonshot kill      # or: touch data/KILL
python -m kalshi_moonshot unkill
```

Real orders also require both `KALSHI_ENV=prod` and `ALLOW_REAL_MONEY=true`. `MODE=observe` never places one. `LIVE_LANES=none` trades nothing even in `MODE=trade`.

## Capabilities

`python -m kalshi_moonshot <command> --help` lists every command. Inside the paper container, prefix it with `docker compose -p moonshot-local exec bot`.

### Lanes

A lane is a `LaneSpec` in `src/kalshi_moonshot/lanes.py`. Built-in lanes come from code. `data/strategies.toml` overrides them and adds new ones. A typo in a field name is an error. `inherit = "other_lane"` copies that lane and lets you override the fields you name. Edits apply on the next decision; the watcher reads exit fields when it reloads open bets.

```toml
[lane.example]
inherit = "commodity"
label = "example"
series = ["KXBTC15M"]
price = [0.78, 0.92]
fixed_stake = 10.0
```

`python -m kalshi_moonshot lanes` prints each lane and whether it is live or paper. `versions` prints the fingerprint of a lane's logic and the record of each past fingerprint. A bet is tagged with the version that placed it, so a tuned lane is not scored as if it had always worked this way.

Two loops run a lane that sets `review_minutes` and names its series:

- **Scope** (`scope_minutes`) fetches that series and writes a watchlist.
- **Review** (`review_minutes`) re-prices the watchlist and queues evaluations.

`entry_socket = true` also runs that review the moment a live quote enters the lane's price band (`entrywatch.py`). The polled review still runs.

### What a live lane does

| Lane | What it buys | How it gets out |
|---|---|---|
| `commodity` | A 15-minute metals or energy favorite already priced in the lane's band | Hold, with a volume burst and a short-window drop exit on the socket |
| `crypto_favorite` | The same idea on BTC, ETH, and DOGE 15-minute markets | Hold. A burst can sell. `strike_through_sale` sells in the last `strike_through_seconds` once the settlement index is that many percent through the strike against the side held. `0` turns it off |
| `crypto_moonshot` | A cheap BTC, SOL, or DOGE contract in the last two minutes, when that coin's own print is larger than usual and the winning side is not being bought harder | Hold to settlement |
| `energy_moonshot` | The same cheap-side idea on WTI, Brent, and natural gas | Hold to settlement |

Crypto 15-minute markets settle on the 60-second average of a CF Benchmarks index (BRTI for bitcoin, and the ETH, SOL, XRP, and DOGE indexes), not on the last trade. Commodity prices come from Hyperliquid. The bot does not use Coinbase or Kraken for a decision.

Other lanes in the file are paper or unused until `LIVE_LANES` names them. Polymarket twins stay off that list.

### Orders and risk

Every order goes through `risk.py`. Caps include dollars per trade, open positions, a New York daily realized-loss limit, and a minimum size so a one-contract fill is skipped. Buys are immediate-or-cancel. A lane may set `fixed_stake`; otherwise size is a fraction of Kelly against the account equity, still under the caps. `bankroll` shows cash, open exposure, and what each lane has staked and returned.

Kalshi keeps a separate cash balance per exchange shard. `shardmove` moves cash between your own shards (dry run unless `--yes`). `SHARD_TOPUP_TARGET_USD` can do that before a live order.

`kill` stops new orders. It does not cancel an order already at the exchange.

### The socket

`kalshiws.py` holds one signed connection:

- `ticker` and `trade` on the markets being watched
- `fill` for this account, including a buy placed by hand in the app
- `cfbenchmarks_value`, about once a second, with the trailing 60-second average

`watcher.py` sells an open bet when that lane's exit rule hits. It re-reads the market before sending the order. A restart leaves it blind until the socket and the open-bet list are back.

`tape.py` records quotes, trades, index ticks, and Hyperliquid prices into the `tape` schema. Decisions must not query that database on the hot path; a watch loads history once and then extends it from the socket.

### The record

Decisions, orders, and jobs are rows in Postgres. `report` scores settled markets (the model's first estimate against the ask). `digest --hours 1` is the one-page read of the last hour: each lane's profit as traded and if held, what exits did, and whether the worker is keeping up. `daily` is profit by day.

The hourly `review` job may only tighten global settings, and only when the settled record is clear. It writes `data/tuning.json`, which overrides `.env` and cannot turn a lane on or raise a risk cap. Delete a key, or the file, to undo it. Suggestions from the local model go to `data/review-notes.md` and are never applied. `REVIEW_ENABLED=false` stops the job.

### Operate

| Command | What it does |
|---|---|
| `doctor` | Environment, live lanes, market data, credentials, kill switch |
| `lanes` | Every lane, live or paper |
| `scan` | One pass, printed, no loop |
| `run` | The worker loop (what the container runs) |
| `jobs` | The queue. `jobs retry` requeues dead jobs that were not mid-order |
| `resolve` | Pull outcomes for markets that have settled |
| `digest` | Last day and last week by default. `--hours 1` for the last hour |
| `report` | Model against the ask on settled markets |
| `daily` | Profit of bets actually taken, per day |
| `bankroll` | Balance, exposure, stake and return per lane |
| `show-buys` | Open real buys |
| `versions` | Lane logic fingerprints and their records |
| `kill` / `unkill` | Stop or allow orders |
| `backup` | `pg_dump` into `data/backups/` |
| `sync` | Pull the account's fills and settlements |
| `mytrades` | Rebuild each trade from fills and label it bot, manual, or mixed |
| `watch TICKER` | Print the live book until you stop it |

The worker re-reads `.env` as it runs. One-off commands re-read it at start.

### Production

Kalshi's matching engine is in AWS `us-east-2`. `infra/bot_stack.py` is one machine there, reached through Systems Manager, plus a disk for Postgres. The archive stack is separate and lives where `infra/cdk.json` puts it.

```bash
aws sso login --profile <profile>
scripts/cloud_push.sh --build     # after a code change: image, restart, and config
scripts/cloud_push.sh             # after a lane or .env change only
scripts/cloud.sh logs             # bot logs
scripts/cloud.sh sql "SELECT ..." # one query
```

`--build` restarts the bot. A config-only push is picked up on the next decision. The lane file is sent in pieces because it is larger than one parameter. `scripts/cloud.sh status` shows the machine and containers.

The tape is archived to S3 as Parquet, one file per table per day (`archive.py`, `infra/archive_stack.py`). It does nothing until `ARCHIVE_BUCKET` is set. `python -m kalshi_moonshot archive` is a dry run; add `--apply` to upload. Local rows are deleted only when `ARCHIVE_DELETE_LOCAL=true`, and only after that day is recorded as verified.

### Studies

These read the database and place no orders.

| Command | What it does |
|---|---|
| `history` / `profile` | Download settled 15-minute candles and describe how a window moves |
| `leadlag` | Whether the quote trails the underlying |
| `evidence` | Score a rule in `rules.toml` on a held-out slice of windows |
| `grid` / `knn` | Replay entries and a nearest-neighbour model of them |
| `bymin`, `patience`, `target`, `gate` | Where in the window, and at what exit, past bets paid |
| `factors`, `patterns` | What settled wins and losses have in common |

`scripts/` holds one-off studies. They are not the bot.

## Work on the repo

```
src/kalshi_moonshot/     the bot
tests/                   pytest, SQLite, no network
data/strategies.toml     lane config, gitignored, present on each machine
.env / .env.local        live and paper settings, gitignored
.secrets/                API keys, gitignored
infra/                   AWS CDK: the Ohio host and the tape archive
scripts/cloud_push.sh    ship config, or config plus a new image
scripts/cloud.sh         logs and SQL on the Ohio host
```

Code that decides belongs in `lanes.py` (the knobs), `runner.py` (entries), and `watcher.py` (exits). `kalshi.py` is the REST client. `kalshiws.py` is the socket. A new exit needs a lane field that defaults to off, a line in `takes_profit` if the position must be watched, and a reason the sell path will accept below the price paid. Shipping the field only in `strategies.toml` is not enough: the running image has to contain the code (`cloud_push.sh --build`).

```bash
make check          # ruff, mypy, pytest. This is what CI runs
make test           # pytest only
make infra-test     # the CDK app's tests, from infra/.venv
```

Dependencies are locked in `uv.lock`. The image installs `requirements.lock` (`uv export --no-dev --no-emit-project -o requirements.lock` to refresh it).

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push to `main` and every pull request: ruff, mypy, then pytest with a 70% branch-coverage floor, on Python 3.12 and 3.13.

It needs work. `main` is red. Recent runs die in ruff (dozens of errors) and never reach mypy or pytest, so the coverage floor is not actually gating anything. Until this is green, a push to `main` does not mean the tests passed. Run `make check` locally before pushing.
