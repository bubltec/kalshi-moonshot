"""Paper against live, lane by lane, on the same hours and the same lane version: where does a paper record flatter the real one?

For each lane with both kinds of bets (or either), the settled bets since `hours` ago, of the lane's current version: how many, how many won, the price paid, net profit after the buy fee (a stop or sale counted at its exit price),
and the margin of error with each 15-minute window as one cluster (the coins move together). Live lanes that buy with a resting bid also show how their orders ended (filled, not filled, refused as a post-only cross), and paper lanes show how many
of their bets the resting rule (paperfill.py) kept. Reads only.
"""

from __future__ import annotations

import math
import statistics
import time
from typing import Any

from .store import Store
from .strategy import fee_per_contract as fee


def _summary(rows: list[Any]) -> str:
    if not rows:
        return "no settled bets"
    by: dict[float, float] = {}
    cost = 0.0
    wins = 0
    for r in rows:
        n = float(r["contracts"] or 0.0)
        ask = float(r["ask"])
        won = r["result"] == r["side"]
        wins += 1 if won else 0
        got = float(r["exit_price"]) if r["exit_price"] is not None else (1.0 if won else 0.0)
        by[r["close_ts"]] = by.get(r["close_ts"], 0.0) + (got - ask - fee(ask)) * n
        cost += (ask + fee(ask)) * n
    net = sum(by.values())
    se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) if len(by) > 1 else float("nan")
    price = statistics.mean(float(r["ask"]) for r in rows)
    return (f"{len(rows):3} bets in {len(by):2} windows, {wins:3} won ({100 * wins / len(rows):3.0f}%), paid {price:.3f}, "
            f"net ${net:+7.2f} on ${cost:7.2f} ({100 * net / cost if cost else 0:+6.1f}%) +-${se:.2f}")


def report(store: Store, hours: float = 24.0, all_versions: bool = False, now: float | None = None) -> str:
    now = time.time() if now is None else now
    since = now - hours * 3600
    lanes = [r[0] for r in store.db.execute(
        "SELECT DISTINCT lane FROM decisions WHERE ts > ? AND action='bet' AND lane IS NOT NULL AND lane NOT IN ('manual','burst_flip','settlement_flip') ORDER BY lane", (since,)).fetchall()]
    out = [f"paper against live, last {hours:g} h; the lane's current version only unless asked" if not all_versions else f"paper against live, last {hours:g} h, all versions", ""]
    for lane in lanes:
        ver = None
        if not all_versions:
            row = store.db.execute("SELECT lane_ver FROM decisions WHERE lane=? AND ts > ? AND lane_ver IS NOT NULL ORDER BY ts DESC LIMIT 1", (lane, since)).fetchone()
            ver = row[0] if row else None
        cond, params = ("AND d.lane_ver = ?", [ver]) if ver else ("", [])
        rows = store.db.execute(
            f"""SELECT d.mode, d.ask, d.contracts, d.side, d.exit_price, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker = d.ticker
                WHERE d.lane = ? AND d.action = 'bet' AND w.result IN ('yes','no') AND d.ts > ? {cond}""", [lane, since, *params]).fetchall()
        live = [r for r in rows if r["mode"] == "trade"]
        paper = [r for r in rows if r["mode"] != "trade"]
        out.append(f"{lane}" + (f"  (version {ver})" if ver else ""))
        out.append(f"  live  {_summary(live)}")
        out.append(f"  paper {_summary(paper)}")
        if live and paper:
            lw = sum(1 for r in live if r["result"] == r["side"]) / len(live)
            pw = sum(1 for r in paper if r["result"] == r["side"]) / len(paper)
            out.append(f"  gap   paper wins {100 * (pw - lw):+.0f} points more than live")
        base = "FROM decisions WHERE lane = ? AND ts > ?" + (" AND lane_ver = ?" if ver else "")
        args = [lane, since, *([ver] if ver else [])]

        def count(extra: str, base: str = base, args: list = args) -> int:
            return int(store.db.execute(f"SELECT COUNT(*) {base} AND {extra}", args).fetchone()[0])
        did_not = count("reason LIKE '%did not fill%'")
        cross = count("reason LIKE '%post only cross%'")
        if did_not or cross:
            filled = count("action = 'bet' AND mode = 'trade'")
            total = filled + did_not + cross
            out.append(f"  live resting orders: {filled} filled, {did_not} not filled, {cross} refused as a post-only cross ({100 * filled / total:.0f}% of {total} filled)")
        judged = count("reason LIKE '%rest-v2%'")
        if judged:
            kept = count("reason LIKE '%rest-v2%' AND reason LIKE '%would have filled%'")
            out.append(f"  paper resting rule: kept {kept} of {judged} bets ({100 * kept / judged:.0f}%)")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
