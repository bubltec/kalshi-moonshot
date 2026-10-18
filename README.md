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
| `strategy.py` | Pure filters for both lanes (moonshot, steady), price-trend check, fee and edge math |
| `search.py` | News headlines about a market's subject (Google News RSS, no key); odds headlines are dropped |
| `llm.py` | Ollama call that returns a probability. It is never shown the market price |
| `risk.py` | The one gate every order passes through |
| `runner.py` | One scan pass; logs every decision; places a capped order only if allowed |
| `store.py` | SQLite (`data/moonshot.db`): decisions, orders, and a local search index of settled cases |
| `report.py` | Brier score of model vs market on settled markets |

## Two lanes

- **moonshot**: cheap contracts (`MIN_PRICE`-`MAX_PRICE`) with a small chance of a large payout.
- **steady**: likely outcomes (ask 0.66-0.75, about +31% to +48% after the fee) closing within
  72 hours whose ask has risen over the last 6 hours and is not rolling over. Small trades
  (`STEADY_USD_PER_TRADE`). Whether a rising ask predicts anything is unproven, so it is scored
  separately: `report` prints one scorecard per lane.

Before each estimate the model also gets recent news headlines on the subject (`SEARCH_ENABLED`),
so a long shot with real momentum can score above its base rate. Sports is skipped by default
(`SKIP_CATEGORIES=Sports`).

## Progress notes

**2026-10-18, retrieval (RAG) over our own record.** Settled decisions are indexed in
`data/moonshot.db` with SQLite full-text search (FTS5, BM25 ranking). Before each estimate the
bot retrieves similar settled questions and shows the model the title, its old probability, and
how it resolved. Prices are never stored in the index.

- What it is: keyword retrieval in one relational file. Not embeddings, not a graph database.
- Limits: it matches words, not meaning, and only about 17 rows were indexed. It cannot learn much yet.
- Next step, once a few hundred markets have settled: Ollama embeddings (`nomic-embed-text`) with
  `sqlite-vec`, still one file. A graph only makes sense if relationships between markets matter.
- Scoring fix: the bot re-scores a market every scan, so `report` now counts each market once. The
  first "17 settled" was about 9 markets. Early result: the model lost to the market on
  short-dated sports totals, which is why sports is skipped.

## Safety model

- `MODE=observe` (default) never places orders.
- `KALSHI_ENV=demo` (default) is Kalshi's play-money environment.
- Real orders need **both** `KALSHI_ENV=prod` and `ALLOW_REAL_MONEY=true`.
- Per-trade cap, per-day cap, max open positions, no duplicate positions.
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
ollama pull qwen3:8b            # downloads the weights once; fits in 16 GB
curl -s localhost:11434/api/tags   # should list qwen3:8b; an empty list means the pull did not finish

docker compose up -d --build    # starts the scan loop
# After that, edits to .env apply on the next scan. Rebuild only when the code changes.
docker compose logs -f bot
docker compose exec bot python -m kalshi_moonshot doctor
docker compose exec bot python -m kalshi_moonshot report
```

Without Docker: `uv venv --python 3.12 && uv pip install -e '.[dev]' && python -m kalshi_moonshot doctor`
(Python 3.12 or 3.13; 3.14 has no `cryptography` wheel yet.)

## Going from observing to trading

1. Run `observe` for a few weeks. `report` needs 30+ settled decisions to mean anything.
2. If `model_beats_market` is false or the bet return is negative, **stop**. There is no edge.
3. If it is positive, set `MODE=trade` on demo, then tiny caps on prod.

## Runs only while this machine is on

Docker on a laptop stops when it sleeps. The same compose file moves unchanged to a small VPS or
an always-on Mac mini (use `docker compose --profile ollama up -d` on Linux).

## Tests

`pytest` (37 tests: signing, fees and sizing, both lanes' filters and trend, risk gate, news
parsing, prompt, retrieval, model parsing, scoring, database migration).
