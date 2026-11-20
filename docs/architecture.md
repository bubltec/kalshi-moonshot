# Architecture (a living document)

**Every agent and person working in this repo reads this first and updates it when the system changes.** It is the map of what runs where, what each piece is for, and where work belongs. `CLAUDE.md` holds the rules for studies and for the buy/sell logic; this file holds the system design. If the two disagree, fix the one that is wrong and say so in the notes log at the bottom.

Rule for editing: change the section the work touches, add a dated line to the notes log, keep both short. A fact you did not check says "unverified".

## 1. What the system is

A bot that trades Kalshi event contracts, mainly the 15-minute crypto and commodity markets. A **lane** is one strategy written as data (`data/strategies.toml`, untracked, shipped by `scripts/cloud_push.sh`). Buying and selling happen only in modules (`buychain.py`, `sellchain.py`). Everything else is recording, studies, or support. The Mac holds code, config and analysis. **Live runs in the cloud.**

## 2. Where things run

| Place | What runs there | How you reach it |
|---|---|---|
| **Bot host** (EC2, us-east-2, 2 CPU, no open ports) | the live bot (`python -m kalshi_moonshot run`), the tape recorder (`... record`, its own container), Postgres 18 (schemas `main` and `tape`), the risk-reading recorder (`riskrecord.py`, a thread in the bot) | AWS Systems Manager only: `scripts/cloud.sh` |
| **Jobs runner** (Fargate, 4 vCPU, 16 GB, started on demand) | one heavy script at a time: refits, walk-forwards, long studies. It restores the newest nightly backup into its own throwaway Postgres, runs the script, writes output to the archive bucket, and ends. It never touches the live host or its database | `scripts/cloud_job.sh` (stack `KalshiMoonshotJobs`, `infra/jobs_stack.py`, entry point `jobrunner.py`) |
| **Archive** (S3 + Athena) | old tape as Parquet, nightly `pg_dump` backups, job outputs under `jobs/<id>/` | `infra/archive_stack.py`; the archiver in `archive.py` writes it |
| **Watchdog** (CloudWatch alarms) | emails when the host's status checks fail or the bot's heartbeat stops | `infra/watchdog_stack.py`, `heartbeat.py`, `docs/watchdog.md` |
| **Site** (S3 + CloudFront) | the public page and the admin page (`kalshi-moonshot.bubbletech.io`) | `scripts/publish_site.sh`, `infra/site_stack.py` |
| **The Mac** | code, config, small analysis, the local demo-credential bot. Not live | `make`, `python -m kalshi_moonshot` |

Infrastructure is code in `infra/` (CDK, one stack per concern, each self-contained so a deploy of one cannot replace the live host: the bot stack's template has drifted, a newer machine image would replace the instance, so do not `cdk deploy` it casually).

## 3. Where work belongs (decide before you run anything)

The bot host is a 2-CPU machine shared with live trading. Load there is the main thing to protect.

| Work | Where |
|---|---|
| Change code or lanes and put it live | `scripts/cloud_push.sh` (`--build` for new code; see its header and CLAUDE.md for the quarter-hour timing) |
| A quick read: row counts, a short query, logs | `scripts/cloud.sh sql ...`, `psql`, `grep`, `status` |
| **Any study, refit, walk-forward or script that loops over the tape or the readings** | **`scripts/cloud_job.sh SCRIPT.py` (the Fargate runner), never `cloud.sh run`** |
| A review of the last few hours | `cloud.sh sql` or a light `cloud.sh run` of a short, niced script; the runner's data is up to a day old |
| Anything that must not wait on a restore | ask first; the host is the last resort |

`cloud.sh run` executes inside the live bot's container. On 2026-11-19 several long studies run that way held the host at a load average of about 11 and left stray processes in the bot's container. Do not repeat it. When launching a long job, never leave it unattended on the host; never run a broad `pkill` there (a pattern can match the bot's own Python).

The runner's data is the newest nightly backup, up to about a day old: right for refits and walk-forwards, wrong for "what happened in the last hour". It sends this Mac's `src/` (`CODE=0` to use the image as built), uses this Mac's `data/strategies.toml`, and copies results to `./job-results/<id>/`.

## 4. Data

- **Postgres on the host** (the only live copy). Query the Docker Postgres, not `data/moonshot.db` (stale). Tables worth knowing: `windows` (each market's result), `risk_readings` (all 15 risk readings for every open market about every 30 s, whole market, no price or time gate, with the market's `age`; since 2026-11-17), the `tape` schema (`quotes`, `flow_1s`, `index_ticks`, `feed_ticks`, `fut_1s`, `fut_ticks`, `index_fast`).
- **Nightly backups** go to the archive bucket (`backups/`); the jobs runner restores them.
- **In memory** (live logic): the quote store, books, bet book and watchlist the entry and sell threads read. No database call on a tick (see CLAUDE.md).
- **Studies** are scripts in `scripts/`, results in `studies/results/` (dated), the question and answer in the commit message.

## 5. Live runtime shape (summary; the rules are in CLAUDE.md)

The websocket subscribes to markets, tickers, trades, books, fills and settlement indexes. The socket reader only stores the tick and wakes threads: an entry thread per (lane, event) for buy decisions (`entrywatch.py`), the watcher for sell decisions (`watcher.py`), a buy-order thread for resting buys (`restbuys.py`). Timers are only background jobs that build or refresh data (history, resolve, account sync, state record, templates). Budget: under 60 ms per tick for a buy or sell decision, measured by `steptime`; `tests/test_architecture_rules.py` fails the build on a timer or a table read in those paths. The image has no numpy: `src/` is stdlib only.

## 6. Open design work

**Binary risk model** (named by the user 2026-11-19): a 0-to-100 score (50 = toss-up; 0 a sure NO, 100 a sure YES; NO is 100 minus it) read at every point of every market from the spot's own curve history, cushion, time left and the value, refit hourly on the trailing hours so it moves with the market. Not an adjustment to the price and not a price. Studies so far (3 days, 11-17 to 11-19): no direction edge from linear readings; the bottom of the score reliably flags bets to skip. Scripts `scripts/risk_score_*.py`, results `studies/results/2026-11-19-risk-score-*.txt`. Fast evaluation design: work in log-odds, no exp on the tick, per-market range readings from a background template.

## 7. Notes log (newest first; add a line for every change to the system)

- 2026-11-19: Created this document. Added the rule that heavy compute goes to the Fargate jobs runner (`cloud_job.sh`), after studies run through `cloud.sh run` loaded the live host. Named the binary risk model.
