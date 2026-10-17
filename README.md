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
| `strategy.py` | Pure long-shot filter, fee and edge math |
| `llm.py` | Ollama call that returns a probability. It is never shown the market price |
| `risk.py` | The one gate every order passes through |
| `runner.py` | One scan pass; logs every decision; places a capped order only if allowed |
| `store.py` | SQLite (`data/moonshot.db`): decisions and orders |
| `report.py` | Brier score of model vs market on settled markets |

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
mkdir -p secrets                # put your Kalshi private key here as kalshi.key

# Ollama on the Mac host (uses the Apple GPU); a container cannot.
brew install ollama && ollama serve &
ollama pull qwen3:8b            # fits in 16 GB

docker compose up -d --build    # starts the scan loop
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

`pytest` (17 tests: signing, fees and sizing, filters, risk gate, model parsing, scoring).
