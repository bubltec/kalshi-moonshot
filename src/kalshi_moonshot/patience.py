"""Would waiting a few seconds for a better price have helped? A replay on the recorded quotes; it places nothing.

The bot buys at the ask the moment it decides. This replays each finished bet as if it had instead rested a limit order
`imp` below that ask for up to `wait` seconds (filled when the recorded ask reached the limit, skipped if never) and
compares the return on the fills. Two things decide whether waiting pays: the cents saved, and which bets it loses. A resting
order fills when the price moves against it and misses the ones that run away, so the report also shows how often the bets
it filled won against the ones it missed.
"""

from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .store import Store
from .strategy import fee_per_contract

LOOKAHEAD = 60.0  # seconds of quotes kept after each decision


@dataclass(frozen=True)
class Bet:
    ticker: str
    side: str
    ask: float  # the price the bot would have paid at the decision, in the side's own terms
    won: bool
    path: tuple[tuple[float, float], ...]  # (seconds after the decision, side price) for each later quote change


@dataclass(frozen=True)
class Result:
    imp: float
    wait: float
    placed: int
    filled: int
    roi: float  # return on the filled bets
    won_filled: float  # share of filled bets that won
    won_missed: float  # share of the bets the limit never filled that won


def load(store: Store, tape: Path | str, lane: str = "commodity", min_price: float = 0.60, suffix: str = "15M") -> list[Bet]:
    db = sqlite3.connect(str(tape))
    rows = store.db.execute(
        """SELECT ts, ticker, side, ask, outcome FROM decisions
           WHERE action='bet' AND lane=? AND ask>=? AND outcome IN ('yes','no') ORDER BY ts""",
        (lane, min_price),
    ).fetchall()
    out: list[Bet] = []
    cache: dict[str, tuple[list[float], list[tuple]]] = {}
    for r in rows:
        if not r["ticker"].split("-")[0].endswith(suffix):
            continue
        if r["ticker"] not in cache:
            q = db.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? ORDER BY ts", (r["ticker"],)).fetchall()
            cache[r["ticker"]] = ([x[0] for x in q], q)
        times, q = cache[r["ticker"]]
        i = bisect.bisect_right(times, r["ts"]) - 1
        if i < 0 or r["ts"] - times[i] > 60:
            continue
        side = r["side"] or "yes"

        def price(x: tuple, side: str = side) -> float | None:
            return x[2] if side == "yes" else (None if x[1] is None else 1 - x[1])

        base = price(q[i])
        if base is None:
            continue
        path = tuple(
            (x[0] - r["ts"], p) for x in q[i + 1:]
            if x[0] - r["ts"] <= LOOKAHEAD and (p := price(x)) is not None
        )
        out.append(Bet(r["ticker"], side, float(r["ask"]), r["outcome"] == side, path))
    db.close()
    return out


def evaluate(bets: list[Bet], imp: float, wait: float) -> Result:
    cost = 0.0
    filled: list[Bet] = []
    missed: list[Bet] = []
    for b in bets:
        base = b.ask
        fill = base if imp == 0 else next((base - imp for dt, p in b.path if dt <= wait and p <= base - imp + 1e-9), None)
        if fill is None:
            missed.append(b)
            continue
        filled.append(b)
        cost += fill + fee_per_contract(fill)
    wins = sum(b.won for b in filled)
    return Result(
        imp, wait, len(bets), len(filled), (wins / cost - 1) if cost else float("nan"),
        wins / len(filled) if filled else float("nan"),
        sum(b.won for b in missed) / len(missed) if missed else float("nan"),
    )


GRID = ((0.0, 0.0), (0.01, 10.0), (0.01, 20.0), (0.02, 10.0), (0.02, 20.0), (0.03, 20.0))


def render(bets: list[Bet]) -> str:
    if len(bets) < 5:
        return f"Only {len(bets)} finished bets have recorded quotes: too few to say anything (the tape keeps 3 days).\n"
    lines = [
        f"{len(bets)} finished bets with recorded quotes. Buying at the ask is the first row.\n",
        f"{'policy':22} {'filled':>9} {'ROI on fills':>13} {'filled won':>11} {'missed won':>11}",
    ]
    for imp, wait in GRID:
        r = evaluate(bets, imp, wait)
        name = "at the ask" if imp == 0 else f"ask-{imp * 100:.0f}c, wait {wait:.0f}s"
        missed = "" if r.won_missed != r.won_missed else f"{r.won_missed:.0%}"
        lines.append(f"{name:22} {r.filled:>4}/{r.placed:<4} {r.roi:>+13.1%} {r.won_filled:>11.0%} {missed:>11}")
    lines.append(
        "\nA limit that fills often but whose fills win less than the misses is picking up the bets that were about to go "
        "wrong and missing the ones that ran. Small samples swing wildly: read this as a log until there are 100+ bets."
    )
    return "\n".join(lines) + "\n"
