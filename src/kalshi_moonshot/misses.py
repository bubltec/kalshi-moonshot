"""The live orders that did not fill, and what became of the bets they would have been.

A live buy that fails to fill is logged as an observe-mode bet whose reason says "live order not filled", so in a lane's record it reads like a paper bet. It is not one: it is what the lane tried to buy and could not.
Whether the misses do better or worse than the fills is the question that says if a lane's live record can be trusted: on 2026-11-04 energy_moonshot filled 14 (1 won) and missed 26 (12 won), while crypto_moonshot filled
129 (24 won, +$140) and missed 100 (8 won). Read-only: python -m kalshi_moonshot misses [--days N].
"""

from __future__ import annotations

import collections
import time

from .config import Settings
from .store import Store

MISS_TEXT = "live order not filled"


def classify(mode: str | None, reason: str | None) -> str | None:
    """'filled' for a live bet, 'missed' for a live attempt that did not fill, None for anything else (a plain paper bet)."""
    if mode == "trade":
        return "filled"
    return "missed" if MISS_TEXT in (reason or "") else None


def summarize(rows: list[dict]) -> dict[str, dict[str, float]]:
    """{lane: {filled, filled_won, filled_staked, filled_payout, missed, missed_won, missed_staked, missed_payout}} from settled rows (lane, mode, reason, side, ask, stake_usd, result)."""
    out: dict[str, dict[str, float]] = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in rows:
        kind = classify(r.get("mode"), r.get("reason"))
        if kind is None or r.get("result") not in ("yes", "no") or not r.get("ask") or not r.get("stake_usd"):
            continue
        a = out[r["lane"]]
        won = r["result"] == r["side"]
        a[kind] += 1
        a[f"{kind}_won"] += won
        a[f"{kind}_staked"] += r["stake_usd"]
        a[f"{kind}_payout"] += (r["stake_usd"] / r["ask"]) if won else 0.0  # a miss is valued as if it had filled at the price it was logged at
    return {k: dict(v) for k, v in out.items()}


def report(s: Settings, days: float = 14.0, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    store = Store(s.db_path, s.exclude_versions)
    rows = [dict(r) for r in store.db.execute(
        "SELECT d.lane, d.mode, d.reason, d.side, d.ask, d.stake_usd, w.result FROM decisions d LEFT JOIN windows w ON w.ticker = d.ticker "
        "WHERE d.action = 'bet' AND d.ts > ? AND (d.mode = 'trade' OR d.reason LIKE ?)", (now - days * 86400.0, f"%{MISS_TEXT}%")).fetchall()]
    out = [f"live attempts of the last {days:g} days: what filled and what did not (a miss is valued as if it had filled at its logged price)",
           "lane                    filled  won   net$ |  missed  won   net$  | fill rate"]
    for lane, a in sorted(summarize(rows).items()):
        f, m = a.get("filled", 0), a.get("missed", 0)
        fnet = a.get("filled_payout", 0.0) - a.get("filled_staked", 0.0)
        mnet = a.get("missed_payout", 0.0) - a.get("missed_staked", 0.0)
        rate = f / (f + m) * 100 if f + m else 0.0
        out.append(f"{lane:22} {int(f):7d} {int(a.get('filled_won', 0)):4d} {fnet:+7.2f} | {int(m):7d} {int(a.get('missed_won', 0)):4d} {mnet:+7.2f} | {rate:5.0f}%")
    return out
