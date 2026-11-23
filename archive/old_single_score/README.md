# Archived 2026-11-22: the old single-score concept

The user's word (2026-11-22): the old paper lanes that read one blended risk number "might have useful concepts but shouldn't be considered for active use". Nothing here is imported by `src/`. The files are kept as they were for reference; to run one again, copy it back into `src/kalshi_moonshot/` (or `tests/`).

| What | Where it came from |
| --- | --- |
| `riskscore.py`, `risktune.py`, `riskrecord.py`, `scenario.py` | the 0-1 risk score, its refit, the 30 s risk-reading recorder, the market-scenario note |
| `buychain_steps.py.txt` | buy steps `risk_score`, `risk_trend`, `binary_risk_shadow`, `binary_risk`, `swing_risk`, `win_value_shadow` |
| `binaryrisk_net_model.py.txt` | the net-per-dollar model in `binaryrisk.py` (fit, predict, admits, the swing and value bars) |
| `runner_pieces.py.txt`, `curverisk_pieces.py.txt`, `methods.py.txt` | the runner/curverisk/watcher/store pieces that served only the score (risk trend read, scenario note, `_risk_score`, `add_risk_readings`) |
| `tests/` | the tests of the above |

Kept in `src/`: `binaryrisk.readings`, `shape`, `curve_index`, `open_strike` (PriskP and the wave lane read them) and the price-free win-chance model with its shadow step `win_chance_shadow`, which the live commodity modules still list.

Lane files that still write a removed step or setting load without it and say so in the log (`lanes.RETIRED_STEPS`, `lanes.RETIRED_FIELDS`). Studies in `scripts/` that import these modules (`scripts/risk_score_*.py`, `scripts/binary_risk_*.py` and others) are kept as evidence and need the file copied back to run.
